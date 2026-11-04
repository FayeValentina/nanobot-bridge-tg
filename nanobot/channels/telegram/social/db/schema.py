"""v22 baseline DDL for the Telegram social journal."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.store import SocialStore

SCHEMA_VERSION = 22

BASELINE_DDL: str = """CREATE TABLE actions (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
                    version TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, error TEXT
                , due REAL, expires REAL, reply_to INTEGER, attempts INTEGER NOT NULL DEFAULT 1, turn_id TEXT, group_id TEXT, ordinal INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'text', media_ref TEXT, message_ids TEXT NOT NULL DEFAULT '[]');
CREATE TABLE activities (id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, title TEXT NOT NULL, goal TEXT NOT NULL, motivation TEXT NOT NULL, checkpoint TEXT NOT NULL DEFAULT '', next_step TEXT NOT NULL, artifacts TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, updated REAL NOT NULL, last_turn TEXT NOT NULL DEFAULT '');
CREATE TABLE activity_steps (activity_id TEXT NOT NULL REFERENCES activities(id), revision INTEGER NOT NULL, turn_id TEXT NOT NULL, status TEXT NOT NULL, checkpoint TEXT NOT NULL, next_step TEXT NOT NULL, artifacts TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(activity_id,revision));
CREATE TABLE dialogue_jobs (
                    job TEXT PRIMARY KEY REFERENCES wakes(id) ON DELETE CASCADE,
                    purpose TEXT NOT NULL, quiet_seconds REAL NOT NULL,
                    quiet_due REAL NOT NULL, total_due REAL NOT NULL,
                    after_seq INTEGER NOT NULL DEFAULT 0, source_seq INTEGER NOT NULL DEFAULT 0
                );
CREATE TABLE dialogue_state (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), last_at REAL,
                    observed_at REAL, observed_seq INTEGER NOT NULL DEFAULT 0, last_sent REAL
                );
CREATE TABLE "dialogue_waits" (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), purpose TEXT NOT NULL,
                    started REAL NOT NULL, quiet_seconds REAL NOT NULL, quiet_due REAL,
                    total_due REAL, after_seq INTEGER NOT NULL, source_seq INTEGER NOT NULL,
                    on_timeout INTEGER NOT NULL, sleep_seconds REAL, condition_expires REAL,
                    sleep_until REAL
                );
CREATE TABLE expression_evidence (
                expression_id TEXT NOT NULL REFERENCES expressions(id), message_id INTEGER NOT NULL,
                seq INTEGER NOT NULL, sender_id TEXT NOT NULL, at REAL NOT NULL, excerpt TEXT NOT NULL,
                revision INTEGER NOT NULL, source TEXT NOT NULL, context TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(expression_id,message_id));
CREATE TABLE expression_guards (
            id INTEGER PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
            entry_id TEXT NOT NULL REFERENCES expressions(id), revision INTEGER NOT NULL,
            situation TEXT NOT NULL, expression TEXT NOT NULL, intent_category TEXT NOT NULL,
            scene_tags TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE expression_selections (
                scope TEXT NOT NULL, turn_id TEXT NOT NULL, kind TEXT NOT NULL, entry_id TEXT NOT NULL,
                revision INTEGER NOT NULL, delivery_id TEXT,
                PRIMARY KEY(scope,turn_id,kind,entry_id),
                FOREIGN KEY(scope,turn_id) REFERENCES expression_turns(scope,turn_id) ON DELETE CASCADE);
CREATE TABLE expression_turns (
                scope TEXT NOT NULL REFERENCES scopes(key), turn_id TEXT NOT NULL,
                target_message_id INTEGER, calls INTEGER NOT NULL, candidate_count INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, latency_ms INTEGER NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope,turn_id));
CREATE TABLE expressions (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), situation TEXT NOT NULL,
                expression TEXT NOT NULL, tone TEXT NOT NULL DEFAULT '', intent_category TEXT NOT NULL DEFAULT '',
                scene_tags TEXT NOT NULL DEFAULT '[]', suitable TEXT NOT NULL DEFAULT '',
                unsuitable TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'candidate',
                revision INTEGER NOT NULL DEFAULT 1, occurrences INTEGER NOT NULL DEFAULT 0,
                last_selected REAL, last_confirmed REAL, created REAL NOT NULL, updated REAL NOT NULL);
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
CREATE TABLE jargon (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL DEFAULT 'slang',
                content TEXT NOT NULL, members TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
                basis TEXT NOT NULL, revision INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                expires REAL, term TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '[]',
                local_meaning TEXT NOT NULL DEFAULT '', public_meaning TEXT NOT NULL DEFAULT '',
                usage TEXT NOT NULL DEFAULT '', examples TEXT NOT NULL DEFAULT '[]', unsuitable TEXT NOT NULL DEFAULT '',
                source_links TEXT NOT NULL DEFAULT '[]', inference_basis TEXT NOT NULL DEFAULT '[]',
                meaning_status TEXT NOT NULL DEFAULT 'uncertain', intent_category TEXT NOT NULL DEFAULT '',
                scene_tags TEXT NOT NULL DEFAULT '[]', occurrences INTEGER NOT NULL DEFAULT 0,
                last_inferred REAL, last_selected REAL, last_confirmed REAL, authority TEXT NOT NULL DEFAULT 'inference', authority_seq INTEGER NOT NULL DEFAULT 0, review_requested INTEGER NOT NULL DEFAULT 0, research_revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE jargon_evidence (
                memory_id TEXT NOT NULL REFERENCES jargon(id), message_id INTEGER NOT NULL,
                seq INTEGER NOT NULL, sender_id TEXT NOT NULL, at REAL NOT NULL, excerpt TEXT NOT NULL,
                revision INTEGER NOT NULL, source TEXT NOT NULL, context TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(memory_id,message_id));
CREATE TABLE jargon_resets (
                scope TEXT PRIMARY KEY REFERENCES scopes(key), through_seq INTEGER NOT NULL);
CREATE TABLE jargon_suppressions (
                scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(scope,kind,value));
CREATE TABLE "judge_state" (scope TEXT PRIMARY KEY, assessed_through INTEGER NOT NULL DEFAULT 0);
CREATE TABLE learning_batches (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL,
                scope_version TEXT NOT NULL, config_revision INTEGER NOT NULL,
                after_event INTEGER NOT NULL, through_event INTEGER NOT NULL, sources TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL,
                reason TEXT NOT NULL DEFAULT '', source_count INTEGER NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0, model TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0, purpose TEXT NOT NULL DEFAULT 'discovery', targets TEXT NOT NULL DEFAULT '[]', search_calls INTEGER NOT NULL DEFAULT 0, fetch_calls INTEGER NOT NULL DEFAULT 0, research_chars INTEGER NOT NULL DEFAULT 0, documents TEXT NOT NULL DEFAULT '[]', model_calls INTEGER NOT NULL DEFAULT 0, error_detail TEXT);
CREATE TABLE learning_changes (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL,
                entry_id TEXT NOT NULL, revision INTEGER NOT NULL, action TEXT NOT NULL,
                origin TEXT NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE learning_counts (
                scope TEXT NOT NULL REFERENCES scopes(key), reason TEXT NOT NULL, count INTEGER NOT NULL,
                first_message_id INTEGER, last_message_id INTEGER, updated REAL NOT NULL,
                PRIMARY KEY(scope,reason));
CREATE TABLE learning_cursors (
                scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL,
                event_id INTEGER NOT NULL DEFAULT 0, message_id INTEGER, revision INTEGER,
                updated REAL, PRIMARY KEY(scope,kind));
CREATE TABLE learning_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL REFERENCES scopes(key),
                message_id INTEGER NOT NULL, seq INTEGER NOT NULL, revision INTEGER NOT NULL,
                sender_id TEXT NOT NULL, at REAL NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL,
                context TEXT NOT NULL, UNIQUE(scope,message_id,revision));
CREATE TABLE learning_message_revisions (
                scope TEXT NOT NULL REFERENCES scopes(key), message_id INTEGER NOT NULL,
                revision INTEGER NOT NULL, PRIMARY KEY(scope,message_id));
CREATE TABLE memories (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key),
                    kind TEXT NOT NULL, content TEXT NOT NULL, members TEXT NOT NULL,
                    status TEXT NOT NULL, basis TEXT NOT NULL, revision INTEGER NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, expires REAL
                );
CREATE TABLE memory_evidence (
                    memory_id TEXT NOT NULL REFERENCES memories(id), message_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL, sender_id TEXT NOT NULL, at REAL NOT NULL,
                    excerpt TEXT NOT NULL, PRIMARY KEY(memory_id,message_id)
                );
CREATE TABLE memory_followups (
                    memory_id TEXT PRIMARY KEY REFERENCES memories(id), origin_turn TEXT NOT NULL,
                    own_intent INTEGER NOT NULL DEFAULT 0, pending_question TEXT NOT NULL DEFAULT '',
                    pending_thought TEXT NOT NULL DEFAULT '', motivation TEXT NOT NULL DEFAULT '',
                    mentioned_at REAL NOT NULL, followup_at REAL, followup_state TEXT NOT NULL DEFAULT 'active',
                    state_reason TEXT NOT NULL DEFAULT '', responded_at REAL
                );
CREATE TABLE memory_resets (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), through_seq INTEGER NOT NULL
                );
CREATE TABLE memory_suppressions (scope TEXT NOT NULL REFERENCES scopes(key), kind TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(scope,kind,value));
CREATE TABLE messages (
                    scope TEXT NOT NULL REFERENCES scopes(key), seq INTEGER NOT NULL,
                    update_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    sender_id TEXT NOT NULL, display_name TEXT NOT NULL, at REAL NOT NULL,
                    text TEXT NOT NULL, reply_to INTEGER, media TEXT NOT NULL, is_self INTEGER NOT NULL, media_refs TEXT NOT NULL DEFAULT '[]', reply_context TEXT, album_id TEXT, judge_facts TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1, learning_source TEXT NOT NULL DEFAULT 'unknown', learning_text TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(scope, seq), UNIQUE(scope, message_id)
                );
CREATE TABLE proactive_state (
                    scope TEXT PRIMARY KEY REFERENCES scopes(key), version TEXT NOT NULL,
                    next_check REAL, last_check REAL, decision TEXT NOT NULL DEFAULT 'disabled',
                    last_seq INTEGER NOT NULL DEFAULT 0, memory_versions TEXT NOT NULL DEFAULT '{}'
                , opportunity_seq INTEGER NOT NULL DEFAULT 0, probability REAL, sample REAL, selected INTEGER, metrics TEXT NOT NULL DEFAULT '{}', tracked_since REAL, stopped INTEGER NOT NULL DEFAULT 0, next_activity_check REAL);
CREATE TABLE requests (scope TEXT NOT NULL REFERENCES scopes(key), at REAL NOT NULL);
CREATE TABLE runtime_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE scopes (
                    key TEXT PRIMARY KEY, channel TEXT NOT NULL, chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL, version TEXT NOT NULL,
                    next_seq INTEGER NOT NULL DEFAULT 1, read_through INTEGER NOT NULL DEFAULT 0,
                    seen_through INTEGER NOT NULL DEFAULT 0
                , mode TEXT NOT NULL DEFAULT 'observing', wake_at REAL, wait_after_seq INTEGER NOT NULL DEFAULT 0, wake_on_timeout INTEGER NOT NULL DEFAULT 0, wake_config TEXT NOT NULL DEFAULT '{}', event_seq INTEGER NOT NULL DEFAULT 0, timeout_through INTEGER NOT NULL DEFAULT -1, wait_infinite INTEGER NOT NULL DEFAULT 0, last_member_at REAL);
CREATE TABLE send_attempts (scope TEXT NOT NULL REFERENCES scopes(key), at REAL NOT NULL);
CREATE TABLE stickers (
                    scope TEXT NOT NULL REFERENCES scopes(key), id TEXT NOT NULL,
                    media_ref TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                    uses INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
                    PRIMARY KEY(scope,id)
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
                , due REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT 'directed', requested REAL, proactive_source TEXT, source_seq INTEGER, condition_config TEXT NOT NULL DEFAULT '{}', admission TEXT);
CREATE INDEX actions_scope ON actions(scope, created);
CREATE UNIQUE INDEX activity_running_scope ON activities(scope) WHERE status='active';
CREATE INDEX expression_guards_scope ON expression_guards(scope);
CREATE INDEX expressions_scene ON expressions(scope,status,intent_category,updated);
CREATE INDEX jargon_scene ON jargon(scope,status,intent_category,updated);
CREATE INDEX jargon_term ON jargon(scope,status,term);
CREATE UNIQUE INDEX learning_batch_active ON learning_batches(scope,kind)
                WHERE status IN ('ready','running','failed');
CREATE INDEX learning_batches_scope ON learning_batches(scope,kind,status,created);
CREATE INDEX learning_changes_entry ON learning_changes(scope,kind,entry_id,revision);
CREATE INDEX learning_events_scope ON learning_events(scope,id);
CREATE INDEX memories_scope ON memories(scope,status,updated);
CREATE INDEX requests_scope ON requests(scope,at);
CREATE INDEX send_attempts_scope ON send_attempts(scope,at);
CREATE INDEX views_scope ON views(scope, created);
CREATE INDEX wakes_due ON wakes(status,due);
CREATE INDEX wakes_scope ON wakes(scope, created);
PRAGMA user_version=22;
"""


def initialize_social_schema(store: SocialStore) -> None:
    if store.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        store.db.close()
        raise ValueError("social database is damaged; restore from backup")
    version = store.db.execute("PRAGMA user_version").fetchone()[0]
    has_tables = store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None
    if version == SCHEMA_VERSION:
        return
    if version == 0 and not has_tables:
        store.db.executescript("BEGIN IMMEDIATE;\n" + BASELINE_DDL + "COMMIT;")
        return
    store.db.close()
    raise ValueError(
        f"unsupported social schema version: {version}; open it with release 961ea247 or earlier "
        "to upgrade, or restore a v22 backup"
    )
