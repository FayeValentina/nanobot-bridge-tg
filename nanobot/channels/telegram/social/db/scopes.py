from __future__ import annotations

import sqlite3
from collections.abc import Callable

from nanobot.channels.telegram.social.models import ConversationScope, SocialState


class Scopes:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], float]) -> None:
        self.db, self.now = db, now

    def setting_revision(self, name: str, value: str, *, force: bool = False) -> int:
        """Compare source settings once and persist a small monotonic revision."""
        row = self.db.execute("SELECT value,revision FROM runtime_settings WHERE name=?", (name,)).fetchone()
        if row is not None and row["value"] == value and not force:
            return int(row["revision"])
        revision = int(row["revision"]) + 1 if row else 1
        with self.db:
            self.db.execute("INSERT INTO runtime_settings VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
                            "value=excluded.value,revision=excluded.revision", (name, value, revision))
        return revision

    def owns(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM scopes WHERE key=?", (key,)).fetchone() is not None

    def all(self) -> list[ConversationScope]:
        return [ConversationScope(row[0], row[1], row[2]) for row in self.db.execute(
            "SELECT channel,chat_id,thread_id FROM scopes",
        )]

    def state(self, key: str) -> SocialState:
        row = self.db.execute("SELECT * FROM scopes WHERE key=?", (key,)).fetchone()
        if row is None:
            raise PermissionError("unknown social scope")
        return SocialState(
            key=str(row["key"]), channel=str(row["channel"]), chat_id=int(row["chat_id"]),
            thread_id=int(row["thread_id"]), version=str(row["version"]),
            next_seq=int(row["next_seq"]), read_through=int(row["read_through"]),
            seen_through=int(row["seen_through"]),
            mode=str(row["mode"]), wake_at=float(row["wake_at"]) if row["wake_at"] is not None else None,
            wait_after_seq=int(row["wait_after_seq"]), wake_on_timeout=bool(row["wake_on_timeout"]),
            wake_config=str(row["wake_config"]),
            event_seq=int(row["event_seq"]), timeout_through=int(row["timeout_through"]),
            wait_infinite=bool(row["wait_infinite"]),
        )

    def upsert(self, scope: ConversationScope, version: str) -> None:
        self.db.execute("INSERT INTO scopes(key,channel,chat_id,thread_id,version) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET version=excluded.version", (scope.session_key, scope.channel, scope.chat_id, scope.thread_id, version))

    def set_seen_through(self, key: str, frontier: int) -> None:
        self.db.execute("UPDATE scopes SET seen_through=? WHERE key=?", (frontier, key))

    def set_read_through(self, key: str, through: int) -> None:
        self.db.execute("UPDATE scopes SET read_through=? WHERE key=?", (through, key))

    def setting_value(self, name: str) -> str | None:
        row = self.db.execute("SELECT value FROM runtime_settings WHERE name=?", (name,)).fetchone()
        return str(row[0]) if row is not None else None

    def last_member_at(self, key: str) -> sqlite3.Row:
        return self.db.execute("SELECT last_member_at FROM scopes WHERE key=?", (key,)).fetchone()

    def latest_member_scope(self, channel: str, chat_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT key FROM scopes WHERE channel=? AND chat_id=? "
                               "ORDER BY last_member_at DESC,key LIMIT 1", (channel, chat_id)).fetchone()

    def allocate_seq(self, key: str, *, event: bool, member_at: float | None) -> int:
        seq = int(self.state(key)["next_seq"])
        self.db.execute("UPDATE scopes SET next_seq=next_seq+1 WHERE key=?", (key,))
        if event:
            self.db.execute("UPDATE scopes SET event_seq=? WHERE key=?", (seq, key))
            if member_at is not None:
                self.db.execute("UPDATE scopes SET last_member_at=MAX(COALESCE(last_member_at,?),?) WHERE key=?",
                                (member_at, member_at, key))
        return seq
