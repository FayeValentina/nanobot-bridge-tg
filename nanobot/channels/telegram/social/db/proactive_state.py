import sqlite3
from collections.abc import Callable


class ProactiveState:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], float]) -> None:
        self.db, self.now = db, now

    def get(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM proactive_state WHERE scope=?", (key,)).fetchone()

    def status(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT next_check,next_activity_check,last_check,decision,opportunity_seq,probability,sample,selected,metrics,stopped "
            "FROM proactive_state WHERE scope=?", (key,)).fetchone()

    def tracked_since(self, key: str) -> sqlite3.Row:
        return self.db.execute("SELECT tracked_since FROM proactive_state WHERE scope=?", (key,)).fetchone()

    def stopped(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT stopped FROM proactive_state WHERE scope=?", (key,)).fetchone()

    def sync(self, key: str, version: str, idle_due: float | None, activity_due: float | None, decision: str, tracked_since: float) -> None:
        self.db.execute("INSERT INTO proactive_state(scope,version,next_check,next_activity_check,decision,tracked_since) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET version=excluded.version,next_check=excluded.next_check,"
            "next_activity_check=excluded.next_activity_check,decision=excluded.decision", (key, version, idle_due, activity_due, decision, tracked_since))

    def record_check(self, next_check: float | None, next_activity_check: float | None, now: float, decision: str | None, probability: float | None, sample: float | None, selected: int | None, metrics: str, key: str) -> None:
        self.db.execute("UPDATE proactive_state SET next_check=?,next_activity_check=?,last_check=?,decision=?,opportunity_seq=opportunity_seq+1,"
            "probability=?,sample=?,selected=?,metrics=? WHERE scope=?", (next_check, next_activity_check, now, decision, probability, sample, selected, metrics, key))

    def set_decision(self, reason: str, key: str) -> None:
        self.db.execute("UPDATE proactive_state SET decision=? WHERE scope=?", (reason, key))

    def set_stopped(self, stopped: int, key: str) -> None:
        self.db.execute("UPDATE proactive_state SET stopped=? WHERE scope=?", (stopped, key))
