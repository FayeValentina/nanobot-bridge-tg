"""Single-owner SQLite observation, exposure, wake and delivery journal."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from nanobot.channels.telegram.social.judge_models import (
    JUDGE_PROMPT_VERSION,
    JudgeDecision,
    JudgeRecord,
    JudgeSnapshot,
)
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    DialogueContext,
    MediaReference,
    ObservedMessage,
    ProactiveCandidate,
    QuotedMessage,
    Snapshot,
    SnapshotMessage,
    SocialState,
    UncertainSend,
    WakeJob,
)


class SocialStore:
    def __init__(self, root: Path, *, now: Callable[[], float] = time.time) -> None:
        self.now = now
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "social.sqlite3"
        if path.is_symlink():
            raise ValueError("social database must not be a symlink")
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
            self.db.close()
            raise ValueError(f"unsupported social schema version: {version}")
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            self.db.close()
            raise ValueError("social database is damaged; restore from backup")
        if version == 0:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE scopes (
                    key TEXT PRIMARY KEY, channel TEXT NOT NULL, chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL, version TEXT NOT NULL,
                    next_seq INTEGER NOT NULL DEFAULT 1, read_through INTEGER NOT NULL DEFAULT 0,
                    seen_through INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE messages (
                    scope TEXT NOT NULL REFERENCES scopes(key), seq INTEGER NOT NULL,
                    update_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    sender_id TEXT NOT NULL, display_name TEXT NOT NULL, at REAL NOT NULL,
                    text TEXT NOT NULL, reply_to INTEGER, media TEXT NOT NULL, is_self INTEGER NOT NULL,
                    PRIMARY KEY(scope, seq), UNIQUE(scope, message_id)
                );
                CREATE TABLE updates (channel TEXT NOT NULL, id INTEGER NOT NULL,
                    PRIMARY KEY(channel, id));
                CREATE TABLE views (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
                    version TEXT NOT NULL, body TEXT NOT NULL, seqs TEXT NOT NULL,
                    turn_id TEXT, seen INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL
                );
                CREATE TABLE wakes (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
                    version TEXT NOT NULL, trigger_seq INTEGER NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                    turn_id TEXT
                );
                CREATE TABLE actions (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
                    version TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, message_id INTEGER, error TEXT
                );
                CREATE INDEX views_scope ON views(scope, created);
                CREATE INDEX wakes_scope ON wakes(scope, created);
                CREATE INDEX actions_scope ON actions(scope, created);
                PRAGMA user_version=1;
                COMMIT;
            """)
        if version < 2:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE scopes ADD COLUMN mode TEXT NOT NULL DEFAULT 'observing';
                ALTER TABLE scopes ADD COLUMN wake_at REAL;
                ALTER TABLE scopes ADD COLUMN wait_after_seq INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE scopes ADD COLUMN wake_on_timeout INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE scopes ADD COLUMN wake_config TEXT NOT NULL DEFAULT '{}';
                ALTER TABLE scopes ADD COLUMN event_seq INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE scopes ADD COLUMN timeout_through INTEGER NOT NULL DEFAULT -1;
                UPDATE scopes SET event_seq=next_seq-1;
                ALTER TABLE wakes ADD COLUMN due REAL NOT NULL DEFAULT 0;
                ALTER TABLE wakes ADD COLUMN reason TEXT NOT NULL DEFAULT 'directed';
                ALTER TABLE wakes ADD COLUMN requested REAL;
                UPDATE wakes SET due=created,requested=created;
                ALTER TABLE actions ADD COLUMN due REAL;
                ALTER TABLE actions ADD COLUMN expires REAL;
                ALTER TABLE actions ADD COLUMN reply_to INTEGER;
                ALTER TABLE actions ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;
                ALTER TABLE actions ADD COLUMN turn_id TEXT;
                ALTER TABLE actions ADD COLUMN group_id TEXT;
                ALTER TABLE actions ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0;
                CREATE TABLE requests (scope TEXT NOT NULL REFERENCES scopes(key), at REAL NOT NULL);
                INSERT INTO requests SELECT scope,requested FROM wakes WHERE requested IS NOT NULL;
                CREATE INDEX requests_scope ON requests(scope,at);
                CREATE TABLE send_attempts (scope TEXT NOT NULL REFERENCES scopes(key), at REAL NOT NULL);
                INSERT INTO send_attempts SELECT scope,created FROM actions;
                CREATE INDEX send_attempts_scope ON send_attempts(scope,at);
                CREATE INDEX wakes_due ON wakes(status,due);
                PRAGMA user_version=2;
                COMMIT;
            """)
        if version < 3:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE messages ADD COLUMN media_refs TEXT NOT NULL DEFAULT '[]';
                ALTER TABLE messages ADD COLUMN reply_context TEXT;
                ALTER TABLE messages ADD COLUMN album_id TEXT;
                ALTER TABLE actions ADD COLUMN kind TEXT NOT NULL DEFAULT 'text';
                ALTER TABLE actions ADD COLUMN media_ref TEXT;
                CREATE TABLE stickers (
                    scope TEXT NOT NULL REFERENCES scopes(key), id TEXT NOT NULL,
                    media_ref TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                    uses INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
                    PRIMARY KEY(scope,id)
                );
                PRAGMA user_version=3;
                COMMIT;
            """)
        if version < 4:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
                    kind TEXT NOT NULL, content TEXT NOT NULL, members TEXT NOT NULL,
                    status TEXT NOT NULL, basis TEXT NOT NULL, revision INTEGER NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, expires REAL
                );
                CREATE INDEX memories_scope ON memories(scope,status,updated);
                CREATE TABLE memory_evidence (
                    memory_id TEXT NOT NULL REFERENCES memories(id), message_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL, sender_id TEXT NOT NULL, at REAL NOT NULL,
                    excerpt TEXT NOT NULL, PRIMARY KEY(memory_id,message_id)
                );
                CREATE TABLE memory_suppressions (
                    scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(scope,kind,digest)
                );
                CREATE TABLE memory_resets (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), through_seq INTEGER NOT NULL
                );
                PRAGMA user_version=4;
                COMMIT;
            """)
        if version < 5:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE wakes ADD COLUMN proactive_source TEXT;
                CREATE TABLE proactive_state (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), version TEXT NOT NULL,
                    next_check REAL, last_check REAL, decision TEXT NOT NULL DEFAULT 'disabled',
                    last_seq INTEGER NOT NULL DEFAULT 0, memory_versions TEXT NOT NULL DEFAULT '{}'
                );
                PRAGMA user_version=5;
                COMMIT;
            """)
        if version < 6:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE message_deliveries (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
                    turn_id TEXT, created REAL NOT NULL, status TEXT NOT NULL,
                    message_ids TEXT NOT NULL, detail TEXT
                );
                CREATE INDEX message_deliveries_scope ON message_deliveries(scope,created);
                PRAGMA user_version=6;
                COMMIT;
            """)
        if version < 7:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE message_deliveries ADD COLUMN version TEXT;
                CREATE TABLE dialogue_state (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), last_at REAL,
                    observed_at REAL, observed_seq INTEGER NOT NULL DEFAULT 0, last_sent REAL
                );
                CREATE TABLE dialogue_waits (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), purpose TEXT NOT NULL,
                    started REAL NOT NULL, quiet_seconds REAL NOT NULL, quiet_due REAL,
                    total_due REAL NOT NULL, after_seq INTEGER NOT NULL, source_seq INTEGER NOT NULL,
                    on_timeout INTEGER NOT NULL, sleep_seconds REAL, condition_expires REAL
                );
                CREATE TABLE dialogue_attention (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), action_id TEXT NOT NULL,
                    after_seq INTEGER NOT NULL, participants TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, check_at REAL
                );
                CREATE TABLE dialogue_jobs (
                    job TEXT PRIMARY KEY REFERENCES wakes(id) ON DELETE CASCADE,
                    purpose TEXT NOT NULL, quiet_seconds REAL NOT NULL,
                    quiet_due REAL NOT NULL, total_due REAL NOT NULL,
                    after_seq INTEGER NOT NULL DEFAULT 0, source_seq INTEGER NOT NULL DEFAULT 0
                );
                PRAGMA user_version=7;
                COMMIT;
            """)
        if version < 8:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE scopes ADD COLUMN wait_infinite INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE wakes ADD COLUMN source_seq INTEGER;
                ALTER TABLE wakes ADD COLUMN condition_config TEXT NOT NULL DEFAULT '{}';
                UPDATE wakes SET source_seq=trigger_seq;
                CREATE TABLE wake_samples (
                    scope TEXT NOT NULL REFERENCES scopes(key), seq INTEGER NOT NULL,
                    version TEXT NOT NULL, probability REAL NOT NULL, value REAL NOT NULL,
                    selected INTEGER NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope,seq)
                );
                CREATE TABLE dialogue_waits_v8 (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), purpose TEXT NOT NULL,
                    started REAL NOT NULL, quiet_seconds REAL NOT NULL, quiet_due REAL,
                    total_due REAL, after_seq INTEGER NOT NULL, source_seq INTEGER NOT NULL,
                    on_timeout INTEGER NOT NULL, sleep_seconds REAL, condition_expires REAL,
                    sleep_until REAL
                );
                INSERT INTO dialogue_waits_v8 SELECT *,NULL FROM dialogue_waits;
                DROP TABLE dialogue_waits;
                ALTER TABLE dialogue_waits_v8 RENAME TO dialogue_waits;
                PRAGMA user_version=8;
                COMMIT;
            """)
        if version < 9:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS judge_state (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), assessed_through INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS judge_batches (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
                    version TEXT NOT NULL, signature TEXT NOT NULL, input_hash TEXT NOT NULL,
                    model TEXT NOT NULL, prompt_version TEXT NOT NULL,
                    after_seq INTEGER NOT NULL, through_seq INTEGER NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, due REAL NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    decision TEXT, error_code TEXT, fallback_at REAL,
                    UNIQUE(scope,version,signature,input_hash)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS judge_running_scope ON judge_batches(scope) WHERE status='running';
                CREATE TABLE IF NOT EXISTS judge_attempts (
                    batch_id TEXT NOT NULL REFERENCES judge_batches(id) ON DELETE CASCADE,
                    attempt INTEGER NOT NULL, started REAL NOT NULL, finished REAL,
                    error_code TEXT, latency_ms INTEGER, input_tokens INTEGER,
                    output_tokens INTEGER, total_tokens INTEGER,
                    PRIMARY KEY(batch_id,attempt)
                );
                CREATE INDEX IF NOT EXISTS judge_attempts_started ON judge_attempts(started);
                PRAGMA user_version=9;
                COMMIT;
            """)
        if version < 10:
            with self.db:
                for table, columns in {
                    "messages": {"judge_facts": "TEXT NOT NULL DEFAULT '{}'"},
                    "judge_batches": {"input_meta": "TEXT", "handoff_job": "TEXT", "condition_config": "TEXT NOT NULL DEFAULT '{}'"},
                    "judge_state": {"pending_first": "REAL", "pending_last": "REAL", "pending_seq": "INTEGER NOT NULL DEFAULT 0",
                                    "pending_due": "REAL", "pending_max": "REAL", "inflight": "INTEGER NOT NULL DEFAULT 0",
                                    "batch_id": "TEXT", "opportunity": "TEXT"},
                }.items():
                    existing = {str(row[1]) for row in self.db.execute(f"PRAGMA table_info({table})")}
                    for column, definition in columns.items():
                        if column not in existing:
                            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                # Existing messages have no V3 admission facts. Bootstrap assessment,
                # without changing main seen/read or replaying pre-upgrade history.
                self.db.execute("INSERT INTO judge_state(scope,assessed_through) SELECT key,next_seq-1 FROM scopes WHERE 1 "
                                "ON CONFLICT(scope) DO UPDATE SET assessed_through=MAX(assessed_through,excluded.assessed_through)")
                self.db.execute("PRAGMA user_version=10")
        self.closed = False
        path.chmod(0o600)

    def close(self) -> None:
        self.closed = True
        self.db.close()

    def owns(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM scopes WHERE key=?", (key,)).fetchone() is not None

    def scopes(self) -> list[ConversationScope]:
        return [ConversationScope(row[0], row[1], row[2]) for row in self.db.execute(
            "SELECT channel,chat_id,thread_id FROM scopes",
        )]

    def bind(self, scope: ConversationScope, version: str) -> None:
        if self.owns(scope.session_key) and self.state(scope.session_key)["version"] == version:
            return
        with self.db:
            self.db.execute(
                "INSERT INTO scopes(key,channel,chat_id,thread_id,version) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET version=excluded.version",
                (scope.session_key, scope.channel, scope.chat_id, scope.thread_id, version),
            )
            self.db.execute(
                "UPDATE wakes SET status='cancelled' WHERE scope=? AND version<>? "
                "AND status IN ('scheduled','queued','running')", (scope.session_key, version),
            )
            self.db.execute(
                "UPDATE scopes SET mode='observing',wake_at=NULL,wake_config='{}',wait_infinite=0 WHERE key=?",
                (scope.session_key,),
            )
            self._clear_dialogue(scope.session_key)
            self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='version_changed' "
                            "WHERE scope=? AND version<>? AND status IN ('pending','running')",
                            (scope.session_key, version))
            self.db.execute("INSERT INTO judge_state(scope,assessed_through) SELECT key,next_seq-1 FROM scopes WHERE key=? "
                            "ON CONFLICT(scope) DO UPDATE SET assessed_through=excluded.assessed_through,"
                            "pending_due=NULL,pending_first=NULL,pending_last=NULL,pending_max=NULL,batch_id=NULL,"
                            "inflight=0,opportunity=NULL", (scope.session_key,))
            self.db.execute(
                "UPDATE actions SET status='failed',error='configuration changed' "
                "WHERE scope=? AND version<>? AND status='queued'", (scope.session_key, version),
            )

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
        state = self.state(snapshot.scope)
        if (state["version"] != snapshot.version or state["mode"] == "paused"
                or not snapshot.created <= self.now() < snapshot.expires):
            return False
        displayed = {message.seq: message for message in snapshot.messages}
        rows = self.db.execute("SELECT seq,message_id,sender_id,text,is_self,judge_facts FROM messages WHERE scope=? AND seq IN "
                               "(SELECT value FROM json_each(?))", (snapshot.scope, json.dumps(list(displayed)))).fetchall()
        if len(rows) != len(displayed) or any(
            row["message_id"] != displayed[row["seq"]].message_id
            or row["sender_id"] != displayed[row["seq"]].sender_id
            or bool(row["is_self"]) != displayed[row["seq"]].is_self
            or (displayed[row["seq"]].content_hash and row["seq"] in snapshot.new_seqs
                and not json.loads(row["judge_facts"]).get("eligible"))
            or (hashlib.sha256(str(row["text"]).encode()).hexdigest() != displayed[row["seq"]].content_hash
                if displayed[row["seq"]].content_hash else row["text"] != displayed[row["seq"]].text)
            or not str(row["text"]).startswith(displayed[row["seq"]].text) for row in rows
        ):
            return False
        # Do not advance assessment across retained messages absent from the input.
        return not any(row[0] not in displayed for row in self.db.execute(
            "SELECT seq FROM messages WHERE scope=? AND seq>? AND seq<=?",
            (snapshot.scope, snapshot.after_seq, snapshot.through_seq),
        ))

    def open_judge_batch(self, snapshot: JudgeSnapshot, signature: str, model: str) -> JudgeRecord:
        if not self._judge_current(snapshot):
            raise PermissionError("judge snapshot is expired, changed, paused or not retained")
        with self.db:
            existing = self.db.execute("SELECT id FROM judge_batches WHERE scope=? AND version=? "
                                       "AND signature=? AND input_hash=?",
                                       (snapshot.scope, snapshot.version, signature, snapshot.input_hash())).fetchone()
            if existing:
                return self.judge_record(str(existing[0]))
            if snapshot.after_seq != self.judge_through(snapshot.scope):
                raise PermissionError("judge batch must start at the assessment watermark")
            batch = uuid4().hex
            self.db.execute("INSERT INTO judge_batches(id,scope,version,signature,input_hash,model,prompt_version,"
                            "after_seq,through_seq,created,expires,due,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
                            (batch, snapshot.scope, snapshot.version, signature, snapshot.input_hash(), model,
                             JUDGE_PROMPT_VERSION, snapshot.after_seq, snapshot.through_seq, snapshot.created,
                             snapshot.expires, self.now()))
            self.db.execute("DELETE FROM judge_batches WHERE created<? AND status NOT IN ('pending','running')",
                            (self.now() - 86400,))
        return self.judge_record(batch)

    def claim_judge_attempt(self, batch: str, snapshot: JudgeSnapshot, *, limit: int,
                            interval: float, max_attempts: int) -> JudgeRecord:
        with self.db:
            record = self.judge_record(batch)
            if record.input_hash != snapshot.input_hash():
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
        with self.db:
            record = self.judge_record(batch)
            if record.input_hash != snapshot.input_hash():
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
        with self.db:
            record = self.judge_record(batch)
            if record.status != "pending" or record.input_hash != snapshot.input_hash():
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
        with self.db:
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
        with nullcontext() if self.db.in_transaction else self.db:
            self.db.execute("INSERT INTO judge_state(scope,assessed_through) VALUES(?,0) ON CONFLICT(scope) DO NOTHING", (key,))
            row = self.db.execute("SELECT * FROM judge_state WHERE scope=?", (key,)).fetchone()
            if seq <= row["assessed_through"] and opportunity is None:
                return
            first = float(row["pending_first"]) if row["pending_first"] is not None else self.now()
            due = min(self.now() + delay, first + maximum)
            self.db.execute("UPDATE judge_state SET pending_first=?,pending_last=?,pending_seq=MAX(pending_seq,?),"
                            "pending_due=?,pending_max=?,opportunity=COALESCE(?,opportunity) WHERE scope=?",
                            (first, self.now(), seq, due, first + maximum, opportunity, key))

    def freeze_judge_input(self, batch: str, snapshot: JudgeSnapshot) -> None:
        # Store reconstruction facts and excerpt lengths, not another chat transcript.
        metadata = snapshot.model_dump(mode="json", exclude={"messages"})
        metadata["excerpts"] = [{"seq": message.seq, "length": len(message.text),
                                 "content_hash": message.content_hash,
                                 "reply_length": len(message.reply.text) if message.reply else 0}
                                for message in snapshot.messages]
        with self.db:
            self.db.execute("UPDATE judge_batches SET input_meta=?,condition_config=? WHERE id=? AND input_meta IS NULL",
                            (json.dumps(metadata, ensure_ascii=False), self.state(snapshot.scope)["wake_config"], batch))

    def _handoff_judgement(self, batch: str, snapshot: JudgeSnapshot, decision: JudgeDecision) -> None:
        """Caller owns the same transaction as the decision/cursor, including a single fallback."""
        row = self.db.execute("SELECT * FROM judge_batches WHERE id=?", (batch,)).fetchone()
        if row is None or row["input_meta"] is None or row["handoff_job"] is not None or decision.decision != "wake":
            return  # Standalone V3-A evaluations do not schedule main work.
        key = snapshot.scope
        for active in self.db.execute("SELECT id,turn_id FROM wakes WHERE scope=? AND "
                                      "(status='completed' OR (status='running' AND expires>?)) ORDER BY created DESC LIMIT 32",
                                      (key, self.now())).fetchall():
            shown = self._shown_sequences(key, str(active["turn_id"]))
            if snapshot.new_seqs and set(snapshot.new_seqs) <= shown and set(decision.source_seqs) <= shown:
                self.db.execute("UPDATE judge_batches SET handoff_job=? WHERE id=?", (active["id"], batch))
                if decision.fulfills_intent and snapshot.intent and snapshot.intent.one_shot and row["condition_config"] == self.state(key)["wake_config"]:
                    self.db.execute("UPDATE scopes SET wake_config='{}' WHERE key=?", (key,))
                return
        reason = "proactive" if snapshot.opportunity is not None and not snapshot.new_seqs else "judge"
        self._schedule(key, snapshot.through_seq, reason, 0)
        wake = self.db.execute("SELECT id,reason FROM wakes WHERE scope=? AND status='scheduled'", (key,)).fetchone()
        if wake is None:
            return
        conditions = row["condition_config"] if decision.fulfills_intent else "{}"
        expires = snapshot.expires
        if decision.fulfills_intent and snapshot.intent and snapshot.intent.condition_expires is not None:
            expires = min(expires, snapshot.intent.condition_expires)
        self.db.execute("UPDATE wakes SET expires=MIN(expires,?),condition_config=?,proactive_source=? WHERE id=?",
                        (expires, conditions, snapshot.opportunity if reason == "proactive" else None, wake["id"]))
        self.db.execute("UPDATE judge_batches SET handoff_job=? WHERE id=?", (wake["id"], batch))
        if snapshot.quiet_due is not None and snapshot.total_due is not None and reason == "judge":
            self.db.execute("INSERT OR REPLACE INTO dialogue_jobs(job,purpose,quiet_seconds,quiet_due,total_due,after_seq,source_seq) "
                            "VALUES(?,?,?,?,?,?,?)", (wake["id"], snapshot.intent.purpose if snapshot.intent and snapshot.intent.purpose else "reply",
                                                     max(0,snapshot.quiet_due-snapshot.created),snapshot.quiet_due,snapshot.total_due,
                                                     snapshot.after_seq,snapshot.through_seq))
        if row["status"] == "fallback":
            self.db.execute("UPDATE judge_state SET assessed_through=MAX(assessed_through,?) WHERE scope=?",
                            (snapshot.through_seq, key))

    def observe(self, msg: ObservedMessage, retention: int, *, reason: str | None = None,
                delay: float = 0, quiet_seconds: float | None = None,
                max_delay: float = 12, fresh: bool = False, wait_relevant: bool = True,
                judge_event: bool = False, judge_delay: float = 1.5,
                directed: bool = False) -> tuple[int, bool]:
        key = msg.scope.session_key
        with self.db:
            existing = self.db.execute(
                "SELECT seq FROM messages WHERE scope=? AND message_id=?", (key, msg.message_id),
            ).fetchone()
            if msg.update_id >= 0 and self.db.execute(
                "SELECT 1 FROM updates WHERE channel=? AND id=?", (msg.scope.channel, msg.update_id),
            ).fetchone():
                return (int(existing[0]) if existing else 0), False
            if msg.update_id >= 0:
                self.db.execute("INSERT INTO updates VALUES(?,?)", (msg.scope.channel, msg.update_id))
            if existing is not None:
                self.db.execute(
                    "UPDATE messages SET text=?,media=?,media_refs=?,reply_context=?,album_id=?,judge_facts='{}' "
                    "WHERE scope=? AND message_id=?",
                    (msg.text[:4000], json.dumps(msg.media),
                     json.dumps([ref.model_dump() for ref in msg.media_refs]),
                     msg.reply.model_dump_json() if msg.reply else None, msg.album_id, key, msg.message_id),
                )
                return int(existing[0]), False
            seq = int(self.state(key)["next_seq"])
            self.db.execute("UPDATE scopes SET next_seq=next_seq+1 WHERE key=?", (key,))
            if not msg.is_self and not msg.is_bot:
                self.db.execute("UPDATE scopes SET event_seq=? WHERE key=?", (seq, key))
            self.db.execute(
                "INSERT INTO messages(scope,seq,update_id,message_id,sender_id,display_name,at,text,"
                "reply_to,media,is_self,media_refs,reply_context,album_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, seq, msg.update_id, msg.message_id, msg.sender_id,
                 msg.display_name[:120], msg.timestamp, msg.text[:4000], msg.reply_to,
                 json.dumps(msg.media), int(msg.is_self),
                 json.dumps([ref.model_dump() for ref in msg.media_refs]),
                 msg.reply.model_dump_json() if msg.reply else None, msg.album_id),
            )
            self.db.execute("UPDATE messages SET judge_facts=? WHERE scope=? AND seq=?",
                            (json.dumps({"eligible": judge_event, "directed": directed, "bot_username": msg.bot_username,
                                         "received_at": self.now()}), key, seq))
            self.db.execute("DELETE FROM messages WHERE scope=? AND seq<=?", (key, seq - retention))
            # Keep update dedup bounded too. Old platform replays below the retained
            # window cannot produce wakes because message timestamps expire them.
            self.db.execute(
                "DELETE FROM updates WHERE channel=? AND id < ?",
                (msg.scope.channel, msg.update_id - max(10000, retention * 100)),
            )
            if fresh:
                self.db.execute("INSERT INTO dialogue_state(scope,last_at) VALUES(?,?) "
                                "ON CONFLICT(scope) DO UPDATE SET last_at=excluded.last_at", (key, self.now()))
                wait = self.db.execute("SELECT * FROM dialogue_waits WHERE scope=?", (key,)).fetchone()
                if wait is not None and wait["purpose"] == "reply" and seq > wait["after_seq"]:
                    # Continue an already requested answer after quiet, with its original total cap.
                    self.db.execute("UPDATE dialogue_waits SET quiet_due=? WHERE scope=?",
                                    (self.now()+float(wait["quiet_seconds"]),key))
            if reason is not None:
                self._schedule(key, seq, reason, delay)
            if judge_event:
                self.queue_judgement(key,seq,delay=judge_delay,maximum=max_delay)
            return seq, True

    def view(
        self, key: str, *, count: int, chars: int, trigger_seq: int | None = None,
        turn_id: str | None = None,
        job_id: str | None = None,
    ) -> Snapshot:
        state = self.state(key)
        read = int(state["read_through"])
        rows = self.db.execute(
            "SELECT * FROM messages WHERE scope=? AND seq>? ORDER BY seq LIMIT ?", (key, read, count),
        ).fetchall()
        if not rows:
            rows = list(reversed(self.db.execute(
                "SELECT * FROM messages WHERE scope=? ORDER BY seq DESC LIMIT ?", (key, count),
            ).fetchall()))
        if trigger_seq and all(row["seq"] != trigger_seq for row in rows):
            trigger = self.db.execute(
                "SELECT * FROM messages WHERE scope=? AND seq=?", (key, trigger_seq),
            ).fetchone()
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
                "seq": int(row["seq"]), "message_id": int(row["message_id"]),
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
        first = self.db.execute("SELECT MIN(seq) FROM messages WHERE scope=?", (key,)).fetchone()[0]
        view_id = uuid4().hex
        result: Snapshot = {
            "view_id": view_id, "now": now, "read_through": read,
            "history_gap": [read + 1, first - 1] if first is not None and first > read + 1 else None,
            "messages": records,
            "wake_job": job_id,
            "uncertain_send": self.uncertain_send(key),
            "mode": state["mode"],
            "wake_reason": self.db.execute("SELECT reason FROM wakes WHERE id=?", (job_id,)).fetchone()[0] if job_id else None,
            "opportunity": None,
            "dialogue": self.dialogue_job(job_id) if job_id else None,
            "judgement": [{"id": str(item["id"]), "status": str(item["status"]),
                           "through_seq": int(item["through_seq"]),
                           "decision": json.loads(item["decision"]) if item["decision"] else None,
                           "error_code": item["error_code"]}
                          for item in self.db.execute("SELECT * FROM judge_batches WHERE handoff_job=? ORDER BY created DESC LIMIT 1", (job_id,))] if job_id else [],
        }
        if job_id:
            source = self.db.execute("SELECT proactive_source FROM wakes WHERE id=?", (job_id,)).fetchone()[0]
            if source:
                result["opportunity"] = ProactiveCandidate.model_validate_json(source).model_dump()
        body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        with self.db:
            self.db.execute("INSERT INTO views VALUES(?,?,?,?,?,?,0,?)", (
                view_id, key, state["version"], body,
                json.dumps([row["seq"] for row in records]), turn_id, now,
            ))
            self.db.execute("DELETE FROM views WHERE scope=? AND created<?", (key, now - 3600))
            self.db.execute(
                "DELETE FROM views WHERE scope=? AND id NOT IN "
                "(SELECT id FROM views WHERE scope=? ORDER BY created DESC LIMIT 128)", (key, key),
            )
        return result

    def note_seen(self, key: str, turn_id: str, model_text: str) -> list[str]:
        state = self.state(key)
        admitted: list[str] = []
        with self.db:
            exposed = False
            for row in self.db.execute(
                "SELECT * FROM views WHERE scope=? AND version=? AND seen=0",
                (key, state["version"]),
            ).fetchall():
                if row["turn_id"] not in (None, turn_id) or row["body"] not in model_text:
                    continue
                job = json.loads(row["body"]).get("wake_job")
                if job:
                    changed = self.db.execute(
                        "UPDATE wakes SET status='running',turn_id=? WHERE id=? AND scope=? "
                        "AND status='queued' AND version=? AND expires>?",
                        (turn_id, job, key, state["version"], self.now()),
                    ).rowcount
                    if changed:
                        admitted.append(str(job))
                self.db.execute("UPDATE views SET seen=1, turn_id=? WHERE id=?", (turn_id, row["id"]))
                exposed = True
            seen = self._shown_sequences(key, turn_id)
            # A read tool can expose a busy-time trigger within the current run.
            # Only real provider exposure consumes it; returning the page alone cannot.
            for seq in seen:
                direct = self.db.execute("SELECT 1 FROM wakes WHERE scope=? AND status='scheduled' "
                                         "AND trigger_seq=? AND reason IN ('directed','nickname')", (key, seq)).fetchone()
                if direct:
                    self.db.execute("UPDATE wakes SET reason='directed',proactive_source=NULL "
                                    "WHERE scope=? AND turn_id=? AND reason='proactive'", (key, turn_id))
                candidates = self.db.execute("SELECT id FROM wakes WHERE scope=? AND version=? AND status='scheduled' "
                                             "AND trigger_seq=? AND expires>?", (key, state["version"], seq, self.now())).fetchall()
                for candidate in candidates:
                    try:
                        self._check_trigger_source(key, str(candidate[0]))
                    except PermissionError:
                        self.db.execute("UPDATE wakes SET status='cancelled' WHERE id=?", (candidate[0],))
                    else:
                        admitted.append(str(candidate[0]))
                self.db.execute("UPDATE wakes SET status='running',turn_id=? WHERE scope=? AND version=? "
                                "AND status='scheduled' AND trigger_seq=? AND expires>?",
                                (turn_id, key, state["version"], seq, self.now()))
            frontier = int(state["seen_through"])
            while frontier + 1 in seen:
                frontier += 1
            self.db.execute("UPDATE scopes SET seen_through=? WHERE key=?", (frontier, key))
            if seen and exposed:
                self.db.execute("INSERT INTO dialogue_state(scope,observed_at,observed_seq) VALUES(?,?,?) "
                                "ON CONFLICT(scope) DO UPDATE SET observed_at=COALESCE(observed_at,excluded.observed_at),"
                                "observed_seq=MAX(observed_seq,excluded.observed_seq)", (key, self.now(), max(seen)))
        return admitted

    def _shown_sequences(self, key: str, turn_id: str) -> set[int]:
        rows = self.db.execute(
            "SELECT seqs FROM views WHERE scope=? AND turn_id=? AND seen=1 AND version=?",
            (key, turn_id, self.state(key)["version"]),
        ).fetchall()
        return {int(seq) for row in rows for seq in json.loads(row["seqs"])}

    def mark_read(self, key: str, turn_id: str, through: int) -> int:
        with self.db:
            current = int(self.state(key)["read_through"])
            if through >= int(self.state(key)["next_seq"]):
                raise ValueError("read watermark exceeds observed history")
            if through < current:
                raise ValueError("read watermark cannot move backwards")
            shown = self._shown_sequences(key, turn_id)
            if any(seq not in shown for seq in range(current + 1, through + 1)):
                raise ValueError("only continuously displayed messages may be acknowledged")
            self.db.execute("UPDATE scopes SET read_through=? WHERE key=?", (through, key))
            return through

    def last_shown_member_text(self, key: str, turn: str) -> str:
        shown = self._shown_sequences(key, turn)
        row = self.db.execute("SELECT text FROM messages WHERE scope=? AND is_self=0 "
                              "AND seq IN (SELECT value FROM json_each(?)) ORDER BY seq DESC LIMIT 1",
                              (key, json.dumps(sorted(shown)))).fetchone()
        return str(row[0]) if row else ""

    def acknowledge_seen(self, key: str, turn_id: str) -> int:
        """Acknowledge only the contiguous prefix actually exposed in this turn."""
        current = int(self.state(key)["read_through"])
        shown = self._shown_sequences(key, turn_id)
        through = current
        while through + 1 in shown:
            through += 1
        return self.mark_read(key, turn_id, through)

    def wake(self, key: str, seq: int, limit: int, *, now: float) -> str | None:
        with self.db:
            count = self.db.execute(
                "SELECT COUNT(*) FROM requests WHERE scope=? AND at>?", (key, now - 3600),
            ).fetchone()[0]
            if count >= limit:
                return None
            job = uuid4().hex
            self.db.execute("INSERT INTO wakes(id,scope,version,trigger_seq,status,created,expires,turn_id,due,requested) "
                            "VALUES(?,?,?,?,?,?,?,NULL,?,?)", (
                job, key, self.state(key)["version"], seq, "queued", now, now + 120, now, now,
            ))
            self.db.execute("DELETE FROM wakes WHERE scope=? AND created<?", (key, now - 86400))
            self.db.execute("INSERT INTO requests VALUES(?,?)", (key, now))
            return job

    def claim(self, key: str, job: str, version: str, turn: str) -> None:
        with self.db:
            self._check_trigger_source(key, job)
            context = self.dialogue_job(job)
            if context is not None and context["source_seq"] > 0 and not self.db.execute(
                "SELECT 1 FROM messages WHERE scope=? AND seq=?", (key, context["source_seq"]),
            ).fetchone():
                raise PermissionError("dialogue source is no longer retained")
            changed = self.db.execute(
                "UPDATE wakes SET status='running', turn_id=? WHERE id=? AND scope=? AND version=? "
                "AND status='queued' AND expires>?", (turn, job, key, version, self.now()),
            ).rowcount
            if changed != 1:
                raise PermissionError("wake is stale, expired or already claimed")
            self.db.execute("UPDATE scopes SET mode='active',wake_at=NULL,wait_infinite=0 WHERE key=?", (key,))
            self.db.execute("DELETE FROM dialogue_waits WHERE scope=?", (key,))
            self.db.execute("DELETE FROM dialogue_attention WHERE scope=?", (key,))

    def active_turn(self, key: str, version: str, turn: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM wakes WHERE scope=? AND version=? AND turn_id=? "
            "AND status='running' AND expires>?", (key, version, turn, self.now()),
        ).fetchone() is not None

    def finish(self, turn: str, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE wakes SET status=? WHERE turn_id=? AND status='running'", (status, turn))
            self.db.execute("UPDATE scopes SET mode='observing' WHERE mode='active' AND key IN "
                            "(SELECT scope FROM wakes WHERE turn_id=?)", (turn,))

    def recovery_jobs(self) -> list[WakeJob]:
        with self.db:
            # A process may die after Telegram accepted an ordinary tool send.
            self.db.execute("UPDATE message_deliveries SET status='unknown',detail='Interrupted before platform receipt; do not resend.' WHERE status='queued'")
            self.db.execute("UPDATE wakes SET status='cancelled' WHERE status IN ('scheduled','queued','running') "
                            "AND EXISTS (SELECT 1 FROM message_deliveries d WHERE d.scope=wakes.scope "
                            "AND d.turn_id=wakes.turn_id AND d.status='unknown')")
            # Refund even expired queued opportunities before status recovery;
            # no model claimed these reservations before the restart.
            for opportunity in self.db.execute("SELECT * FROM wakes WHERE reason='proactive' AND status='queued'").fetchall():
                self._refund_unclaimed_opportunity(opportunity)
            # A durable wait decision is already the end of that run, even if
            # shutdown happened before the runner's completion hook.
            self.db.execute("UPDATE wakes SET status='completed' WHERE status='running' AND scope IN "
                            "(SELECT key FROM scopes WHERE mode IN ('waiting','sleeping'))")
            self.db.execute("UPDATE actions SET status='failed',error='unattempted after restart' "
                            "WHERE status='queued' AND attempts=0")
            self.db.execute("UPDATE actions SET status='unknown' WHERE status IN ('queued','sending')")
            # A model re-awakening on the same old question could otherwise
            # recreate a send whose platform result was lost during shutdown.
            self.db.execute(
                "UPDATE wakes SET status='cancelled' WHERE status IN ('scheduled','queued','running') "
                "AND EXISTS (SELECT 1 FROM actions WHERE actions.scope=wakes.scope "
                "AND actions.version=wakes.version AND actions.status='unknown' "
                "AND actions.created>=wakes.created)",
            )
            self.db.execute("UPDATE wakes SET status='expired' WHERE expires<=? AND status IN "
                            "('scheduled','queued','running')", (self.now(),))
            rows = self.db.execute(
                "SELECT * FROM wakes WHERE status IN ('scheduled','queued','running') ORDER BY created DESC",
            ).fetchall()
            result: list[WakeJob] = []
            seen: set[str] = set()
            for row in rows:
                status = "cancelled" if row["scope"] in seen else "scheduled"
                seen.add(row["scope"])
                self.db.execute("UPDATE wakes SET status=?,turn_id=NULL WHERE id=?", (status, row["id"]))
                # A restart must preserve dynamic quiet deadlines and total caps.
                dialogue = self.db.execute("SELECT quiet_due,total_due FROM dialogue_jobs WHERE job=?", (row["id"],)).fetchone()
                if dialogue is None:
                    self.db.execute("UPDATE wakes SET due=? WHERE id=?", (self.now(), row["id"]))
                else:
                    self.db.execute("UPDATE wakes SET due=? WHERE id=?", (min(dialogue[0],dialogue[1]),row["id"]))
                if status in ("scheduled", "queued"):
                    result.append(WakeJob(
                        id=str(row["id"]), scope=str(row["scope"]), version=str(row["version"]),
                        trigger_seq=int(row["trigger_seq"]), status=status,
                        created=float(row["created"]), expires=float(row["expires"]),
                        turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
                        due=float(row["due"]), reason=str(row["reason"]),
                    ))
            self.db.execute("UPDATE scopes SET mode='observing' WHERE mode='active'")
            return result

    def _refund_unclaimed_opportunity(self, row: sqlite3.Row) -> None:
        if row["status"] == "queued" and row["turn_id"] is None and row["requested"] is not None:
            self.db.execute("DELETE FROM requests WHERE rowid=(SELECT rowid FROM requests WHERE scope=? AND at=? LIMIT 1)",
                            (row["scope"], row["requested"]))
            self.db.execute("UPDATE wakes SET requested=NULL WHERE id=?", (row["id"],))

    def cancel_opportunity(self, job: str, reason: str) -> None:
        """Caller owns the transaction; an unclaimed slot was not a model call."""
        row = self.db.execute("SELECT * FROM wakes WHERE id=? AND reason='proactive'", (job,)).fetchone()
        if row is None:
            return
        self._refund_unclaimed_opportunity(row)
        self.db.execute("UPDATE wakes SET status='cancelled' WHERE id=?", (job,))
        self.db.execute("UPDATE actions SET status='failed',error=? WHERE scope=? AND status='queued' "
                        "AND turn_id=?", (f"opportunity skipped: {reason}", row["scope"], row["turn_id"]))

    def reserve_send(self, key: str, version: str, text: str, limit: int, *, turn: str | None = None,
                     sticker: MediaReference | None = None) -> str:
        with self.db:
            if self.state(key)["version"] != version:
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
            if count >= limit:
                raise PermissionError("hourly send budget exhausted")
            action = uuid4().hex
            self.db.execute("INSERT INTO actions(id,scope,version,content,status,created,turn_id,kind,media_ref) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", (
                action, key, version, text, "sending", self.now(), turn,
                "sticker" if sticker else "text", sticker.model_dump_json() if sticker else None,
            ))
            self.db.execute("DELETE FROM actions WHERE scope=? AND created<?", (key, self.now() - 604800))
            self.db.execute("INSERT INTO send_attempts VALUES(?,?)", (key, self.now()))
            self.db.execute("DELETE FROM send_attempts WHERE at<=?", (self.now() - 3600,))
            return action

    def uncertain_send(self, key: str) -> UncertainSend | None:
        row = self.db.execute(
            "SELECT id,content FROM actions WHERE scope=? AND version=? AND status='unknown' "
            "AND created>? ORDER BY created DESC LIMIT 1",
            (key, self.state(key)["version"], self.now() - 86400),
        ).fetchone()
        return UncertainSend(action_id=str(row["id"]), text=str(row["content"])[:100], status="unknown") if row else None

    def delivery(self, action: str, status: str, message_id: int | None = None, error: str | None = None) -> None:
        with self.db:
            previous = self.db.execute("SELECT * FROM actions WHERE id=?", (action,)).fetchone()
            if status == "sent" and previous is not None and previous["status"] != "sent" and previous["kind"] == "sticker":
                ref = MediaReference.model_validate_json(previous["media_ref"])
                self.db.execute("UPDATE stickers SET uses=uses+1,updated=? WHERE scope=? AND id=?",
                                (self.now(), previous["scope"], ref.unique_id))
            self.db.execute(
                "UPDATE actions SET status=?,message_id=?,error=?,due=? WHERE id=?",
                (status, message_id, error[:200] if error else None, self.now(), action),
            )
            if status in ("failed", "unknown"):
                self.db.execute("UPDATE actions SET status='failed',error='earlier part was not confirmed' "
                                "WHERE status='queued' AND group_id=(SELECT group_id FROM actions WHERE id=?) "
                                "AND ordinal>(SELECT ordinal FROM actions WHERE id=?)", (action, action))

    def has_message(self, key: str, message_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM messages WHERE scope=? AND (message_id=? OR (reply_to=? AND reply_context IS NOT NULL))",
            (key, message_id, message_id),
        ).fetchone() is not None

    def media_for_message(self, key: str, message_id: int) -> list[MediaReference]:
        row = self.db.execute("SELECT media_refs FROM messages WHERE scope=? AND message_id=?",
                              (key, message_id)).fetchone()
        if row is not None:
            return [MediaReference.model_validate(ref) for ref in json.loads(row[0])]
        row = self.db.execute("SELECT reply_context FROM messages WHERE scope=? AND reply_to=? "
                              "AND reply_context IS NOT NULL ORDER BY seq DESC LIMIT 1", (key, message_id)).fetchone()
        if row is not None:
            return QuotedMessage.model_validate_json(row[0]).media_refs
        raise PermissionError("media message was not observed in this group/topic")

    def collect_sticker(self, key: str, ref: MediaReference, note: str | None, limit: int) -> str:
        if ref.kind != "sticker" or ref.animated or ref.video:
            raise ValueError("only static stickers can be collected in this release")
        note = " ".join(note.split()) if note is not None else None
        if note is not None and len(note) > 400:
            raise ValueError("sticker note exceeds 400 characters")
        with self.db:
            exists = self.db.execute("SELECT 1 FROM stickers WHERE scope=? AND id=?", (key, ref.unique_id)).fetchone()
            count = self.db.execute("SELECT COUNT(*) FROM stickers WHERE scope=?", (key,)).fetchone()[0]
            if not exists and count >= limit:
                raise PermissionError("sticker collection limit reached")
            self.db.execute("INSERT INTO stickers(scope,id,media_ref,note,updated) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(scope,id) DO UPDATE SET media_ref=excluded.media_ref,"
                            "note=COALESCE(?,stickers.note),updated=excluded.updated",
                            (key, ref.unique_id, ref.model_dump_json(), note or "", self.now(), note))
        return ref.unique_id

    def sticker(self, key: str, sticker_id: str) -> MediaReference:
        row = self.db.execute("SELECT media_ref FROM stickers WHERE scope=? AND id=?", (key, sticker_id)).fetchone()
        if row is None:
            raise PermissionError("sticker is not collected in this group/topic")
        return MediaReference.model_validate_json(row[0])

    def list_stickers(self, key: str, query: str) -> list[dict[str, object]]:
        if len(query) > 100:
            raise ValueError("sticker query exceeds 100 characters")
        rows = self.db.execute("SELECT * FROM stickers WHERE scope=? AND "
                               "(instr(lower(note),lower(?))>0 OR instr(media_ref,?)>0) "
                               "ORDER BY updated DESC LIMIT 30", (key, query, query)).fetchall()
        return [{"sticker_id": row["id"], "note": row["note"], "uses": row["uses"],
                 "emoji": MediaReference.model_validate_json(row["media_ref"]).emoji} for row in rows]

    def pause(self, key: str) -> None:
        with self.db:
            self._clear_dialogue(key)
            self.db.execute("UPDATE scopes SET mode='paused',wake_at=NULL,wait_infinite=0 WHERE key=?", (key,))
            self.db.execute("UPDATE wakes SET status='cancelled' WHERE scope=? "
                            "AND status IN ('scheduled','queued','running')", (key,))
            self.db.execute("UPDATE actions SET status='failed',error='paused' "
                            "WHERE scope=? AND status='queued'", (key,))
            self.db.execute("UPDATE judge_state SET pending_due=NULL,batch_id=NULL WHERE scope=?", (key,))
            self.db.execute("UPDATE judge_batches SET status='cancelled',error_code='paused' "
                            "WHERE scope=? AND status IN ('pending','running')", (key,))

    def _clear_dialogue(self, key: str) -> None:
        """Caller owns the configuration/pause transaction."""
        for table in ("dialogue_state", "dialogue_waits", "dialogue_attention"):
            self.db.execute(f"DELETE FROM {table} WHERE scope=?", (key,))

    def dialogue_job(self, job: str) -> DialogueContext | None:
        row = self.db.execute("SELECT * FROM dialogue_jobs WHERE job=?", (job,)).fetchone()
        if row is None:
            return None
        return {"purpose": str(row["purpose"]), "quiet": self.now() >= float(row["quiet_due"]),
                "quiet_due": float(row["quiet_due"]), "total_due": float(row["total_due"]),
                "source_seq": int(row["source_seq"]), "after_seq": int(row["after_seq"])}

    def wait_for_quiet(self, key: str, turn: str, *, purpose: str, quiet: float,
                       seconds: float | None, on_timeout: bool, sleep_seconds: float | None = None,
                       quiet_due: float | None = None, condition_expires: float | None = None,
                       selective: bool = False, sleep_until: float | None = None,
                       max_delay: float = 12) -> dict[str, object]:
        """Persist a continuation, never a long-running tool or an agent lock."""
        now = self.now()
        shown = self._shown_sequences(key, turn)
        after = max(shown, default=0)
        original_source = self.db.execute("SELECT MAX(trigger_seq) FROM wakes WHERE scope=? AND turn_id=? AND status='running'",
                                          (key, turn)).fetchone()[0]
        source = int(original_source) if original_source in shown else max(shown, default=0)
        activity = self.db.execute("SELECT * FROM dialogue_state WHERE scope=?", (key,)).fetchone()
        last = float(activity["last_at"]) if activity and activity["last_at"] is not None else now
        total = now + seconds if seconds is not None else None
        if purpose == "reply":
            if total is None:
                raise ValueError("reply waiting must have a finite total limit")
            if not source:
                raise ValueError("reply waiting requires messages actually displayed in this turn")
            original = self.db.execute("SELECT MIN(d.total_due) FROM dialogue_jobs d JOIN wakes w ON w.id=d.job "
                                       "WHERE w.scope=? AND w.turn_id=?", (key, turn)).fetchone()[0]
            if original is not None:
                total = min(total, float(original))
            quiet_due = last + quiet
            if now >= min(quiet_due, total):
                return {"deferred": False, "purpose": purpose, "quiet": now >= quiet_due}
        elif purpose == "messages":
            pending = self.db.execute("SELECT MAX(trigger_seq) FROM wakes WHERE scope=? AND status='scheduled' "
                                      "AND reason='judge' AND trigger_seq>?", (key, after)).fetchone()[0]
            candidate = int(pending) if pending is not None else self.state(key)["event_seq"] if not selective else 0
            if candidate > after:
                source, quiet_due = candidate, last + quiet
                if total is None:
                    total = now + max_delay
            else:
                quiet_due = None
        with self.db:
            self.db.execute("DELETE FROM dialogue_attention WHERE scope=?", (key,))
            self.db.execute("INSERT OR REPLACE INTO dialogue_waits VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (key, purpose, now, quiet, quiet_due, total, after, source, int(on_timeout), sleep_seconds, condition_expires, sleep_until))
            self.db.execute("UPDATE scopes SET mode='waiting',wake_at=NULL,wait_after_seq=?,wait_infinite=? WHERE key=?",
                            (after, int(seconds is None), key))
            # A newer direct mention always survives the decision to wait.
            self.db.execute("UPDATE wakes SET status='cancelled' WHERE scope=? AND status='scheduled' "
                            "AND reason NOT IN ('directed','nickname','judge') AND trigger_seq<=?", (key, after))
            if purpose in {"reply", "messages"}:
                self.db.execute("UPDATE judge_state SET pending_due=MIN(pending_last+?,pending_max,COALESCE(?,pending_max)) "
                                "WHERE scope=? AND batch_id IS NULL AND pending_due IS NOT NULL", (quiet,total,key))
        return {"deferred": True, "purpose": purpose, "mode": "waiting", "total_due": total,
                "quiet_due": quiet_due}

    def sleep_observation_due(self, key: str, window: float) -> float | None:
        """Receiving content alone does not establish model observation."""
        row = self.db.execute("SELECT * FROM dialogue_state WHERE scope=?", (key,)).fetchone()
        now = self.now()
        if row is None:
            return now + window
        last = float(row["last_at"]) if row["last_at"] is not None else now
        if row["last_sent"] is not None:
            last = max(last, float(row["last_sent"]))
        if 0 <= now - last and now - last >= window:
            return None  # The conversation has already been continuously quiet.
        observed = float(row["observed_at"]) if row["observed_at"] is not None else now
        if observed <= now and now >= observed + window and int(row["observed_seq"]) >= self.state(key)["event_seq"]:
            return None
        fully_observed = int(row["observed_seq"]) >= self.state(key)["event_seq"]
        observation_due = observed + window if fully_observed and observed <= now < observed + window else last + window
        return min(observation_due, last + window)

    def consume_dialogue(self) -> None:
        now = self.now()
        with self.db:
            # Backwards clock jumps invalidate transient attention, not history.
            self.db.execute("UPDATE wakes SET status='cancelled' WHERE status='scheduled' AND created>? "
                            "AND id IN (SELECT job FROM dialogue_jobs)", (now,))
            self.db.execute("UPDATE dialogue_state SET last_at=MIN(last_at,?),last_sent=MIN(last_sent,?),"
                            "observed_at=CASE WHEN observed_at>? THEN NULL ELSE observed_at END", (now, now, now))
            self.db.execute("DELETE FROM dialogue_attention WHERE expires<=? OR created>?", (now, now))
            self.db.execute("UPDATE dialogue_attention SET check_at=NULL WHERE check_at<=?", (now,))
            for row in self.db.execute("SELECT * FROM dialogue_waits").fetchall():
                key = str(row["scope"])
                quiet_due = float(row["quiet_due"]) if row["quiet_due"] is not None else None
                limits = [float(row[name]) for name in ("quiet_due", "total_due") if row[name] is not None]
                due = min(limits) if limits else None
                expired_condition = row["condition_expires"] is not None and float(row["condition_expires"]) <= now
                if now >= float(row["started"]) and (due is None or now < due) and not expired_condition:
                    continue
                self.db.execute("DELETE FROM dialogue_waits WHERE scope=?", (key,))
                self.db.execute("UPDATE scopes SET mode='observing',wait_infinite=0 WHERE key=? AND mode='waiting'", (key,))
                if expired_condition or due is None or now < float(row["started"]) or now >= due + 120:
                    continue
                if row["purpose"] == "observe":
                    # A pending/new event gets a model opportunity before sleeping.
                    pending = self.db.execute("SELECT 1 FROM wakes WHERE scope=? AND status IN ('scheduled','queued','running') "
                                              "AND (turn_id IS NULL OR status<>'running')", (key,)).fetchone()
                    if pending is None and self.state(key)["event_seq"] <= int(row["after_seq"]):
                        wake_at = (float(row["sleep_until"]) if row["sleep_until"] is not None else
                                   now + float(row["sleep_seconds"]) if row["sleep_seconds"] is not None else None)
                        self.db.execute("UPDATE scopes SET mode='sleeping',wake_at=?,wake_on_timeout=?,wait_infinite=? WHERE key=?",
                                        (wake_at, row["on_timeout"], int(wake_at is None), key))
                    continue
                new = int(row["source_seq"]) > int(row["after_seq"])
                if row["purpose"] != "reply" and not new and not row["on_timeout"]:
                    continue
                if row["purpose"] == "messages" and not new:
                    state = self.state(key)
                    if state["event_seq"] <= state["timeout_through"]:
                        continue
                    self.db.execute("UPDATE scopes SET timeout_through=event_seq WHERE key=?", (key,))
                reason = "wait_reply" if row["purpose"] == "reply" else "wait_messages"
                seq = int(row["source_seq"])
                # Consume and enqueue in the same transaction, even if the source was marked read.
                self._schedule(key, seq, reason, 0)
                job = self.db.execute("SELECT id FROM wakes WHERE scope=? AND status='scheduled'", (key,)).fetchone()
                if job:
                    self.db.execute("INSERT OR REPLACE INTO dialogue_jobs VALUES(?,?,?,?,?,?,?)",
                                    (job[0], row["purpose"], row["quiet_seconds"], quiet_due if quiet_due is not None else now + 1,
                                     row["total_due"], row["after_seq"], seq))
                    if new:
                        self.db.execute("UPDATE wakes SET condition_config=? WHERE id=? AND reason='wait_messages'",
                                        (self.state(key)["wake_config"], job[0]))
                    if row["condition_expires"] is not None:
                        self.db.execute("UPDATE wakes SET expires=MIN(expires,?) WHERE id=? AND reason='wait_messages'",
                                        (row["condition_expires"], job[0]))

    def dialogue_status(self, key: str) -> dict[str, object]:
        wait = self.db.execute("SELECT * FROM dialogue_waits WHERE scope=?", (key,)).fetchone()
        focus = self.db.execute("SELECT * FROM dialogue_attention WHERE scope=?", (key,)).fetchone()
        return {"wait": dict(wait) if wait else None, "attention": dict(focus) if focus else None}

    def defer(self, key: str, turn: str, *, sleeping: bool, seconds: float | None, on_timeout: bool) -> None:
        shown = self._shown_sequences(key, turn)
        with self.db:
            self.db.execute("DELETE FROM dialogue_waits WHERE scope=?", (key,))
            self.db.execute("DELETE FROM dialogue_attention WHERE scope=?", (key,))
            self.db.execute(
                "UPDATE scopes SET mode=?,wake_at=?,wake_on_timeout=?,wait_after_seq=?,wait_infinite=? WHERE key=?",
                ("sleeping" if sleeping else "waiting", self.now() + seconds if seconds is not None else None,
                 int(on_timeout), max(shown, default=0), int(seconds is None), key),
            )
            # A newly arrived direct address must survive a simultaneous wait.
            self.db.execute("UPDATE wakes SET status='cancelled' WHERE scope=? AND status='scheduled' "
                            "AND reason NOT IN ('directed','nickname','judge') AND (trigger_seq<=? OR ?)",
                            (key, max(shown, default=0), int(sleeping)))

    def set_wake_config(self, key: str, body: str) -> None:
        with self.db:
            for candidate in self.db.execute("SELECT * FROM wakes WHERE scope=? AND condition_config<>'{}' "
                                             "AND condition_config<>? AND status IN ('scheduled','queued')", (key, body)).fetchall():
                self._refund_unclaimed_opportunity(candidate)
                self.db.execute("UPDATE wakes SET status='cancelled' WHERE id=?", (candidate["id"],))
            self.db.execute("UPDATE scopes SET wake_config=? WHERE key=?", (body, key))
            if body == "{}":
                expired = self.db.execute("DELETE FROM dialogue_waits WHERE scope=? AND condition_expires<=?",
                                          (key, self.now())).rowcount
                if expired:
                    self.db.execute("UPDATE scopes SET mode='observing',wait_infinite=0 WHERE key=? AND mode='waiting'", (key,))
                self.db.execute("UPDATE scopes SET mode='observing',wait_infinite=0,wake_at=NULL WHERE key=? "
                                "AND wait_infinite=1 AND mode IN ('waiting','sleeping')", (key,))

    def schedule(self, key: str, seq: int, reason: str, delay: float) -> None:
        with self.db:
            self._schedule(key, seq, reason, delay)

    def _schedule(self, key: str, seq: int, reason: str, delay: float) -> None:
        """Caller owns the transaction, including observation/dedup when applicable."""
        now = self.now()
        rank = {"judge": 4, "proactive": -1, "timeout": 0, "wait_reply": 1, "wait_messages": 1,
                "probability": 0, "attention": 1, "message": 1, "sender": 2, "keyword": 3,
                "question": 4, "nickname": 5, "directed": 6}
        state = self.state(key)
        if state["mode"] == "paused":
            return
        # A queued opportunity has not reached a model yet. Let an actual
        # conversation replace it; its old bus item will fail ordinary claim.
        for opportunity in self.db.execute("SELECT id FROM wakes WHERE scope=? AND reason='proactive' AND status='queued'", (key,)).fetchall():
            self.cancel_opportunity(str(opportunity[0]), "conversation arrived")
        for queued in self.db.execute("SELECT * FROM wakes WHERE scope=? AND status='queued' AND turn_id IS NULL",
                                      (key,)).fetchall():
            if reason in {"probability", "timeout"} and rank[str(queued["reason"])] > rank[reason]:
                return  # An unclaimed higher-priority conversation already has the slot.
            if queued["reason"] in {"probability", "timeout"} and reason in {"directed", "nickname", "question", "keyword", "sender"}:
                self._refund_unclaimed_opportunity(queued)
                self.db.execute("UPDATE wakes SET status='cancelled' WHERE id=?", (queued["id"],))
        row = self.db.execute("SELECT * FROM wakes WHERE scope=? AND version=? "
                              "AND status='scheduled'", (key, state["version"])).fetchone()
        condition_config = state["wake_config"] if reason in {"keyword", "sender", "message", "question", "probability"} else "{}"
        if row is not None:
            chosen = reason if rank[reason] >= rank[str(row["reason"])] else str(row["reason"])
            source = seq if chosen == reason else int(row["source_seq"] or row["trigger_seq"])
            if chosen != reason:
                condition_config = str(row["condition_config"])
            self.db.execute("UPDATE wakes SET trigger_seq=?,source_seq=?,condition_config=?,reason=?,due=MIN(due,?),expires=?,proactive_source=NULL WHERE id=?",
                            (max(seq, int(row["trigger_seq"])), source, condition_config, chosen, now + delay,
                             now + 120, row["id"]))
        else:
            self.db.execute("INSERT INTO wakes(id,scope,version,trigger_seq,source_seq,condition_config,status,created,expires,due,reason) "
                            "VALUES(?,?,?,?,?,?,'scheduled',?,?,?,?)",
                            (uuid4().hex, key, state["version"], seq, seq, condition_config, now, now + 120, now + delay, reason))
        self.db.execute("DELETE FROM wakes WHERE scope=? AND created<? AND status NOT IN "
                        "('scheduled','queued','running')", (key, now - 86400))

    def consume_timers(self) -> None:
        now = self.now()
        self.consume_dialogue()
        for scope in self.scopes():
            state = self.state(scope.session_key)
            if state["wake_at"] is None or state["wake_at"] > now:
                continue
            with self.db:
                self.db.execute("UPDATE scopes SET mode='observing',wake_at=NULL,wait_infinite=0 WHERE key=?", (scope.session_key,))
                if (state["wake_on_timeout"] and now < state["wake_at"] + 120
                        and state["event_seq"] > state["timeout_through"]):
                    # Insert in the same transaction as consuming the timer.
                    self.db.execute("INSERT INTO wakes(id,scope,version,trigger_seq,status,created,expires,due,reason) "
                                    "SELECT ?,?,?,?,'scheduled',?,?,?,'timeout' WHERE NOT EXISTS "
                                    "(SELECT 1 FROM wakes WHERE scope=? AND status='scheduled')",
                                    (uuid4().hex, scope.session_key, state["version"], state["next_seq"] - 1,
                                     now, now + 120, now, scope.session_key))
                    self.db.execute("UPDATE scopes SET timeout_through=event_seq WHERE key=?", (scope.session_key,))

    def ready(self, limit: int, cooldown: float) -> list[WakeJob]:
        now = self.now()
        ready: list[WakeJob] = []
        with self.db:
            self.db.execute("UPDATE wakes SET status='expired' WHERE expires<=? AND status IN "
                            "('scheduled','queued','running')", (now,))
            for row in self.db.execute("SELECT * FROM wakes WHERE status='scheduled' AND due<=? "
                                       "ORDER BY due", (now,)).fetchall():
                key = str(row["scope"])
                state = self.state(key)
                if state["version"] != row["version"] or state["mode"] == "paused":
                    self.db.execute("UPDATE wakes SET status='cancelled' WHERE id=?", (row["id"],))
                    continue
                try:
                    self._check_trigger_source(key, str(row["id"]))
                except PermissionError:
                    self.db.execute("UPDATE wakes SET status='cancelled' WHERE id=?", (row["id"],))
                    continue
                context = self.dialogue_job(str(row["id"]))
                if (context is not None and context["source_seq"] > 0 and not self.db.execute(
                    "SELECT 1 FROM messages WHERE scope=? AND seq=?", (key, context["source_seq"]),
                ).fetchone()):
                    self.db.execute("UPDATE wakes SET status='cancelled' WHERE id=?", (row["id"],))
                    continue
                busy = self.db.execute("SELECT MAX(expires) FROM wakes WHERE scope=? "
                                       "AND status IN ('queued','running')", (key,)).fetchone()[0]
                if busy is not None:
                    self.db.execute("UPDATE wakes SET due=? WHERE id=?", (min(float(busy), now + 1), row["id"]))
                    continue
                requests = self.db.execute("SELECT at FROM requests WHERE scope=? AND at>? "
                                           "ORDER BY at", (key, now - 3600)).fetchall()
                due = max(now, float(requests[-1][0]) + cooldown if requests else now)
                if len(requests) >= limit:
                    due = max(due, float(requests[-limit][0]) + 3600)
                if due > now:
                    self.db.execute("UPDATE wakes SET due=? WHERE id=?", (due, row["id"]))
                    continue
                changed = self.db.execute("UPDATE wakes SET status='queued',requested=? WHERE id=? "
                                          "AND status='scheduled'", (now, row["id"])).rowcount
                if changed:
                    self.db.execute("INSERT INTO requests VALUES(?,?)", (key, now))
                    ready.append(WakeJob(
                        id=str(row["id"]), scope=key, version=str(row["version"]),
                        trigger_seq=int(row["trigger_seq"]), status="queued", created=float(row["created"]),
                        expires=float(row["expires"]), turn_id=None, due=now, reason=str(row["reason"]),
                    ))
            self.db.execute("DELETE FROM requests WHERE at<=?", (now - 3600,))
        return ready

    def _check_trigger_source(self, key: str, job: str) -> None:
        row = self.db.execute("SELECT * FROM wakes WHERE id=? AND scope=?", (job, key)).fetchone()
        if row is not None and row["condition_config"] != "{}" and row["condition_config"] != self.state(key)["wake_config"]:
            raise PermissionError("temporary wake conditions changed")
        if row is None:
            return
        for batch in self.db.execute("SELECT input_meta,decision FROM judge_batches WHERE handoff_job=?", (job,)):
            if batch[0] is None:
                continue
            metadata = json.loads(batch[0])
            sources = json.loads(batch[1])["source_seqs"] if batch[1] else metadata["new_seqs"]
            hashes = {item["seq"]:item["content_hash"] for item in metadata["excerpts"]}
            for seq in sources:
                message = self.db.execute("SELECT text,judge_facts FROM messages WHERE scope=? AND seq=?", (key,seq)).fetchone()
                if (message is None or (hashes.get(seq) and hashlib.sha256(str(message[0]).encode()).hexdigest()!=hashes[seq])
                        or (seq in metadata["new_seqs"] and not json.loads(message[1]).get("eligible"))):
                    raise PermissionError("judged source is no longer retained or changed")

    def release_pending(self, key: str) -> None:
        with self.db:
            self.db.execute("UPDATE wakes SET due=MIN(due,?) WHERE scope=? AND status='scheduled' "
                            "AND id NOT IN (SELECT job FROM dialogue_jobs)", (self.now(), key))

    def next_due(self) -> float | None:
        row = self.db.execute("SELECT MIN(at) FROM (SELECT MIN(MIN(due,expires)) AS at FROM wakes "
                              "WHERE status='scheduled' UNION ALL SELECT MIN(wake_at) FROM scopes "
                              "UNION ALL SELECT MIN(MIN(due,expires)) FROM actions WHERE status='queued' "
                              "UNION ALL SELECT MIN(next_check) FROM proactive_state "
                              "UNION ALL SELECT MIN(quiet_due) FROM dialogue_waits "
                              "UNION ALL SELECT MIN(total_due) FROM dialogue_waits "
                              "UNION ALL SELECT MIN(condition_expires) FROM dialogue_waits "
                              "UNION ALL SELECT MIN(CAST(json_extract(wake_config,'$.expires_at') AS REAL)) FROM scopes WHERE wake_config<>'{}' "
                              "UNION ALL SELECT MIN(pending_due) FROM judge_state WHERE inflight=0 "
                              "UNION ALL SELECT MIN(MIN(COALESCE(check_at,expires),expires)) FROM dialogue_attention)").fetchone()
        return float(row[0]) if row[0] is not None else None

    def last_send(self, key: str) -> float | None:
        row = self.db.execute("SELECT MAX(COALESCE(due,created)) FROM actions WHERE scope=? AND attempts>0", (key,)).fetchone()
        return float(row[0]) if row[0] is not None else None

    def queue_retry(self, action: str, after: float, reply_to: int | None) -> bool:
        now = self.now()
        with self.db:
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
                      reply_to: int | None, limit: int) -> list[sqlite3.Row]:
        group = uuid4().hex
        now = self.now()
        with self.db:
            count = self.db.execute("SELECT COUNT(*) FROM send_attempts WHERE scope=? AND at>?",
                                    (key, now - 3600)).fetchone()[0]
            if count + len(texts) > limit:
                raise PermissionError("hourly send budget cannot accommodate all parts")
            expires = self.db.execute("SELECT MAX(expires) FROM wakes WHERE turn_id=? AND status='running'",
                                      (turn,)).fetchone()[0]
            if expires is None:
                raise PermissionError("parts require an active authorized turn")
            for index, text in enumerate(texts):
                if self.db.execute("SELECT 1 FROM actions WHERE scope=? AND version=? AND status='unknown' "
                                    "AND trim(content)=trim(?) AND created>?", (key, version, text, now - 86400)).fetchone():
                    raise PermissionError("delivery is unknown; do not blindly repeat a part")
                self.db.execute("INSERT INTO actions(id,scope,version,content,status,created,due,expires,reply_to,"
                                "attempts,turn_id,group_id,ordinal) VALUES(?,?,?,?,'queued',?,?,?,?,0,?,?,?)",
                                (uuid4().hex, key, version, text, now, now, expires, reply_to, turn, group, index))
        return self.db.execute("SELECT * FROM actions WHERE group_id=? ORDER BY ordinal", (group,)).fetchall()

    def send_predecessor(self, action: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM actions WHERE group_id=(SELECT group_id FROM actions WHERE id=?) "
                               "AND ordinal<(SELECT ordinal FROM actions WHERE id=?) AND status<>'sent' "
                               "ORDER BY ordinal LIMIT 1", (action, action)).fetchone()

    def postpone_send(self, action: str, due: float) -> None:
        with self.db:
            self.db.execute("UPDATE actions SET due=? WHERE id=? AND status='queued'", (due, action))

    def claim_retry(self, action: str, limit: int) -> bool:
        with self.db:
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
