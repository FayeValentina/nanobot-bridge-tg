"""V5-C scoped jargon meanings, explicit authority and bounded research evidence."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.expressions import ExpressionSource, Scene, normalized
from nanobot.channels.telegram.social.store import SocialStore

JargonStatus = Literal["candidate", "understand_only", "usable", "disabled"]


class JargonCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    term: str = Field(min_length=1, max_length=32)
    aliases: list[str] = Field(default_factory=list, max_length=5)
    message_id: int = Field(gt=0)
    revision: int = Field(gt=0)
    sender_id: str = Field(min_length=1, max_length=100)
    excerpt: str = Field(min_length=1, max_length=400)
    contextual: bool
    ordinary: bool
    local_only: bool
    sensitive: bool
    uncertain: bool
    needs_public: bool
    safe_to_use: bool
    disputed: bool = False
    hypothesis: str = Field(default="", max_length=240)
    local_meaning: str = Field(default="", max_length=240)
    explanation_quote: str = Field(default="", max_length=400)
    scene_tags: list[Scene] = Field(default_factory=list, max_length=4)
    intent_category: str = Field(default="", max_length=40)


class JargonDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    jargon: list[JargonCandidate] = Field(max_length=20)


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(max_length=2000)
    quote: str = Field(min_length=12, max_length=300)


class ReadPage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    url: str
    text: str
    extractor: str
    at: float


class PublicFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    term: str = Field(min_length=1, max_length=32)
    public_meaning: str = Field(default="", max_length=400)
    usage: str = Field(default="", max_length=200)
    citations: list[Citation] = Field(default_factory=list, max_length=3)
    uncertain: bool
    safe_to_use: bool
    conflict: bool


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    findings: list[PublicFinding] = Field(max_length=20)


class MeaningHints(BaseModel):
    model_config = ConfigDict(extra="ignore")
    local_only: bool = False
    sensitive: bool = False
    safe_to_use: bool = False


class SavedResearch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    public_citations: list[Citation] = Field(default_factory=list)
    hypothesis: str = ""


class JargonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["list", "get", "submit", "explain", "correct", "disable", "set_status", "review"]
    entry_id: str | None = Field(default=None, min_length=1, max_length=64)
    term: str | None = Field(default=None, min_length=1, max_length=32)
    aliases: list[str] = Field(default_factory=list, max_length=5)
    local_meaning: str = Field(default="", max_length=800)
    explanation_quote: str = Field(default="", max_length=1200)
    message_ids: list[int] = Field(default_factory=list, max_length=10)
    origin: Literal["human", "operator"] = "human"
    expected_revision: int | None = Field(default=None, gt=0)
    status: JargonStatus | None = None
    research: bool = False
    query: str = Field(default="", max_length=100)
    include_inactive: bool = False
    reason: str = Field(default="", max_length=400)
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=1000000)


class JargonLibrary:
    def __init__(self, store: SocialStore) -> None:
        self.store = store
        self.db = store.db

    def generation(self, scope: str) -> int:
        return int(self.store.learning.jargon_generation(scope)[0])

    def expire(self, scope: str) -> None:
        for row in self.db.execute("SELECT id FROM jargon WHERE scope=? AND status<>'disabled' AND expires<=?", (scope, self.store.now())).fetchall():
            self.retire_sources(scope, str(row[0]))
            self.db.execute("UPDATE jargon SET status='disabled',meaning_status='expired',revision=revision+1,updated=? WHERE id=?", (self.store.now(), row[0]))
            self.change_log(scope, str(row[0]), "expire", "system", "TTL expired")

    def get(self, scope: str, entry_id: str) -> dict[str, object]:
        row = self.db.execute("SELECT * FROM jargon WHERE scope=? AND id=?", (scope, entry_id)).fetchone()
        if row is None:
            raise ValueError("jargon entry does not exist in this scope")
        entry = dict(row)
        for field in ("aliases", "scene_tags", "examples", "source_links", "inference_basis", "members"):
            entry[field] = json.loads(row[field])
        entry["member_ids"] = entry["members"]
        entry["use_policy"] = "may use naturally when relevant" if row["status"] == "usable" else "understanding only; do not actively reuse" if row["status"] == "understand_only" else "uncertain or disabled; do not use as established meaning"
        entry["evidence"] = [dict(e) for e in reversed(self.db.execute("SELECT * FROM jargon_evidence WHERE memory_id=? ORDER BY seq DESC LIMIT 10", (entry_id,)).fetchall())]
        return entry

    def entries(self, scope: str) -> list[dict[str, object]]:
        return [self.get(scope, str(row[0])) for row in self.db.execute("SELECT id FROM jargon WHERE scope=? ORDER BY updated DESC,id", (scope,))]

    def find(self, scope: str, term: str) -> str | None:
        for row in self.db.execute("SELECT id,term,aliases FROM jargon WHERE scope=?", (scope,)):
            if normalized(term) in {normalized(str(row["term"])), *(normalized(str(a)) for a in json.loads(row["aliases"]))}:
                return str(row["id"])
        return None

    def change_log(self, scope: str, entry_id: str, action: str, origin: str, reason: str) -> None:
        revision = self.db.execute("SELECT revision FROM jargon WHERE id=?", (entry_id,)).fetchone()[0]
        self.store.learning.record_jargon_change(scope, entry_id, revision, action, origin, reason, self.store.now())

    def retire_sources(self, scope: str, entry_id: str, keep: tuple[int, ...] = ()) -> None:
        self.db.executemany("INSERT OR IGNORE INTO jargon_suppressions VALUES(?,'message',?)",
            [(scope, str(r[0])) for r in self.db.execute("SELECT message_id FROM jargon_evidence WHERE memory_id=?", (entry_id,)) if r[0] not in keep])

    def add_evidence(self, entry_id: str, sources: list[ExpressionSource]) -> None:
        for source in sources:
            self.db.execute("INSERT OR IGNORE INTO jargon_evidence VALUES(?,?,?,?,?,?,?,?,?)", (entry_id, source.message_id, source.seq,
                source.sender_id, source.at, source.text[:1200], source.revision, source.source, source.context))
        self.db.execute("UPDATE jargon SET occurrences=(SELECT COUNT(*) FROM jargon_evidence WHERE memory_id=?) WHERE id=?", (entry_id, entry_id))

    def create(self, scope: str, term: str, sources: list[ExpressionSource], aliases: list[str]) -> str:
        if self.db.execute("SELECT 1 FROM jargon_suppressions WHERE scope=? AND kind='term' AND value=?", (scope, normalized(term))).fetchone():
            raise ValueError("this term was retired; explicitly correct its stable entry")
        if self.db.execute("SELECT (SELECT COUNT(*) FROM jargon WHERE scope=? AND status<>'disabled') + (SELECT COUNT(*) FROM memories WHERE scope=? AND status IN ('candidate','confirmed'))", (scope, scope)).fetchone()[0] >= 300:
            raise ValueError("jargon index is full")
        entry_id = uuid4().hex
        self.db.execute("INSERT INTO jargon(id,scope,content,status,basis,revision,created,updated,term,aliases) VALUES(?,?,?,'candidate','inference',1,?,?,?,?)",
            (entry_id, scope, term, self.store.now(), self.store.now(), term, json.dumps(aliases, ensure_ascii=False)))
        self.add_evidence(entry_id, sources)
        self.change_log(scope, entry_id, "create", "discovery", "uncertain term")
        return entry_id

    def sources(self, scope: str, turn_id: str, ids: list[int]) -> list[ExpressionSource]:
        if not ids:
            raise ValueError("human source message_ids are required")
        shown = self.store.views.displayed_evidence(scope, turn_id)
        cutoff = self.db.execute("SELECT through_seq FROM jargon_resets WHERE scope=?", (scope,)).fetchone()
        result: list[ExpressionSource] = []
        for message_id in dict.fromkeys(ids):
            if message_id not in shown:
                raise ValueError("memory evidence must have been displayed in this turn")
            item = shown[message_id]
            if cutoff is not None and item.seq <= int(cutoff[0]):
                raise ValueError("evidence predates the last memory clear; use new evidence")
            if self.db.execute("SELECT 1 FROM jargon_suppressions WHERE scope=? AND kind='message' AND value=?",
                               (scope, str(message_id))).fetchone() is not None:
                raise ValueError("evidence was retired by a correction/deletion; use new evidence")
            current = self.store.messages.learning_text(scope, message_id)
            if (current is None or current["revision"] != item.revision or current["learning_source"] != "human_text"
                    or not current["learning_text"].startswith(item.excerpt)):
                raise ValueError("slang evidence requires retained human top-level text")
            item = item.model_copy(update={"revision": int(current["revision"])})
            result.append(ExpressionSource(message_id=item.message_id, revision=item.revision, seq=item.seq,
                sender_id=item.sender_id, at=item.timestamp, source="human_text", text=item.excerpt, context="{}"))
        return result

    def authoritative(self, scope: str, entry_id: str, meaning: str, sources: list[ExpressionSource], origin: str,
                      status: JargonStatus = "understand_only", reason: str = "explicit human explanation") -> None:
        if not meaning.strip():
            raise ValueError("a correction/explanation requires local_meaning")
        old = self.get(scope, entry_id)
        old_basis = old["inference_basis"]
        public_evidence = SavedResearch.model_validate(old_basis if isinstance(old_basis, dict) else {})
        self.retire_sources(scope, entry_id, tuple(s.message_id for s in sources))
        if sources:
            self.db.execute("DELETE FROM jargon_evidence WHERE memory_id=?", (entry_id,))
            self.add_evidence(entry_id, sources)
        self.db.execute("UPDATE jargon SET local_meaning=?,content=?,status=?,authority=?,authority_seq=?,basis='direct',meaning_status='confirmed',"
                        "inference_basis=?,revision=revision+1,review_requested=0,updated=? WHERE id=?",
            (meaning, meaning, status, origin, max([s.seq for s in sources], default=int(self.store.scopes.state(scope)["next_seq"]) - 1),
             public_evidence.model_dump_json(exclude={"hypothesis"}) if public_evidence.public_citations else '[]', self.store.now(), entry_id))
        self.change_log(scope, entry_id, "correct", origin, json.dumps({"reason": reason, "previous_local_meaning": old["local_meaning"]}, ensure_ascii=False))

    def apply(self, scope: str, turn_id: str, request: JargonRequest) -> dict[str, object]:
        with transaction(self.db):
            self.expire(scope)
            if request.action == "list":
                items = [e for e in self.entries(scope) if (request.include_inactive or e["status"] != "disabled")
                         and (not request.query or normalized(request.query) in normalized(str(e["term"]) + str(e["aliases"]) + str(e["content"])))]
                records: list[dict[str, object]] = []
                for item in items[request.offset:request.offset + request.limit]:
                    detail = {key: value for key, value in item.items() if key != "evidence"}
                    if records and len(json.dumps([*records, detail], ensure_ascii=False)) > 16000:
                        break
                    records.append(detail)
                return {"jargon": records, "next_offset": request.offset + len(records) if request.offset + len(records) < len(items) else None}
            entry_id = request.entry_id or (self.find(scope, request.term) if request.term else None)
            if request.action == "get":
                return {"jargon": self.get(scope, entry_id) if entry_id else None}
            current = self.get(scope, entry_id) if entry_id else None
            if request.action not in {"submit", "explain"} and current is None:
                raise ValueError("entry_id or an existing term is required")
            if current and request.expected_revision != current["revision"]:
                raise ValueError("expected_revision must match the current jargon revision")
            if request.action in {"submit", "explain", "correct"}:
                sources = [] if request.origin == "operator" and request.action == "correct" else self.sources(scope, turn_id, request.message_ids)
                if not current:
                    if not request.term or not any(request.term in s.text for s in sources):
                        raise ValueError("term must occur in human top-level evidence")
                    entry_id = self.create(scope, request.term, sources, request.aliases)
                assert entry_id
                if request.action in {"explain", "correct"}:
                    if request.origin == "human" and not any(request.explanation_quote and request.explanation_quote in s.text
                        and request.local_meaning in request.explanation_quote and (request.term or str(current["term"] if current else "")) in request.explanation_quote for s in sources):
                        raise ValueError("explicit explanation must quote the human meaning and term verbatim")
                    if request.origin == "operator" and not request.reason:
                        raise ValueError("operator correction requires a reason")
                    self.authoritative(scope, entry_id, request.local_meaning, sources, request.origin, request.status or "understand_only", request.reason)
                elif current and current["status"] != "disabled":
                    self.add_evidence(entry_id, sources)
                if request.research:
                    self.db.execute("UPDATE jargon SET review_requested=1 WHERE id=?", (entry_id,))
                return {"jargon": self.get(scope, entry_id)}
            assert entry_id and current
            if request.action == "review":
                self.store.learning.recheck_jargon(scope, 1 + self.store.learning.config.jargon.max_retries)
                self.db.execute("UPDATE jargon SET review_requested=1 WHERE id=?", (entry_id,))
                self.change_log(scope, entry_id, "review", "operator", request.reason or "explicit recheck")
            else:
                status = "disabled" if request.action == "disable" else request.status
                if status is None:
                    raise ValueError("set_status requires status")
                if status in {"understand_only", "usable"} and not (current["local_meaning"] or current["public_meaning"]):
                    raise ValueError("cannot promote an unknown meaning")
                self.retire_sources(scope, entry_id)
                self.db.execute("UPDATE jargon SET status=?,revision=revision+1,authority='operator',updated=? WHERE id=?", (status, self.store.now(), entry_id))
                self.change_log(scope, entry_id, "status", "operator", request.reason or "explicit status revision")
            return {"jargon": self.get(scope, entry_id)}

    def clear(self, scope: str) -> int:
        entries = self.entries(scope)
        for entry in entries:
            entry_id = str(entry["id"])
            self.retire_sources(scope, entry_id)
            self.db.execute("INSERT OR IGNORE INTO jargon_suppressions VALUES(?,'term',?)", (scope, normalized(str(entry["term"]))))
            self.db.execute("UPDATE jargon SET content='',local_meaning='',public_meaning='',usage='',source_links='[]',inference_basis='[]',"
                            "status='disabled',meaning_status='deleted',revision=revision+1,authority='operator',review_requested=0,updated=? WHERE id=?", (self.store.now(), entry_id))
            self.db.execute("DELETE FROM jargon_evidence WHERE memory_id=?", (entry_id,))
            self.change_log(scope, entry_id, "delete", "operator", "index deletion")
        self.db.execute("INSERT INTO jargon_resets VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET through_seq=excluded.through_seq", (scope, self.store.scopes.state(scope)["next_seq"] - 1))
        return len(entries)

    @staticmethod
    def validate_sources(result: JargonDiscovery, sources: list[ExpressionSource]) -> None:
        lookup = {s.message_id: s for s in sources}
        for item in result.jargon:
            source = lookup.get(item.message_id)
            if not source or source.revision != item.revision or source.sender_id != item.sender_id or item.excerpt not in source.text or item.term not in item.excerpt:
                raise ValueError("jargon evidence must match a human top-level source")
            if item.explanation_quote and not (item.local_meaning and item.explanation_quote in source.text and item.local_meaning in item.explanation_quote and item.term in item.explanation_quote):
                raise ValueError("invented explicit explanation")

    @staticmethod
    def eligible(item: JargonCandidate) -> bool:
        return item.contextual and not item.ordinary and normalized(item.term) not in {"今天", "明天", "吃饭", "睡觉", "天气", "公交", "火车", "照片", "建议", "本大爷", "白饭", "算力", "token"}

    def discover(self, scope: str, result: JargonDiscovery, sources: list[ExpressionSource]) -> list[str]:
        self.validate_sources(result, sources)
        lookup = {s.message_id: s for s in sources}
        ids: list[str] = []
        for item in result.jargon:
            source = lookup.get(item.message_id)
            if not source or source.revision != item.revision or source.sender_id != item.sender_id or item.excerpt not in source.text or item.term not in item.excerpt:
                raise ValueError("jargon evidence must match a human top-level source")
            if not self.eligible(item):
                continue
            if re.search(r"(?m)^\s*>|```", source.text):
                continue
            if self.db.execute("SELECT 1 FROM jargon_suppressions WHERE scope=? AND kind='message' AND value=?", (scope, str(source.message_id))).fetchone():
                continue
            reset = self.db.execute("SELECT through_seq FROM jargon_resets WHERE scope=?", (scope,)).fetchone()
            if reset and source.seq <= reset[0]:
                continue
            entry_id = self.find(scope, item.term)
            current = self.get(scope, entry_id) if entry_id else None
            if current and current["status"] == "disabled":
                continue
            explicit = bool(item.local_meaning and item.explanation_quote and item.explanation_quote in source.text
                            and item.local_meaning in item.explanation_quote and item.term in item.explanation_quote)
            if item.explanation_quote and not explicit:
                raise ValueError("invented explicit explanation")
            if current and current["authority"] != "inference":
                if explicit and current["authority"] == "human" and source.seq > int(str(current["authority_seq"])):
                    self.authoritative(scope, str(entry_id), item.local_meaning, [source], "human")
                    ids.append(str(entry_id))
                continue
            if not entry_id:
                entry_id = self.create(scope, item.term, [source], item.aliases)
            else:
                self.add_evidence(entry_id, [source])
            if explicit:
                self.authoritative(scope, entry_id, item.local_meaning, [source], "human")
            else:
                self.db.execute("UPDATE jargon SET inference_basis=?,scene_tags=?,intent_category=?,updated=? WHERE id=?",
                    (json.dumps({"hypothesis": item.hypothesis, "uncertain": True, "local_only": item.local_only, "sensitive": item.sensitive,
                                 "safe_to_use": item.safe_to_use, "needs_public": item.needs_public and item.uncertain and not item.local_only}, ensure_ascii=False),
                     json.dumps(item.scene_tags), item.intent_category, self.store.now(), entry_id))
            if item.disputed:
                self.db.execute("UPDATE jargon SET status='disabled',meaning_status='disputed',revision=revision+1 WHERE id=?", (entry_id,))
                self.change_log(scope, entry_id, "disable", "discovery", "disputed meaning")
            ids.append(entry_id)
        return list(dict.fromkeys(ids))

    def research(self, scope: str, entry_id: str, finding: PublicFinding, pages: list[ReadPage], expected_revision: int) -> None:
        current = self.get(scope, entry_id)
        if current["revision"] != expected_revision:
            raise ValueError("jargon changed during research")
        if normalized(finding.term) != normalized(str(current["term"])):
            raise ValueError("research term changed")
        if finding.conflict and not current["local_meaning"] and current["authority"] == "inference":
            self.db.execute("UPDATE jargon SET status='disabled',meaning_status='disputed',revision=revision+1,updated=? WHERE id=?", (self.store.now(), entry_id))
            self.change_log(scope, entry_id, "disable", "research", "conflicting public evidence without confirmed local meaning")
            return
        if not finding.public_meaning or finding.uncertain:
            return
        valid = [c for c in finding.citations if any(c.url == p.url and c.quote in p.text and normalized(finding.term) in normalized(p.text) for p in pages)]
        if len(valid) != len(finding.citations) or not valid:
            raise ValueError("public meaning requires actually read body citations")
        status = str(current["status"])
        local = str(current["local_meaning"])
        raw_hints = current["inference_basis"]
        hints = MeaningHints.model_validate(raw_hints if isinstance(raw_hints, dict) else {})
        if current["authority"] == "inference" and status != "disabled":
            status = "understand_only" if finding.conflict or local or hints.local_only or hints.sensitive or not hints.safe_to_use or not finding.safe_to_use else "usable"
        self.db.execute("UPDATE jargon SET public_meaning=?,usage=?,source_links=?,meaning_status=?,status=?,"
                        "content=?,last_inferred=?,revision=revision+1,updated=? WHERE id=?",
            (finding.public_meaning, finding.usage, json.dumps([c.url for c in valid]), "divergent" if finding.conflict else "researched", status,
             local or finding.public_meaning, self.store.now(), self.store.now(), entry_id))
        self.db.execute("UPDATE jargon SET inference_basis=? WHERE id=?", (json.dumps({**hints.model_dump(), "public_citations": [c.model_dump() for c in valid], "uncertain": False}, ensure_ascii=False), entry_id))
        self.change_log(scope, entry_id, "research", "research", "read body evidence; local meaning takes precedence")

    def requested_reviews(self, scope: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT id FROM jargon WHERE scope=? AND review_requested=1 ORDER BY updated LIMIT 20", (scope,)).fetchall()

    def review_evidence(self, entry_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM jargon_evidence WHERE memory_id=? ORDER BY seq DESC LIMIT 10", (entry_id,)).fetchall()

    def finish_research(self, last_inferred: float, entry_id: str) -> None:
        self.db.execute("UPDATE jargon SET research_revision=revision,review_requested=0,last_inferred=? WHERE id=?", (last_inferred, entry_id))

    def active_count(self, scope: str) -> int:
        return self.db.execute("SELECT COUNT(*) n FROM jargon WHERE scope=? AND status IN ('candidate','understand_only','usable')", (scope,)).fetchone()[0]
