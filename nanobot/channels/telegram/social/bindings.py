"""Resolve authorized scope bindings and cache role text and setting revisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from nanobot.agent.tools.context import RequestContext
from nanobot.channels.telegram.social.models import ConversationScope
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import TelegramSocialConfig, TelegramSocialScopeConfig
from nanobot.utils.prompt_templates import render_template

SOCIAL_PROTOCOL = render_template("telegram/social_protocol.md", strip=True)


@dataclass(frozen=True)
class ResolvedScope:
    binding: TelegramSocialScopeConfig
    persona: str
    persona_revision: int
    version: str


class ScopeBindings:
    def __init__(self, store: SocialStore, roles: Path, channel: str) -> None:
        self.store = store
        self.roles = roles
        self.channel = channel
        self._roles_root = roles.resolve()
        self._role_cache: dict[Path, tuple[int, int, str]] = {}
        self._revisions: dict[str, tuple[str, int]] = {}

    def configure(self, config: TelegramSocialConfig) -> None:
        self.config = config

    def binding(self, scope: ConversationScope) -> TelegramSocialScopeConfig | None:
        if not self.config.enabled or scope.channel != self.channel:
            return None
        candidates = [binding for binding in self.config.scopes if binding.chat_id == scope.chat_id]
        # Explicit topic overrides, including disabled ones, take precedence.
        binding = next((item for item in candidates
                        if not item.all_topics and item.thread_id == scope.thread_id), None)
        if binding is None:
            binding = next((item for item in candidates if item.all_topics), None)
        return binding if binding is not None and binding.enabled else None

    def validate_context(self, ctx: RequestContext, *, require_claim: bool = True) -> ConversationScope:
        scope = ConversationScope.from_session_key(ctx.session_key or "")
        if (
            scope is None or ctx.channel != scope.channel or ctx.chat_id != str(scope.chat_id)
            or int(ctx.metadata.get("message_thread_id") or 0) != scope.thread_id
            or ctx.turn_id is None
        ):
            raise PermissionError("social tool requires an authoritative group/topic turn")
        if self.binding(scope) is None:
            raise PermissionError("social tools require an enabled group/topic")
        if self.store.scopes.state(scope.session_key)["mode"] == "paused":
            raise PermissionError("social conversation is paused")
        if ctx.attributes.get("social_deferred"):
            raise PermissionError("this turn has already yielded to social_wait")
        if require_claim and ctx.metadata.get("_social_job") is not None and not self.store.wakes.active_turn(scope.session_key, self.store.scopes.state(scope.session_key)["version"], ctx.turn_id):
            raise PermissionError("social tool requires an active authorized wake")
        return scope

    def role_file(self, binding: TelegramSocialScopeConfig) -> Path:
        return (self.roles / f"{binding.role_id}.md").resolve()

    def resolve(self, scope: ConversationScope) -> ResolvedScope:
        binding = self.binding(scope)
        if binding is None:
            raise PermissionError("group/topic is not authorized for social mode")
        path = self.role_file(binding)
        if not path.is_relative_to(self._roles_root):
            raise PermissionError("role file is outside the authorized directory")
        unavailable = f"Role file unavailable: {path}. Use ordinary file tools to inspect or repair it when requested."
        try:
            stat = path.stat()
        except OSError:
            persona = unavailable
        else:
            cached = self._role_cache.get(path)
            if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                persona = cached[2]
            else:
                try:
                    if stat.st_size > 32000:
                        raise ValueError("role file exceeds 32000 bytes")
                    persona = path.read_text(encoding="utf-8")
                    if not persona.strip():
                        raise ValueError("role file is empty")
                except (OSError, ValueError):
                    persona = unavailable
                self._role_cache[path] = (stat.st_mtime_ns, stat.st_size, persona)
        assembled = f"{SOCIAL_PROTOCOL}\n\n# Operator persona\n{persona}\n\n# Operator group background\n{binding.background}"
        persona_key = f"persona:{scope.session_key}"
        scope_key = f"scope:{scope.session_key}"
        for name, value in (
            (persona_key, assembled),
            (scope_key, json.dumps({"role_id": binding.role_id, "paused": binding.paused}, sort_keys=True)),
        ):
            previous = self._revisions.get(name)
            if previous is None or previous[0] != value:
                self._revisions[name] = (value, self.store.scopes.setting_revision(name, value))
        return ResolvedScope(
            binding=binding, persona=assembled,
            persona_revision=self._revisions[persona_key][1],
            version=str(self._revisions[scope_key][1]),
        )
