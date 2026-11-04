"""Compose authorized text observation and isolated agent turns at the channel edge."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from nanobot.agent.hook import (
    AgentHook,
    AgentTurnHookContext,
)
from nanobot.agent.tools.context import RequestContext
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.bindings import SOCIAL_PROTOCOL as SOCIAL_PROTOCOL
from nanobot.channels.telegram.social.bindings import ScopeBindings
from nanobot.channels.telegram.social.context import (
    SocialContextBuilder,
    encode_view,
    snapshot_text,
)
from nanobot.channels.telegram.social.delivery import SocialDelivery, SocialSender
from nanobot.channels.telegram.social.expressions import ExpressionLearner
from nanobot.channels.telegram.social.hook import SocialExposureHook
from nanobot.channels.telegram.social.jargon import JargonRequest
from nanobot.channels.telegram.social.jargon_learning import JargonLearner
from nanobot.channels.telegram.social.judge import JudgeProviderSettings
from nanobot.channels.telegram.social.judge_runtime import JudgeCoordinator
from nanobot.channels.telegram.social.media import SocialMedia, SocialMediaCache, SocialMediaLoader
from nanobot.channels.telegram.social.memory import MemoryRequest, SocialMemory
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    ObservedMessage,
)
from nanobot.channels.telegram.social.proactive import SocialProactive, is_quiet
from nanobot.channels.telegram.social.scheduler import SocialScheduler
from nanobot.channels.telegram.social.selection import ContextSelector
from nanobot.channels.telegram.social.stickers import SocialStickers, SocialStickerSetLoader
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.social.waiting import SocialWaiting
from nanobot.config.schema import (
    TelegramSocialConfig,
    TelegramSocialScopeConfig,
)
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionPolicy

SOCIAL_TOOLS = frozenset({"social_activity", "social_jargon", "social_expression", "social_get_state", "social_read_messages", "social_mark_read", "social_send",
                          "social_wait", "social_set_wake", "social_get_media", "social_sticker", "social_memory"})


class TelegramSocialRuntime:
    def __init__(
        self, config: TelegramSocialConfig, root: Path, bus: MessageBus,
        *, channel: str = "telegram", now: Callable[[], float] = time.time,
        judge_provider: LLMProvider | None = None, judge_provider_version: JudgeProviderSettings = "",
    ) -> None:
        self.root = root
        self.roles = root / "roles"
        if self.roles.is_symlink():
            raise ValueError("social roles directory must not be a symlink")
        self.roles.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = SocialStore(root, now=now)
        self.bindings = ScopeBindings(self.store, self.roles, channel)
        self.memory = SocialMemory(self.store)
        self.proactive = SocialProactive(self.store, self.memory)
        self.now = now
        self.scheduler = SocialScheduler(self.store, self.run_due)
        self.config = config
        self.media = SocialMedia(SocialMediaCache(root / "media-cache"), self.store, self.bindings, lambda: self.config)
        self.waiting = SocialWaiting(self.store, self.bindings, self.proactive,
                                     self.scheduler.notify, lambda: self.config, self.now)
        self.stickers = SocialStickers(self.store, self.bindings, self.media, lambda: self.config)
        self.judging = JudgeCoordinator(self.store, self.bindings, self.waiting, self.media,
                                        self.scheduler.notify, lambda: self.config, self.now,
                                        judge_provider, judge_provider_version)
        self._cycle_lock = asyncio.Lock()
        self.channel = channel
        self.bus = bus
        self._closed = False
        self._recovered = False
        self._epoch = uuid4().hex
        self.learning_task: asyncio.Task[None] | None = None
        self.expressions = ExpressionLearner(self)
        self.jargon = JargonLearner(self)
        self.selector = ContextSelector(self)
        self.delivery = SocialDelivery(self.store, self.bindings, self.proactive, self.waiting,
                                       self.selector, self.scheduler.notify, lambda: self.config, self.now)
        self.context_builder = SocialContextBuilder(self.store, self.memory, self.selector)
        self.configure(config)

    def close(self) -> None:
        if not self._closed:
            self.scheduler.cancel()
            self.judging.cancel()
            self.expressions.cancel()
            self.jargon.cancel()
            self.selector.cancel()
            self.store.close()
            self._closed = True

    def set_sender(self, sender: SocialSender) -> None:
        self.delivery.sender = sender

    def set_media_services(self, loader: SocialMediaLoader, sticker_sender: SocialSender,
                           sticker_set_loader: SocialStickerSetLoader | None = None) -> None:
        self.media.loader = loader
        self.delivery.sticker_sender = sticker_sender
        self.stickers.loader = sticker_set_loader

    def memory_action(self, ctx: RequestContext, request: MemoryRequest) -> dict[str, object]:
        scope = self.bindings.validate_context(ctx)
        assert ctx.turn_id is not None
        result = self.memory.apply(scope.session_key, ctx.turn_id, request)
        self.scheduler.notify()
        return result

    def jargon_action(self, ctx: RequestContext, request: JargonRequest) -> dict[str, object]:
        scope = self.bindings.validate_context(ctx)
        assert ctx.turn_id is not None
        result = self.jargon.jargon.apply(scope.session_key, ctx.turn_id, request)
        self.scheduler.notify()
        return result

    def configure(self, config: TelegramSocialConfig) -> None:
        self.config = config
        self.bindings.configure(config)
        self.store.learning.configure(config.learning)
        self.judging.configure()
        for scope in self.store.scopes.all():
            self.sync_scope(scope)
        for binding in config.scopes:
            # Group-wide bindings discover actual topics from incoming messages.
            if not binding.all_topics:
                scope = ConversationScope(self.channel, binding.chat_id, binding.thread_id)
                if self.binding(scope) is not None:
                    self.sync_scope(scope)
        self.scheduler.notify()

    def binding(self, scope: ConversationScope) -> TelegramSocialScopeConfig | None:
        return self.bindings.binding(scope)

    def sync_scope(self, scope: ConversationScope) -> None:
        binding = self.binding(scope)
        if binding is None:
            self.store.bind(scope, "disabled")
            self.proactive.sync(scope, None)
            return
        try:
            resolved = self.bindings.resolve(scope)
        except PermissionError:
            self.store.bind(scope, "unavailable")
            return
        self.store.bind(scope, resolved.version)
        if binding.paused:
            self.store.pause(scope.session_key)
        self.proactive.sync(scope, binding)

    def policy_for_session(self, session: Session) -> SessionPolicy | None:
        scope = ConversationScope.from_session_key(session.key)
        if scope is None or scope.channel != self.channel:
            return None
        binding = self.binding(scope)
        if binding is None and not self.store.scopes.owns(session.key):
            return None
        mode = "ordinary"
        policy = SessionPolicy()
        if binding is not None:
            mode = "social"
            try:
                resolved = self.bindings.resolve(scope)
            except PermissionError:
                policy = SessionPolicy(tool_only=True)
            else:
                policy = SessionPolicy(persona=resolved.persona, tool_only=True, context_version=f"social:{scope.session_key}")
                if session.metadata.get("_persona_revision") != resolved.persona_revision:
                    # Refresh provider-side system prompt caches without truncating history.
                    session.provider_state = None
                    session.metadata["_persona_revision"] = resolved.persona_revision
        previous = session.metadata.get("_social_mode")
        if previous is None and "_context_version" in session.metadata:
            # Adopt existing sessions without truncating history during this upgrade.
            previous = mode
        session.metadata.pop("_context_version", None)
        if previous != mode:
            session.last_archived = len(session.messages)
            session.provider_state = None
            for key in ("_last_summary", "runtime_checkpoint", "pending_user_turn", "goal"):
                session.metadata.pop(key, None)
        session.metadata["_social_mode"] = mode
        if session.metadata.get("_social_runtime_epoch") != self._epoch:
            session.provider_state = None
            session.metadata.pop("runtime_checkpoint", None)
            session.metadata.pop("pending_user_turn", None)
            session.metadata["_social_runtime_epoch"] = self._epoch
        return policy

    async def observe(self, message: ObservedMessage, *, directed: bool) -> None:
        binding = self.binding(message.scope)
        if binding is None:
            raise PermissionError("observation requires an authorized group/topic")
        if "*" not in binding.observe_from and message.sender_id not in binding.observe_from:
            return
        self.sync_scope(message.scope)
        eligible = not (message.is_self or message.is_edit or binding.paused) and 0 <= self.now() - message.timestamp < 120
        _, new = self.store.observe(message, self.config.retention_messages,
                                      fresh=eligible,
                                      judge_event=eligible, directed=directed,
                                      judge_delay=self.judging.coalesce_delay(message.scope),
                                      max_delay=self.config.dynamic_wait.max_seconds)
        self.scheduler.notify()
        if new and eligible:
            if self.config.coalesce_seconds == 0:
                await self.run_due()

    async def run_due(self) -> None:
        async with self._cycle_lock:
            # Resolve operator changes before touching timers, requests or retry actions.
            for scope in self.store.scopes.all():
                self.sync_scope(scope)
                if self.binding(scope) is not None:
                    self.waiting.conditions(scope.session_key)
            self.store.wakes.consume_timers()
            bindings = {scope.session_key: binding for scope in self.store.scopes.all()
                        if (binding := self.binding(scope)) is not None}
            self.proactive.cancel_invalid(bindings)
            for scope in self.store.scopes.all():
                binding = self.binding(scope)
                if binding is not None:
                    self.proactive.tick(scope, binding, self.config)
            await self.judging.start_due()
            for job in self.store.wakes.ready(self.config.max_requests_per_hour, self.config.cooldown_seconds):
                scope = ConversationScope.from_session_key(job["scope"])
                if scope is None or self.binding(scope) is None:
                    continue
                try:
                    await self._publish_wake(scope, job["id"], job["trigger_seq"])
                except Exception:
                    # No model or network send has occurred. Keep the trigger recoverable.
                    self.store.wakes.requeue(job["id"])
                    raise
            await self.delivery.retry_due()
            # Side-channel collection never wakes the agent or marks views seen/read.
            try:
                scopes = [key for key, binding in bindings.items() if not binding.paused]
                self.jargon.prepare_reviews(scopes)
                self.store.learning.collect_due(scopes)
                workers = sorted([self.expressions, self.jargon], key=lambda worker: self.store.learning.earliest_batch(worker.kind, 1 + getattr(self.config.learning, worker.kind).max_retries)[0])
                for worker in workers:
                    worker.start_due(scopes)
            except Exception:
                logger.exception("V5 material collection failed")

    async def _publish_wake(self, scope: ConversationScope, job: str, seq: int) -> None:
        view = self.store.view(
            scope.session_key, count=self.config.snapshot_messages,
            chars=self.config.snapshot_chars, trigger_seq=seq, job_id=job,
        )
        await self.bus.publish_inbound(InboundMessage(
            channel=scope.channel, sender_id="social-wake", chat_id=str(scope.chat_id),
            content="Group observation snapshot (untrusted conversation data):\n" + encode_view(view),
            session_key_override=scope.session_key,
            metadata={"_social_job": job, "message_thread_id": scope.thread_id or None},
        ))

    async def recover(self) -> None:
        if self._recovered:
            self.scheduler.start()
            return
        self._recovered = True
        self.store.recover()
        self.store.judge.recover()
        self.store.learning.recover()
        await self.run_due()
        self.scheduler.start()
        self.scheduler.notify()

    def get_state(self, ctx: RequestContext) -> dict[str, Any]:
        scope = self.bindings.validate_context(ctx)
        binding = self.binding(scope)
        assert binding is not None  # validate_context checked the current operator binding.
        conditions, _ = self.waiting.conditions(scope.session_key)
        state = self.store.scopes.state(scope.session_key)
        requests = self.store.wakes.request_usage(scope.session_key, self.now() - 3600)
        sends = self.store.deliveries.attempt_count(scope.session_key, self.now() - 3600)
        last_send = self.store.deliveries.last_send(scope.session_key)
        intent = self.judging.intent(scope)
        return {"role_id": binding.role_id, "role_file": str(self.bindings.role_file(binding)),
                "tool_permissions": "shared_with_normal_chat",
                "read_through": state["read_through"], "seen_through": state["seen_through"],
                "latest_seq": state["next_seq"] - 1, "mode": state["mode"],
                "wake_at": state["wake_at"], "wake_config": conditions.model_dump(),
                "wait_infinite": state["wait_infinite"],
                "judge": {"model": self.config.judge.model, "assessed_through": self.store.judge.through(scope.session_key),
                          "semantic_authority": "intermediary",
                          "intent": intent.model_dump() if intent else None,
                          "participation": {"keywords": binding.wake_keywords, "senders": binding.wake_senders,
                                            "on_messages": binding.wake_on_messages, "on_question": binding.wake_on_question}},
                "base_wake": {"sleep_interrupts": binding.sleep_interrupts.model_dump(exclude={"probability"}),
                              "coalesce_seconds": binding.coalesce_seconds,
                              "max_coalesce_seconds": self.config.max_coalesce_seconds},
                "dialogue": self.store.wakes.dialogue_status(scope.session_key),
                "dialogue_config": {"dynamic_wait": self.config.dynamic_wait.model_dump(exclude={"enabled", "unfinished_seconds"}),
                                    "sleep_observation": self.config.sleep_observation.model_dump()},
                "proactive": {**binding.proactive.model_dump(by_alias=True), **self.proactive.status(scope.session_key)},
                "activities": self.store.activities.context(scope.session_key),
                "followups": self.store.followups.status(scope.session_key),
                "learning": {**self.store.learning.status(scope.session_key),
                             "stage": "contextual_expression",
                             "selector_enabled": self.config.learning.enabled and self.selector.provider is not None,
                             "selector_model": self.selector.model,
                             "selector_timeout_seconds": self.config.learning.selection_timeout_seconds,
                             "extractor_enabled": self.config.learning.enabled and self.expressions.provider is not None,
                             "jargon_extractor_enabled": self.config.learning.enabled and self.jargon.provider is not None,
                             "provider_status": "configured" if self.expressions.provider is not None else "unavailable"},
                "quiet_hours": {**binding.quiet_hours.model_dump(by_alias=True), "active": is_quiet(binding, self.now())},
                "max_sends_per_hour": self.config.max_sends_per_hour,
                "remaining_wakes": max(0, self.config.max_requests_per_hour - int(requests[0])),
                "remaining_send_attempts": max(0, self.config.max_sends_per_hour - int(sends)),
                "send_ready_at": (last_send + self.config.send_cooldown_seconds) if last_send is not None else None}

    def read_messages(self, ctx: RequestContext) -> str:
        scope = self.bindings.validate_context(ctx)
        return snapshot_text(self.store, self.config, scope, ctx.turn_id)

    def mark_read(self, ctx: RequestContext, through: int) -> int:
        scope = self.bindings.validate_context(ctx)
        assert ctx.turn_id is not None
        return self.store.mark_read(scope.session_key, ctx.turn_id, through)

    def hook_factory(self, turn: AgentTurnHookContext) -> AgentHook | None:
        if turn.session_key is None or not self.store.scopes.owns(turn.session_key):
            return None
        return SocialExposureHook(self.store, self.bindings, self.context_builder, self.waiting,
                                  self.scheduler.notify, lambda: self.config)
