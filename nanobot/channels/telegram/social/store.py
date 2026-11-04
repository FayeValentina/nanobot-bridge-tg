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
from nanobot.channels.telegram.social.db.connection import open_social_db, savepoint, transaction
from nanobot.channels.telegram.social.db.deliveries import Deliveries
from nanobot.channels.telegram.social.db.judge import Judge
from nanobot.channels.telegram.social.db.messages import Messages
from nanobot.channels.telegram.social.db.proactive_state import ProactiveState
from nanobot.channels.telegram.social.db.schema import initialize_social_schema
from nanobot.channels.telegram.social.db.scopes import Scopes
from nanobot.channels.telegram.social.db.stickers import StickerLibrary
from nanobot.channels.telegram.social.db.views import Views, complete_view_ids
from nanobot.channels.telegram.social.db.wakes import Wakes
from nanobot.channels.telegram.social.followups import SocialFollowups
from nanobot.channels.telegram.social.learning import SocialLearning
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    MediaReference,
    ObservedMessage,
    ProactiveCandidate,
    QuotedMessage,
    Snapshot,
    SnapshotMessage,
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
        self.judge = Judge(self.db, now, self.scopes, self.messages, self.views, self.wakes)
        self.deliveries = Deliveries(self.db, now, self.scopes)
        self.proactive_state = ProactiveState(self.db, now)
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
            self.judge.reset_binding(scope.session_key, version)
            self.deliveries.fail_other_versions(scope.session_key, version)
            self.followups.sync_receipts(scope.session_key)

    def _collect_learning(self, message: ObservedMessage, seq: int, revision: int) -> None:
        # A collector fault must not roll back the ordinary observation or V3 event.
        try:
            with savepoint(self.db, "learning_collection"):
                self.learning.observe(message, seq, revision)
        except Exception:
            logger.exception("V5 material capture failed for message {}", message.message_id)
            self.learning.count(message.scope.session_key, "capture_error", 1, message.message_id, message.message_id)

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
                self.judge.queue(key,seq,delay=judge_delay,maximum=max_delay)
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
            "uncertain_send": self.deliveries.uncertain_send(key),
            "mode": state["mode"],
            "wake_reason": wake["reason"] if job_id else None,
            "opportunity": None,
            "dialogue": self.wakes.dialogue_job(job_id) if job_id else None,
            "judgement": [{"id": str(item["id"]), "status": str(item["status"]),
                           "through_seq": int(item["through_seq"]),
                           "decision": json.loads(item["decision"]) if item["decision"] else None,
                           "error_code": item["error_code"]}
                          for item in self.judge.latest_for_job(job_id)] if job_id else [],
            "view_end": view_id,
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
        ids = complete_view_ids(model_text)
        state = self.scopes.state(key)
        admitted: list[str] = []
        with transaction(self.db):
            exposed = False
            for row in self.views.unseen(key, state["version"], ids):
                if row["turn_id"] not in (None, turn_id):
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
            self.deliveries.recover()

            self.wakes.recover()
            self.followups.sync_receipts()

    def cancel_opportunity(self, job: str, reason: str) -> None:
        with transaction(self.db):
            row = self.wakes.cancel_opportunity(job)
            if row is not None:
                self.deliveries.fail_queued_for_turn(row["scope"], row["turn_id"], f"opportunity skipped: {reason}")
                self.followups.sync_receipts(str(row["scope"]))

    def reserve_send(self, key: str, version: str, text: str, *, kind: str, turn: str | None = None,
                     sticker: MediaReference | None = None, followup_id: str | None = None, units: int = 1) -> str:
        with transaction(self.db):
            action = uuid4().hex
            self.deliveries.insert_action(action, key, version, text, "sending", self.now(), turn, kind, sticker.model_dump_json() if sticker else None)
            if followup_id is not None:
                if turn is None:
                    raise ValueError("a proactive follow-up requires a source turn")
                self.followups.reserve(key, followup_id, turn, action)
            self.deliveries.prune_actions(key, self.now() - 604800)
            self.deliveries.charge_attempts([(key, self.now())] * units)
            self.deliveries.prune_attempts(self.now() - 3600)
            return action

    def delivery(self, action: str, status: str, message_ids: tuple[int, ...] = (), error: str | None = None) -> bool:
        with transaction(self.db):
            previous = self.deliveries.get(action)
            changed = self.deliveries.record_delivery(status, json.dumps(list(message_ids)), error[:200] if error else None, self.now(), action)
            if changed != 1:
                return False
            if status == "sent" and previous is not None and previous["kind"] == "sticker":
                ref = MediaReference.model_validate_json(previous["media_ref"])
                self.stickers.count_use(previous["scope"], ref.unique_id)
            self.followups.receipt(action, status, confirmed=bool(message_ids))
            if status in ("failed", "unknown"):
                self.deliveries.fail_later_parts(action)
                for child in self.deliveries.failed_parts(action):
                    self.followups.receipt(str(child[0]), "failed")
            return True

    def claim_retry(self, action: str, limit: int) -> bool:
        with transaction(self.db):
            row = self.deliveries.queued_action(action)
            if row is None:
                return False
            count = self.deliveries.attempt_count(row["scope"], self.now() - 3600)
            if row["expires"] <= self.now() or count >= limit:
                self.delivery(action, "failed", error="budget or expiry")
                return False
            self.deliveries.claim(action, row["scope"])
            return True

    def pause(self, key: str) -> None:
        with transaction(self.db):
            self.wakes.pause(key)
            self.deliveries.fail_queued(key, "paused")
            self.followups.sync_receipts(key)
            self.judge.pause(key)

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
                self.judge.shorten_pending(key, quiet, result["total_due"])
            return result

    def enqueue_parts(self, key: str, version: str, turn: str, texts: list[str],
                      reply_to: int | None, *, followup_id: str | None = None) -> list[sqlite3.Row]:
        group = uuid4().hex
        now = self.now()
        with transaction(self.db):
            expires = self.wakes.turn_expires(turn)
            if expires is None:
                raise PermissionError("parts require an active authorized turn")
            for index, text in enumerate(texts):
                action = uuid4().hex
                self.deliveries.insert_part(action, key, version, text, now, now, expires, reply_to, turn, group, index)
                if followup_id is not None:
                    self.followups.reserve(key, followup_id, turn, action)
        return self.deliveries.group_parts(group)
