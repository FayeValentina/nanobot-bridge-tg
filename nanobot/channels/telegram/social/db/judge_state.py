"""Persist only the judge assessment watermark."""

import sqlite3


class JudgeState:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def through(self, key: str) -> int:
        row = self.db.execute("SELECT assessed_through FROM judge_state WHERE scope=?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def set_through(self, key: str, seq: int) -> None:
        self.db.execute("INSERT INTO judge_state(scope,assessed_through) VALUES(?,?) "
                        "ON CONFLICT(scope) DO UPDATE SET assessed_through=excluded.assessed_through", (key, seq))

    def reset_binding(self, key: str) -> None:
        self.db.execute("INSERT INTO judge_state(scope,assessed_through) SELECT key,next_seq-1 FROM scopes WHERE key=? "
                        "ON CONFLICT(scope) DO UPDATE SET assessed_through=excluded.assessed_through", (key,))
