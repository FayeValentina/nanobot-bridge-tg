"""Ordered upgrades for the Telegram social journal; no runtime scheduling."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.store import SocialStore

SCHEMA_VERSION = 21


def initialize_social_schema(store: SocialStore) -> None:
    version = store.db.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= SCHEMA_VERSION:
        store.db.close()
        raise ValueError(f"unsupported social schema version: {version}")
    if store.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        store.db.close()
        raise ValueError("social database is damaged; restore from backup")
    if version == 0:
        store.db.executescript("""
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
                created REAL NOT NULL, message_ids TEXT NOT NULL DEFAULT '[]', error TEXT
            );
            CREATE INDEX views_scope ON views(scope, created);
            CREATE INDEX wakes_scope ON wakes(scope, created);
            CREATE INDEX actions_scope ON actions(scope, created);
            PRAGMA user_version=1;
            COMMIT;
        """)
    if version < 2:
        store.db.executescript("""
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
        store.db.executescript("""
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
        store.db.executescript("""
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
                scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(scope,kind,value)
            );
            CREATE TABLE memory_resets (
                scope TEXT PRIMARY KEY REFERENCES scopes(key), through_seq INTEGER NOT NULL
            );
            PRAGMA user_version=4;
            COMMIT;
        """)
    if version < 5:
        store.db.executescript("""
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
    if 0 < version < 6:
        store.db.executescript("""
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
        store.db.executescript(f"""
            BEGIN IMMEDIATE;
            {"ALTER TABLE message_deliveries ADD COLUMN version TEXT;" if version else ""}
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
        store.db.executescript("""
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
        store.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS judge_state (
                scope TEXT PRIMARY KEY REFERENCES scopes(key), assessed_through INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS judge_batches (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
                version TEXT NOT NULL, judge_revision INTEGER NOT NULL,
                model TEXT NOT NULL, prompt_version TEXT NOT NULL,
                after_seq INTEGER NOT NULL, through_seq INTEGER NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, due REAL NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                decision TEXT, error_code TEXT, fallback_at REAL
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
        with store.db:
            for table, columns in {
                "messages": {"judge_facts": "TEXT NOT NULL DEFAULT '{}'"},
                "judge_batches": {"input_meta": "TEXT", "handoff_job": "TEXT", "condition_config": "TEXT NOT NULL DEFAULT '{}'"},
                "judge_state": {"pending_first": "REAL", "pending_last": "REAL", "pending_seq": "INTEGER NOT NULL DEFAULT 0",
                                "pending_due": "REAL", "pending_max": "REAL", "inflight": "INTEGER NOT NULL DEFAULT 0",
                                "batch_id": "TEXT", "opportunity": "TEXT"},
            }.items():
                existing = {str(row[1]) for row in store.db.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        store.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            # Existing messages have no V3 admission facts. Bootstrap assessment,
            # without changing main seen/read or replaying pre-upgrade history.
            store.db.execute("INSERT INTO judge_state(scope,assessed_through) SELECT key,next_seq-1 FROM scopes WHERE 1 "
                            "ON CONFLICT(scope) DO UPDATE SET assessed_through=MAX(assessed_through,excluded.assessed_through)")
            store.db.execute("PRAGMA user_version=10")
    if version < 11:
        _upgrade_without_hashes(store)
    if version < 12:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            store.db.execute("ALTER TABLE scopes ADD COLUMN last_member_at REAL")
            store.db.execute("UPDATE scopes SET last_member_at=(SELECT last_at FROM dialogue_state WHERE scope=scopes.key)")
            for column, definition in {
                "opportunity_seq": "INTEGER NOT NULL DEFAULT 0",
                "probability": "REAL", "sample": "REAL", "selected": "INTEGER",
                "metrics": "TEXT NOT NULL DEFAULT '{}'",
                "tracked_since": "REAL", "stopped": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                store.db.execute(f"ALTER TABLE proactive_state ADD COLUMN {column} {definition}")
            store.db.execute("UPDATE proactive_state SET tracked_since=?,next_check=NULL", (store.now(),))
            store.db.execute("PRAGMA user_version=12")
    if version < 13:
        with store.db:
            store.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE memory_followups (
                memory_id TEXT PRIMARY KEY REFERENCES memories(id), origin_turn TEXT NOT NULL,
                own_intent INTEGER NOT NULL DEFAULT 0, pending_question TEXT NOT NULL DEFAULT '',
                pending_thought TEXT NOT NULL DEFAULT '', motivation TEXT NOT NULL DEFAULT '',
                mentioned_at REAL NOT NULL, followup_at REAL, followup_state TEXT NOT NULL DEFAULT 'active',
                state_reason TEXT NOT NULL DEFAULT '', responded_at REAL
            );
            CREATE TABLE followup_attempts (
                id TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memories(id),
                scope TEXT NOT NULL REFERENCES scopes(key), turn_id TEXT NOT NULL, wake_id TEXT,
                revision INTEGER NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(memory_id,turn_id)
            );
            CREATE TABLE followup_sends (
                delivery_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES followup_attempts(id),
                revision INTEGER NOT NULL, status TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0
            );
            PRAGMA user_version=13;
            COMMIT;
            """)
    if version < 14:
        _upgrade_learning_schema(store)
    if version < 15:
        _upgrade_expression_schema(store)
    if version < 16:
        _upgrade_jargon_schema(store)
    if version < 17:
        _upgrade_activity_schema(store)
    if version < 18:
        with store.db:
            if "error_detail" not in {row[1] for row in store.db.execute("PRAGMA table_info(learning_batches)")}:
                store.db.execute("ALTER TABLE learning_batches ADD COLUMN error_detail TEXT")
            store.db.execute("PRAGMA user_version=18")
    if version < 19:
        with store.db:
            if "next_activity_check" not in {row[1] for row in store.db.execute("PRAGMA table_info(proactive_state)")}:
                store.db.execute("ALTER TABLE proactive_state ADD COLUMN next_activity_check REAL")
            store.db.execute("UPDATE proactive_state SET next_activity_check=next_check WHERE next_check IS NOT NULL")
            store.db.execute("PRAGMA user_version=19")
    if version < 20:
        with store.db:
            store.db.execute("DROP TABLE IF EXISTS dialogue_attention")
            store.db.execute("DROP TABLE IF EXISTS wake_samples")
            store.db.execute("PRAGMA user_version=20")

    if 0 < version < 21:
        store.db.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE actions ADD COLUMN message_ids TEXT NOT NULL DEFAULT '[]';
            UPDATE actions SET message_ids=json_array(message_id) WHERE message_id IS NOT NULL;
            INSERT INTO actions(id,scope,version,content,status,created,message_ids,error,turn_id,kind,attempts)
                SELECT id,scope,COALESCE(version,(SELECT version FROM scopes WHERE key=message_deliveries.scope)),
                    '',CASE status WHEN 'queued' THEN 'sending' ELSE status END,created,message_ids,detail,
                    turn_id,'message',1 FROM message_deliveries;
            DROP TABLE message_deliveries;
            ALTER TABLE actions DROP COLUMN message_id;
            PRAGMA user_version=21;
            COMMIT;
        """)
    if version == 0:
        store.db.execute("PRAGMA user_version=21")


def _upgrade_without_hashes(store: SocialStore) -> None:
    """Retain journals and watermarks while retiring the old derived keys."""
    store.db.execute("PRAGMA foreign_keys=OFF")
    try:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in store.db.execute("PRAGMA table_info(messages)")}
            if "revision" not in columns:
                store.db.execute("ALTER TABLE messages ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            store.db.execute("CREATE TABLE IF NOT EXISTS runtime_settings (name TEXT PRIMARY KEY, "
                            "value TEXT NOT NULL, revision INTEGER NOT NULL)")
            columns = {row[1] for row in store.db.execute("PRAGMA table_info(judge_batches)")}
            revision = "judge_revision" if "judge_revision" in columns else "0"
            store.db.execute("CREATE TABLE judge_batches_v11 (id TEXT PRIMARY KEY, "
                "scope TEXT NOT NULL REFERENCES scopes(key), version TEXT NOT NULL, judge_revision INTEGER NOT NULL, "
                "model TEXT NOT NULL, prompt_version TEXT NOT NULL, after_seq INTEGER NOT NULL, through_seq INTEGER NOT NULL, "
                "created REAL NOT NULL, expires REAL NOT NULL, due REAL NOT NULL, status TEXT NOT NULL, "
                "attempts INTEGER NOT NULL DEFAULT 0, decision TEXT, error_code TEXT, fallback_at REAL, "
                "input_meta TEXT, handoff_job TEXT, condition_config TEXT NOT NULL DEFAULT '{}')")
            store.db.execute(f"INSERT INTO judge_batches_v11 SELECT id,scope,version,{revision},model,prompt_version,"
                "after_seq,through_seq,created,expires,due,status,attempts,decision,error_code,fallback_at,"
                "input_meta,handoff_job,condition_config FROM judge_batches")
            store.db.execute("DROP TABLE judge_batches")
            store.db.execute("ALTER TABLE judge_batches_v11 RENAME TO judge_batches")
            store.db.execute("CREATE UNIQUE INDEX judge_running_scope ON judge_batches(scope) WHERE status='running'")
            store.db.execute("UPDATE judge_batches SET status='cancelled',error_code='protocol_changed' "
                            "WHERE status IN ('pending','running')")
            store.db.execute("UPDATE judge_state SET batch_id=NULL,inflight=0,pending_due=? "
                            "WHERE batch_id IS NOT NULL", (store.now(),))
            # Only legacy derived versions need replacing. Native record IDs stay intact.
            tables = ("views", "wakes", "actions", "proactive_state", "message_deliveries", "wake_samples", "judge_batches")
            existing = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tables = tuple(table for table in tables if table in existing)
            for scope in store.db.execute("SELECT key,version FROM scopes").fetchall():
                key, current = scope
                mapping = {current: "1"} if re.fullmatch(r"[0-9a-f]{64}", current) else {}
                for table in tables:
                    for row in store.db.execute(f"SELECT DISTINCT version FROM {table} WHERE scope=?", (key,)).fetchall():
                        if row[0] and re.fullmatch(r"[0-9a-f]{64}", row[0]):
                            mapping.setdefault(row[0], f"legacy-{len(mapping)+1}")
                    for old, new in mapping.items():
                        store.db.execute(f"UPDATE {table} SET version=? WHERE scope=? AND version=?", (new, key, old))
                if current in mapping:
                    store.db.execute("UPDATE scopes SET version=? WHERE key=?", (mapping[current], key))
                for row in store.db.execute("SELECT id,input_meta FROM judge_batches WHERE scope=? AND input_meta IS NOT NULL", (key,)).fetchall():
                    data = json.loads(row["input_meta"])
                    data["batch_id"] = row["id"]
                    data["version"] = mapping.get(data["version"], data["version"])
                    for item in data["excerpts"]:
                        item.pop("content_hash", None)
                        item["revision"] = 1
                    store.db.execute("UPDATE judge_batches SET input_meta=? WHERE id=?", (json.dumps(data), row["id"]))
            columns = {row[1] for row in store.db.execute("PRAGMA table_info(memory_suppressions)")}
            if "digest" in columns:
                # Old opaque keys cannot recover corrected text. Keep a source
                # cutoff so retiring them cannot relearn facts from old evidence.
                store.db.execute("INSERT INTO memory_resets SELECT s.key,s.next_seq-1 FROM scopes s WHERE s.key IN "
                    "(SELECT scope FROM memory_suppressions) ON CONFLICT(scope) DO UPDATE SET "
                    "through_seq=MAX(through_seq,excluded.through_seq)")
                store.db.execute("DROP TABLE memory_suppressions")
                store.db.execute("CREATE TABLE memory_suppressions (scope TEXT NOT NULL REFERENCES scopes(key), "
                                "kind TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(scope,kind,value))")
                store.db.executemany("INSERT OR IGNORE INTO memory_suppressions VALUES(?,'content',?)", [
                    (row["scope"], " ".join(str(row["content"]).casefold().split()))
                    for row in store.db.execute("SELECT scope,content FROM memories WHERE status IN ('deleted','expired')")
                ])
                store.db.execute("INSERT OR IGNORE INTO memory_suppressions SELECT m.scope,'message',CAST(e.message_id AS TEXT) "
                    "FROM memory_evidence e JOIN memories m ON m.id=e.memory_id WHERE m.status IN ('deleted','expired')")
            store.db.execute("PRAGMA user_version=11")
    finally:
        store.db.execute("PRAGMA foreign_keys=ON")



def _upgrade_jargon_schema(store: SocialStore) -> None:
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for field in ("authority TEXT NOT NULL DEFAULT 'inference'", "authority_seq INTEGER NOT NULL DEFAULT 0",
                      "review_requested INTEGER NOT NULL DEFAULT 0", "research_revision INTEGER NOT NULL DEFAULT 0"):
            if field.split()[0] not in {r[1] for r in store.db.execute("PRAGMA table_info(jargon)")}:
                store.db.execute(f"ALTER TABLE jargon ADD COLUMN {field}")
        for field in ("purpose TEXT NOT NULL DEFAULT 'discovery'", "targets TEXT NOT NULL DEFAULT '[]'",
                      "search_calls INTEGER NOT NULL DEFAULT 0", "fetch_calls INTEGER NOT NULL DEFAULT 0",
                      "research_chars INTEGER NOT NULL DEFAULT 0", "documents TEXT NOT NULL DEFAULT '[]'",
                      "model_calls INTEGER NOT NULL DEFAULT 0"):
            if field.split()[0] not in {r[1] for r in store.db.execute("PRAGMA table_info(learning_batches)")}:
                store.db.execute(f"ALTER TABLE learning_batches ADD COLUMN {field}")
        store.db.execute("UPDATE jargon SET authority='human',authority_seq=COALESCE((SELECT MAX(seq) FROM jargon_evidence WHERE memory_id=jargon.id),0) WHERE status='confirmed'")
        store.db.execute("UPDATE jargon SET status=CASE WHEN status='confirmed' THEN 'understand_only' "
                         "WHEN status IN ('deleted','expired') THEN 'disabled' ELSE status END")
        store.db.execute("PRAGMA user_version=16")



def _upgrade_expression_schema(store: SocialStore) -> None:
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.execute("""CREATE TABLE IF NOT EXISTS expression_guards (
            id INTEGER PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
            entry_id TEXT NOT NULL REFERENCES expressions(id), revision INTEGER NOT NULL,
            situation TEXT NOT NULL, expression TEXT NOT NULL, intent_category TEXT NOT NULL,
            scene_tags TEXT NOT NULL, created REAL NOT NULL)""")
        store.db.execute("CREATE INDEX IF NOT EXISTS expression_guards_scope ON expression_guards(scope)")
        store.db.execute("PRAGMA user_version=15")



def _upgrade_learning_schema(store: SocialStore) -> None:
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



def _upgrade_activity_schema(store: SocialStore) -> None:
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.execute("CREATE TABLE IF NOT EXISTS activities (id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), "
                         "revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, title TEXT NOT NULL, "
                         "goal TEXT NOT NULL, motivation TEXT NOT NULL, checkpoint TEXT NOT NULL DEFAULT '', "
                         "next_step TEXT NOT NULL, artifacts TEXT NOT NULL DEFAULT '[]', "
                         "created REAL NOT NULL, updated REAL NOT NULL, last_turn TEXT NOT NULL DEFAULT '')")
        store.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS activity_running_scope ON activities(scope) WHERE status='active'")
        store.db.execute("CREATE TABLE IF NOT EXISTS activity_steps (activity_id TEXT NOT NULL REFERENCES activities(id), "
                         "revision INTEGER NOT NULL, turn_id TEXT NOT NULL, status TEXT NOT NULL, "
                         "checkpoint TEXT NOT NULL, next_step TEXT NOT NULL, artifacts TEXT NOT NULL, at REAL NOT NULL, "
                         "PRIMARY KEY(activity_id,revision))")
        store.db.execute("PRAGMA user_version=17")

