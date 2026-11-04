from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.db.messages import Messages
from nanobot.channels.telegram.social.db.scopes import Scopes
from nanobot.channels.telegram.social.db.views import Views
from nanobot.channels.telegram.social.db.wakes import WakeReason, Wakes
from nanobot.channels.telegram.social.judge_models import (
    JUDGE_PROMPT_VERSION,
    JudgeDecision,
    JudgeRecord,
    JudgeSnapshot,
)


class Judge:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], float], scopes: Scopes,
                 messages: Messages, views: Views, wakes: Wakes) -> None:
        self.db, self.now = db, now
        self.scopes, self.messages, self.views, self.wakes = scopes, messages, views, wakes

    def through(self, key: str) -> int:
        row = self.db.execute("SELECT assessed_through FROM judge_state WHERE scope=?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def record(self, batch: str) -> JudgeRecord:
        row = self.db.execute("SELECT * FROM judge_batches WHERE id=?", (batch,)).fetchone()
        if row is None:
            raise ValueError("unknown judge batch")
        body = dict(row)
        body["decision"] = JudgeDecision.model_validate_json(body["decision"]) if body["decision"] else None
        return JudgeRecord.model_validate({key: value for key, value in body.items() if key in JudgeRecord.model_fields})

    def current(self, snapshot: JudgeSnapshot) -> bool:
        state = self.scopes.state(snapshot.scope)
        if (state["version"] != snapshot.version or state["mode"] == "paused"
                or not snapshot.created <= self.now() < snapshot.expires):
            return False
        displayed = {message.seq: message for message in snapshot.messages}
        rows = self.messages.displayed_revisions(snapshot.scope, list(displayed))
        if len(rows) != len(displayed) or any(
            row["message_id"] != displayed[row["seq"]].message_id
            or row["sender_id"] != displayed[row["seq"]].sender_id
            or bool(row["is_self"]) != displayed[row["seq"]].is_self
            or row["revision"] != displayed[row["seq"]].revision
            or not str(row["text"]).startswith(displayed[row["seq"]].text) for row in rows
        ):
            return False
        # Do not advance assessment across retained messages absent from the input.
        return not any(row[0] not in displayed for row in self.messages.sequences_between(snapshot.scope, snapshot.after_seq, snapshot.through_seq))

    def open_batch(self, snapshot: JudgeSnapshot, revision: int, model: str) -> JudgeRecord:
        if not self.current(snapshot):
            raise PermissionError("judge snapshot is expired, changed, paused or not retained")
        with transaction(self.db):
            existing = self.db.execute("SELECT id,judge_revision FROM judge_batches WHERE id=?", (snapshot.batch_id,)).fetchone()
            if existing:
                if existing["judge_revision"] != revision or not self._input_matches(snapshot.batch_id, snapshot):
                    raise PermissionError("judge batch input changed")
                return self.record(str(existing[0]))
            if snapshot.after_seq != self.through(snapshot.scope):
                raise PermissionError("judge batch must start at the assessment watermark")
            batch = snapshot.batch_id
            self.db.execute("INSERT INTO judge_batches(id,scope,version,judge_revision,model,prompt_version,after_seq,through_seq,created,expires,due,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending')",
                            (batch, snapshot.scope, snapshot.version, revision, model,
                             JUDGE_PROMPT_VERSION, snapshot.after_seq, snapshot.through_seq, snapshot.created,
                             snapshot.expires, self.now()))
            self.db.execute("UPDATE judge_batches SET input_meta=?,condition_config=? WHERE id=?",
                            (json.dumps(self._metadata(snapshot)), self.scopes.state(snapshot.scope)["wake_config"], batch))
            self.db.execute("DELETE FROM judge_batches WHERE created<? AND status NOT IN ('pending','running')",
                            (self.now() - 86400,))
        return self.record(batch)

    def claim_attempt(self, batch: str, snapshot: JudgeSnapshot, *, limit: int,
                            interval: float, max_attempts: int) -> JudgeRecord:
        with transaction(self.db):
            record = self.record(batch)
            if not self._input_matches(batch, snapshot):
                raise PermissionError("judge batch input changed")
            if record.status != "pending":
                return record
            if not self.current(snapshot) or snapshot.after_seq != self.through(snapshot.scope):
                status = "expired" if self.now() >= record.expires else "cancelled"
                self.db.execute("UPDATE judge_batches SET status=?,error_code='stale_input' WHERE id=?", (status, batch))
                return self.record(batch)
            if record.attempts >= max_attempts or record.due > self.now():
                return record
            active = self.db.execute("SELECT 1 FROM judge_batches WHERE scope=? AND status='running'", (record.scope,)).fetchone()
            recent = self.db.execute("SELECT started FROM judge_attempts WHERE started>? ORDER BY started",
                                     (self.now() - 3600,)).fetchall()
            last = self.db.execute("SELECT MAX(a.started) FROM judge_attempts a JOIN judge_batches b ON b.id=a.batch_id WHERE b.scope=?", (record.scope,)).fetchone()[0]
            due = max(record.due, float(last) + interval if last is not None else self.now())
            if active:
                due = max(due, self.now() + 1)
            if len(recent) >= limit:
                due = max(due, float(recent[-limit][0]) + 3600)
            if due > self.now():
                self.db.execute("UPDATE judge_batches SET due=?,error_code='budget_or_busy' WHERE id=?", (due, batch))
                return self.record(batch)
            self.db.execute("UPDATE judge_batches SET status='running',attempts=attempts+1 WHERE id=?", (batch,))
            self.db.execute("INSERT INTO judge_attempts(batch_id,attempt,started) VALUES(?,?,?)",
                            (batch, record.attempts + 1, self.now()))
        return self.record(batch)

    def finish_attempt(self, batch: str, snapshot: JudgeSnapshot, *, attempt: int,
                             decision: JudgeDecision | None, error_code: str | None,
                             delay: float, latency_ms: int, input_tokens: int | None,
                             output_tokens: int | None, total_tokens: int | None) -> JudgeRecord:
        if decision is not None:
            decision.validate_for(snapshot)
        with transaction(self.db):
            record = self.record(batch)
            if not self._input_matches(batch, snapshot):
                raise PermissionError("judge batch input changed")
            self.db.execute("UPDATE judge_attempts SET finished=?,error_code=?,latency_ms=?,input_tokens=?,output_tokens=?,total_tokens=? WHERE batch_id=? AND attempt=? AND finished IS NULL",
                            (self.now(), error_code, latency_ms, input_tokens, output_tokens, total_tokens, batch, attempt))
            if record.status != "running" or record.attempts != attempt:
                return record  # Keep late usage, but never resurrect a cancelled decision.
            if not self.current(snapshot) or self.through(snapshot.scope) != snapshot.after_seq:
                status = "expired" if self.now() >= snapshot.expires else "cancelled"
                self.db.execute("UPDATE judge_batches SET status=?,error_code='stale_input' WHERE id=?", (status, batch))
            elif decision is not None:
                self.db.execute("UPDATE judge_batches SET status='decided',decision=?,error_code=NULL WHERE id=?",
                                (decision.model_dump_json(), batch))
                self.db.execute("INSERT INTO judge_state(scope,assessed_through) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET assessed_through=excluded.assessed_through", (snapshot.scope, snapshot.through_seq))
                self._handoff(batch, snapshot, decision)
            else:
                self.db.execute("UPDATE judge_batches SET status='pending',due=?,error_code=? WHERE id=?",
                                (self.now() + delay, error_code, batch))
        return self.record(batch)

    def decide_locally(self, batch: str, snapshot: JudgeSnapshot, reason: str) -> JudgeRecord:
        """Record a host wake decision for a batch the intermediary need not assess."""
        decision = JudgeDecision(decision="wake", through_seq=snapshot.through_seq,
                                 source_seqs=(snapshot.new_seqs[-1],), fulfills_intent=False, reason=reason)
        decision.validate_for(snapshot)
        with transaction(self.db):
            record = self.record(batch)
            if not self._input_matches(batch, snapshot):
                raise PermissionError("judge batch input changed")
            if record.status != "pending":
                return record
            if not self.current(snapshot) or self.through(snapshot.scope) != snapshot.after_seq:
                status = "expired" if self.now() >= snapshot.expires else "cancelled"
                self.db.execute("UPDATE judge_batches SET status=?,error_code='stale_input' WHERE id=?", (status, batch))
            else:
                self.db.execute("UPDATE judge_batches SET status='decided',decision=?,error_code=NULL WHERE id=?",
                                (decision.model_dump_json(), batch))
                self.db.execute("INSERT INTO judge_state(scope,assessed_through) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET assessed_through=excluded.assessed_through", (snapshot.scope, snapshot.through_seq))
                self._handoff(batch, snapshot, decision)
        return self.record(batch)

    def fallback(self, batch: str, snapshot: JudgeSnapshot, *, limit: int) -> JudgeRecord:
        """Persist a single handoff intent; V3-A never creates or executes a main wake."""
        with transaction(self.db):
            record = self.record(batch)
            if record.status != "pending" or not self._input_matches(batch, snapshot):
                return record
            if not self.current(snapshot) or self.through(snapshot.scope) != snapshot.after_seq:
                self.db.execute("UPDATE judge_batches SET status=?,error_code='stale_input' WHERE id=?",
                                ("expired" if self.now() >= snapshot.expires else "cancelled", batch))
            else:
                recent = self.db.execute("SELECT fallback_at FROM judge_batches WHERE fallback_at>? ORDER BY fallback_at",
                                         (self.now() - 3600,)).fetchall()
                if limit and len(recent) < limit:
                    self.db.execute("UPDATE judge_batches SET status='fallback',fallback_at=? WHERE id=?", (self.now(), batch))
                    self._handoff(batch, snapshot, JudgeDecision(
                        decision="wake", through_seq=snapshot.through_seq,
                        source_seqs=(snapshot.new_seqs[-1],) if snapshot.new_seqs else tuple(
                            message.seq for message in snapshot.messages if not message.is_self
                        )[-1:], reason="Intermediary unavailable; one bounded main-model assessment.",
                    ))
                else:
                    due = float(recent[-limit][0]) + 3600 if limit and recent else record.expires
                    self.db.execute("UPDATE judge_batches SET due=? WHERE id=?", (due, batch))
        return self.record(batch)

    def recover(self) -> None:
        """Explicit single-owner startup recovery; attempts already sent remain charged."""
        with transaction(self.db):
            now = self.now()
            self.db.execute("UPDATE judge_attempts SET finished=?,error_code='interrupted' WHERE finished IS NULL", (now,))
            self.db.execute("UPDATE judge_batches SET status='expired',error_code='expired' WHERE status IN ('pending','running') AND expires<=?", (now,))
            self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='version_changed' WHERE status IN ('pending','running') AND (created>? OR version<>(SELECT version FROM scopes WHERE key=scope) OR (SELECT mode FROM scopes WHERE key=scope)='paused')", (now,))
            self.db.execute("UPDATE judge_batches SET status='pending',due=?,error_code='interrupted' WHERE status='running'", (now,))
            self.db.execute("UPDATE judge_state SET inflight=0")

    def queue(self, key: str, seq: int, *, delay: float, maximum: float,
                        opportunity: str | None = None) -> None:
        """Only coalescing/time facts, never a language or probability filter."""
        with transaction(self.db):
            self.db.execute("INSERT INTO judge_state(scope,assessed_through) VALUES(?,0) ON CONFLICT(scope) DO NOTHING", (key,))
            row = self.state(key)
            if seq <= row["assessed_through"] and opportunity is None:
                return
            first = float(row["pending_first"]) if row["pending_first"] is not None else self.now()
            due = min(self.now() + delay, first + maximum)
            self.db.execute("UPDATE judge_state SET pending_first=?,pending_last=?,pending_seq=MAX(pending_seq,?),pending_due=?,pending_max=?,opportunity=COALESCE(?,opportunity) WHERE scope=?",
                            (first, self.now(), seq, due, first + maximum, opportunity, key))

    @staticmethod
    def _metadata(snapshot: JudgeSnapshot) -> dict[str, object]:
        # Freeze reconstruction facts, not another transcript or derived key.
        metadata = snapshot.model_dump(mode="json", exclude={"messages"})
        metadata["excerpts"] = [{"seq": message.seq, "length": len(message.text),
                                 "revision": message.revision,
                                 "reply_length": len(message.reply.text) if message.reply else 0}
                                for message in snapshot.messages]
        return metadata

    def _input_matches(self, batch: str, snapshot: JudgeSnapshot) -> bool:
        row = self.input_metadata(batch)
        return row is not None and row[0] is not None and json.loads(row[0]) == self._metadata(snapshot)

    def _handoff(self, batch: str, snapshot: JudgeSnapshot, decision: JudgeDecision) -> None:
        """Caller owns the same transaction as the decision/cursor, including a single fallback."""
        row = self.db.execute("SELECT * FROM judge_batches WHERE id=?", (batch,)).fetchone()
        if (row is None or row["handoff_job"] is not None or decision.decision != "wake"
                or not self.db.execute("SELECT 1 FROM judge_state WHERE scope=? AND batch_id=?", (snapshot.scope, batch)).fetchone()):
            return  # Standalone V3-A evaluations do not schedule main work.
        key = snapshot.scope
        for active in self.wakes.recent_turns(key):
            shown = self.views.shown_sequences(key, str(active["turn_id"]))
            if snapshot.new_seqs and set(snapshot.new_seqs) <= shown and set(decision.source_seqs) <= shown:
                self.db.execute("UPDATE judge_batches SET handoff_job=? WHERE id=?", (active["id"], batch))
                if decision.fulfills_intent and snapshot.intent and snapshot.intent.one_shot and row["condition_config"] == self.scopes.state(key)["wake_config"]:
                    self.wakes.clear_wake_config(key)
                return
        reason = WakeReason.PROACTIVE if snapshot.opportunity is not None and not snapshot.new_seqs else WakeReason.JUDGE
        self.wakes.schedule(key, snapshot.through_seq, reason, 0)
        wake = self.wakes.scheduled(key)
        if wake is None:
            return
        conditions = row["condition_config"] if decision.fulfills_intent else "{}"
        expires = snapshot.expires
        if decision.fulfills_intent and snapshot.intent and snapshot.intent.condition_expires is not None:
            expires = min(expires, snapshot.intent.condition_expires)
        self.wakes.attach_judgement(wake["id"], expires, conditions, snapshot.opportunity if reason == "proactive" else None)
        self.db.execute("UPDATE judge_batches SET handoff_job=? WHERE id=?", (wake["id"], batch))
        if snapshot.quiet_due is not None and snapshot.total_due is not None and reason == "judge":
            self.wakes.add_dialogue_job(wake["id"], snapshot.intent.purpose if snapshot.intent and snapshot.intent.purpose else "reply",
                                        max(0,snapshot.quiet_due-snapshot.created),snapshot.quiet_due,snapshot.total_due,
                                        snapshot.after_seq,snapshot.through_seq)
        if row["status"] == "fallback":
            self.advance_through(snapshot.through_seq, key)

    def reset_binding(self, key: str, version: str) -> None:
        self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='version_changed' WHERE scope=? AND version<>? AND status IN ('pending','running')", (key, version))
        self.db.execute("INSERT INTO judge_state(scope,assessed_through) SELECT key,next_seq-1 FROM scopes WHERE key=? ON CONFLICT(scope) DO UPDATE SET pending_due=CASE WHEN pending_due IS NOT NULL OR batch_id IS NOT NULL THEN ? ELSE NULL END,batch_id=NULL,inflight=0,opportunity=NULL", (key, self.now()))

    def pause(self, key: str) -> None:
        self.db.execute("UPDATE judge_state SET pending_due=NULL,batch_id=NULL WHERE scope=?", (key,))
        self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='paused' WHERE scope=? AND status IN ('pending','running')", (key,))

    def shorten_pending(self, key: str, quiet: float, total: object) -> None:
        self.db.execute("UPDATE judge_state SET pending_due=MIN(pending_last+?,pending_max,COALESCE(?,pending_max)) WHERE scope=? AND batch_id IS NULL AND pending_due IS NOT NULL", (quiet, total, key))

    def latest_for_job(self, job: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM judge_batches WHERE handoff_job=? ORDER BY created DESC LIMIT 1", (job,)).fetchall()

    def cancel_other_revisions(self, revision: int) -> None:
        self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='provider_changed' WHERE status IN ('pending','running') AND judge_revision<>?", (revision,))

    def requeue_provider_changes(self, due: float) -> None:
        self.db.execute("UPDATE judge_state SET batch_id=NULL,pending_due=? WHERE batch_id IN (SELECT id FROM judge_batches WHERE status='cancelled' AND error_code='provider_changed')", (due,))

    def set_through(self, through: int, key: str) -> None:
        self.db.execute("UPDATE judge_state SET assessed_through=? WHERE scope=?", (through, key))

    def input_metadata(self, batch: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT input_meta FROM judge_batches WHERE id=?", (batch,)).fetchone()

    def state(self, key: str) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM judge_state WHERE scope=?", (key,)).fetchone()

    def due_scopes(self, now: float) -> list[sqlite3.Row]:
        return self.db.execute("SELECT scope,batch_id FROM judge_state WHERE inflight=0 AND pending_due<=?", (now,)).fetchall()

    def claim_scope(self, key: str) -> None:
        self.db.execute("UPDATE judge_state SET inflight=1 WHERE scope=?", (key,))

    def attach_batch(self, batch: str, key: str) -> None:
        self.db.execute("UPDATE judge_state SET batch_id=?,pending_first=NULL,pending_due=NULL,opportunity=NULL WHERE scope=?", (batch, key))

    def finish_failed_commit(self, finished: float, batch: str) -> None:
        self.db.execute("UPDATE judge_attempts SET finished=?,error_code='commit_failed' WHERE batch_id=? AND finished IS NULL", (finished, batch))

    def retry_failed_commit(self, due: float, batch: str) -> None:
        self.db.execute("UPDATE judge_batches SET status='pending',error_code='commit_failed',due=? WHERE id=? AND status='running'", (due, batch))

    def cancel_invalid_input(self, batch: str) -> None:
        self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='invalid_input' WHERE id=? AND status IN ('pending','running')", (batch,))

    def reschedule_batch(self, batch: str, due: float, key: str) -> None:
        self.db.execute("UPDATE judge_state SET inflight=0,batch_id=?,pending_due=? WHERE scope=?", (batch, due, key))

    def release_scope(self, due: float | None, key: str) -> None:
        self.db.execute("UPDATE judge_state SET inflight=0,batch_id=NULL,pending_due=? WHERE scope=?", (due, key))

    def advance_through(self, through: int, key: str) -> None:
        self.db.execute("UPDATE judge_state SET assessed_through=MAX(assessed_through,?) WHERE scope=?", (through, key))

    def discard_pending(self, key: str) -> None:
        self.db.execute("UPDATE judge_state SET assessed_through=MAX(assessed_through,pending_seq),opportunity=NULL WHERE scope=?", (key,))

    def pending(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM judge_state WHERE scope=? AND (pending_due IS NOT NULL OR inflight=1)", (key,)).fetchone()

    def any_pending(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM judge_state WHERE inflight=1 OR pending_due IS NOT NULL LIMIT 1").fetchone()
