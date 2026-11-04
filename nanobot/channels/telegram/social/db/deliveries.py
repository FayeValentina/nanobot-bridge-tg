from __future__ import annotations

import sqlite3
from collections.abc import Callable

from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.db.scopes import Scopes
from nanobot.channels.telegram.social.models import UncertainSend


class Deliveries:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], float], scopes: Scopes) -> None:
        self.db, self.now = db, now
        self.scopes = scopes

    def uncertain_send(self, key: str) -> UncertainSend | None:
        row = self.db.execute(
            "SELECT id,content FROM actions WHERE scope=? AND version=? AND status='unknown' AND created>? ORDER BY created DESC LIMIT 1",
            (key, self.scopes.state(key)["version"], self.now() - 86400),
        ).fetchone()
        return UncertainSend(action_id=str(row["id"]), text=str(row["content"])[:100], status="unknown") if row else None

    def last_send(self, key: str) -> float | None:
        row = self.db.execute("SELECT MAX(COALESCE(due,created)) FROM actions WHERE scope=? AND attempts>0", (key,)).fetchone()
        return float(row[0]) if row[0] is not None else None

    def queue_retry(self, action: str, after: float, reply_to: int | None) -> bool:
        now = self.now()
        with transaction(self.db):
            row = self.db.execute("SELECT * FROM actions WHERE id=?", (action,)).fetchone()
            expires = float(row["expires"]) if row["expires"] is not None else float(row["created"]) + 120
            if after < 0 or now + after >= expires or int(row["attempts"]) >= 3:
                return False  # The caller records the failure through SocialStore.delivery, which syncs receipts.
            self.db.execute("UPDATE actions SET status='queued',due=?,expires=?,reply_to=? WHERE id=? AND status IN ('sending','queued')",
                            (now + max(after, 1), expires, reply_to, action))
            return True

    def ready_sends(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM actions WHERE status='queued' AND (due<=? OR expires<=?) ORDER BY created,ordinal", (self.now(), self.now())).fetchall()

    def send_predecessor(self, action: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM actions WHERE group_id=(SELECT group_id FROM actions WHERE id=?) AND ordinal<(SELECT ordinal FROM actions WHERE id=?) AND status<>'sent' ORDER BY ordinal LIMIT 1", (action, action)).fetchone()

    def postpone_send(self, action: str, due: float) -> None:
        with transaction(self.db):
            self.db.execute("UPDATE actions SET due=? WHERE id=? AND status='queued'", (due, action))

    def queued_action(self, action: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM actions WHERE id=? AND status='queued'", (action,)).fetchone()

    def claim(self, action: str, key: str) -> None:
        self.db.execute("UPDATE actions SET status='sending',attempts=attempts+1,due=? WHERE id=? AND status='queued'", (self.now(), action))
        self.db.execute("INSERT INTO send_attempts VALUES(?,?)", (key, self.now()))

    def recover(self) -> None:
        self.db.execute("UPDATE actions SET status='failed',error='unattempted after restart' WHERE status='queued' AND attempts=0")
        self.db.execute("UPDATE actions SET status='unknown' WHERE status IN ('queued','sending')")

    def fail_other_versions(self, key: str, version: str) -> None:
        self.db.execute("UPDATE actions SET status='failed',error='configuration changed' WHERE scope=? AND version<>? AND status='queued'", (key, version))

    def insert_action(self, action: str, key: str, version: str, text: str, status: str, created: float, turn: str | None, kind: str, media_ref: str | None) -> None:
        self.db.execute("INSERT INTO actions(id,scope,version,content,status,created,turn_id,kind,media_ref) VALUES(?,?,?,?,?,?,?,?,?)", (action, key, version, text, status, created, turn, kind, media_ref))

    def prune_actions(self, key: str, before: float) -> None:
        self.db.execute("DELETE FROM actions WHERE scope=? AND created<?", (key, before))

    def charge_attempts(self, attempts: list[tuple[str, float]]) -> None:
        self.db.executemany("INSERT INTO send_attempts VALUES(?,?)", attempts)

    def prune_attempts(self, before: float) -> None:
        self.db.execute("DELETE FROM send_attempts WHERE at<=?", (before,))

    def record_delivery(self, status: str, message_ids: str, error: str | None, due: float, action: str) -> int:
        return self.db.execute("UPDATE actions SET status=?,message_ids=?,error=?,due=? WHERE id=? AND status IN ('sending','queued')", (status, message_ids, error, due, action)).rowcount

    def fail_queued_for_turn(self, scope: str, turn: str | None, error: str) -> None:
        self.db.execute("UPDATE actions SET status='failed',error=? WHERE scope=? AND status='queued' AND turn_id=?", (error, scope, turn))

    def fail_later_parts(self, action: str) -> None:
        self.db.execute("UPDATE actions SET status='failed',error='earlier part was not confirmed' WHERE status='queued' AND group_id=(SELECT group_id FROM actions WHERE id=?) AND ordinal>(SELECT ordinal FROM actions WHERE id=?)", (action, action))

    def failed_parts(self, action: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT id FROM actions WHERE group_id=(SELECT group_id FROM actions WHERE id=?) AND status='failed'", (action,)).fetchall()

    def insert_part(self, action: str, key: str, version: str, text: str, created: float, due: float, expires: float, reply_to: int | None, turn: str, group: str, ordinal: int) -> None:
        self.db.execute("INSERT INTO actions(id,scope,version,content,status,created,due,expires,reply_to,attempts,turn_id,group_id,ordinal) VALUES(?,?,?,?,'queued',?,?,?,?,0,?,?,?)", (action, key, version, text, created, due, expires, reply_to, turn, group, ordinal))

    def group_parts(self, group: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM actions WHERE group_id=? ORDER BY ordinal", (group,)).fetchall()

    def unknown_content(self, key: str, version: str, text: str, since: float) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM actions WHERE scope=? AND version=? AND status='unknown' AND trim(content)=trim(?) AND created>?", (key, version, text, since)).fetchone()

    def get(self, action: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM actions WHERE id=?", (action,)).fetchone()

    def attempt_count(self, key: str, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM send_attempts WHERE scope=? AND at>?", (key, since)).fetchone()[0]

    def unknown_part(self, key: str, version: str, text: str, since: float) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM actions WHERE scope=? AND version=? AND status='unknown' AND trim(content)=trim(?) AND created>?", (key, version, text, since)).fetchone()

    def queued(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM actions WHERE scope=? AND status='queued'", (key,)).fetchone()

    def set_reply_expiry(self, reply_to: int | None, turn: str | None, action: str) -> None:
        self.db.execute("UPDATE actions SET reply_to=?,expires=(SELECT MAX(expires) FROM wakes WHERE turn_id=? AND status='running') WHERE id=?", (reply_to, turn, action))

    def action_media(self, action: str) -> sqlite3.Row:
        return self.db.execute("SELECT kind,media_ref,turn_id,version FROM actions WHERE id=?", (action,)).fetchone()

    def part_position(self, action: str) -> sqlite3.Row:
        return self.db.execute("SELECT group_id,ordinal FROM actions WHERE id=?", (action,)).fetchone()

    def sent_sticker(self, action: str, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT media_ref FROM actions WHERE id=? AND scope=? AND status='sent' AND kind='sticker'", (action, key)).fetchone()

    def confirmed_turn(self, key: str, turn: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM actions WHERE scope=? AND turn_id=? AND (status='sent' OR json_array_length(message_ids)>0) LIMIT 1", (key, turn)).fetchone()

    def attempted_content(self, key: str, version: str, turn: str, text: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM actions WHERE scope=? AND version=? AND turn_id=? AND trim(content)=trim(?) AND status IN ('sent','sending','unknown')", (key, version, turn, text)).fetchone()

    def latest_content(self, key: str, version: str, turn: str | None, kind: str, text: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM actions WHERE scope=? AND version=? AND turn_id=? AND (kind=? OR (?='text' AND kind='message')) AND trim(content)=trim(?) ORDER BY created DESC LIMIT 1", (key, version, turn, kind, kind, text)).fetchone()

    def turn_groups(self, key: str, version: str, turn: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT DISTINCT group_id FROM actions WHERE scope=? AND version=? AND turn_id=? AND group_id IS NOT NULL", (key, version, turn)).fetchall()

    def later_part(self, group: str, ordinal: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM actions WHERE group_id=? AND ordinal>?", (group, ordinal)).fetchone()

    def turn_actions(self, key: str, turn: str | None) -> list[sqlite3.Row]:
        return self.db.execute("SELECT id,kind,status,message_ids FROM actions WHERE scope=? AND turn_id=?", (key, turn)).fetchall()

    def confirmed_receipt(self, key: str, turn: str, delivery: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM actions WHERE scope=? AND turn_id=? AND id=? AND (status='sent' OR json_array_length(message_ids)>0)", (key, turn, delivery)).fetchone()

    def recent_usage(self, key: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT id,turn_id,created,CASE WHEN kind='sticker' THEN '' ELSE content END AS text,CASE WHEN kind='sticker' THEN media_ref ELSE NULL END AS sticker FROM actions WHERE scope=? AND status='sent' ORDER BY created DESC,id DESC LIMIT 80", (key,)).fetchall()

    def fail_queued(self, key: str, error: str) -> None:
        self.db.execute("UPDATE actions SET status='failed',error=? WHERE scope=? AND status='queued'", (error, key))
