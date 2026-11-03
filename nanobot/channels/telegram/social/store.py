"""Single-owner SQLite observation, exposure, wake and delivery journal."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

from loguru import logger

from nanobot.channels.telegram.social.activity import SocialActivity
from nanobot.channels.telegram.social.db.connection import open_social_db, transaction
from nanobot.channels.telegram.social.db.messages import Messages
from nanobot.channels.telegram.social.db.schema import initialize_social_schema
from nanobot.channels.telegram.social.db.scopes import Scopes
from nanobot.channels.telegram.social.db.stickers import StickerLibrary
from nanobot.channels.telegram.social.db.views import Views
from nanobot.channels.telegram.social.db.wakes import WakeReason, Wakes
from nanobot.channels.telegram.social.followups import SocialFollowups
from nanobot.channels.telegram.social.judge_models import (
    JUDGE_PROMPT_VERSION,
    JudgeDecision,
    JudgeRecord,
    JudgeSnapshot,
)
from nanobot.channels.telegram.social.learning import SocialLearning
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    MediaReference,
    ObservedMessage,
    ProactiveCandidate,
    QuotedMessage,
    Snapshot,
    SnapshotMessage,
    UncertainSend,
)


class SocialStore:
    def __init__(self, root: Path, *, now: Callable[[], float] = time.time) -> None:
        self.now = now
        self.db = open_social_db(root)
        initialize_social_schema(self)
        self.scopes = Scopes(self.db, now)
        self.messages = Messages(self.db, now)
        self.views = Views(self.db, now, self.scopes)
        self.stickers = StickerLibrary(self.db, now)
        self.wakes = Wakes(self.db, now, self.scopes, self.messages, self.views)
        self.activities = SocialActivity(self)
        self.learning = SocialLearning(self)
        self.followups = SocialFollowups(self)
        self.closed = False
        (root / "social.sqlite3").chmod(0o600)

    def close(self) -> None:
        self.closed = True
        self.db.close()

    def bind(self, scope: ConversationScope, version: str) -> None:
        if self.scopes.owns(scope.session_key) and self.scopes.state(scope.session_key)["version"] == version:
            return
        with transaction(self.db):
            self.scopes.upsert(scope, version)
            self.wakes.reset_binding(scope.session_key, version)
            self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='version_changed' "
                            "WHERE scope=? AND version<>? AND status IN ('pending','running')",
                            (scope.session_key, version))
            self.db.execute("INSERT INTO judge_state(scope,assessed_through) SELECT key,next_seq-1 FROM scopes WHERE key=? "
                            "ON CONFLICT(scope) DO UPDATE SET "
                            "pending_due=CASE WHEN pending_due IS NOT NULL OR batch_id IS NOT NULL THEN ? ELSE NULL END,batch_id=NULL,"
                            "inflight=0,opportunity=NULL", (scope.session_key, self.now()))
            self.db.execute(
                "UPDATE actions SET status='failed',error='configuration changed' "
                "WHERE scope=? AND version<>? AND status='queued'", (scope.session_key, version),
            )
            self.followups.sync_receipts(scope.session_key)

    def judge_through(self, key: str) -> int:
        row = self.db.execute("SELECT assessed_through FROM judge_state WHERE scope=?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def judge_record(self, batch: str) -> JudgeRecord:
        row = self.db.execute("SELECT * FROM judge_batches WHERE id=?", (batch,)).fetchone()
        if row is None:
            raise ValueError("unknown judge batch")
        body = dict(row)
        body["decision"] = JudgeDecision.model_validate_json(body["decision"]) if body["decision"] else None
        return JudgeRecord.model_validate({key: value for key, value in body.items() if key in JudgeRecord.model_fields})

    def _judge_current(self, snapshot: JudgeSnapshot) -> bool:
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

    def open_judge_batch(self, snapshot: JudgeSnapshot, revision: int, model: str) -> JudgeRecord:
        if not self._judge_current(snapshot):
            raise PermissionError("judge snapshot is expired, changed, paused or not retained")
        with transaction(self.db):
            existing = self.db.execute("SELECT id,judge_revision FROM judge_batches WHERE id=?", (snapshot.batch_id,)).fetchone()
            if existing:
                if existing["judge_revision"] != revision or not self._judge_input_matches(snapshot.batch_id, snapshot):
                    raise PermissionError("judge batch input changed")
                return self.judge_record(str(existing[0]))
            if snapshot.after_seq != self.judge_through(snapshot.scope):
                raise PermissionError("judge batch must start at the assessment watermark")
            batch = snapshot.batch_id
            self.db.execute("INSERT INTO judge_batches(id,scope,version,judge_revision,model,prompt_version,"
                            "after_seq,through_seq,created,expires,due,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending')",
                            (batch, snapshot.scope, snapshot.version, revision, model,
                             JUDGE_PROMPT_VERSION, snapshot.after_seq, snapshot.through_seq, snapshot.created,
                             snapshot.expires, self.now()))
            self.db.execute("UPDATE judge_batches SET input_meta=?,condition_config=? WHERE id=?",
                            (json.dumps(self._judge_metadata(snapshot)), self.scopes.state(snapshot.scope)["wake_config"], batch))
            self.db.execute("DELETE FROM judge_batches WHERE created<? AND status NOT IN ('pending','running')",
                            (self.now() - 86400,))
        return self.judge_record(batch)

    def claim_judge_attempt(self, batch: str, snapshot: JudgeSnapshot, *, limit: int,
                            interval: float, max_attempts: int) -> JudgeRecord:
        with transaction(self.db):
            record = self.judge_record(batch)
            if not self._judge_input_matches(batch, snapshot):
                raise PermissionError("judge batch input changed")
            if record.status != "pending":
                return record
            if not self._judge_current(snapshot) or snapshot.after_seq != self.judge_through(snapshot.scope):
                status = "expired" if self.now() >= record.expires else "cancelled"
                self.db.execute("UPDATE judge_batches SET status=?,error_code='stale_input' WHERE id=?", (status, batch))
                return self.judge_record(batch)
            if record.attempts >= max_attempts or record.due > self.now():
                return record
            active = self.db.execute("SELECT 1 FROM judge_batches WHERE scope=? AND status='running'", (record.scope,)).fetchone()
            recent = self.db.execute("SELECT started FROM judge_attempts WHERE started>? ORDER BY started",
                                     (self.now() - 3600,)).fetchall()
            last = self.db.execute("SELECT MAX(a.started) FROM judge_attempts a JOIN judge_batches b ON b.id=a.batch_id "
                                   "WHERE b.scope=?", (record.scope,)).fetchone()[0]
            due = max(record.due, float(last) + interval if last is not None else self.now())
            if active:
                due = max(due, self.now() + 1)
            if len(recent) >= limit:
                due = max(due, float(recent[-limit][0]) + 3600)
            if due > self.now():
                self.db.execute("UPDATE judge_batches SET due=?,error_code='budget_or_busy' WHERE id=?", (due, batch))
                return self.judge_record(batch)
            self.db.execute("UPDATE judge_batches SET status='running',attempts=attempts+1 WHERE id=?", (batch,))
            self.db.execute("INSERT INTO judge_attempts(batch_id,attempt,started) VALUES(?,?,?)",
                            (batch, record.attempts + 1, self.now()))
        return self.judge_record(batch)

    def finish_judge_attempt(self, batch: str, snapshot: JudgeSnapshot, *, attempt: int,
                             decision: JudgeDecision | None, error_code: str | None,
                             delay: float, latency_ms: int, input_tokens: int | None,
                             output_tokens: int | None, total_tokens: int | None) -> JudgeRecord:
        if decision is not None:
            decision.validate_for(snapshot)
        with transaction(self.db):
            record = self.judge_record(batch)
            if not self._judge_input_matches(batch, snapshot):
                raise PermissionError("judge batch input changed")
            self.db.execute("UPDATE judge_attempts SET finished=?,error_code=?,latency_ms=?,input_tokens=?,"
                            "output_tokens=?,total_tokens=? WHERE batch_id=? AND attempt=? AND finished IS NULL",
                            (self.now(), error_code, latency_ms, input_tokens, output_tokens, total_tokens, batch, attempt))
            if record.status != "running" or record.attempts != attempt:
                return record  # Keep late usage, but never resurrect a cancelled decision.
            if not self._judge_current(snapshot) or self.judge_through(snapshot.scope) != snapshot.after_seq:
                status = "expired" if self.now() >= snapshot.expires else "cancelled"
                self.db.execute("UPDATE judge_batches SET status=?,error_code='stale_input' WHERE id=?", (status, batch))
            elif decision is not None:
                self.db.execute("UPDATE judge_batches SET status='decided',decision=?,error_code=NULL WHERE id=?",
                                (decision.model_dump_json(), batch))
                self.db.execute("INSERT INTO judge_state(scope,assessed_through) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET "
                                "assessed_through=excluded.assessed_through", (snapshot.scope, snapshot.through_seq))
                self._handoff_judgement(batch, snapshot, decision)
            else:
                self.db.execute("UPDATE judge_batches SET status='pending',due=?,error_code=? WHERE id=?",
                                (self.now() + delay, error_code, batch))
        return self.judge_record(batch)

    def judge_fallback(self, batch: str, snapshot: JudgeSnapshot, *, limit: int) -> JudgeRecord:
        """Persist a single handoff intent; V3-A never creates or executes a main wake."""
        with transaction(self.db):
            record = self.judge_record(batch)
            if record.status != "pending" or not self._judge_input_matches(batch, snapshot):
                return record
            if not self._judge_current(snapshot) or self.judge_through(snapshot.scope) != snapshot.after_seq:
                self.db.execute("UPDATE judge_batches SET status=?,error_code='stale_input' WHERE id=?",
                                ("expired" if self.now() >= snapshot.expires else "cancelled", batch))
            else:
                recent = self.db.execute("SELECT fallback_at FROM judge_batches WHERE fallback_at>? ORDER BY fallback_at",
                                         (self.now() - 3600,)).fetchall()
                if limit and len(recent) < limit:
                    self.db.execute("UPDATE judge_batches SET status='fallback',fallback_at=? WHERE id=?", (self.now(), batch))
                    self._handoff_judgement(batch, snapshot, JudgeDecision(
                        decision="wake", through_seq=snapshot.through_seq,
                        source_seqs=(snapshot.new_seqs[-1],) if snapshot.new_seqs else tuple(
                            message.seq for message in snapshot.messages if not message.is_self
                        )[-1:], reason="Intermediary unavailable; one bounded main-model assessment.",
                    ))
                else:
                    due = float(recent[-limit][0]) + 3600 if limit and recent else record.expires
                    self.db.execute("UPDATE judge_batches SET due=? WHERE id=?", (due, batch))
        return self.judge_record(batch)

    def recover_judge_batches(self) -> None:
        """Explicit single-owner startup recovery; attempts already sent remain charged."""
        with transaction(self.db):
            now = self.now()
            self.db.execute("UPDATE judge_attempts SET finished=?,error_code='interrupted' WHERE finished IS NULL", (now,))
            self.db.execute("UPDATE judge_batches SET status='expired',error_code='expired' "
                            "WHERE status IN ('pending','running') AND expires<=?", (now,))
            self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='version_changed' "
                            "WHERE status IN ('pending','running') AND (created>? OR version<>(SELECT version FROM scopes WHERE key=scope) "
                            "OR (SELECT mode FROM scopes WHERE key=scope)='paused')", (now,))
            self.db.execute("UPDATE judge_batches SET status='pending',due=?,error_code='interrupted' WHERE status='running'", (now,))
            self.db.execute("UPDATE judge_state SET inflight=0")

    def queue_judgement(self, key: str, seq: int, *, delay: float, maximum: float,
                        opportunity: str | None = None) -> None:
        """Only coalescing/time facts, never a language or probability filter."""
        with transaction(self.db):
            self.db.execute("INSERT INTO judge_state(scope,assessed_through) VALUES(?,0) ON CONFLICT(scope) DO NOTHING", (key,))
            row = self.db.execute("SELECT * FROM judge_state WHERE scope=?", (key,)).fetchone()
            if seq <= row["assessed_through"] and opportunity is None:
                return
            first = float(row["pending_first"]) if row["pending_first"] is not None else self.now()
            due = min(self.now() + delay, first + maximum)
            self.db.execute("UPDATE judge_state SET pending_first=?,pending_last=?,pending_seq=MAX(pending_seq,?),"
                            "pending_due=?,pending_max=?,opportunity=COALESCE(?,opportunity) WHERE scope=?",
                            (first, self.now(), seq, due, first + maximum, opportunity, key))

    @staticmethod
    def _judge_metadata(snapshot: JudgeSnapshot) -> dict[str, object]:
        # Freeze reconstruction facts, not another transcript or derived key.
        metadata = snapshot.model_dump(mode="json", exclude={"messages"})
        metadata["excerpts"] = [{"seq": message.seq, "length": len(message.text),
                                 "revision": message.revision,
                                 "reply_length": len(message.reply.text) if message.reply else 0}
                                for message in snapshot.messages]
        return metadata

    def _judge_input_matches(self, batch: str, snapshot: JudgeSnapshot) -> bool:
        row = self.db.execute("SELECT input_meta FROM judge_batches WHERE id=?", (batch,)).fetchone()
        return row is not None and row[0] is not None and json.loads(row[0]) == self._judge_metadata(snapshot)

    def _handoff_judgement(self, batch: str, snapshot: JudgeSnapshot, decision: JudgeDecision) -> None:
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
            self.db.execute("UPDATE judge_state SET assessed_through=MAX(assessed_through,?) WHERE scope=?",
                            (snapshot.through_seq, key))

    def _collect_learning(self, message: ObservedMessage, seq: int, revision: int) -> None:
        # A collector fault must not roll back the ordinary observation or V3 event.
        self.db.execute("SAVEPOINT learning_collection")
        try:
            self.learning.observe(message, seq, revision)
        except Exception:
            self.db.execute("ROLLBACK TO learning_collection")
            logger.exception("V5 material capture failed for message {}", message.message_id)
            self.learning.count(message.scope.session_key, "capture_error", 1, message.message_id, message.message_id)
        finally:
            self.db.execute("RELEASE learning_collection")

    def observe(self, msg: ObservedMessage, retention: int, *,
                max_delay: float = 12, fresh: bool = False,
                judge_event: bool = False, judge_delay: float = 1.5,
                directed: bool = False) -> tuple[int, bool]:
        key = msg.scope.session_key
        with transaction(self.db):
            existing = self.messages.observation(key, msg.message_id)
            if msg.update_id >= 0 and self.messages.update_seen(msg.scope.channel, msg.update_id):
                return (int(existing[0]) if existing else 0), False
            if msg.update_id >= 0:
                self.messages.record_update(msg.scope.channel, msg.update_id)
            revision = self.learning.observation_revision(key, msg.message_id, int(existing[1]) if existing else None)
            if existing is not None:
                self.messages.edit(key, msg, revision)
                self._collect_learning(msg, int(existing[0]), revision)
                return int(existing[0]), False
            event = not msg.is_self and not msg.is_bot
            member_at = msg.timestamp if event and not msg.is_edit and 0 <= self.now() - msg.timestamp < 120 else None
            seq = self.scopes.allocate_seq(key, event=event, member_at=member_at)
            self.messages.insert(key, seq, msg)
            self.messages.set_judge_facts(key, seq, msg, judge_event, directed)
            self.messages.set_revision(key, msg.message_id, revision)
            self._collect_learning(msg, seq, revision)
            self.messages.prune(key, seq - retention)
            # Keep update dedup bounded too. Old platform replays below the retained
            # window cannot produce wakes because message timestamps expire them.
            self.messages.prune_updates(msg.scope.channel, msg.update_id - max(10000, retention * 100))
            if fresh:
                self.wakes.note_member_activity(key, seq)
            if judge_event:
                self.queue_judgement(key,seq,delay=judge_delay,maximum=max_delay)
            return seq, True

    def view(
        self, key: str, *, count: int, chars: int, trigger_seq: int | None = None,
        turn_id: str | None = None,
        job_id: str | None = None,
    ) -> Snapshot:
        state = self.scopes.state(key)
        read = int(state["read_through"])
        rows = self.messages.after(key, read, count)
        if not rows:
            rows = list(reversed(self.messages.recent(key, count)))
        if trigger_seq and all(row["seq"] != trigger_seq for row in rows):
            trigger = self.messages.by_seq(key, trigger_seq)
            if trigger is not None:
                rows = [*rows[:count - 1], trigger]
        now = self.now()
        records: list[SnapshotMessage] = []
        remaining = chars - 1200
        for row in rows:
            quoted = QuotedMessage.model_validate_json(row["reply_context"]).summary() if row["reply_context"] else None
            if quoted is not None:
                quoted["text"] = str(quoted["text"])[:max(0, min(400, remaining // 8))]
            record: SnapshotMessage = {
                "seq": int(row["seq"]), "message_id": int(row["message_id"]), "revision": int(row["revision"]),
                "sender_id": str(row["sender_id"]), "name": str(row["display_name"]),
                "timestamp": float(row["at"]), "age_seconds": max(0, int(now - float(row["at"]))),
                "text": row["text"][:max(0, min(1200, remaining // (4 if quoted else 2)))],
                "reply_to": int(row["reply_to"]) if row["reply_to"] is not None else None,
                "media": json.loads(row["media"]),
                "media_details": [MediaReference.model_validate(ref).summary() for ref in json.loads(row["media_refs"])],
                "is_self": bool(row["is_self"]),
                "reply": quoted,
                "album_id": str(row["album_id"]) if row["album_id"] else None,
            }
            size = len(json.dumps(record, ensure_ascii=False))
            # Quotes add fixed identity/media fields. Shorten both excerpts
            # before dropping an otherwise useful message at small budgets.
            while quoted is not None and size > remaining and (record["text"] or quoted["text"]):
                record["text"] = record["text"][:len(record["text"]) // 2]
                quoted["text"] = str(quoted["text"])[:len(str(quoted["text"])) // 2]
                size = len(json.dumps(record, ensure_ascii=False))
            if size > remaining:
                break
            records.append(record)
            remaining -= size
        first = self.messages.first_seq(key)[0]
        view_id = uuid4().hex
        # A supplied job_id is a persisted wake selected by the caller.
        wake = dict(cast(sqlite3.Row, self.wakes.get(job_id))) if job_id else {}
        result: Snapshot = {
            "view_id": view_id, "now": now, "read_through": read,
            "history_gap": [read + 1, first - 1] if first is not None and first > read + 1 else None,
            "messages": records,
            "wake_job": job_id,
            "uncertain_send": self.uncertain_send(key),
            "mode": state["mode"],
            "wake_reason": wake["reason"] if job_id else None,
            "opportunity": None,
            "dialogue": self.wakes.dialogue_job(job_id) if job_id else None,
            "judgement": [{"id": str(item["id"]), "status": str(item["status"]),
                           "through_seq": int(item["through_seq"]),
                           "decision": json.loads(item["decision"]) if item["decision"] else None,
                           "error_code": item["error_code"]}
                          for item in self.db.execute("SELECT * FROM judge_batches WHERE handoff_job=? ORDER BY created DESC LIMIT 1", (job_id,))] if job_id else [],
        }
        if job_id:
            source = wake["proactive_source"]
            if source:
                result["opportunity"] = ProactiveCandidate.model_validate_json(source).model_dump()
        body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        with transaction(self.db):
            self.views.record(view_id, key, state["version"], body,
                              [row["seq"] for row in records], turn_id, now)
        return result

    def note_seen(self, key: str, turn_id: str, model_text: str) -> list[str]:
        state = self.scopes.state(key)
        admitted: list[str] = []
        with transaction(self.db):
            exposed = False
            for row in self.views.unseen(key, state["version"]):
                if row["turn_id"] not in (None, turn_id) or row["body"] not in model_text:
                    continue
                job = json.loads(row["body"]).get("wake_job")
                if job:
                    changed = self.wakes.admit_view_job(key, state["version"], job, turn_id)
                    if changed:
                        admitted.append(str(job))
                self.views.mark_seen(row["id"], turn_id)
                exposed = True
            seen = self.views.shown_sequences(key, turn_id)
            # A read tool can expose a busy-time trigger within the current run.
            # Only real provider exposure consumes it; returning the page alone cannot.
            admitted.extend(self.wakes.admit_shown(key, state["version"], list(seen), turn_id))
            frontier = int(state["seen_through"])
            while frontier + 1 in seen:
                frontier += 1
            self.scopes.set_seen_through(key, frontier)
            if seen and exposed:
                self.wakes.note_observed(key, max(seen))
        return admitted

    def mark_read(self, key: str, turn_id: str, through: int) -> int:
        with transaction(self.db):
            current = int(self.scopes.state(key)["read_through"])
            if through >= int(self.scopes.state(key)["next_seq"]):
                raise ValueError("read watermark exceeds observed history")
            if through < current:
                raise ValueError("read watermark cannot move backwards")
            shown = self.views.shown_sequences(key, turn_id)
            if any(seq not in shown for seq in range(current + 1, through + 1)):
                raise ValueError("only continuously displayed messages may be acknowledged")
            self.scopes.set_read_through(key, through)
            return through

    def acknowledge_seen(self, key: str, turn_id: str) -> int:
        """Acknowledge only the contiguous prefix actually exposed in this turn."""
        current = int(self.scopes.state(key)["read_through"])
        shown = self.views.shown_sequences(key, turn_id)
        through = current
        while through + 1 in shown:
            through += 1
        return self.mark_read(key, turn_id, through)

    def recover(self) -> None:
        with transaction(self.db):
            # A process may die after Telegram accepted an ordinary tool send.
            self.db.execute("UPDATE message_deliveries SET status='unknown',detail='Interrupted before platform receipt; do not resend.' WHERE status='queued'")
            self.db.execute("UPDATE actions SET status='failed',error='unattempted after restart' "
                            "WHERE status='queued' AND attempts=0")
            self.db.execute("UPDATE actions SET status='unknown' WHERE status IN ('queued','sending')")
            self.wakes.recover()
            self.followups.sync_receipts()

    def cancel_opportunity(self, job: str, reason: str) -> None:
        with transaction(self.db):
            row = self.wakes.cancel_opportunity(job)
            if row is not None:
                self.db.execute("UPDATE actions SET status='failed',error=? WHERE scope=? AND status='queued' "
                                "AND turn_id=?", (f"opportunity skipped: {reason}", row["scope"], row["turn_id"]))
                self.followups.sync_receipts(str(row["scope"]))

    def reserve_send(self, key: str, version: str, text: str, limit: int, *, turn: str | None = None,
                     sticker: MediaReference | None = None, followup_id: str | None = None, units: int = 1) -> str:
        with transaction(self.db):
            if self.scopes.state(key)["version"] != version:
                raise PermissionError("conversation configuration changed")
            if self.db.execute(
                "SELECT 1 FROM actions WHERE scope=? AND version=? AND status='unknown' "
                "AND kind=? AND trim(content)=trim(?) AND created>?",
                (key, version, "sticker" if sticker else "text", text, self.now() - 86400),
            ).fetchone():
                raise PermissionError("delivery of this text is unknown; do not blindly repeat it")
            count = self.db.execute(
                "SELECT COUNT(*) FROM send_attempts WHERE scope=? AND at>?", (key, self.now() - 3600),
            ).fetchone()[0]
            if count + units > limit:
                raise PermissionError("hourly send budget exhausted")
            action = uuid4().hex
            self.db.execute("INSERT INTO actions(id,scope,version,content,status,created,turn_id,kind,media_ref) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", (
                action, key, version, text, "sending", self.now(), turn,
                "sticker" if sticker else "text", sticker.model_dump_json() if sticker else None,
            ))
            if followup_id is not None:
                if turn is None:
                    raise ValueError("a proactive follow-up requires a source turn")
                self.followups.reserve(key, followup_id, turn, action)
            self.db.execute("DELETE FROM actions WHERE scope=? AND created<?", (key, self.now() - 604800))
            self.db.executemany("INSERT INTO send_attempts VALUES(?,?)", [(key, self.now())] * units)
            self.db.execute("DELETE FROM send_attempts WHERE at<=?", (self.now() - 3600,))
            return action

    def uncertain_send(self, key: str) -> UncertainSend | None:
        row = self.db.execute(
            "SELECT id,content FROM actions WHERE scope=? AND version=? AND status='unknown' "
            "AND created>? ORDER BY created DESC LIMIT 1",
            (key, self.scopes.state(key)["version"], self.now() - 86400),
        ).fetchone()
        return UncertainSend(action_id=str(row["id"]), text=str(row["content"])[:100], status="unknown") if row else None

    def delivery(self, action: str, status: str, message_id: int | None = None, error: str | None = None) -> None:
        with transaction(self.db):
            previous = self.db.execute("SELECT * FROM actions WHERE id=?", (action,)).fetchone()
            if status == "sent" and previous is not None and previous["status"] != "sent" and previous["kind"] == "sticker":
                ref = MediaReference.model_validate_json(previous["media_ref"])
                self.stickers.count_use(previous["scope"], ref.unique_id)
            self.db.execute(
                "UPDATE actions SET status=?,message_id=?,error=?,due=? WHERE id=?",
                (status, message_id, error[:200] if error else None, self.now(), action),
            )
            self.followups.receipt(action, status)
            if status in ("failed", "unknown"):
                self.db.execute("UPDATE actions SET status='failed',error='earlier part was not confirmed' "
                                "WHERE status='queued' AND group_id=(SELECT group_id FROM actions WHERE id=?) "
                                "AND ordinal>(SELECT ordinal FROM actions WHERE id=?)", (action, action))
                for child in self.db.execute("SELECT id FROM actions WHERE group_id=(SELECT group_id FROM actions WHERE id=?) AND status='failed'", (action,)):
                    self.followups.receipt(str(child[0]), "failed")

    def pause(self, key: str) -> None:
        with transaction(self.db):
            self.wakes.pause(key)
            self.db.execute("UPDATE actions SET status='failed',error='paused' "
                            "WHERE scope=? AND status='queued'", (key,))
            self.followups.sync_receipts(key)
            self.db.execute("UPDATE judge_state SET pending_due=NULL,batch_id=NULL WHERE scope=?", (key,))
            self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='paused' "
                            "WHERE scope=? AND status IN ('pending','running')", (key,))

    def wait_for_quiet(self, key: str, turn: str, *, purpose: str, quiet: float,
                       seconds: float | None, on_timeout: bool, sleep_seconds: float | None = None,
                       quiet_due: float | None = None, condition_expires: float | None = None,
                       selective: bool = False, sleep_until: float | None = None,
                       max_delay: float = 12) -> dict[str, object]:
        with transaction(self.db):
            result = self.wakes.wait_for_quiet(key, turn, purpose=purpose, quiet=quiet, seconds=seconds,
                on_timeout=on_timeout, sleep_seconds=sleep_seconds, quiet_due=quiet_due,
                condition_expires=condition_expires, selective=selective, sleep_until=sleep_until, max_delay=max_delay)
            if result["deferred"] and purpose in {"reply", "messages"}:
                self.db.execute("UPDATE judge_state SET pending_due=MIN(pending_last+?,pending_max,COALESCE(?,pending_max)) "
                                "WHERE scope=? AND batch_id IS NULL AND pending_due IS NOT NULL", (quiet,result["total_due"],key))
            return result

    def last_send(self, key: str) -> float | None:
        row = self.db.execute("SELECT MAX(COALESCE(due,created)) FROM actions WHERE scope=? AND attempts>0", (key,)).fetchone()
        return float(row[0]) if row[0] is not None else None

    def queue_retry(self, action: str, after: float, reply_to: int | None) -> bool:
        now = self.now()
        with transaction(self.db):
            row = self.db.execute("SELECT * FROM actions WHERE id=?", (action,)).fetchone()
            expires = float(row["expires"]) if row["expires"] is not None else float(row["created"]) + 120
            if after < 0 or now + after >= expires or int(row["attempts"]) >= 3:
                self.db.execute("UPDATE actions SET status='failed',error='retry limit or expiry' WHERE id=?", (action,))
                return False
            self.db.execute("UPDATE actions SET status='queued',due=?,expires=?,reply_to=? WHERE id=?",
                            (now + max(after, 1), expires, reply_to, action))
            return True

    def ready_sends(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM actions WHERE status='queued' AND (due<=? OR expires<=?) "
                               "ORDER BY created,ordinal", (self.now(), self.now())).fetchall()

    def enqueue_parts(self, key: str, version: str, turn: str, texts: list[str],
                      reply_to: int | None, limit: int, *, followup_id: str | None = None) -> list[sqlite3.Row]:
        group = uuid4().hex
        now = self.now()
        with transaction(self.db):
            count = self.db.execute("SELECT COUNT(*) FROM send_attempts WHERE scope=? AND at>?",
                                    (key, now - 3600)).fetchone()[0]
            if count + len(texts) > limit:
                raise PermissionError("hourly send budget cannot accommodate all parts")
            expires = self.wakes.turn_expires(turn)
            if expires is None:
                raise PermissionError("parts require an active authorized turn")
            for index, text in enumerate(texts):
                if self.db.execute("SELECT 1 FROM actions WHERE scope=? AND version=? AND status='unknown' "
                                    "AND trim(content)=trim(?) AND created>?", (key, version, text, now - 86400)).fetchone():
                    raise PermissionError("delivery is unknown; do not blindly repeat a part")
                action = uuid4().hex
                self.db.execute("INSERT INTO actions(id,scope,version,content,status,created,due,expires,reply_to,"
                                "attempts,turn_id,group_id,ordinal) VALUES(?,?,?,?,'queued',?,?,?,?,0,?,?,?)",
                                (action, key, version, text, now, now, expires, reply_to, turn, group, index))
                if followup_id is not None:
                    self.followups.reserve(key, followup_id, turn, action)
        return self.db.execute("SELECT * FROM actions WHERE group_id=? ORDER BY ordinal", (group,)).fetchall()

    def send_predecessor(self, action: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM actions WHERE group_id=(SELECT group_id FROM actions WHERE id=?) "
                               "AND ordinal<(SELECT ordinal FROM actions WHERE id=?) AND status<>'sent' "
                               "ORDER BY ordinal LIMIT 1", (action, action)).fetchone()

    def postpone_send(self, action: str, due: float) -> None:
        with transaction(self.db):
            self.db.execute("UPDATE actions SET due=? WHERE id=? AND status='queued'", (due, action))

    def claim_retry(self, action: str, limit: int) -> bool:
        with transaction(self.db):
            row = self.db.execute("SELECT * FROM actions WHERE id=? AND status='queued'", (action,)).fetchone()
            if row is None:
                return False
            count = self.db.execute("SELECT COUNT(*) FROM send_attempts WHERE scope=? "
                                    "AND at>?", (row["scope"], self.now() - 3600)).fetchone()[0]
            if row["expires"] <= self.now() or count >= limit:
                self.db.execute("UPDATE actions SET status='failed',error='budget or expiry' WHERE id=?", (action,))
                return False
            self.db.execute("UPDATE actions SET status='sending',attempts=attempts+1,due=? WHERE id=?", (self.now(), action))
            self.db.execute("INSERT INTO send_attempts VALUES(?,?)", (row["scope"], self.now()))
            return True
