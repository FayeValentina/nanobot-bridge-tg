"""Local opportunity filtering on the existing SQLite queue; no model calls."""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

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

    def _next_check(self, binding: TelegramSocialScopeConfig) -> float:
        policy = binding.proactive
        return self.store.now() + policy.min_interval_seconds + self.random_value() * (
            policy.max_interval_seconds - policy.min_interval_seconds)

    def sync(self, scope: ConversationScope, binding: TelegramSocialScopeConfig | None) -> None:
        key = scope.session_key
        version = self.store.state(key)["version"]
        enabled = binding is not None and binding.proactive.enabled and not binding.paused
        row = self.db.execute("SELECT * FROM proactive_state WHERE scope=?", (key,)).fetchone()
        if row is not None and row["version"] == version and (row["next_check"] is not None) == enabled:
            return
        due = self._next_check(binding) if enabled and binding is not None else None
        with self.db:
            self.db.execute("INSERT INTO proactive_state(scope,version,next_check,decision,tracked_since) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(scope) DO UPDATE SET version=excluded.version,next_check=excluded.next_check,"
                            "decision=excluded.decision", (key, version, due, "waiting" if enabled else "disabled", self.store.now()))

    def status(self, key: str) -> dict[str, object]:
        row = self.db.execute("SELECT next_check,last_check,decision,opportunity_seq,probability,sample,selected,metrics,stopped "
                              "FROM proactive_state WHERE scope=?", (key,)).fetchone()
        result: dict[str, object] = dict(row) if row is not None else {"next_check": None, "last_check": None, "decision": "disabled"}
        if row is not None:
            result["metrics"] = json.loads(row["metrics"])
            result["retired_config"] = ["intervalSeconds"]
            result["requireContext_applies_to"] = "legacy source candidates only"
            wake = self.db.execute("SELECT id,status,turn_id,proactive_source FROM wakes WHERE scope=? AND reason='proactive' "
                                   "ORDER BY created DESC LIMIT 1", (key,)).fetchone()
            if wake is not None:
                result["last_opportunity"] = {
                    "wake_id": wake["id"], "main_status": wake["status"], "turn_id": wake["turn_id"],
                    "source": json.loads(wake["proactive_source"]),
                    "sends": [dict(item) for item in self.db.execute(
                        "SELECT id,status,message_id FROM actions WHERE scope=? AND turn_id=?", (key, wake["turn_id"]))],
                    "message_deliveries": [dict(item) for item in self.db.execute(
                        "SELECT id,status,message_ids FROM message_deliveries WHERE scope=? AND turn_id=?", (key, wake["turn_id"]))],
                }
        return result

    def _deferred(self, key: str) -> bool:
        state = self.store.state(key)
        return (state["mode"] in {"waiting", "paused"}
                or self.db.execute("SELECT 1 FROM dialogue_waits WHERE scope=? AND purpose<>'sleep'", (key,)).fetchone() is not None
                or (state["wake_config"] != "{}" and state["mode"] == "sleeping"))

    def _participation(self, key: str) -> dict[str, object]:
        """Only confirmed public message IDs count, including the ordinary message tool."""
        now = self.store.now()
        rows = self.db.execute("SELECT m.message_id,MAX(m.at) AS at,COALESCE(a.group_id,d.id,CAST(m.message_id AS TEXT)) AS send_group "
                              "FROM messages m LEFT JOIN actions a ON a.scope=m.scope AND a.message_id=m.message_id AND a.status='sent' "
                              "LEFT JOIN message_deliveries d ON d.scope=m.scope AND d.status='sent' "
                              "AND m.message_id IN (SELECT value FROM json_each(d.message_ids)) "
                              "WHERE m.scope=? AND m.is_self=1 GROUP BY m.message_id", (key,)).fetchall()
        recent = [row for row in rows if now - 3600 < float(row["at"]) <= now]
        tracked = self.db.execute("SELECT tracked_since FROM proactive_state WHERE scope=?", (key,)).fetchone()[0]
        first = self.db.execute("SELECT seq,at FROM messages WHERE scope=? ORDER BY seq LIMIT 1", (key,)).fetchone()
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
        if self.db.execute("SELECT stopped FROM proactive_state WHERE scope=?", (key,)).fetchone()[0]:
            return "stopped"
        if self.store.state(key)["mode"] == "paused" or (not for_send and self._deferred(key)):
            return "deferred"
        if candidate.expires_at <= now:
            return "expired"
        if candidate.kind == "memory":
            row = self.db.execute("SELECT status,revision,expires FROM memories WHERE scope=? AND id=?",
                                  (key, candidate.memory_id)).fetchone()
            if row is None or row["status"] not in {"candidate", "confirmed"} or row["revision"] != candidate.revision or (row["expires"] is not None and float(row["expires"]) <= now):
                return "memory_changed"
        elif candidate.kind == "unread":
            state = self.store.state(key)
            row = self.db.execute("SELECT at FROM messages WHERE scope=? AND seq=?", (key, candidate.seq)).fetchone()
            if row is None or (not for_send and state["read_through"] >= candidate.seq) or not 0 <= now - float(row[0]) < binding.proactive.context_max_age_seconds:
                return "context_expired"
        elif candidate.kind == "idle":
            if not for_send:
                state = self.db.execute("SELECT last_member_at FROM scopes WHERE key=?", (key,)).fetchone()
                if state[0] is not None and now - float(state[0]) < binding.proactive.idle_seconds:
                    return "member_active"
                if self.db.execute("SELECT 1 FROM judge_state WHERE scope=? AND (pending_due IS NOT NULL OR inflight=1)", (key,)).fetchone():
                    return "conversation_arrived"
        elif binding.proactive.require_context:
            return "context_required"
        return None

    def tick(self, scope: ConversationScope, binding: TelegramSocialScopeConfig, config: TelegramSocialConfig) -> None:
        key, now = scope.session_key, self.store.now()
        row = self.db.execute("SELECT * FROM proactive_state WHERE scope=?", (key,)).fetchone()
        if row is None or row["next_check"] is None or float(row["next_check"]) > now:
            return
        decision = "probability_miss"
        candidate: ProactiveCandidate | None = None
        policy = binding.proactive
        probability: float | None = None
        sample: float | None = None
        metrics = self._participation(key)
        hourly = metrics["confirmed_messages_hour"]
        member_at = self.db.execute("SELECT last_member_at FROM scopes WHERE key=?", (key,)).fetchone()[0]
        idle = now - float(member_at if member_at is not None else row["tracked_since"])
        metrics.update({"last_member_at": member_at, "idle_seconds": idle, "base_probability": policy.probability})
        if now >= float(row["next_check"]) + binding.proactive.ttl_seconds:
            decision = "missed"
        elif row["stopped"]:
            decision = "stopped"
        elif binding.paused or not policy.enabled:
            decision = "disabled"
        elif is_quiet(binding, now):
            decision = "quiet_hours"
        elif self._deferred(key):
            decision = "deferred"
        elif self.db.execute("SELECT 1 FROM wakes WHERE scope=? AND status IN ('scheduled','queued','running') AND expires>?", (key, now)).fetchone():
            decision = "busy"
        elif self.db.execute("SELECT 1 FROM judge_state WHERE scope=? AND (pending_due IS NOT NULL OR inflight=1)", (key,)).fetchone():
            decision = "busy"
        elif self.store.state(key)["mode"] == "active":
            decision = "busy"
        elif idle < policy.idle_seconds:
            decision = "member_active"
        elif (self.db.execute("SELECT COUNT(*) FROM send_attempts WHERE scope=? AND at>?", (key, now - 3600)).fetchone()[0] >= config.max_sends_per_hour
              or (isinstance(hourly, int) and hourly >= config.max_sends_per_hour)):
            decision = "send_budget"
        elif self.db.execute("SELECT COUNT(*) FROM requests WHERE scope=? AND at>?", (key, now - 3600)).fetchone()[0] >= config.max_requests_per_hour:
            decision = "request_budget"
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
                decision = "scheduled"
                candidate = ProactiveCandidate(kind="idle", seq=0, expires_at=now + policy.ttl_seconds,
                                               opportunity_id=int(row["opportunity_seq"]) + 1)
        with self.db:
            self.db.execute("UPDATE proactive_state SET next_check=?,last_check=?,decision=?,opportunity_seq=opportunity_seq+1,"
                            "probability=?,sample=?,selected=?,metrics=? WHERE scope=?",
                            (self._next_check(binding), now, decision, probability, sample,
                             int(candidate is not None) if sample is not None else None, json.dumps(metrics), key))
            if candidate is not None:
                # A configured timer event has no source member message or read credit.
                self.db.execute("INSERT INTO wakes(id,scope,version,trigger_seq,source_seq,status,created,expires,due,reason,proactive_source) "
                                "VALUES(?,?,?,0,0,'scheduled',?,?,?,'proactive',?)",
                                (uuid4().hex, key, self.store.state(key)["version"], now, candidate.expires_at,
                                 now, candidate.model_dump_json()))

    def cancel_invalid(self, bindings: dict[str, TelegramSocialScopeConfig]) -> None:
        for row in self.db.execute("SELECT id,scope,proactive_source,status FROM wakes WHERE reason='proactive' "
                                   "AND status IN ('scheduled','queued','running')").fetchall():
            candidate = ProactiveCandidate.model_validate_json(row["proactive_source"])
            reason = self.valid(str(row["scope"]), bindings.get(str(row["scope"])), candidate, for_send=row["status"] == "running")
            if reason is not None:
                self._cancel(str(row["id"]), str(row["scope"]), reason)

    def _cancel(self, job: str, key: str, reason: str) -> None:
        with self.db:
            self.store.cancel_opportunity(job, reason)
            self.db.execute("UPDATE proactive_state SET decision=? WHERE scope=?", (reason, key))

    def check_job(self, key: str, job: str, binding: TelegramSocialScopeConfig | None, *, for_send: bool = False) -> None:
        row = self.db.execute("SELECT reason,proactive_source,status FROM wakes WHERE id=? AND scope=?", (job, key)).fetchone()
        if row is None or row["reason"] != "proactive":
            return
        reason = "stale" if row["status"] in {"cancelled", "expired", "failed"} else self.valid(key, binding, ProactiveCandidate.model_validate_json(row["proactive_source"]), for_send=for_send)
        if reason is not None:
            self._cancel(job, key, reason)
            raise PermissionError(f"proactive opportunity skipped: {reason}")

    def check_send(self, scope: ConversationScope, turn: str | None, binding: TelegramSocialScopeConfig | None) -> None:
        if turn is None:
            return
        for row in self.db.execute("SELECT id FROM wakes WHERE scope=? AND turn_id=? AND reason='proactive'", (scope.session_key, turn)).fetchall():
            self.check_job(scope.session_key, str(row[0]), binding, for_send=True)
