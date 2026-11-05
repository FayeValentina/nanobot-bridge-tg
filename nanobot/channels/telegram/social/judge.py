"""In-memory admission windows and one intermediary call per selective batch."""

# oauth-cli-kit has typed Python sources but does not publish type stubs.
# pyright: reportMissingTypeStubs=false
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from loguru import logger
from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
from oauth_cli_kit.storage import FileTokenStorage
from pydantic import BaseModel, ConfigDict, Field

from nanobot.channels.telegram.social.bindings import ScopeBindings
from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.expressions import redacted_error_detail
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    QuotedMessage,
    strip_json_fence,
)
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import (
    Config,
    ModelPresetConfig,
    TelegramSocialConfig,
    TelegramSocialJudgeConfig,
)
from nanobot.llm_usage import LLMCallRecord, record_llm_call
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.factory import make_provider
from nanobot.providers.factory import provider_signature as provider_settings
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.waiting import SocialWaiting

JUDGE_PROMPT_VERSION = "social-judge-v9"
BATCH_TTL_SECONDS = 120
JudgeProviderSettings = tuple[object, ...] | str


@dataclass
class PendingBatch:
    first_at: float
    last_at: float
    through_seq: int
    due_cap: float | None = None


class PendingWindows:
    """One coalescing window per scope; only assessment watermarks are persisted."""

    def __init__(self) -> None:
        self.windows: dict[str, PendingBatch] = {}

    def add(self, key: str, seq: int, at: float) -> None:
        old = self.windows.get(key)
        self.windows[key] = PendingBatch(old.first_at if old else at, at,
                                         max(old.through_seq, seq) if old else seq)

    def due(self, key: str, delay: float, maximum: float) -> float:
        batch = self.windows[key]
        return min(batch.first_at + maximum,
                   batch.due_cap if batch.due_cap is not None else batch.last_at + delay)

    def shorten(self, key: str, quiet: float, total: float | None) -> None:
        if batch := self.windows.get(key):
            batch.due_cap = min(batch.last_at + quiet, total) if total is not None else batch.last_at + quiet

    def take(self, key: str) -> PendingBatch | None:
        return self.windows.pop(key, None)

    def discard(self, key: str) -> None:
        self.windows.pop(key, None)

    def has(self, key: str) -> bool:
        return key in self.windows

    def any(self) -> bool:
        return bool(self.windows)

    def next_due(self, delay_for: Callable[[str], float], maximum: float) -> float | None:
        return min((self.due(key, delay_for(key), maximum) for key in self.windows), default=None)

    def keys(self) -> list[str]:
        return list(self.windows)


class JudgeReply(BaseModel):
    kind: Literal["bot", "member", "topic_root", "unknown"]
    message_id: int
    sender_id: str = ""
    text: str = ""


class JudgeMessage(BaseModel):
    seq: int
    message_id: int
    sender_id: str
    name: str
    timestamp: float
    text: str
    is_self: bool = False
    is_bot: bool = False
    mentions_bot: bool = False
    reply: JudgeReply | None = None
    media: tuple[str, ...] = ()


class JudgeIntent(BaseModel):
    description: str
    keywords: tuple[str, ...] = ()
    senders: tuple[str, ...] = ()
    on_question: bool = False
    one_shot: bool = True
    expires_at: float | None = None


class JudgeInput(BaseModel):
    bot_username: str = ""
    bot_name: str
    nicknames: tuple[str, ...] = ()
    intent: JudgeIntent | None = None
    context: tuple[JudgeMessage, ...] = ()
    new: tuple[JudgeMessage, ...]

    def prompt_json(self) -> str:
        return self.model_dump_json()


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["wake", "observe"]
    fulfills_intent: bool = False
    reason: str = Field(max_length=120)


def make_judge_provider(config: Config, settings: TelegramSocialJudgeConfig) -> tuple[LLMProvider, JudgeProviderSettings]:
    """Reuse provider resolution/auth; never inherit main-model fallback chains."""
    isolated = config.model_copy(deep=True)
    isolated.agents.defaults.fallback_models = []
    preset = ModelPresetConfig(model=settings.model, provider=settings.provider,
                               max_tokens=settings.max_tokens, temperature=0,
                               reasoning_effort=settings.reasoning_effort)
    provider_config = isolated.get_provider(settings.model, preset=preset)
    if settings.provider == "openai_codex" and provider_config is not None:
        provider_config.extra_body = {
            **(provider_config.extra_body or {}),
            "text": {"verbosity": "low", "format": {
                "type": "json_schema", "name": "social_judge", "strict": True,
                "schema": {**JudgeVerdict.model_json_schema(),
                           "required": list(JudgeVerdict.model_fields)},
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
    """One intermediary call: no retry, no budget, no persistence. Failure means wake."""

    def __init__(self, settings: TelegramSocialJudgeConfig, provider: LLMProvider,
                 provider_version: JudgeProviderSettings) -> None:
        self.settings = settings.model_copy(deep=True)
        self.provider, self.provider_version = provider, provider_version

    async def assess(self, data: JudgeInput) -> JudgeVerdict | None:
        settings = self.settings
        identity = {"bot_username": data.bot_username, "bot_name": data.bot_name, "nicknames": data.nicknames}
        system_prompt = render_template("telegram/social_judge.md") + "\nRuntime target bot identity: " + json.dumps(identity, ensure_ascii=False)
        started_at_ms, started = time.time_ns() // 1_000_000, time.monotonic_ns()
        response: LLMResponse | None = None
        error: str | None = None
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                response = await self.provider.chat(
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": data.prompt_json()}],
                    tools=None, tool_choice="none", temperature=0, model=settings.model,
                    max_tokens=settings.max_tokens, reasoning_effort=settings.reasoning_effort)
            if response.finish_reason != "stop" or response.tool_calls:
                error = "invalid_response"
            elif not response.content or len(response.content) > 4096:
                error = "invalid_content"
            else:
                return JudgeVerdict.model_validate_json(strip_json_fence(response.content))
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except TimeoutError:
            error = "timeout"
        except Exception as exc:
            error = "judge_exception"
            logger.warning("Social judge failed: {}", redacted_error_detail(exc))
        finally:
            record_llm_call(LLMCallRecord(
                started_at_ms=started_at_ms, duration_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
                provider=self.provider.provider_name, model=settings.model, source="system", stream=False,
                finish_reason=response.finish_reason if response else error or "error",
                usage=response.usage if response else None,
                error_status_code=response.error_status_code if response else None,
                error_kind=error or (response.error_kind if response else None)))
        logger.warning("Social judge unavailable: {}", error)
        return None


class JudgeCoordinator:
    """A guards in observe, B platform facts wake, C asks the judge. Unsure means wake."""

    def __init__(self, store: SocialStore, bindings: ScopeBindings, waiting: SocialWaiting,
                 windows: PendingWindows, notify: Callable[[], None], config: Callable[[], TelegramSocialConfig],
                 now: Callable[[], float], provider: LLMProvider | None = None,
                 provider_version: JudgeProviderSettings = "") -> None:
        self.store, self.bindings, self.waiting, self.windows = store, bindings, waiting, windows
        self.notify, self.config, self.now = notify, config, now
        self.provider, self.provider_version = provider, provider_version
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.configure()

    def configure(self) -> None:
        if self.provider is None:
            self.provider, self.provider_version = make_judge_provider(Config(), self.config().judge)
        self.judge = SocialJudge(self.config().judge, self.provider, self.provider_version)

    def set_provider(self, provider: LLMProvider, version: JudgeProviderSettings) -> None:
        self.provider, self.provider_version = provider, version
        self.configure()
        self.notify()

    def cancel(self) -> None:
        for task in self.tasks.values():
            task.cancel()

    def coalesce_delay(self, scope: ConversationScope) -> float:
        binding = self.bindings.binding(scope)
        assert binding is not None
        delay = binding.coalesce_seconds if binding.coalesce_seconds is not None else self.config().coalesce_seconds
        wait = self.store.wakes.wait(scope.session_key)
        if wait and wait["purpose"] in {"reply", "messages"}:
            delay = float(wait["quiet_seconds"])
        return delay

    def note(self, scope: ConversationScope, seq: int, received_at: float) -> None:
        self.windows.add(scope.session_key, seq, received_at)
        self.notify()

    def recover(self) -> None:
        for scope in self.store.scopes.all():
            key = scope.session_key
            for row in self.store.messages.after(key, self.store.judge.through(key), -1):
                facts = json.loads(row["judge_facts"])
                if facts.get("eligible") and facts["received_at"] + BATCH_TTL_SECONDS > self.now():
                    self.note(scope, row["seq"], facts["received_at"])

    def next_due(self) -> float | None:
        # Window keys come only from ConversationScope.session_key in note/recover.
        return min((self.windows.due(key, self.coalesce_delay(cast(ConversationScope, ConversationScope.from_session_key(key))),
                                    self.config().dynamic_wait.max_seconds)
                    for key in self.windows.keys() if key not in self.tasks), default=None)

    def busy(self) -> bool:
        return bool(self.tasks) or self.windows.any()

    async def start_due(self) -> None:
        for key in self.windows.keys():
            scope = ConversationScope.from_session_key(key)
            if scope is None or key in self.tasks:
                continue
            if self.bindings.binding(scope) is None or self.store.scopes.state(key)["mode"] == "paused":
                self.windows.discard(key)
                continue
            if self.windows.due(key, self.coalesce_delay(scope), self.config().dynamic_wait.max_seconds) <= self.now():
                # Iterating existing keys synchronously guarantees take returns this batch.
                batch = cast(PendingBatch, self.windows.take(key))
                self.tasks[key] = asyncio.create_task(self._run(scope, batch), name="social-judge")
        if self.tasks:
            await asyncio.sleep(0)

    def local_admission(self, data: JudgeInput, key: str) -> str | None:
        if any(message.mentions_bot for message in data.new):
            return "directed"
        conditions, dynamic = self.waiting.conditions(key)
        if dynamic and bool(conditions.keywords or conditions.senders or conditions.on_question):
            return None
        return None if all(message.is_bot for message in data.new) else "conversation"

    def intent(self, key: str) -> JudgeIntent | None:
        conditions, dynamic = self.waiting.conditions(key)
        wait = self.store.wakes.wait(key)
        if not dynamic or not (conditions.keywords or conditions.senders or conditions.on_question):
            return None
        observing = wait is not None and wait["purpose"] == "observe"
        return JudgeIntent(
            description=("Background observation before sleep, not a selective wait for questions. " if observing else
                         "An explicit wait is selective; ordinary unrelated messages need not interrupt it. Clear calls remain reachable. ")
                        + json.dumps({"waiting_for_questions": conditions.on_question if not observing else False}),
            keywords=tuple(conditions.keywords), senders=tuple(conditions.senders), on_question=conditions.on_question,
            one_shot=conditions.one_shot, expires_at=conditions.expires_at)

    async def _run(self, scope: ConversationScope, batch: PendingBatch) -> None:
        key = scope.session_key
        try:
            after = self.store.judge.through(key)
            rows = [row for row in self.store.messages.range(key, after, batch.through_seq, -1)
                    if (facts := json.loads(row["judge_facts"])).get("eligible")
                    and facts["received_at"] + BATCH_TTL_SECONDS > self.now()]
            if not rows:
                with transaction(self.store.db):
                    self.store.judge.set_through(key, batch.through_seq)
                return
            history = list(reversed(self.store.messages.history_through(
                key, rows[0]["seq"] - 1, max(0, self.config().judge.context_messages - len(rows)))))
            messages: list[JudgeMessage] = []
            for row in history + rows:
                facts = json.loads(row["judge_facts"])
                quoted = QuotedMessage.model_validate_json(row["reply_context"]) if row["reply_context"] else None
                reply = JudgeReply(kind="topic_root" if quoted.is_topic_root else "bot" if quoted.is_bot else "member",
                                   message_id=quoted.message_id, sender_id=quoted.sender_id, text=quoted.text) if quoted else None
                if reply is None and row["reply_to"] is not None:
                    target = self.store.messages.by_message_id(key, row["reply_to"])
                    if target is not None:
                        reply = JudgeReply(kind="bot" if target["is_self"] else "member", message_id=target["message_id"],
                                           sender_id=target["sender_id"], text=target["text"][:1000])
                messages.append(JudgeMessage(seq=row["seq"], message_id=row["message_id"], sender_id=row["sender_id"],
                    name=row["display_name"], timestamp=row["at"], text=row["text"], is_self=bool(row["is_self"]),
                    is_bot=facts.get("is_bot", False), mentions_bot=bool(facts.get("directed")), reply=reply,
                    media=tuple(json.loads(row["media"]))))
            binding = self.bindings.binding(scope)
            assert binding is not None
            intent = self.intent(key)
            condition_config = self.store.scopes.state(key)["wake_config"]
            wait = self.store.wakes.wait(key)
            quiet_due = batch.last_at + self.coalesce_delay(scope)
            total_due = batch.first_at + self.config().dynamic_wait.max_seconds
            data = JudgeInput(bot_name=binding.nicknames[0] if binding.nicknames else "Bot", nicknames=tuple(binding.nicknames),
                bot_username=next((json.loads(row["judge_facts"]).get("bot_username") for row in reversed(rows)
                                   if json.loads(row["judge_facts"]).get("bot_username")), ""),
                intent=intent, context=tuple(messages[:len(history)]), new=tuple(messages[len(history):]))
            by = self.local_admission(data, key)
            verdict = JudgeVerdict(decision="wake", reason={"directed": "host: directed at the bot",
                "conversation": "host: fresh conversation without a selective wait"}[by]) if by else await self.judge.assess(data)
            verdict = verdict or JudgeVerdict(decision="wake", reason="judge unavailable")
            with transaction(self.store.db):
                self.store.judge.set_through(key, batch.through_seq)
                if self.store.scopes.state(key)["mode"] == "paused" or self.bindings.binding(scope) is None:
                    return
                if verdict.decision == "wake":
                    self.store.wakes.admit_batch(key, [message.seq for message in data.new], batch.through_seq,
                        admission={"by": by or "judge", "reason": verdict.reason}, fulfills_intent=verdict.fulfills_intent,
                        one_shot=intent.one_shot if intent else False, condition_config=condition_config,
                        condition_expires=intent.expires_at if intent else None, expires=batch.first_at + BATCH_TTL_SECONDS,
                        purpose=wait["purpose"] if wait else "reply", quiet_seconds=max(0, quiet_due - batch.first_at),
                        quiet_due=quiet_due, total_due=total_due, after_seq=after)
        finally:
            self.tasks.pop(key, None)
            self.notify()
