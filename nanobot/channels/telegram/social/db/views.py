from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from nanobot.channels.telegram.social.db.scopes import Scopes


class Views:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], float], scopes: Scopes) -> None:
        self.db, self.now = db, now
        self.scopes = scopes

    def shown_sequences(self, key: str, turn_id: str) -> set[int]:
        rows = self.db.execute(
            "SELECT seqs FROM views WHERE scope=? AND turn_id=? AND seen=1 AND version=?",
            (key, turn_id, self.scopes.state(key)["version"]),
        ).fetchall()
        return {int(seq) for row in rows for seq in json.loads(row["seqs"])}

    def mark_seen(self, view_id: str, turn_id: str) -> None:
        self.db.execute("UPDATE views SET seen=1, turn_id=? WHERE id=?", (turn_id, view_id))

    def unseen(self, key: str, version: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM views WHERE scope=? AND version=? AND seen=0", (key, version)).fetchall()

    def displayed_bodies(self, key: str, turn_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT body FROM views WHERE scope=? AND turn_id=? "
                               "AND seen=1 AND version=? ORDER BY created", (key, turn_id, self.scopes.state(key)["version"])).fetchall()

    def latest_body(self, key: str, turn_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT body FROM views WHERE scope=? AND turn_id=? ORDER BY created DESC,rowid DESC LIMIT 1", (key, turn_id)).fetchone()

    def turn_bodies(self, key: str, turn: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT body,turn_id FROM views WHERE scope=? AND (turn_id=? OR turn_id IS NULL) "
                               "ORDER BY created DESC,rowid DESC LIMIT 128", (key, turn)).fetchall()

    def record(self, view_id: str, key: str, version: str, body: str, seqs: list[int],
               turn_id: str | None, created: float) -> None:
        self.db.execute("INSERT INTO views VALUES(?,?,?,?,?,?,0,?)", (view_id, key, version, body,
                                                                   json.dumps(seqs), turn_id, created))
        self.db.execute("DELETE FROM views WHERE scope=? AND created<?", (key, created - 3600))
        self.db.execute(
            "DELETE FROM views WHERE scope=? AND id NOT IN "
            "(SELECT id FROM views WHERE scope=? ORDER BY created DESC LIMIT 128)", (key, key),
        )
