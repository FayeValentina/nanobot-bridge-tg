"""wait / sleep / set_wake semantics and model-configured wake conditions."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from nanobot.agent.tools.context import RequestContext
from nanobot.channels.telegram.social.bindings import ScopeBindings
from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.models import ConversationScope
from nanobot.channels.telegram.social.proactive import SocialProactive
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.social.wake import wait_duration
from nanobot.config.schema import (
    TelegramSocialConfig,
    TelegramSocialScopeConfig,
    TelegramSocialSleepInterruptConfig,
    TelegramSocialWakeConfig,
)

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.judge import PendingWindows


class SocialWaiting:
    """wait / sleep / set_wake semantics and model-configured wake conditions."""

    def __init__(self, store: SocialStore, bindings: ScopeBindings, proactive: SocialProactive,
                 windows: PendingWindows,
                 notify: Callable[[], None], config: Callable[[], TelegramSocialConfig],
                 now: Callable[[], float]) -> None:
        self.store = store
        self.bindings = bindings
        self.proactive = proactive
        self.windows = windows
        self.notify = notify
        self.config = config
        self.now = now

    def conditions(self, key: str) -> tuple[TelegramSocialWakeConfig, bool]:
        raw = self.store.scopes.state(key)["wake_config"]
        conditions = TelegramSocialWakeConfig.model_validate_json(raw)
        if conditions.expires_at is not None and conditions.expires_at <= self.now():
            self.store.wakes.set_wake_config(key, "{}")
            return TelegramSocialWakeConfig(), False
        return conditions, raw != "{}"

    def effective(self, key: str, binding: TelegramSocialScopeConfig) -> tuple[TelegramSocialWakeConfig, bool]:
        conditions, dynamic = self.conditions(key)
        if not dynamic:
            conditions = TelegramSocialWakeConfig(
                keywords=binding.wake_keywords, senders=binding.wake_senders,
                on_messages=binding.wake_on_messages, on_question=binding.wake_on_question,
                probability=0, one_shot=False,
                coalesce_seconds=binding.coalesce_seconds,
            )
        return conditions, dynamic

    def consume(self, key: str, job: str) -> None:
        """Admission includes a fresh busy-time candidate exposed in the current run."""
        row = self.store.wakes.running_conditions(key, job)
        if row is None or row[0] == "{}":
            return
        conditions, dynamic = self.conditions(key)
        if dynamic and conditions.one_shot and row[0] == self.store.scopes.state(key)["wake_config"]:
            self.store.wakes.set_wake_config(key, "{}")

    def admit_request(self, ctx: RequestContext) -> None:
        job = ctx.metadata.get("_social_job")
        # Ordinary tools, commands, cron and background turns use their normal
        # admission path. Only scheduler deliveries claim a social WakeJob.
        if job is None:
            return
        scope = self.bindings.validate_context(ctx, require_claim=False)
        if not isinstance(job, str) or ctx.turn_id is None:
            raise PermissionError("social execution requires an authorized wake")
        self.conditions(scope.session_key)
        self.proactive.check_job(scope.session_key, job, self.bindings.binding(scope))
        self.store.wakes.claim(scope.session_key, job, self.store.scopes.state(scope.session_key)["version"], ctx.turn_id)
        self.consume(scope.session_key, job)

    def wait(self, ctx: RequestContext, seconds: float | None = None, *, sleeping: bool = False,
             on_timeout: bool = False, purpose: Literal["reply", "messages"] | None = None,
             quiet_seconds: float | None = None, infinite: bool = False,
             until: str | None = None) -> dict[str, Any]:
        scope = self.bindings.validate_context(ctx)
        seconds, deadline = wait_duration(seconds, infinite=infinite, until=until, on_timeout=on_timeout,
                                          now=self.now(), maximum=self.config().max_wait_seconds)
        assert ctx.turn_id is not None
        if purpose is not None:
            if purpose not in {"reply", "messages"} or sleeping:
                raise ValueError("purpose must be reply/messages and cannot be combined with sleeping")
            if purpose == "reply" and seconds is None:
                raise ValueError("reply waiting must have a finite total limit")
            quiet = self.config().dynamic_wait.quiet_seconds if quiet_seconds is None else quiet_seconds
            if not math.isfinite(quiet) or not 0.1 <= quiet <= self.config().dynamic_wait.max_seconds:
                raise ValueError("quiet interval exceeds the operator limit")
            binding = self.bindings.binding(scope)
            assert binding is not None  # validate_context checked the current binding.
            conditions, dynamic = self.effective(scope.session_key, binding)
            result = self.store.wait_for_quiet(scope.session_key, ctx.turn_id, purpose=purpose,
                                               quiet=quiet, seconds=seconds, on_timeout=on_timeout,
                                               condition_expires=conditions.expires_at if purpose == "messages" and (dynamic or infinite) else None,
                                               selective=True, max_delay=self.config().dynamic_wait.max_seconds)
            # wait_for_quiet returns total_due as the dialogue deadline or None.
            if result["deferred"]:
                self.windows.shorten(scope.session_key, quiet, cast(float | None, result["total_due"]))
            ctx.attributes["social_deferred"] = result["deferred"]
            self.notify()
            return result
        if quiet_seconds is not None:
            raise ValueError("quiet_seconds requires an explicit purpose")
        if sleeping and self.config().sleep_observation.enabled:
            due = self.store.wakes.sleep_observation_due(scope.session_key, self.config().sleep_observation.window_seconds)
            if due is not None:
                if deadline is not None:
                    due = min(due, deadline)
                conditions, dynamic = self.conditions(scope.session_key)
                result = self.store.wait_for_quiet(scope.session_key, ctx.turn_id, purpose="observe",
                                                   quiet=self.config().sleep_observation.window_seconds,
                                                   seconds=max(0.1, due - self.now()), on_timeout=on_timeout,
                                                   sleep_seconds=seconds, quiet_due=due, sleep_until=deadline,
                                                   condition_expires=conditions.expires_at if infinite and dynamic else None)
                ctx.attributes["social_deferred"] = True
                self.notify()
                return result
        self.store.wakes.defer(scope.session_key, ctx.turn_id, sleeping=sleeping, seconds=seconds, on_timeout=on_timeout)
        ctx.attributes["social_deferred"] = True
        self.notify()
        return {"mode": "sleeping" if sleeping else "waiting", "wake_at": self.now() + seconds if seconds is not None else None,
                "on_timeout": on_timeout, "infinite": infinite}

    def set_wake(self, ctx: RequestContext, *, keywords: list[str], senders: list[str],
                 on_messages: bool, ttl_seconds: float = 3600, one_shot: bool = True,
                 on_question: bool = False,
                 sleep_interrupts: TelegramSocialSleepInterruptConfig | None = None,
                 coalesce_seconds: float | None = None,
                 proactive_paused: bool | None = None) -> dict[str, Any]:
        scope = self.bindings.validate_context(ctx)
        binding = self.bindings.binding(scope)
        if binding is None or not binding.allow_ambient_wake:
            raise PermissionError("operator disabled model-configured wake conditions")
        if not math.isfinite(ttl_seconds) or not 1 <= ttl_seconds <= self.config().max_wait_seconds:
            raise ValueError("wake conditions TTL exceeds the operator limit")
        if coalesce_seconds is not None:
            limit = min(self.config().max_coalesce_seconds, self.config().dynamic_wait.max_seconds) if self.config().dynamic_wait.enabled else self.config().max_coalesce_seconds
            if not math.isfinite(coalesce_seconds) or not 0 <= coalesce_seconds <= limit:
                raise ValueError("coalescing interval exceeds the operator limit")
        if sleep_interrupts is not None and any(
            getattr(sleep_interrupts, name) and not getattr(binding.sleep_interrupts, name)
            for name in TelegramSocialSleepInterruptConfig.model_fields
        ):
            raise PermissionError("sleep interruption exceeds the operator policy")
        if proactive_paused is not None:
            with transaction(self.store.db):
                self.store.proactive_state.set_stopped(int(proactive_paused), scope.session_key)
            if proactive_paused:
                self.proactive.cancel_invalid({item.session_key: current for item in self.store.scopes.all()
                                               if (current := self.bindings.binding(item)) is not None})
            self.notify()
            if not keywords and not senders and not on_messages and not on_question:
                return {"proactive_paused": proactive_paused}
        if not keywords and not senders and not on_messages and not on_question:
            self.store.wakes.set_wake_config(scope.session_key, "{}")
            return {"cleared": True}
        conditions = TelegramSocialWakeConfig(keywords=keywords, senders=senders, on_messages=on_messages,
                                              on_question=on_question,
                                              sleep_interrupts=sleep_interrupts, coalesce_seconds=coalesce_seconds,
                                              expires_at=self.now() + ttl_seconds, one_shot=one_shot)
        result = conditions.model_dump()
        self.store.wakes.set_wake_config(scope.session_key, conditions.model_dump_json())
        return result

    def keep_reply_intent(self, scope: ConversationScope, turn: str) -> None:
        """Use the existing local wait; a missing reply never calls a model."""
        key = scope.session_key
        binding = self.bindings.binding(scope)
        if binding is None or binding.paused or self.store.scopes.state(key)["mode"] not in {"active", "observing"}:
            return
        confirmed = self.store.deliveries.confirmed_turn(key, turn)
        if confirmed is None:
            return
        conditions, dynamic = self.effective(key, binding)
        result = self.store.wait_for_quiet(key, turn, purpose="messages", quiet=self.config().dynamic_wait.quiet_seconds,
                                  seconds=min(30, self.config().max_wait_seconds), on_timeout=False, selective=True,
                                  condition_expires=conditions.expires_at if dynamic else None,
                                  max_delay=self.config().dynamic_wait.max_seconds)
        # wait_for_quiet returns total_due as the dialogue deadline or None.
        if result["deferred"]:
            self.windows.shorten(key, self.config().dynamic_wait.quiet_seconds, cast(float | None, result["total_due"]))
        self.notify()
