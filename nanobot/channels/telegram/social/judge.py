"""Bounded intermediary requests with durable, atomic scheduling handoffs."""

# oauth-cli-kit has typed Python sources but does not publish type stubs.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
from oauth_cli_kit.storage import FileTokenStorage
from pydantic import ValidationError

from nanobot.channels.telegram.social.judge_models import (
    JUDGE_PROMPT_VERSION,
    JudgeDecision,
    JudgeRecord,
    JudgeSnapshot,
)
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import Config, ModelPresetConfig, TelegramSocialJudgeConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.factory import make_provider
from nanobot.providers.factory import provider_signature as provider_settings
from nanobot.utils.prompt_templates import render_template


def judge_preset(settings: TelegramSocialJudgeConfig) -> ModelPresetConfig:
    return ModelPresetConfig(model=settings.model, provider=settings.provider,
                             max_tokens=settings.max_tokens, temperature=0,
                             reasoning_effort=settings.reasoning_effort)


JudgeProviderSettings = tuple[object, ...] | str
JudgeMediaLoader = Callable[[JudgeSnapshot], Awaitable[list[dict[str, Any]]]]


def make_judge_provider(config: Config, settings: TelegramSocialJudgeConfig) -> tuple[LLMProvider, JudgeProviderSettings]:
    """Reuse provider resolution/auth; never inherit main-model fallback chains."""
    isolated = config.model_copy(deep=True)
    isolated.agents.defaults.fallback_models = []
    preset = judge_preset(settings)
    provider_config = isolated.get_provider(settings.model, preset=preset)
    if settings.provider == "openai_codex" and provider_config is not None:
        provider_config.extra_body = {
            **(provider_config.extra_body or {}),
            "text": {"verbosity": "low", "format": {
                "type": "json_schema", "name": "social_judge", "strict": True,
                "schema": {**JudgeDecision.model_json_schema(),
                           "required": list(JudgeDecision.model_fields)},
            }},
        }
    values = provider_settings(isolated, preset=preset)
    provider = make_provider(isolated, preset=preset)
    if settings.oauth_storage_dir is not None:
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider

        if not isinstance(provider, OpenAICodexProvider):
            raise ValueError("judge oauthStorageDir requires the Codex OAuth provider")
        provider.token_storage = FileTokenStorage(
            data_dir=Path(settings.oauth_storage_dir).expanduser(),
            token_filename=OPENAI_CODEX_PROVIDER.token_filename, import_codex_cli=False,
        )
    return provider, values


class SocialJudge:
    """Call once per assessment, journaling pending retries without polling or main calls."""

    def __init__(self, store: SocialStore, settings: TelegramSocialJudgeConfig,
                 provider: LLMProvider, *, provider_changed: bool = False,
                 media_loader: JudgeMediaLoader | None = None) -> None:
        self.store = store
        self.settings = settings.model_copy(deep=True)
        self.provider = provider
        self.media_loader = media_loader
        self.prompt = render_template("telegram/social_judge.md")
        self.revision = store.setting_revision("judge", json.dumps({
            "settings": self.settings.model_dump(),
            "prompt_version": JUDGE_PROMPT_VERSION, "prompt": self.prompt,
        }, sort_keys=True), force=provider_changed)

    @classmethod
    def from_config(cls, store: SocialStore, config: Config,
                    settings: TelegramSocialJudgeConfig) -> SocialJudge:
        provider, _ = make_judge_provider(config, settings)
        return cls(store, settings, provider)

    def system_prompt(self, snapshot: JudgeSnapshot) -> str:
        """Anchor the assessed account in runtime facts, outside chat history."""
        identity = {"bot_username": snapshot.bot_username, "bot_name": snapshot.bot_name,
                    "nicknames": snapshot.nicknames}
        return self.prompt + "\nRuntime target bot identity: " + json.dumps(identity, ensure_ascii=False)

    async def assess(self, snapshot: JudgeSnapshot) -> JudgeRecord:
        """Never mark main views or publish messages; persist decisions with their handoff.

        Pending records expose a persisted due time. The owning scheduler (V3-B)
        may submit the same still-valid snapshot after that time; it must not poll.
        """
        settings = self.settings
        payload = snapshot.prompt_json()
        if len(snapshot.messages) > settings.context_messages or len(payload) > settings.context_chars:
            raise ValueError("judge snapshot exceeds configured context limits")
        if snapshot.expires - snapshot.created > settings.batch_ttl_seconds:
            raise ValueError("judge snapshot exceeds configured batch TTL")
        record = self.store.open_judge_batch(snapshot, self.revision, settings.model)
        if record.status != "pending":
            return record
        if record.attempts >= 1 + settings.max_retries:
            return self.store.judge_fallback(record.id, snapshot, limit=settings.max_fallbacks_per_hour)
        record = self.store.claim_judge_attempt(
            record.id, snapshot, limit=settings.max_requests_per_hour,
            interval=settings.min_interval_seconds, max_attempts=1 + settings.max_retries,
        )
        if record.status != "running":
            return record
        response: LLMResponse | None = None
        decision: JudgeDecision | None = None
        error: str | None = None
        delay = settings.retry_delay_seconds
        started = time.monotonic()
        try:
            async with asyncio.timeout(min(settings.timeout_seconds, snapshot.expires - self.store.now())):
                # Prepare only after claiming the durable request budget. Image bytes
                # are transient provider input, never persisted in batch metadata.
                media = await self.media_loader(snapshot) if self.media_loader else []
                content = [{"type": "text", "text": payload}, *media] if media else payload
                # Raw chat, not chat_with_retry: all retry attempts are bounded and charged here.
                response = await self.provider.chat(
                    messages=[{"role": "system", "content": self.system_prompt(snapshot)}, {"role": "user", "content": content}],
                    tools=None, model=settings.model, max_tokens=settings.max_tokens,
                    temperature=0, reasoning_effort=settings.reasoning_effort,
                    tool_choice="none",
                )
            if response.finish_reason == "error":
                error = "provider_error"
                retry_after = response.error_retry_after_s or response.retry_after
                if retry_after is not None and retry_after > 0:
                    delay = max(delay, min(retry_after, settings.batch_ttl_seconds))
            elif response.tool_calls or response.finish_reason != "stop":
                error = "invalid_response"
            elif response.content is None or len(response.content) > 4096:
                error = "invalid_decision"
            else:
                candidate = JudgeDecision.model_validate_json(response.content)
                candidate.validate_for(snapshot)
                decision = candidate
        except asyncio.CancelledError:
            if not self.store.closed:
                self.store.finish_judge_attempt(
                    record.id, snapshot, attempt=record.attempts, decision=None,
                    error_code="interrupted", delay=0,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    input_tokens=None, output_tokens=None, total_tokens=None,
                )
            raise
        except TimeoutError:
            error = "timeout"
        except (ValidationError, ValueError):
            error = "invalid_decision"
        except Exception:
            # Persist fixed codes only; provider exception text may contain credentials/payloads.
            error = "provider_exception"
        usage = response.usage if response is not None else None
        record = self.store.finish_judge_attempt(
            record.id, snapshot, attempt=record.attempts, decision=decision, error_code=error,
            delay=delay, latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
        )
        if record.status == "pending" and record.attempts >= 1 + settings.max_retries:
            record = self.store.judge_fallback(record.id, snapshot, limit=settings.max_fallbacks_per_hour)
        return record
