"""Single-owner SQLite observation, exposure, wake and delivery journal."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from nanobot.channels.telegram.social.models import (
    ConversationScope,
    MediaReference,
    ObservedMessage,
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
        if version not in (0, 1, 2, 3):
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
        path.chmod(0o600)

    def close(self) -> None:
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
                "UPDATE scopes SET mode='observing',wake_at=NULL,wake_config='{}' WHERE key=?",
                (scope.session_key,),
            )
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
        )

    def observe(self, msg: ObservedMessage, retention: int, *, reason: str | None = None,
                delay: float = 0) -> tuple[int, bool]:
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
                    "UPDATE messages SET text=?,media=?,media_refs=?,reply_context=?,album_id=? "
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
            self.db.execute("DELETE FROM messages WHERE scope=? AND seq<=?", (key, seq - retention))
            # Keep update dedup bounded too. Old platform replays below the retained
            # window cannot produce wakes because message timestamps expire them.
            self.db.execute(
                "DELETE FROM updates WHERE channel=? AND id < ?",
                (msg.scope.channel, msg.update_id - max(10000, retention * 100)),
            )
            if reason is not None:
                self._schedule(key, seq, reason, delay)
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
        }
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

    def note_seen(self, key: str, turn_id: str, model_text: str) -> None:
        state = self.state(key)
        with self.db:
            for row in self.db.execute(
                "SELECT * FROM views WHERE scope=? AND version=? AND seen=0",
                (key, state["version"]),
            ).fetchall():
                if row["turn_id"] not in (None, turn_id) or row["body"] not in model_text:
                    continue
                job = json.loads(row["body"]).get("wake_job")
                if job:
                    self.db.execute(
                        "UPDATE wakes SET status='running',turn_id=? WHERE id=? AND scope=? "
                        "AND status='queued' AND version=? AND expires>?",
                        (turn_id, job, key, state["version"], self.now()),
                    )
                self.db.execute("UPDATE views SET seen=1, turn_id=? WHERE id=?", (turn_id, row["id"]))
            seen = self._shown_sequences(key, turn_id)
            # A read tool can expose a busy-time trigger within the current run.
            # Only real provider exposure consumes it; returning the page alone cannot.
            for seq in seen:
                self.db.execute("UPDATE wakes SET status='running',turn_id=? WHERE scope=? AND version=? "
                                "AND status='scheduled' AND trigger_seq=? AND expires>?",
                                (turn_id, key, state["version"], seq, self.now()))
            frontier = int(state["seen_through"])
            while frontier + 1 in seen:
                frontier += 1
            self.db.execute("UPDATE scopes SET seen_through=? WHERE key=?", (frontier, key))

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
            changed = self.db.execute(
                "UPDATE wakes SET status='running', turn_id=? WHERE id=? AND scope=? AND version=? "
                "AND status='queued' AND expires>?", (turn, job, key, version, self.now()),
            ).rowcount
            if changed != 1:
                raise PermissionError("wake is stale, expired or already claimed")
            self.db.execute("UPDATE scopes SET mode='active',wake_at=NULL WHERE key=?", (key,))

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
                self.db.execute("UPDATE wakes SET due=? WHERE id=?", (self.now(), row["id"]))
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
            self.db.execute("UPDATE scopes SET mode='paused',wake_at=NULL WHERE key=?", (key,))
            self.db.execute("UPDATE wakes SET status='cancelled' WHERE scope=? "
                            "AND status IN ('scheduled','queued','running')", (key,))
            self.db.execute("UPDATE actions SET status='failed',error='paused' "
                            "WHERE scope=? AND status='queued'", (key,))

    def defer(self, key: str, turn: str, *, sleeping: bool, seconds: float, on_timeout: bool) -> None:
        shown = self._shown_sequences(key, turn)
        with self.db:
            self.db.execute(
                "UPDATE scopes SET mode=?,wake_at=?,wake_on_timeout=?,wait_after_seq=? WHERE key=?",
                ("sleeping" if sleeping else "waiting", self.now() + seconds,
                 int(on_timeout), max(shown, default=0), key),
            )
            # A newly arrived direct address must survive a simultaneous wait.
            self.db.execute("UPDATE wakes SET status='cancelled' WHERE scope=? AND status='scheduled' "
                            "AND reason NOT IN ('directed','nickname') AND (trigger_seq<=? OR ?)",
                            (key, max(shown, default=0), int(sleeping)))

    def set_wake_config(self, key: str, body: str) -> None:
        with self.db:
            self.db.execute("UPDATE scopes SET wake_config=? WHERE key=?", (body, key))

    def schedule(self, key: str, seq: int, reason: str, delay: float) -> None:
        with self.db:
            self._schedule(key, seq, reason, delay)

    def _schedule(self, key: str, seq: int, reason: str, delay: float) -> None:
        """Caller owns the transaction, including observation/dedup when applicable."""
        now = self.now()
        rank = {"timeout": 0, "message": 1, "sender": 2, "keyword": 3, "nickname": 4, "directed": 5}
        state = self.state(key)
        if state["mode"] == "paused":
            return
        row = self.db.execute("SELECT * FROM wakes WHERE scope=? AND version=? "
                              "AND status='scheduled'", (key, state["version"])).fetchone()
        if row is not None:
            chosen = reason if rank[reason] >= rank[str(row["reason"])] else str(row["reason"])
            self.db.execute("UPDATE wakes SET trigger_seq=?,reason=?,due=MIN(due,?),expires=? WHERE id=?",
                            (max(seq, int(row["trigger_seq"])), chosen, now + delay,
                             now + 120, row["id"]))
        else:
            self.db.execute("INSERT INTO wakes(id,scope,version,trigger_seq,status,created,expires,due,reason) "
                            "VALUES(?,?,?,?,'scheduled',?,?,?,?)",
                            (uuid4().hex, key, state["version"], seq, now, now + 120, now + delay, reason))
        self.db.execute("DELETE FROM wakes WHERE scope=? AND created<? AND status NOT IN "
                        "('scheduled','queued','running')", (key, now - 86400))

    def consume_timers(self) -> None:
        now = self.now()
        for scope in self.scopes():
            state = self.state(scope.session_key)
            if state["wake_at"] is None or state["wake_at"] > now:
                continue
            with self.db:
                self.db.execute("UPDATE scopes SET mode='observing',wake_at=NULL WHERE key=?", (scope.session_key,))
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

    def release_pending(self, key: str) -> None:
        with self.db:
            self.db.execute("UPDATE wakes SET due=MIN(due,?) WHERE scope=? AND status='scheduled'", (self.now(), key))

    def next_due(self) -> float | None:
        row = self.db.execute("SELECT MIN(at) FROM (SELECT MIN(MIN(due,expires)) AS at FROM wakes "
                              "WHERE status='scheduled' UNION ALL SELECT MIN(wake_at) FROM scopes "
                              "UNION ALL SELECT MIN(MIN(due,expires)) FROM actions WHERE status='queued')").fetchone()
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
