"""Local opportunity filtering on the existing SQLite queue; no model calls."""

from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.memory import SocialMemory
from nanobot.channels.telegram.social.models import ConversationScope, ProactiveCandidate
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import TelegramSocialConfig, TelegramSocialScopeConfig


def is_quiet(binding: TelegramSocialScopeConfig, now: float) -> bool:
    quiet = binding.quiet_hours
    if not quiet.enabled:
        return False
    local = datetime.fromtimestamp(now, ZoneInfo(quiet.timezone))
    minute = local.hour * 60 + local.minute
    start, end = (int(value[:2]) * 60 + int(value[3:]) for value in (quiet.start, quiet.end))
    return start <= minute < end if start < end else minute >= start or minute < end


class SocialProactive:
    def __init__(self, store: SocialStore, memory: SocialMemory, *,
                 random_value: Callable[[], float] = random.random) -> None:
        self.store = store
        self.memory = memory
        self.db = store.db
        self.random_value = random_value

    def _next_idle_check(self, binding: TelegramSocialScopeConfig) -> float:
        policy = binding.proactive
        return self.store.now() + policy.min_interval_seconds + self.random_value() * (
            policy.max_interval_seconds - policy.min_interval_seconds)

    def _next_activity_check(self, binding: TelegramSocialScopeConfig) -> float:
        return self.store.now() + binding.proactive.activity_interval_seconds

    def sync(self, scope: ConversationScope, binding: TelegramSocialScopeConfig | None) -> None:
        key = scope.session_key
        version = self.store.scopes.state(key)["version"]
        enabled = binding is not None and binding.proactive.enabled and not binding.paused
        activity_enabled = binding is not None and binding.proactive.activity_enabled
        row = self.store.proactive_state.get(key)
        name = f"proactive_schedule:{key}"
        source = json.dumps({"enabled": enabled, "policy": binding.proactive.model_dump() if binding else None}, sort_keys=True)
        previous = self.store.scopes.setting_value(name)
        same_policy = previous is None or previous == source
        self.store.scopes.setting_revision(name, source)
        if (row is not None and row["version"] == version and (row["next_check"] is not None) == enabled
                and (row["next_activity_check"] is not None) == (enabled and activity_enabled) and same_policy):
            return
        idle_due = (row["next_check"] if row is not None and row["next_check"] is not None and enabled and same_policy
                    else self._next_idle_check(binding) if enabled and binding is not None else None)
        activity_due = (row["next_activity_check"] if row is not None and row["next_activity_check"] is not None and enabled and activity_enabled and same_policy
                        else self._next_activity_check(binding) if enabled and activity_enabled and binding is not None else None)
        with transaction(self.db):
            self.store.proactive_state.sync(key, version, idle_due, activity_due, "waiting" if enabled else "disabled", self.store.now())

    def status(self, key: str) -> dict[str, object]:
        row = self.store.proactive_state.status(key)
        result: dict[str, object] = dict(row) if row is not None else {"next_check": None, "last_check": None, "decision": "disabled"}
        if row is not None:
            result["metrics"] = json.loads(row["metrics"])
            result["requireContext_applies_to"] = "legacy source candidates only"
            wake = self.store.wakes.latest_opportunity(key)
            if wake is not None:
                result["last_opportunity"] = {
                    "wake_id": wake["id"], "main_status": wake["status"], "turn_id": wake["turn_id"],
                    "source": json.loads(wake["proactive_source"]),
                    "sends": [dict(item) for item in self.store.deliveries.turn_actions(key, wake["turn_id"])],
                }
        return result

    def _deferred(self, key: str) -> bool:
        state = self.store.scopes.state(key)
        return (state["mode"] in {"waiting", "paused"}
                or self.store.wakes.has_non_sleep_wait(key)
                or (state["wake_config"] != "{}" and state["mode"] == "sleeping"))

    def _participation(self, key: str) -> dict[str, object]:
        """Only confirmed public message IDs count, including the ordinary message tool."""
        now = self.store.now()
        rows = self.store.messages.participation(key)
        recent = [row for row in rows if now - 3600 < float(row["at"]) <= now]
        tracked = self.store.proactive_state.tracked_since(key)[0]
        first = self.store.messages.first(key)
        coverage = float(tracked)
        if first is not None and first["seq"] > 1:
            coverage = max(coverage, float(first["at"]))
        last = max((float(row["at"]) for row in rows), default=None)
        return {"confirmed_messages_hour": len(recent), "confirmed_groups_hour": len({row["send_group"] for row in recent}),
                "last_confirmed_send": last, "history_complete": coverage <= now - 3600,
                "coverage_since": coverage}

    def valid(self, key: str, binding: TelegramSocialScopeConfig | None, candidate: ProactiveCandidate,
              *, for_send: bool = False) -> str | None:
        now = self.store.now()
        if binding is None or binding.paused or not binding.proactive.enabled:
            return "disabled"
        if is_quiet(binding, now):
            return "quiet_hours"
        # Bound scopes are synced before candidate validation.
        if cast(sqlite3.Row, self.store.proactive_state.stopped(key))[0]:
            return "stopped"
        if self.store.scopes.state(key)["mode"] == "paused" or (not for_send and self._deferred(key)):
            return "deferred"
        if candidate.expires_at <= now:
            return "expired"
        if candidate.kind == "memory":
            row = self.memory.candidate_state(key, candidate.memory_id)
            if row is None or row["status"] not in {"candidate", "confirmed"} or row["revision"] != candidate.revision or (row["expires"] is not None and float(row["expires"]) <= now):
                return "memory_changed"
        elif candidate.kind == "unread":
            state = self.store.scopes.state(key)
            row = self.store.messages.timestamp(key, candidate.seq)
            if row is None or (not for_send and state["read_through"] >= candidate.seq) or not 0 <= now - float(row[0]) < binding.proactive.context_max_age_seconds:
                return "context_expired"
        elif candidate.kind in {"idle", "activity"}:
            if candidate.kind == "activity" and not binding.proactive.activity_enabled:
                return "disabled"
            if not for_send:
                state = self.store.scopes.last_member_at(key)
                if state[0] is not None and now - float(state[0]) < binding.proactive.idle_seconds:
                    return "member_active"
                if self.store.judge.pending(key):
                    return "conversation_arrived"
        elif binding.proactive.require_context:
            return "context_required"
        return None

    def tick(self, scope: ConversationScope, binding: TelegramSocialScopeConfig, config: TelegramSocialConfig) -> None:
        key, now = scope.session_key, self.store.now()
        row = self.store.proactive_state.get(key)
        if row is None:
            return
        policy = binding.proactive
        idle_ready = row["next_check"] is not None and float(row["next_check"]) <= now
        activity_ready = policy.activity_enabled and row["next_activity_check"] is not None and float(row["next_activity_check"]) <= now
        if not idle_ready and not activity_ready:
            return
        candidate: ProactiveCandidate | None = None
        probability: float | None = None
        sample: float | None = None
        metrics = self._participation(key)
        hourly = metrics["confirmed_messages_hour"]
        member_at = self.store.scopes.last_member_at(key)[0]
        idle = now - float(member_at if member_at is not None else row["tracked_since"])
        metrics.update({"last_member_at": member_at, "idle_seconds": idle, "base_probability": policy.probability})
        common = None
        if row["stopped"]:
            common = "stopped"
        elif binding.paused or not policy.enabled:
            common = "disabled"
        elif is_quiet(binding, now):
            common = "quiet_hours"
        elif self._deferred(key):
            common = "deferred"
        elif self.store.wakes.active_wake(key, now):
            common = "busy"
        elif self.store.judge.pending(key):
            common = "busy"
        elif self.store.scopes.state(key)["mode"] == "active":
            common = "busy"
        elif idle < policy.idle_seconds:
            common = "member_active"
        idle_decision = None
        if idle_ready:
            idle_decision = "probability_miss"
            if now >= float(row["next_check"]) + policy.ttl_seconds:
                idle_decision = "missed"
            elif common:
                idle_decision = common
            elif (self.store.deliveries.attempt_count(key, now - 3600) >= config.max_sends_per_hour
                  or (isinstance(hourly, int) and hourly >= config.max_sends_per_hour)):
                idle_decision = "send_budget"
            elif self.store.wakes.request_count(key, now - 3600) >= config.max_requests_per_hour:
                idle_decision = "request_budget"
            else:
                metrics["pending_thought_available"] = self.store.followups.has_pending_thought(key)
                probability = policy.probability
                if metrics["pending_thought_available"]:
                    probability = min(1, probability * policy.pending_thought_multiplier)
                last = metrics["last_confirmed_send"]
                if isinstance(last, (float, int)) and now - last < policy.recent_send_seconds:
                    probability *= policy.recent_send_multiplier
                local = datetime.fromtimestamp(now, ZoneInfo(policy.timezone))
                if local.hour >= 23 or local.hour < 8:
                    probability *= policy.night_multiplier
                if isinstance(hourly, int) and hourly >= policy.hourly_send_threshold:
                    probability *= policy.frequent_send_multiplier
                sample = self.random_value()
                if sample < probability:
                    idle_decision = "scheduled"
                    candidate = ProactiveCandidate(kind="idle", seq=0, expires_at=now + policy.ttl_seconds,
                                                   opportunity_id=int(row["opportunity_seq"]) + 1)
        activity_decision = None
        if activity_ready:
            if candidate is not None:
                activity_decision = "idle_selected"
            elif now >= float(row["next_activity_check"]) + policy.activity_ttl_seconds:
                activity_decision = "missed"
            elif common:
                activity_decision = common
            else:
                # Work is independent of the probability and budget for public speech.
                # With allTopics, use the conversation members most recently used rather
                # than running identical idle projects in every dormant topic.
                target = self.store.scopes.latest_member_scope(scope.channel, scope.chat_id)
                if binding.all_topics and target is not None and target[0] != key:
                    activity_decision = "other_topic_active"
                elif self.store.wakes.request_count(key, now - 3600) >= config.max_requests_per_hour:
                    activity_decision = "request_budget"
                else:
                    activity_decision = "scheduled"
                    candidate = ProactiveCandidate(kind="activity", seq=0, expires_at=now + policy.activity_ttl_seconds,
                                                   opportunity_id=int(row["opportunity_seq"]) + 1)
        decision = idle_decision if activity_decision is None else f"idle:{idle_decision or 'not_due'};activity:{activity_decision}"
        with transaction(self.db):
            self.store.proactive_state.record_check(self._next_idle_check(binding) if idle_ready else row["next_check"], self._next_activity_check(binding) if activity_ready else row["next_activity_check"], now, decision, probability, sample, int(candidate is not None) if sample is not None or activity_decision is not None else None, json.dumps(metrics), key)
            if candidate is not None:
                # A configured timer event has no source member message or read credit.
                self.store.wakes.schedule_opportunity(key, self.store.scopes.state(key)["version"], candidate, now)

    def cancel_invalid(self, bindings: dict[str, TelegramSocialScopeConfig]) -> None:
        for row in self.store.wakes.active_opportunities():
            candidate = ProactiveCandidate.model_validate_json(row["proactive_source"])
            reason = self.valid(str(row["scope"]), bindings.get(str(row["scope"])), candidate, for_send=row["status"] == "running")
            if reason is not None:
                self._cancel(str(row["id"]), str(row["scope"]), reason)

    def _cancel(self, job: str, key: str, reason: str) -> None:
        with transaction(self.db):
            self.store.cancel_opportunity(job, reason)
            self.store.proactive_state.set_decision(reason, key)

    def check_job(self, key: str, job: str, binding: TelegramSocialScopeConfig | None, *, for_send: bool = False) -> None:
        row = self.store.wakes.opportunity(key, job)
        if row is None or row["reason"] != "proactive":
            return
        reason = "stale" if row["status"] in {"cancelled", "expired", "failed"} else self.valid(key, binding, ProactiveCandidate.model_validate_json(row["proactive_source"]), for_send=for_send)
        if reason is not None:
            self._cancel(job, key, reason)
            raise PermissionError(f"proactive opportunity skipped: {reason}")

    def check_send(self, scope: ConversationScope, turn: str | None, binding: TelegramSocialScopeConfig | None) -> None:
        if turn is None:
            return
        for row in self.store.wakes.turn_opportunities(scope.session_key, turn):
            self.check_job(scope.session_key, str(row[0]), binding, for_send=True)
