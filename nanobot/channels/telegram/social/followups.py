"""Pending conversation intentions and receipts on the existing memory/store owner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.store import SocialStore

FollowupState = Literal["active", "responded", "completed", "dropped"]


class FollowupUpdate(BaseModel):
    pending_question: str | None = Field(default=None, max_length=800)
    pending_thought: str | None = Field(default=None, max_length=800)
    motivation: str | None = Field(default=None, max_length=400)
    followup_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    followup_state: FollowupState | None = None
    state_reason: str | None = Field(default=None, max_length=400)


class FollowupFields(BaseModel):
    origin: Literal["evidence", "self"] = "evidence"
    origin_turn: str | None = None
    pending_question: str = ""
    pending_thought: str = ""
    motivation: str = ""
    mentioned_at: float | None = None
    followup_at: float | None = None
    followup_state: FollowupState | None = None
    state_reason: str = ""
    responded_at: float | None = None
    confirmed_attempts: int = 0
    reserved_attempts: int = 0
    remaining_attempts: int = 2
    unresolved_send: bool = False
    last_attempt: float | None = None


class SocialFollowups:
    def __init__(self, store: SocialStore) -> None:
        self.store = store
        self.db = store.db

    def describe(self, memory_id: str) -> FollowupFields:
        row = self.db.execute("SELECT * FROM memory_followups WHERE memory_id=?", (memory_id,)).fetchone()
        if row is None:
            return FollowupFields()
        counts = self.db.execute("SELECT SUM(status='sent'),SUM(status IN ('queued','sending','unknown')),MAX(created) "
                                 "FROM followup_attempts WHERE memory_id=?", (memory_id,)).fetchone()
        confirmed, reserved = int(counts[0] or 0), int(counts[1] or 0)
        unknown = self.db.execute("SELECT 1 FROM followup_sends s JOIN followup_attempts a ON a.id=s.attempt_id "
                                  "WHERE a.memory_id=? AND s.status='unknown' LIMIT 1", (memory_id,)).fetchone()
        return FollowupFields.model_validate({
            **{key: row[key] for key in FollowupUpdate.model_fields},
            "origin": "self" if row["own_intent"] else "evidence", "origin_turn": row["origin_turn"],
            "mentioned_at": row["mentioned_at"], "responded_at": row["responded_at"],
            "confirmed_attempts": confirmed, "reserved_attempts": reserved,
            "remaining_attempts": max(0, 2-confirmed-reserved), "unresolved_send": unknown is not None,
            "last_attempt": counts[2],
        })

    def update(self, memory_id: str, turn: str, update: FollowupUpdate, *, own_intent: bool = False) -> None:
        """Caller owns the memory update transaction; revisions never reset attempts."""
        now = self.store.now()
        self.db.execute("INSERT OR IGNORE INTO memory_followups(memory_id,origin_turn,own_intent,mentioned_at) VALUES(?,?,?,?)",
                        (memory_id, turn, int(own_intent), now))
        values = update.model_dump(include=set(FollowupUpdate.model_fields) & update.model_fields_set)
        for name, value in values.items():
            if value is None and name != "followup_at":
                continue
            self.db.execute(f"UPDATE memory_followups SET {name}=? WHERE memory_id=?", (value, memory_id))
        self.db.execute("UPDATE memory_followups SET mentioned_at=? WHERE memory_id=?", (now, memory_id))
        if update.followup_state == "responded":
            self.db.execute("UPDATE memory_followups SET responded_at=? WHERE memory_id=?", (now, memory_id))
        if self.describe(memory_id).confirmed_attempts >= 2:
            self.db.execute("UPDATE memory_followups SET followup_state='dropped',state_reason='attempt_limit' "
                            "WHERE memory_id=? AND followup_state='active'", (memory_id,))

    def _live(self, scope: str, memory_id: str, *, finishing: bool = False) -> int:
        stopped = self.db.execute("SELECT stopped FROM proactive_state WHERE scope=?", (scope,)).fetchone()
        if self.store.state(scope)["mode"] == "paused" or (stopped is not None and stopped[0]):
            raise PermissionError("proactive follow-up contact is paused or stopped")
        row = self.db.execute("SELECT m.revision,m.status,m.expires,f.* FROM memories m "
                              "JOIN memory_followups f ON f.memory_id=m.id WHERE m.scope=? AND m.id=?",
                              (scope, memory_id)).fetchone()
        now = self.store.now()
        if (row is None or row["status"] not in {"candidate", "confirmed"}
                or (row["expires"] is not None and row["expires"] <= now)):
            raise PermissionError("follow-up item is missing, deleted or expired")
        active = row["followup_state"] == "active"
        finishing_limit = finishing and row["followup_state"] == "dropped" and row["state_reason"] == "attempt_limit"
        if not (active or finishing_limit) or not (row["pending_question"] or row["pending_thought"]):
            raise PermissionError("follow-up item is completed, responded to or dropped")
        if row["followup_at"] is not None and row["followup_at"] > now:
            raise PermissionError("follow-up item is not due yet")
        return int(row["revision"])

    def reserve(self, scope: str, memory_id: str, turn: str, delivery: str) -> None:
        """Reserve one attempt per matter/turn; every paced child keeps its receipt."""
        revision = self._live(scope, memory_id)
        info = self.describe(memory_id)
        if info.unresolved_send:
            raise PermissionError("follow-up has an unknown send; resolve its receipt before another attempt")
        attempt = self.db.execute("SELECT id FROM followup_attempts WHERE memory_id=? AND turn_id=?",
                                  (memory_id, turn)).fetchone()
        if attempt is None:
            if info.remaining_attempts == 0:
                raise PermissionError("follow-up proactive limit reached; remaining_attempts=0")
            wake = self.db.execute("SELECT id FROM wakes WHERE scope=? AND turn_id=? ORDER BY created DESC LIMIT 1",
                                   (scope, turn)).fetchone()
            attempt_id = uuid4().hex
            self.db.execute("INSERT INTO followup_attempts(id,memory_id,scope,turn_id,wake_id,revision,status,created,updated) "
                            "VALUES(?,?,?,?,?,?,'queued',?,?)", (attempt_id,memory_id,scope,turn,wake[0] if wake else None,
                                                                revision,self.store.now(),self.store.now()))
        else:
            attempt_id = str(attempt[0])
        self.db.execute("INSERT INTO followup_sends(delivery_id,attempt_id,revision,status) VALUES(?,?,?,'queued')",
                        (delivery, attempt_id, revision))
        self.receipt(delivery, "queued")

    def check_delivery(self, delivery: str) -> None:
        row = self.db.execute("SELECT a.scope,a.memory_id,s.revision FROM followup_sends s "
                              "JOIN followup_attempts a ON a.id=s.attempt_id WHERE s.delivery_id=?", (delivery,)).fetchone()
        if row is not None and self._live(str(row["scope"]), str(row["memory_id"]), finishing=True) != row["revision"]:
            raise PermissionError("follow-up item changed before delivery")

    def receipt(self, delivery: str, status: str, *, confirmed: bool = False) -> None:
        """Caller owns the delivery transaction, including ordinary message receipts."""
        row = self.db.execute("SELECT a.id,a.memory_id FROM followup_sends s JOIN followup_attempts a ON a.id=s.attempt_id "
                              "WHERE s.delivery_id=?", (delivery,)).fetchone()
        if row is None:
            return
        self.db.execute("UPDATE followup_sends SET status=?,confirmed=MAX(confirmed,?) WHERE delivery_id=?",
                        (status, int(confirmed or status == "sent"), delivery))
        statuses = {item[0] for item in self.db.execute("SELECT status FROM followup_sends WHERE attempt_id=?", (row["id"],))}
        aggregate = next((value for value in ("sent", "unknown", "sending", "queued") if value in statuses), "failed")
        if self.db.execute("SELECT 1 FROM followup_sends WHERE attempt_id=? AND confirmed=1", (row["id"],)).fetchone():
            aggregate = "sent"
        self.db.execute("UPDATE followup_attempts SET status=?,updated=? WHERE id=?", (aggregate,self.store.now(),row["id"]))
        if self.describe(str(row["memory_id"])).confirmed_attempts >= 2:
            self.db.execute("UPDATE memory_followups SET followup_state='dropped',state_reason='attempt_limit' "
                            "WHERE memory_id=? AND followup_state='active'", (row["memory_id"],))

    def sync_receipts(self, scope: str | None = None) -> None:
        # Native journals own cancellation/recovery. Pruning a source does not erase
        # a final receipt already retained with the matter.
        query = ("SELECT s.delivery_id,COALESCE(a.status,d.status) AS status "
                 "FROM followup_sends s LEFT JOIN actions a ON a.id=s.delivery_id "
                 "LEFT JOIN message_deliveries d ON d.id=s.delivery_id "
                 "WHERE COALESCE(a.status,d.status) IS NOT NULL AND COALESCE(a.status,d.status)<>s.status")
        if scope is not None:
            query += " AND (a.scope=? OR d.scope=?)"
        for row in self.db.execute(query, (scope,scope) if scope is not None else ()).fetchall():
            self.receipt(str(row["delivery_id"]), str(row["status"]))

    def has_pending_thought(self, scope: str) -> bool:
        for row in self.db.execute("SELECT m.id FROM memories m JOIN memory_followups f ON f.memory_id=m.id "
                                   "WHERE m.scope=? AND m.status IN ('candidate','confirmed') "
                                   "AND (m.expires IS NULL OR m.expires>?) AND f.followup_state='active' "
                                   "AND f.pending_thought<>'' AND (f.followup_at IS NULL OR f.followup_at<=?)",
                                   (scope,self.store.now(),self.store.now())):
            info = self.describe(str(row[0]))
            if info.remaining_attempts > 0 and info.reserved_attempts == 0 and not info.unresolved_send:
                return True
        return False

    def summary(self, scope: str) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for row in self.db.execute("SELECT m.id,m.content FROM memories m JOIN memory_followups f ON f.memory_id=m.id "
                                   "WHERE m.scope=? AND m.status IN ('candidate','confirmed') "
                                   "AND (m.expires IS NULL OR m.expires>?) AND f.followup_state IN ('active','responded') "
                                   "ORDER BY f.mentioned_at DESC LIMIT 4", (scope,self.store.now())):
            info = self.describe(str(row["id"]))
            result.append({"id": row["id"], "content": str(row["content"])[:120],
                           "pending_question": info.pending_question[:160], "pending_thought": info.pending_thought[:160],
                           "motivation": info.motivation[:80], "followup_state": info.followup_state,
                           "confirmed_attempts": info.confirmed_attempts, "responded_at": info.responded_at})
        return result

    def status(self, scope: str) -> list[dict[str, object]]:
        """Small receipt trace for the existing status tool, including dropped matters."""
        result: list[dict[str, object]] = []
        for row in self.db.execute("SELECT m.id,m.status,m.expires FROM memories m JOIN memory_followups f ON f.memory_id=m.id "
                                   "WHERE m.scope=? ORDER BY f.mentioned_at DESC LIMIT 4", (scope,)):
            record = self.describe(str(row["id"])).model_dump(include={
                "followup_state", "state_reason", "confirmed_attempts", "reserved_attempts", "remaining_attempts", "unresolved_send", "last_attempt"})
            attempts: list[dict[str, object]] = []
            for attempt in self.db.execute("SELECT id,turn_id,wake_id,status,created FROM followup_attempts "
                                            "WHERE memory_id=? ORDER BY created DESC LIMIT 2", (row["id"],)):
                attempts.append({**dict(attempt), "receipts": [dict(receipt) for receipt in self.db.execute(
                    "SELECT delivery_id,status,confirmed FROM followup_sends WHERE attempt_id=? ORDER BY rowid DESC LIMIT 3", (attempt["id"],))]})
            result.append({"memory_id":row["id"],"memory_status":row["status"],"expires":row["expires"],
                           **record,"attempts":attempts})
        return result
