"""Local opportunity filtering on the existing SQLite queue; no model calls."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from nanobot.channels.telegram.social.memory import MemoryRequest, SocialMemory
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
    def __init__(self, store: SocialStore, memory: SocialMemory) -> None:
        self.store = store
        self.memory = memory
        self.db = store.db

    def sync(self, scope: ConversationScope, binding: TelegramSocialScopeConfig | None) -> None:
        key = scope.session_key
        version = self.store.state(key)["version"]
        enabled = binding is not None and binding.proactive.enabled and not binding.paused
        row = self.db.execute("SELECT * FROM proactive_state WHERE scope=?", (key,)).fetchone()
        due = self.store.now() + binding.proactive.interval_seconds if enabled and binding is not None else None
        if row is not None and row["version"] == version and (row["next_check"] is not None) == enabled:
            return
        with self.db:
            self.db.execute("INSERT INTO proactive_state(scope,version,next_check,decision) VALUES(?,?,?,?) "
                            "ON CONFLICT(scope) DO UPDATE SET version=excluded.version,next_check=excluded.next_check,"
                            "decision=excluded.decision", (key, version, due, "waiting" if enabled else "disabled"))

    def status(self, key: str) -> dict[str, object]:
        row = self.db.execute("SELECT next_check,last_check,decision FROM proactive_state WHERE scope=?", (key,)).fetchone()
        return dict(row) if row is not None else {"next_check": None, "last_check": None, "decision": "disabled"}

    def _candidate(self, key: str, binding: TelegramSocialScopeConfig, versions: dict[str, int], last_seq: int) -> ProactiveCandidate | None:
        now = self.store.now()
        state = self.store.state(key)
        with self.db:
            memories = self.memory.list(key, MemoryRequest(action="list"), all_rows=True)
        active = {entry.id for entry in memories}
        for memory_id in list(versions):
            if memory_id not in active:
                del versions[memory_id]
        ideas = sorted((entry for entry in memories if entry.kind in {"pending", "topic"}
                        and versions.get(entry.id, 0) < entry.revision),
                       key=lambda entry: (entry.kind == "pending", entry.updated), reverse=True)
        if ideas:
            entry = ideas[0]
            return ProactiveCandidate(kind="memory", memory_id=entry.id, revision=entry.revision,
                                      seq=state["event_seq"], expires_at=min(now + binding.proactive.ttl_seconds, entry.expires or float("inf")))
        seq = state["event_seq"]
        if seq > max(last_seq, state["read_through"], self.store.judge_through(key)):
            row = self.db.execute("SELECT at FROM messages WHERE scope=? AND seq=?", (key, seq)).fetchone()
            if row is not None and 0 <= now - float(row[0]) < binding.proactive.context_max_age_seconds:
                return ProactiveCandidate(kind="unread", seq=seq, expires_at=min(
                    now + binding.proactive.ttl_seconds, float(row[0]) + binding.proactive.context_max_age_seconds))
        if not binding.proactive.require_context:
            return ProactiveCandidate(kind="configured", seq=seq, expires_at=now + binding.proactive.ttl_seconds)
        return None

    def valid(self, key: str, binding: TelegramSocialScopeConfig | None, candidate: ProactiveCandidate,
              *, for_send: bool = False) -> str | None:
        now = self.store.now()
        if binding is None or binding.paused or not binding.proactive.enabled:
            return "disabled"
        if is_quiet(binding, now):
            return "quiet_hours"
        mode = self.store.state(key)["mode"]
        if mode == "paused" or (not for_send and mode in {"sleeping", "waiting"}):
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
        elif binding.proactive.require_context:
            return "context_required"
        return None

    def tick(self, scope: ConversationScope, binding: TelegramSocialScopeConfig, config: TelegramSocialConfig) -> None:
        key, now = scope.session_key, self.store.now()
        row = self.db.execute("SELECT * FROM proactive_state WHERE scope=?", (key,)).fetchone()
        if row is None or row["next_check"] is None or float(row["next_check"]) > now:
            return
        decision = "no_context"
        versions = TypeAdapter(dict[str, int]).validate_json(row["memory_versions"])
        candidate: ProactiveCandidate | None = None
        if now >= float(row["next_check"]) + binding.proactive.ttl_seconds:
            decision = "missed"
        elif is_quiet(binding, now):
            decision = "quiet_hours"
        elif self.store.state(key)["mode"] in {"waiting", "sleeping", "paused"}:
            decision = "deferred"
        elif self.db.execute("SELECT 1 FROM wakes WHERE scope=? AND status IN ('scheduled','queued','running') AND expires>?", (key, now)).fetchone():
            decision = "busy"
        elif self.db.execute("SELECT 1 FROM judge_state WHERE scope=? AND (pending_due IS NOT NULL OR inflight=1)", (key,)).fetchone():
            decision = "busy"
        elif self.db.execute("SELECT COUNT(*) FROM send_attempts WHERE scope=? AND at>?", (key, now - 3600)).fetchone()[0] >= config.max_sends_per_hour:
            decision = "send_budget"
        elif self.db.execute("SELECT COUNT(*) FROM requests WHERE scope=? AND at>?", (key, now - 3600)).fetchone()[0] >= config.max_requests_per_hour:
            decision = "request_budget"
        else:
            candidate = self._candidate(key, binding, versions, int(row["last_seq"]))
            if candidate is not None:
                decision = "scheduled"
        with self.db:
            if candidate is not None:
                if candidate.memory_id is not None and candidate.revision is not None:
                    versions[candidate.memory_id] = candidate.revision
            self.db.execute("UPDATE proactive_state SET next_check=?,last_check=?,decision=?,last_seq=?,memory_versions=? WHERE scope=?",
                            (now + binding.proactive.interval_seconds, now, decision,
                             max(int(row["last_seq"]), self.store.state(key)["event_seq"]) if candidate is not None else int(row["last_seq"]),
                             json.dumps(versions), key))
            if candidate is not None:
                self.store.queue_judgement(key, candidate.seq, delay=0, maximum=config.dynamic_wait.max_seconds,
                                           opportunity=candidate.model_dump_json())

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
