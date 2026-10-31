"""V5 bounded material collection on the SocialStore connection.

Events are the durable best-effort queue. A ready batch freezes bounded source text;
there are no leases, backlog snapshots, content keys, or main-model exposure calls.
"""
from __future__ import annotations

import json
from collections import deque
from contextlib import nullcontext
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from loguru import logger

from nanobot.channels.telegram.social.models import ObservedMessage
from nanobot.config.schema import TelegramSocialLearningConfig

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.store import SocialStore

LearningKind = Literal["expression", "jargon"]


def upgrade_expression_schema(store: SocialStore) -> None:
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.execute("""CREATE TABLE IF NOT EXISTS expression_guards (
            id INTEGER PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
            entry_id TEXT NOT NULL REFERENCES expressions(id), revision INTEGER NOT NULL,
            situation TEXT NOT NULL, expression TEXT NOT NULL, intent_category TEXT NOT NULL,
            scene_tags TEXT NOT NULL, created REAL NOT NULL)""")
        store.db.execute("CREATE INDEX IF NOT EXISTS expression_guards_scope ON expression_guards(scope)")
        store.db.execute("PRAGMA user_version=15")


def upgrade_learning_schema(store: SocialStore) -> None:
    db = store.db
    # There is deliberately no legacy slang conversion or ID mapping.
    if db.execute("SELECT 1 FROM memories WHERE kind='slang' LIMIT 1").fetchone():
        raise ValueError("V5-A requires the verified empty legacy slang baseline")
    with db:
        db.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if "learning_source" not in columns:
            db.execute("ALTER TABLE messages ADD COLUMN learning_source TEXT NOT NULL DEFAULT 'unknown'")
            db.execute("ALTER TABLE messages ADD COLUMN learning_text TEXT NOT NULL DEFAULT ''")
        # IF NOT EXISTS also supports the repository's partial historical-schema fixtures.
        statements = [
            """CREATE TABLE IF NOT EXISTS learning_message_revisions (
                scope TEXT NOT NULL REFERENCES scopes(key), message_id INTEGER NOT NULL,
                revision INTEGER NOT NULL, PRIMARY KEY(scope,message_id))""",
            """CREATE TABLE IF NOT EXISTS learning_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL REFERENCES scopes(key),
                message_id INTEGER NOT NULL, seq INTEGER NOT NULL, revision INTEGER NOT NULL,
                sender_id TEXT NOT NULL, at REAL NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL,
                context TEXT NOT NULL, UNIQUE(scope,message_id,revision))""",
            "CREATE INDEX IF NOT EXISTS learning_events_scope ON learning_events(scope,id)",
            """CREATE TABLE IF NOT EXISTS learning_cursors (
                scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL,
                event_id INTEGER NOT NULL DEFAULT 0, message_id INTEGER, revision INTEGER,
                updated REAL, PRIMARY KEY(scope,kind))""",
            """CREATE TABLE IF NOT EXISTS learning_batches (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL,
                scope_version TEXT NOT NULL, config_revision INTEGER NOT NULL,
                after_event INTEGER NOT NULL, through_event INTEGER NOT NULL, sources TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL,
                reason TEXT NOT NULL DEFAULT '', source_count INTEGER NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0, model TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0)""",
            "CREATE INDEX IF NOT EXISTS learning_batches_scope ON learning_batches(scope,kind,status,created)",
            """CREATE UNIQUE INDEX IF NOT EXISTS learning_batch_active ON learning_batches(scope,kind)
                WHERE status IN ('ready','running','failed')""",
            """CREATE TABLE IF NOT EXISTS learning_counts (
                scope TEXT NOT NULL REFERENCES scopes(key), reason TEXT NOT NULL, count INTEGER NOT NULL,
                first_message_id INTEGER, last_message_id INTEGER, updated REAL NOT NULL,
                PRIMARY KEY(scope,reason))""",
            """CREATE TABLE IF NOT EXISTS expressions (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), situation TEXT NOT NULL,
                expression TEXT NOT NULL, tone TEXT NOT NULL DEFAULT '', intent_category TEXT NOT NULL DEFAULT '',
                scene_tags TEXT NOT NULL DEFAULT '[]', suitable TEXT NOT NULL DEFAULT '',
                unsuitable TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'candidate',
                revision INTEGER NOT NULL DEFAULT 1, occurrences INTEGER NOT NULL DEFAULT 0,
                last_selected REAL, last_confirmed REAL, created REAL NOT NULL, updated REAL NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS expressions_scene ON expressions(scope,status,intent_category,updated)",
            """CREATE TABLE IF NOT EXISTS jargon (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL DEFAULT 'slang',
                content TEXT NOT NULL, members TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
                basis TEXT NOT NULL, revision INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                expires REAL, term TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '[]',
                local_meaning TEXT NOT NULL DEFAULT '', public_meaning TEXT NOT NULL DEFAULT '',
                usage TEXT NOT NULL DEFAULT '', examples TEXT NOT NULL DEFAULT '[]', unsuitable TEXT NOT NULL DEFAULT '',
                source_links TEXT NOT NULL DEFAULT '[]', inference_basis TEXT NOT NULL DEFAULT '[]',
                meaning_status TEXT NOT NULL DEFAULT 'uncertain', intent_category TEXT NOT NULL DEFAULT '',
                scene_tags TEXT NOT NULL DEFAULT '[]', occurrences INTEGER NOT NULL DEFAULT 0,
                last_inferred REAL, last_selected REAL, last_confirmed REAL)""",
            "CREATE INDEX IF NOT EXISTS jargon_scene ON jargon(scope,status,intent_category,updated)",
            "CREATE INDEX IF NOT EXISTS jargon_term ON jargon(scope,status,term)",
            """CREATE TABLE IF NOT EXISTS jargon_evidence (
                memory_id TEXT NOT NULL REFERENCES jargon(id), message_id INTEGER NOT NULL,
                seq INTEGER NOT NULL, sender_id TEXT NOT NULL, at REAL NOT NULL, excerpt TEXT NOT NULL,
                revision INTEGER NOT NULL, source TEXT NOT NULL, context TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(memory_id,message_id))""",
            """CREATE TABLE IF NOT EXISTS expression_evidence (
                expression_id TEXT NOT NULL REFERENCES expressions(id), message_id INTEGER NOT NULL,
                seq INTEGER NOT NULL, sender_id TEXT NOT NULL, at REAL NOT NULL, excerpt TEXT NOT NULL,
                revision INTEGER NOT NULL, source TEXT NOT NULL, context TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(expression_id,message_id))""",
            """CREATE TABLE IF NOT EXISTS jargon_suppressions (
                scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(scope,kind,value))""",
            """CREATE TABLE IF NOT EXISTS jargon_resets (
                scope TEXT PRIMARY KEY REFERENCES scopes(key), through_seq INTEGER NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS learning_changes (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL,
                entry_id TEXT NOT NULL, revision INTEGER NOT NULL, action TEXT NOT NULL,
                origin TEXT NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS learning_changes_entry ON learning_changes(scope,kind,entry_id,revision)",
            """CREATE TABLE IF NOT EXISTS expression_turns (
                scope TEXT NOT NULL REFERENCES scopes(key), turn_id TEXT NOT NULL,
                target_message_id INTEGER, calls INTEGER NOT NULL, candidate_count INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, latency_ms INTEGER NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope,turn_id))""",
            """CREATE TABLE IF NOT EXISTS expression_selections (
                scope TEXT NOT NULL, turn_id TEXT NOT NULL, kind TEXT NOT NULL, entry_id TEXT NOT NULL,
                revision INTEGER NOT NULL, delivery_id TEXT,
                PRIMARY KEY(scope,turn_id,kind,entry_id),
                FOREIGN KEY(scope,turn_id) REFERENCES expression_turns(scope,turn_id) ON DELETE CASCADE)""",
        ]
        for statement in statements:
            db.execute(statement)
        db.execute("PRAGMA user_version=14")


class SocialLearning:
    def __init__(self, store: SocialStore) -> None:
        self.store = store
        self.db = store.db
        self.config = TelegramSocialLearningConfig()
        self.config_revision = 0
        self.recent: dict[str, deque[dict[str, object]]] = {}

    def configure(self, config: TelegramSocialLearningConfig) -> None:
        self.config = config
        self.config_revision = self.store.setting_revision("learning", config.model_dump_json())
        self.recent = {scope: deque(rows, maxlen=config.selection_history_turns)
                       for scope, rows in self.recent.items()}
        with nullcontext() if self.db.in_transaction else self.db:
            self.db.execute("UPDATE learning_batches SET status='cancelled',reason='config_changed',updated=? "
                            "WHERE status IN ('ready','running','failed') AND config_revision<>?",
                            (self.store.now(), self.config_revision))

    def observation_revision(self, scope: str, message_id: int, retained_revision: int | None) -> int:
        """Retain only ID/revision metadata so late edits survive rolling text eviction.

        Caller already checked the platform update ID and owns the observation
        transaction; this is neither a transcript nor a content comparison key.
        """
        old = self.db.execute("SELECT revision FROM learning_message_revisions WHERE scope=? AND message_id=?", (scope, message_id)).fetchone()
        revision = max(int(old[0]) if old else 0, retained_revision or 0) + 1
        self.db.execute("INSERT INTO learning_message_revisions VALUES(?,?,?) ON CONFLICT(scope,message_id) "
                        "DO UPDATE SET revision=excluded.revision", (scope, message_id, revision))
        return revision

    @staticmethod
    def source(message: ObservedMessage) -> tuple[str, str]:
        if message.is_self:
            return "self", ""
        if message.is_bot:
            return "bot", ""
        if message.is_forwarded:
            return "forwarded_or_via_bot", ""
        if message.learning_text is None:
            return "unknown_or_nontext", ""
        return ("human_text", message.learning_text[:4000]) if message.learning_text.strip() else ("nontext", "")

    def count(self, scope: str, reason: str, count: int, first: int, last: int) -> None:
        self.db.execute("INSERT INTO learning_counts VALUES(?,?,?,?,?,?) ON CONFLICT(scope,reason) "
                        "DO UPDATE SET count=count+excluded.count,last_message_id=excluded.last_message_id,updated=excluded.updated",
                        (scope, reason, count, first, last, self.store.now()))

    def observe(self, message: ObservedMessage, seq: int, revision: int) -> None:
        """Called inside the observation transaction, BEFORE rolling-history pruning."""
        key = message.scope.session_key
        source, text = self.source(message)
        self.db.execute("UPDATE messages SET learning_source=?,learning_text=? WHERE scope=? AND message_id=?",
                        (source, text, key, message.message_id))
        # Even when collection is disabled, edited/deleted sources invalidate old results.
        self.invalidate(key, message.message_id, revision)
        if not self.config.enabled:
            return
        if source != "human_text":
            self.count(key, source, 1, message.message_id, message.message_id)
            return
        if not message.is_edit and not 0 <= self.store.now() - message.timestamp < 120:
            self.count(key, "stale_event", 1, message.message_id, message.message_id)
            return
        neighbors: list[dict[str, object]] = []
        remaining = self.config.context_chars
        for row in self.db.execute("SELECT message_id,revision,sender_id,learning_source,text FROM messages "
                                   "WHERE scope=? AND seq<? ORDER BY seq DESC LIMIT ?", (key, seq, self.config.context_messages)):
            excerpt = str(row["text"])[:remaining]
            remaining -= len(excerpt)
            neighbors.append({"message_id": row["message_id"], "revision": row["revision"], "sender_id": row["sender_id"],
                              "source": row["learning_source"], "text": excerpt, "learnable": False})
        reply = {"message_id": message.reply.message_id, "sender_id": message.reply.sender_id,
                 "text": message.reply.text[:remaining], "learnable": False} if message.reply else None
        context = json.dumps({"reply_to": message.reply_to, "reply": reply, "neighbors": neighbors}, ensure_ascii=False)
        self.db.execute("INSERT OR IGNORE INTO learning_events(scope,message_id,seq,revision,sender_id,at,source,text,context) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (key, message.message_id, seq, revision, message.sender_id,
                         message.timestamp, source, text[:self.config.excerpt_chars], context))
        overflow = self.db.execute("SELECT id,message_id FROM learning_events WHERE scope=? ORDER BY id DESC LIMIT -1 OFFSET ?",
                                   (key, self.config.window_messages)).fetchall()
        if overflow:
            for kind in ("expression", "jargon"):
                cursor = self.db.execute("SELECT event_id FROM learning_cursors WHERE scope=? AND kind=?", (key, kind)).fetchone()
                active = self.db.execute("SELECT sources FROM learning_batches WHERE scope=? AND kind=? AND status IN ('ready','running','failed')", (key, kind)).fetchone()
                frozen: set[int] = {int(item["id"]) for item in json.loads(active[0])} if active else set()
                missed = [row for row in reversed(overflow) if row["id"] > (cursor[0] if cursor else 0) and row["id"] not in frozen]
                if missed:
                    self.count(key, f"unbatched_overflow:{kind}", len(missed), int(missed[0]["message_id"]), int(missed[-1]["message_id"]))
            self.db.executemany("DELETE FROM learning_events WHERE id=?", [(row["id"],) for row in overflow])

    def invalidate(self, scope: str, message_id: int, revision: int | None) -> None:
        for kind, table, evidence, id_column in (
            ("expression", "expressions", "expression_evidence", "expression_id"),
            ("jargon", "jargon", "jargon_evidence", "memory_id"),
        ):
            for row in self.db.execute(f"SELECT DISTINCT t.id,t.revision FROM {table} t JOIN {evidence} e ON e.{id_column}=t.id "
                                       "WHERE t.scope=? AND e.message_id=? AND (? IS NULL OR e.revision<>?) "
                                       "AND t.status IN ('candidate','confirmed','usable','available')", (scope, message_id, revision, revision)).fetchall():
                self.db.execute(f"UPDATE {table} SET status='expired',revision=revision+1,updated=? WHERE id=?", (self.store.now(), row["id"]))
                self.db.execute("INSERT INTO learning_changes(scope,kind,entry_id,revision,action,origin,reason,at) "
                                "VALUES(?,?,?,?,'invalidate','source','source_changed_or_deleted',?)",
                                (scope, kind, row["id"], row["revision"] + 1, self.store.now()))
        self.db.execute("UPDATE learning_batches SET status='cancelled',reason='source_changed_or_deleted',updated=? "
                        "WHERE scope=? AND status IN ('ready','running','failed') AND EXISTS "
                        "(SELECT 1 FROM json_each(sources) WHERE json_extract(value,'$.message_id')=? "
                        "AND (? IS NULL OR json_extract(value,'$.revision')<>?))",
                        (self.store.now(), scope, message_id, revision, revision))
        self.db.execute("DELETE FROM learning_events WHERE scope=? AND message_id=? AND (? IS NULL OR revision<>?)",
                        (scope, message_id, revision, revision))

    def prepare(self, scope: str, kind: LearningKind) -> str | None:
        """Persist one bounded ready batch per chain. A never calls it 'learned'."""
        if not self.config.enabled or self.store.state(scope)["mode"] == "paused":
            return None
        with nullcontext() if self.db.in_transaction else self.db:
            active = self.db.execute("SELECT id FROM learning_batches WHERE scope=? AND kind=? AND status IN ('ready','running','failed')",
                                     (scope, kind)).fetchone()
            if active:
                return str(active[0])
            last = self.db.execute("SELECT MAX(created) FROM learning_batches WHERE scope=? AND kind=? AND status='succeeded'", (scope, kind)).fetchone()[0]
            if last is not None and self.store.now() < float(last) + self.config.interval_seconds:
                return None
            self.db.execute("INSERT OR IGNORE INTO learning_cursors(scope,kind) VALUES(?,?)", (scope, kind))
            after = self.db.execute("SELECT event_id FROM learning_cursors WHERE scope=? AND kind=?", (scope, kind)).fetchone()[0]
            rows = self.db.execute("SELECT * FROM learning_events WHERE scope=? AND id>? ORDER BY id LIMIT ?",
                                   (scope, after, min(80, self.config.window_messages))).fetchall()
            if len(rows) < self.config.min_messages:
                return None
            batch = uuid4().hex
            self.db.execute("INSERT INTO learning_batches(id,scope,kind,scope_version,config_revision,after_event,through_event,sources,"
                            "status,created,updated,due,source_count) VALUES(?,?,?,?,?,?,?,?,'ready',?,?,?,?)",
                            (batch, scope, kind, self.store.state(scope)["version"], self.config_revision, after, rows[-1]["id"],
                             json.dumps([dict(row) for row in rows], ensure_ascii=False), self.store.now(), self.store.now(), self.store.now(), len(rows)))
            return batch

    def collect_due(self, scopes: list[str]) -> None:
        with nullcontext() if self.db.in_transaction else self.db:
            for scope in scopes:
                version = self.store.state(scope)["version"]
                self.db.execute("UPDATE learning_batches SET status='cancelled',reason='scope_changed',updated=? "
                                "WHERE scope=? AND scope_version<>? AND status IN ('ready','running','failed')", (self.store.now(), scope, version))
                for kind in ("expression", "jargon"):
                    self.prepare(scope, kind)
            self.db.execute("UPDATE learning_batches SET status='cancelled',reason='scope_revoked',updated=? "
                            "WHERE status IN ('ready','running','failed') AND scope NOT IN (SELECT value FROM json_each(?))",
                            (self.store.now(), json.dumps(scopes)))
            # Retain only 25 terminal summaries per scope/chain; never prune active work.
            self.db.execute("DELETE FROM learning_batches WHERE id IN (SELECT id FROM (SELECT id, "
                            "ROW_NUMBER() OVER (PARTITION BY scope,kind ORDER BY created DESC) n FROM learning_batches "
                            "WHERE status IN ('succeeded','cancelled','skipped')) WHERE n>25)")

    def start(self, batch: str) -> None:
        """Single-owner attempt accounting before a future B/C network request."""
        with nullcontext() if self.db.in_transaction else self.db:
            row = self.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
            if row is None or row["status"] not in {"ready", "failed"}:
                raise ValueError("batch is no longer active")
            if row["attempts"] >= 2:
                raise ValueError("batch attempt budget exhausted")
            if (not self.config.enabled or self.store.state(row["scope"])["mode"] == "paused"
                    or row["config_revision"] != self.config_revision or row["scope_version"] != self.store.state(row["scope"])["version"]):
                raise ValueError("batch configuration changed or paused")
            self.db.execute("UPDATE learning_batches SET status='running',attempts=attempts+1,updated=? WHERE id=?", (self.store.now(), batch))

    def finish(self, batch: str, *, success: bool, reason: str = "", result_count: int = 0) -> None:
        """B/C commit entries/evidence in this SAME transaction, then finish; failure keeps cursor."""
        with nullcontext() if self.db.in_transaction else self.db:
            row = self.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
            if row is None or row["status"] not in {"ready", "running", "failed"}:
                raise ValueError("batch is no longer active")
            if row["status"] != "running":
                self.start(batch)
            if (not self.config.enabled or self.store.state(row["scope"])["mode"] == "paused"
                    or row["config_revision"] != self.config_revision or row["scope_version"] != self.store.state(row["scope"])["version"]):
                raise ValueError("batch configuration changed")
            sources = json.loads(row["sources"])
            # An evicted observation remains readable in the durable bounded batch;
            # explicit edits/deletes cancel it in invalidate(), rather than masquerading as eviction.
            for source in sources:
                current = self.db.execute("SELECT revision,learning_source,learning_text FROM messages WHERE scope=? AND message_id=?",
                                          (row["scope"], source["message_id"])).fetchone()
                if current and (current["revision"] != source["revision"] or current["learning_source"] != "human_text"
                                or not current["learning_text"].startswith(source["text"])):
                    raise ValueError("batch source changed")
            self.db.execute("UPDATE learning_batches SET status=?,reason=?,result_count=?,updated=? WHERE id=?",
                            ("succeeded" if success else "failed", reason, result_count, self.store.now(), batch))
            self.count(str(row["scope"]), f"{row['kind']}:{'succeeded' if success else 'failed'}", 1, sources[0]["message_id"], sources[-1]["message_id"])
            if success:
                last = sources[-1]
                self.db.execute("UPDATE learning_cursors SET event_id=?,message_id=?,revision=?,updated=? WHERE scope=? AND kind=?",
                                (row["through_event"], last["message_id"], last["revision"], self.store.now(), row["scope"], row["kind"]))

    def recover(self) -> None:
        with nullcontext() if self.db.in_transaction else self.db:
            self.db.execute("UPDATE learning_batches SET status='ready',reason='interrupted',updated=? WHERE status='running' AND attempts<2", (self.store.now(),))
            self.db.execute("UPDATE learning_batches SET status='failed',reason='attempt_budget_exhausted',updated=? WHERE status='running'", (self.store.now(),))

    def record_selection(self, scope: str, turn_id: str, *, target_message_id: int | None,
                         candidates: int, selected: list[tuple[str, str, int]], explanation: dict[str, object],
                         status: str = "selected", input_tokens: int = 0, output_tokens: int = 0, latency_ms: int = 0) -> None:
        with nullcontext() if self.db.in_transaction else self.db:
            for kind, entry, revision in selected:
                table = {"expression": "expressions", "jargon": "jargon", "sticker": "stickers"}.get(kind)
                if table is None:
                    raise ValueError("unknown selection kind")
                if kind == "sticker":
                    valid = self.db.execute("SELECT 1 FROM stickers WHERE scope=? AND id=?", (scope, entry)).fetchone()
                else:
                    valid = self.db.execute(f"SELECT 1 FROM {table} WHERE scope=? AND id=? AND revision=? "
                                            "AND status IN ('usable','available','confirmed','candidate')", (scope, entry, revision)).fetchone()
                if valid is None:
                    raise ValueError("selected entry is missing, inactive, changed or outside this scope")
            self.db.execute("INSERT INTO expression_turns VALUES(?,?,?,1,?,?,?,?,?,?)",
                            (scope, turn_id, target_message_id, candidates, input_tokens, output_tokens, latency_ms, status, self.store.now()))
            self.db.executemany("INSERT INTO expression_selections(scope,turn_id,kind,entry_id,revision) VALUES(?,?,?,?,?)",
                                [(scope, turn_id, kind, entry, revision) for kind, entry, revision in selected])
        detail = {"turn_id": turn_id, "target_message_id": target_message_id, "selected": selected, **explanation}
        self.recent.setdefault(scope, deque(maxlen=self.config.selection_history_turns)).append(detail)
        logger.bind(v5_selection={"scope": scope, **detail}).info("V5 turn selection {}", json.dumps({"scope": scope, **detail}, ensure_ascii=False))

    def confirm_use(self, scope: str, turn_id: str, kind: str, entry_id: str, revision: int, delivery_id: str) -> None:
        """Explicit usage attribution, separate from selection; only real receipts qualify.

        D will call this with actually used IDs. A does not attribute every selected
        item just because some text was sent, and never increases human occurrences.
        """
        with nullcontext() if self.db.in_transaction else self.db:
            receipt = self.db.execute("SELECT 1 FROM actions WHERE scope=? AND turn_id=? AND id=? AND status='sent' "
                                      "UNION ALL SELECT 1 FROM message_deliveries WHERE scope=? AND turn_id=? AND id=? "
                                      "AND (status='sent' OR json_array_length(message_ids)>0)",
                                      (scope, turn_id, delivery_id, scope, turn_id, delivery_id)).fetchone()
            if receipt is None:
                raise ValueError("usage requires a confirmed delivery in this turn and scope")
            changed = self.db.execute("UPDATE expression_selections SET delivery_id=? WHERE scope=? AND turn_id=? AND kind=? AND entry_id=? AND revision=?",
                                      (delivery_id, scope, turn_id, kind, entry_id, revision)).rowcount
            if not changed:
                raise ValueError("usage requires the selected entry revision")
            table = {"expression": "expressions", "jargon": "jargon"}.get(kind)
            if table is not None:
                self.db.execute(f"UPDATE {table} SET last_confirmed=? WHERE scope=? AND id=? AND revision=?", (self.store.now(), scope, entry_id, revision))

    def status(self, scope: str) -> dict[str, object]:
        return {"enabled": self.config.enabled, "stage": "expression_learning", "extractor_enabled": self.config.enabled,
                "config": self.config.model_dump(by_alias=True), "config_revision": self.config_revision,
                "buffered_materials": self.db.execute("SELECT COUNT(*) FROM learning_events WHERE scope=?", (scope,)).fetchone()[0],
                "pending_by_kind": {kind: self.db.execute("SELECT COUNT(*) FROM learning_events WHERE scope=? AND id>COALESCE("
                                                         "(SELECT event_id FROM learning_cursors WHERE scope=? AND kind=?),0)", (scope, scope, kind)).fetchone()[0]
                                    for kind in ("expression", "jargon")},
                "cursors": [dict(row) for row in self.db.execute("SELECT * FROM learning_cursors WHERE scope=?", (scope,))],
                "batches": [dict(row) for row in self.db.execute("SELECT id,kind,status,attempts,source_count,result_count,reason,model,input_tokens,output_tokens,latency_ms,created,updated FROM learning_batches WHERE scope=? ORDER BY created DESC LIMIT 20", (scope,))],
                "counts": [dict(row) for row in self.db.execute("SELECT * FROM learning_counts WHERE scope=?", (scope,))],
                "usable_expressions": self.db.execute("SELECT COUNT(*) FROM expressions WHERE scope=? AND status IN ('usable','available')", (scope,)).fetchone()[0],
                "jargon_entries": self.db.execute("SELECT COUNT(*) FROM jargon WHERE scope=? AND status IN ('candidate','confirmed')", (scope,)).fetchone()[0],
                "recent_selections": list(self.recent.get(scope, []))}
