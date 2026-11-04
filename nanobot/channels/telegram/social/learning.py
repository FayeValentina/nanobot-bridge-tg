"""V5 bounded material collection on the SocialStore connection.

Events are the durable best-effort queue. A ready batch freezes bounded source text;
there are no leases, backlog snapshots, content keys, or main-model exposure calls.
"""
from __future__ import annotations

import json
import sqlite3
from collections import deque
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from loguru import logger

from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.models import ObservedMessage
from nanobot.config.schema import TelegramSocialLearningConfig

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.expressions import ExpressionLibrary
    from nanobot.channels.telegram.social.jargon import JargonLibrary
    from nanobot.channels.telegram.social.store import SocialStore

LearningKind = Literal["expression", "jargon"]


class SocialLearning:
    def __init__(self, store: SocialStore) -> None:
        self.store = store
        self.db = store.db
        self.config = TelegramSocialLearningConfig()
        self.config_revision = 0
        self.recent: dict[str, deque[dict[str, object]]] = {}

    def attach_libraries(self, expressions: ExpressionLibrary, jargon: JargonLibrary) -> None:
        self.expressions, self.jargon = expressions, jargon

    def configure(self, config: TelegramSocialLearningConfig) -> None:
        self.config = config
        self.config_revision = self.store.scopes.setting_revision("learning", config.model_dump_json())
        self.recent = {scope: deque(rows, maxlen=config.selection_history_turns)
                       for scope, rows in self.recent.items()}
        with transaction(self.db):
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
        self.store.messages.set_learning_source(key, message.message_id, source, text)
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
        for row in self.store.messages.learning_neighbors(key, seq, self.config.context_messages):
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
        for kind, library in (("expression", self.expressions), ("jargon", self.jargon)):
            for row in library.invalidate_source(scope, message_id, revision):
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
        if not self.config.enabled or self.store.scopes.state(scope)["mode"] == "paused":
            return None
        with transaction(self.db):
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
                            (batch, scope, kind, self.store.scopes.state(scope)["version"], self.config_revision, after, rows[-1]["id"],
                             json.dumps([dict(row) for row in rows], ensure_ascii=False), self.store.now(), self.store.now(), self.store.now(), len(rows)))
            return batch

    def collect_due(self, scopes: list[str]) -> None:
        with transaction(self.db):
            for scope in scopes:
                version = self.store.scopes.state(scope)["version"]
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
        with transaction(self.db):
            row = self.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
            if row is None or row["status"] not in {"ready", "failed"}:
                raise ValueError("batch is no longer active")
            if row["attempts"] >= 2:
                raise ValueError("batch attempt budget exhausted")
            if (not self.config.enabled or self.store.scopes.state(row["scope"])["mode"] == "paused"
                    or row["config_revision"] != self.config_revision or row["scope_version"] != self.store.scopes.state(row["scope"])["version"]):
                raise ValueError("batch configuration changed or paused")
            self.db.execute("UPDATE learning_batches SET status='running',attempts=attempts+1,updated=? WHERE id=?", (self.store.now(), batch))

    def finish(self, batch: str, *, success: bool, reason: str = "", result_count: int = 0) -> None:
        """B/C commit entries/evidence in this SAME transaction, then finish; failure keeps cursor."""
        with transaction(self.db):
            row = self.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
            if row is None or row["status"] not in {"ready", "running", "failed"}:
                raise ValueError("batch is no longer active")
            if row["status"] != "running":
                self.start(batch)
            if (not self.config.enabled or self.store.scopes.state(row["scope"])["mode"] == "paused"
                    or row["config_revision"] != self.config_revision or row["scope_version"] != self.store.scopes.state(row["scope"])["version"]):
                raise ValueError("batch configuration changed")
            sources = json.loads(row["sources"])
            # An evicted observation remains readable in the durable bounded batch;
            # explicit edits/deletes cancel it in invalidate(), rather than masquerading as eviction.
            for source in sources:
                current = self.store.messages.learning_text(row["scope"], source["message_id"])
                if current and (current["revision"] != source["revision"] or current["learning_source"] != "human_text"
                                or not current["learning_text"].startswith(source["text"])):
                    raise ValueError("batch source changed")
            self.db.execute("UPDATE learning_batches SET status=?,reason=?,result_count=?,updated=? WHERE id=?",
                            ("succeeded" if success else "failed", reason, result_count, self.store.now(), batch))
            self.count(str(row["scope"]), f"{row['kind']}:{'succeeded' if success else 'failed'}", 1, sources[0]["message_id"], sources[-1]["message_id"])
            if success and row["purpose"] == "discovery":
                last = sources[-1]
                self.db.execute("UPDATE learning_cursors SET event_id=?,message_id=?,revision=?,updated=? WHERE scope=? AND kind=?",
                                (row["through_event"], last["message_id"], last["revision"], self.store.now(), row["scope"], row["kind"]))

    def recover(self) -> None:
        with transaction(self.db):
            for kind in ("expression", "jargon"):
                limit = 1 + getattr(self.config, kind).max_retries
                self.db.execute("UPDATE learning_batches SET status='ready',reason='interrupted',updated=? WHERE kind=? AND status='running' AND attempts<?", (self.store.now(), kind, limit))
                self.db.execute("UPDATE learning_batches SET status='failed',reason='attempt_budget_exhausted',updated=? WHERE kind=? AND status='running'", (self.store.now(), kind))

    def record_selection(self, scope: str, turn_id: str, *, target_message_id: int | None,
                         candidates: int, selected: list[tuple[str, str, int]], explanation: dict[str, object],
                         status: str = "selected", input_tokens: int = 0, output_tokens: int = 0, latency_ms: int = 0,
                         calls: int = 1) -> None:
        with transaction(self.db):
            for kind, entry, revision in selected:
                if kind not in {"expression", "jargon", "sticker"}:
                    raise ValueError("unknown selection kind")
                if kind == "sticker":
                    valid = self.store.stickers.exists(scope, entry)
                elif kind == "jargon":
                    valid = self.jargon.selection_entry(scope, entry, revision)
                else:
                    valid = self.expressions.selection_entry(scope, entry, revision)
                if valid is None:
                    raise ValueError("selected entry is missing, inactive, changed or outside this scope")
            self.db.execute("INSERT INTO expression_turns VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (scope, turn_id, target_message_id, calls, candidates, input_tokens, output_tokens, latency_ms, status, self.store.now()))
            self.db.executemany("INSERT INTO expression_selections(scope,turn_id,kind,entry_id,revision) VALUES(?,?,?,?,?)",
                                [(scope, turn_id, kind, entry, revision) for kind, entry, revision in selected])
            for kind, entry, revision in selected:
                if kind in {'expression', 'jargon'}:
                    (self.expressions if kind == "expression" else self.jargon).mark_usage(scope, entry, revision, "last_selected")
        detail = {"turn_id": turn_id, "target_message_id": target_message_id, "selected": selected,
                  "status": status, "calls": calls, "latency_ms": latency_ms, **explanation}
        self.recent.setdefault(scope, deque(maxlen=self.config.selection_history_turns)).append(detail)
        logger.bind(v5_selection={"scope": scope, **detail}).info("V5 turn selection {}", json.dumps({"scope": scope, **detail}, ensure_ascii=False))

    def confirm_use(self, scope: str, turn_id: str, kind: str, entry_id: str, revision: int, delivery_id: str) -> None:
        """Explicit usage attribution, separate from selection; only real receipts qualify.

        D will call this with actually used IDs. A does not attribute every selected
        item just because some text was sent, and never increases human occurrences.
        """
        with transaction(self.db):
            if kind == "jargon" and not self.jargon.usable_entry(scope, entry_id, revision):
                raise ValueError("only current usable jargon can be attributed as natural use")
            receipt = self.store.deliveries.confirmed_receipt(scope, turn_id, delivery_id)
            if receipt is None:
                raise ValueError("usage requires a confirmed delivery in this turn and scope")
            changed = self.db.execute("UPDATE expression_selections SET delivery_id=? WHERE scope=? AND turn_id=? AND kind=? AND entry_id=? AND revision=?",
                                      (delivery_id, scope, turn_id, kind, entry_id, revision)).rowcount
            if not changed:
                raise ValueError("usage requires the selected entry revision")
            if kind in {"expression", "jargon"}:
                (self.expressions if kind == "expression" else self.jargon).mark_usage(scope, entry_id, revision, "last_confirmed")

    def status(self, scope: str) -> dict[str, object]:
        return {"enabled": self.config.enabled, "stage": "expression_and_jargon_learning", "extractor_enabled": self.config.enabled,
                "config": self.config.model_dump(by_alias=True), "config_revision": self.config_revision,
                "buffered_materials": self.db.execute("SELECT COUNT(*) FROM learning_events WHERE scope=?", (scope,)).fetchone()[0],
                "pending_by_kind": {kind: self.db.execute("SELECT COUNT(*) FROM learning_events WHERE scope=? AND id>COALESCE("
                                                         "(SELECT event_id FROM learning_cursors WHERE scope=? AND kind=?),0)", (scope, scope, kind)).fetchone()[0]
                                    for kind in ("expression", "jargon")},
                "cursors": [dict(row) for row in self.db.execute("SELECT * FROM learning_cursors WHERE scope=?", (scope,))],
                "batches": [dict(row) for row in self.db.execute("SELECT id,kind,purpose,status,attempts,source_count,result_count,reason,substr(error_detail,1,120) AS error_detail,model,model_calls,input_tokens,output_tokens,search_calls,fetch_calls,research_chars,latency_ms,created,updated FROM learning_batches WHERE scope=? ORDER BY created DESC LIMIT 20", (scope,))],
                "counts": [dict(row) for row in self.db.execute("SELECT * FROM learning_counts WHERE scope=?", (scope,))],
                "usable_expressions": self.expressions.usable_count(scope),
                **self.jargon.counts(scope),
                "recent_selections": list(self.recent.get(scope, []))}

    def expression_generation(self, scope: str) -> sqlite3.Row:
        return self.db.execute("SELECT COALESCE(MAX(id),0) FROM learning_changes WHERE scope=? AND kind='expression'", (scope,)).fetchone()

    def record_expression_change(self, scope: str, entry_id: str, revision: int, action: str, reason: str, at: float) -> None:
        self.db.execute("INSERT INTO learning_changes(scope,kind,entry_id,revision,action,origin,reason,at) VALUES(?,'expression',?,?,?,'operator',?,?)", (scope, entry_id, revision, action, reason, at))

    def jargon_generation(self, scope: str) -> sqlite3.Row:
        return self.db.execute("SELECT COALESCE(MAX(id),0) FROM learning_changes WHERE scope=? AND kind='jargon'", (scope,)).fetchone()

    def record_jargon_change(self, scope: str, entry_id: str, revision: int, action: str, origin: str, reason: str, at: float) -> None:
        self.db.execute("INSERT INTO learning_changes(scope,kind,entry_id,revision,action,origin,reason,at) VALUES(?,'jargon',?,?,?,?,?,?)", (scope, entry_id, revision, action, origin, reason, at))

    def recheck_jargon(self, scope: str, attempts: int) -> None:
        self.db.execute("UPDATE learning_batches SET status='cancelled',reason='explicit_recheck' WHERE scope=? AND kind='jargon' AND status='failed' AND attempts>=?", (scope, attempts))

    def batch(self, batch: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()

    def batch_status(self, batch: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT status,scope,sources FROM learning_batches WHERE id=?", (batch,)).fetchone()

    def fail_batch(self, error: str, detail: str | None, updated: float, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET status='failed',reason=?,error_detail=COALESCE(?,error_detail),updated=? WHERE id=?", (error, detail, updated, batch))

    def refund_attempt(self, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET status='ready',attempts=attempts-1,updated=? WHERE id=? AND status='running'", (self.store.now(), batch))

    def clear_batch_error(self, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET error_detail=NULL WHERE id=?", (batch,))

    def last_batch_due(self) -> sqlite3.Row:
        return self.db.execute("SELECT MAX(due) FROM learning_batches WHERE attempts>0 OR model_calls>0").fetchone()

    def pending_batches(self, kind: LearningKind) -> list[sqlite3.Row]:
        return self.db.execute("SELECT id,scope,attempts FROM learning_batches WHERE kind=? AND status IN ('ready','failed') ORDER BY created,id", (kind,)).fetchall()

    def expression_context_budget(self, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET reason='library_context_budget' WHERE id=?", (batch,))

    def start_expression_call(self, due: float, model: str, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET due=?,model=?,model_calls=model_calls+1 WHERE id=?", (due, model, batch))

    def record_expression_usage(self, input_tokens: int, output_tokens: int, latency_ms: int, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET input_tokens=input_tokens+?,output_tokens=output_tokens+?,latency_ms=latency_ms+? WHERE id=?", (input_tokens, output_tokens, latency_ms, batch))

    def earliest_batch(self, kind: LearningKind, attempts: int) -> sqlite3.Row:
        return self.db.execute("SELECT COALESCE(MIN(created),1e30) FROM learning_batches WHERE kind=? AND status IN ('ready','failed') AND attempts<?", (kind, attempts)).fetchone()

    def active_jargon_batch(self, scope: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM learning_batches WHERE scope=? AND kind='jargon' AND status IN ('ready','running','failed')", (scope,)).fetchone()

    def ensure_jargon_cursor(self, scope: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO learning_cursors(scope,kind) VALUES(?,'jargon')", (scope,))

    def jargon_cursor(self, scope: str) -> sqlite3.Row:
        return self.db.execute("SELECT event_id FROM learning_cursors WHERE scope=? AND kind='jargon'", (scope,)).fetchone()

    def prepare_jargon_review(self, batch: str, scope: str, scope_version: str, config_revision: int, after_event: int, through_event: int, sources: str, created: float, updated: float, due: float, source_count: int, targets: str) -> None:
        self.db.execute("INSERT INTO learning_batches(id,scope,kind,scope_version,config_revision,after_event,through_event,sources,status,created,updated,due,source_count,purpose,targets) VALUES(?,?,'jargon',?,?,?,?,?,'ready',?,?,?,?,'review',?)", (batch, scope, scope_version, config_revision, after_event, through_event, sources, created, updated, due, source_count, targets))

    def charge_jargon_call(self, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET model_calls=model_calls+1 WHERE id=?", (batch,))

    def record_jargon_usage(self, input_tokens: int, output_tokens: int, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?", (input_tokens, output_tokens, batch))

    def batch_documents(self, batch: str) -> sqlite3.Row:
        return self.db.execute("SELECT documents FROM learning_batches WHERE id=?", (batch,)).fetchone()

    def charge_jargon_search(self, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET search_calls=search_calls+1 WHERE id=?", (batch,))

    def charge_jargon_fetch(self, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET fetch_calls=fetch_calls+1 WHERE id=?", (batch,))

    def record_jargon_documents(self, documents: str, research_chars: int, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET documents=?,research_chars=research_chars+? WHERE id=?", (documents, research_chars, batch))

    def start_jargon_call(self, due: float, model: str, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET due=?,model=? WHERE id=?", (due, model, batch))

    def record_jargon_latency(self, latency_ms: int, batch: str) -> None:
        self.db.execute("UPDATE learning_batches SET latency_ms=latency_ms+? WHERE id=?", (latency_ms, batch))

    def has_selection(self, scope: str, turn: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT 1 FROM expression_turns WHERE scope=? AND turn_id=?", (scope, turn)).fetchone()

    def selected_sticker(self, scope: str, turn: str, sticker_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT revision FROM expression_selections WHERE scope=? AND turn_id=? AND kind='sticker' AND entry_id=?", (scope, turn, sticker_id)).fetchone()
