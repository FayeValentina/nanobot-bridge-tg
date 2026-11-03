"""Jargon discovery/research on the V5 shared worker, batch and budget lifecycle."""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.channels.telegram.social.expressions import (
    ExpressionLearner,
    ExpressionSource,
    normalized,
    redacted_error_detail,
)
from nanobot.channels.telegram.social.jargon import (
    JargonDiscovery,
    JargonLibrary,
    ReadPage,
    ResearchResult,
)
from nanobot.channels.telegram.social.learning import LearningKind
from nanobot.channels.telegram.social.models import ConversationScope
from nanobot.config.schema import TelegramSocialLearningModelConfig
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime


class FetchedBody(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    status: int | None = None
    error: str | None = None
    url: str = ""
    final_url: str = Field(default="", alias="finalUrl")
    text: str = ""
    extractor: str = ""


class JargonLearner(ExpressionLearner):
    kind: LearningKind = "jargon"

    def __init__(self, runtime: TelegramSocialRuntime) -> None:
        super().__init__(runtime)
        self.jargon = JargonLibrary(self.store)
        self.tools: ToolRegistry | None = None
        self.prompt = render_template("telegram/social_jargon_discovery.md")
        self.research_prompt = render_template("telegram/social_jargon_research.md")

    def prepare_reviews(self, scopes: list[str]) -> None:
        if not self.store.learning.config.enabled:
            return
        for scope in scopes:
            if self.store.scopes.state(scope)["mode"] == "paused":
                continue
            entries = self.store.db.execute("SELECT id FROM jargon WHERE scope=? AND review_requested=1 ORDER BY updated LIMIT 20", (scope,)).fetchall()
            if not entries or self.store.db.execute("SELECT 1 FROM learning_batches WHERE scope=? AND kind='jargon' AND status IN ('ready','running','failed')", (scope,)).fetchone():
                continue
            sources: dict[int, dict[str, object]] = {}
            for entry in entries:
                for row in self.store.db.execute("SELECT * FROM jargon_evidence WHERE memory_id=? ORDER BY seq DESC LIMIT 10", (entry[0],)):
                    sources[row["message_id"]] = {"message_id": row["message_id"], "revision": row["revision"], "sender_id": row["sender_id"],
                        "seq": row["seq"], "at": row["at"], "source": row["source"], "text": row["excerpt"], "context": row["context"]}
            if not sources:
                continue
            with self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO learning_cursors(scope,kind) VALUES(?,'jargon')", (scope,))
                after = self.store.db.execute("SELECT event_id FROM learning_cursors WHERE scope=? AND kind='jargon'", (scope,)).fetchone()[0]
                self.store.db.execute("INSERT INTO learning_batches(id,scope,kind,scope_version,config_revision,after_event,through_event,sources,status,created,updated,due,source_count,purpose,targets) VALUES(?,?,'jargon',?,?,?,?,?,'ready',?,?,?,?,'review',?)",
                    (uuid4().hex, scope, self.store.scopes.state(scope)["version"], self.store.learning.config_revision, after, after,
                     json.dumps(list(sources.values()), ensure_ascii=False), self.store.now(), self.store.now(), self.store.now(), len(sources), json.dumps([e[0] for e in entries])))

    async def ask(self, batch: str, prompt: str, payload: object, settings: TelegramSocialLearningModelConfig) -> str:
        if self.provider is None:
            raise ValueError("jargon provider unavailable")
        with self.store.db:
            self.store.db.execute("UPDATE learning_batches SET model_calls=model_calls+1 WHERE id=?", (batch,))
        async with asyncio.timeout(settings.timeout_seconds):
            response = await self.provider.chat(messages=[{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                tools=None, tool_choice="none", model=self.model, temperature=0, max_tokens=settings.max_tokens)
        with self.store.db:
            if response.usage:
                self.store.db.execute("UPDATE learning_batches SET input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?",
                    (response.usage.input_tokens, response.usage.output_tokens, batch))
        if response.finish_reason != "stop" or response.tool_calls or response.content is None or len(response.content) > 32000:
            raise ValueError("invalid jargon response")
        return response.content

    async def read_public(self, batch: str, terms: list[str], settings: TelegramSocialLearningModelConfig) -> list[ReadPage]:
        config = self.store.learning.config
        row = self.store.db.execute("SELECT documents FROM learning_batches WHERE id=?", (batch,)).fetchone()
        pages = [ReadPage.model_validate(p) for p in json.loads(row[0])]
        if self.tools is None or self.tools.get("web_search") is None or self.tools.get("web_fetch") is None:
            return pages
        for term in terms:
            if any(normalized(term) in normalized(page.text) for page in pages):
                continue
            budget = self.store.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
            if budget["search_calls"] >= config.research_max_searches or budget["fetch_calls"] >= config.research_max_fetches or budget["research_chars"] >= config.research_max_chars:
                break
            # Charge BEFORE every call; retries/restart cannot reset network budgets.
            with self.store.db:
                self.store.db.execute("UPDATE learning_batches SET search_calls=search_calls+1 WHERE id=?", (batch,))
            try:
                async with asyncio.timeout(settings.timeout_seconds):
                    result = await self.tools.execute("web_search", {"query": f'{term} 网络用语 含义 来源', "count": 3})
            except Exception as exc:
                logger.debug("Social research batch={} kind={} search failed: {}", batch, self.kind, redacted_error_detail(exc))
                continue
            if not isinstance(result, str):
                continue
            urls = list(dict.fromkeys(re.findall(r"https?://[^\s<>\]\)\"']+", result)))
            for url in urls[:3]:
                budget = self.store.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
                remaining = config.research_max_chars - int(budget["research_chars"])
                if budget["fetch_calls"] >= config.research_max_fetches or remaining <= 0:
                    break
                with self.store.db:
                    self.store.db.execute("UPDATE learning_batches SET fetch_calls=fetch_calls+1 WHERE id=?", (batch,))
                try:
                    async with asyncio.timeout(settings.timeout_seconds):
                        body = await self.tools.execute("web_fetch", {"url": url, "extractMode": "text", "maxChars": remaining})
                    if not isinstance(body, str):
                        continue
                    data = FetchedBody.model_validate_json(body)
                    if data.error or data.status != 200:
                        continue
                    if len(data.text.strip()) < 100 or not data.extractor:
                        continue
                    page = ReadPage(url=data.final_url or data.url, text=data.text[:remaining], extractor=data.extractor, at=self.store.now())
                    pages.append(page)
                    with self.store.db:
                        self.store.db.execute("UPDATE learning_batches SET documents=?,research_chars=research_chars+? WHERE id=?", (json.dumps([p.model_dump() for p in pages], ensure_ascii=False), len(page.text), batch))
                    # One actual body is enough to attempt verification for this term.
                    if term.casefold() in page.text.casefold():
                        break
                except Exception as exc:
                    logger.debug("Social research batch={} kind={} fetch failed: {}", batch, self.kind, redacted_error_detail(exc))
                    continue
        return pages

    async def run(self, batch: str) -> None:
        started = time.monotonic()
        error = ""
        error_detail: str | None = None
        try:
            row = self.store.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
            if row is None or self.provider is None:
                return
            scope = ConversationScope.from_session_key(str(row["scope"]))
            settings = self.store.learning.config.jargon.model_copy(deep=True)
            if scope is None or (binding := self.runtime.binding(scope)) is None or binding.paused or self.busy() or row["attempts"] >= 1 + settings.max_retries:
                return
            sources = [ExpressionSource.model_validate(s) for s in json.loads(row["sources"])]
            with self.store.db:
                self.jargon.expire(scope.session_key)
            generation = self.jargon.generation(scope.session_key)
            self.store.learning.start(batch)
            with self.store.db:
                self.store.db.execute("UPDATE learning_batches SET due=?,model=? WHERE id=?", (self.store.now() + self.store.learning.config.interval_seconds, self.model, batch))
            discovery = JargonDiscovery(jargon=[])
            terms: list[str] = []
            if row["purpose"] == "discovery":
                library = [{k: e[k] for k in ("id", "term", "aliases", "status", "authority", "revision", "local_meaning", "public_meaning")} for e in self.jargon.entries(scope.session_key)]
                result = await self.ask(batch, self.prompt, {"sources": [s.model_dump() for s in sources], "existing": library,
                    "max_candidates": self.store.learning.config.jargon_candidates, "schema": JargonDiscovery.model_json_schema()}, settings)
                discovery = JargonDiscovery.model_validate_json(result)
                self.jargon.validate_sources(discovery, sources)
                if len(discovery.jargon) > self.store.learning.config.jargon_candidates:
                    raise ValueError("jargon candidate budget exceeded")
                for item in discovery.jargon:
                    existing = self.jargon.find(scope.session_key, item.term)
                    current = self.jargon.get(scope.session_key, existing) if existing else None
                    if (self.jargon.eligible(item) and item.needs_public and item.uncertain and not item.local_only and not item.sensitive and not item.explanation_quote
                            and (current is None or current["status"] == "candidate" and current["authority"] == "inference" and current["research_revision"] == 0)):
                        terms.append(item.term)
            else:
                terms = [str(self.jargon.get(scope.session_key, str(entry_id))["term"]) for entry_id in json.loads(row["targets"])]
            terms = list(dict.fromkeys(terms))[:self.store.learning.config.research_max_searches]
            pages = await self.read_public(batch, terms, settings) if terms else []
            findings = ResearchResult(findings=[])
            if terms and pages:
                result = await self.ask(batch, self.research_prompt, {"terms": terms, "pages": [p.model_dump() for p in pages],
                    "schema": ResearchResult.model_json_schema()}, settings)
                findings = ResearchResult.model_validate_json(result)
                if len({normalized(f.term) for f in findings.findings}) != len(findings.findings):
                    raise ValueError("duplicate research finding")
            binding = self.runtime.binding(scope)
            if binding is None or binding.paused or self.jargon.generation(scope.session_key) != generation:
                raise ValueError("jargon/scope changed during extraction")
            with self.store.db:
                ids = self.jargon.discover(scope.session_key, discovery, sources)
                for finding in findings.findings:
                    if finding.term not in terms:
                        raise ValueError("unsolicited research term")
                    entry_id = self.jargon.find(scope.session_key, finding.term)
                    if entry_id:
                        current = self.jargon.get(scope.session_key, entry_id)
                        self.jargon.research(scope.session_key, entry_id, finding, pages, int(str(current["revision"])))
                for term in terms:
                    entry_id = self.jargon.find(scope.session_key, term)
                    if entry_id:
                        self.store.db.execute("UPDATE jargon SET research_revision=revision,review_requested=0,last_inferred=? WHERE id=?", (self.store.now(), entry_id))
                self.store.learning.finish(batch, success=True, result_count=len(ids))
        except asyncio.CancelledError:
            error = "interrupted"
            raise
        except TimeoutError as exc:
            error = "timeout"
            error_detail = redacted_error_detail(exc)
            logger.warning("Social learning batch={} kind={} error={}: {}", batch, self.kind, error, error_detail)
        except ValueError as exc:
            error = "invalid_or_changed_input"
            error_detail = redacted_error_detail(exc)
            logger.opt(exception=True).warning("Social learning batch={} kind={} error={}: {}", batch, self.kind, error, error_detail)
        except Exception as exc:
            error = "provider_exception"
            error_detail = redacted_error_detail(exc)
            logger.opt(exception=True).warning("Social learning batch={} kind={} error={}: {}", batch, self.kind, error, error_detail)
        finally:
            if not self.store.closed:
                with self.store.db:
                    row = self.store.db.execute("SELECT status,scope,sources FROM learning_batches WHERE id=?", (batch,)).fetchone()
                    if row and row["status"] == "running" and error:
                        self.store.db.execute("UPDATE learning_batches SET status='failed',reason=?,error_detail=COALESCE(?,error_detail),updated=? WHERE id=?", (error, error_detail, self.store.now(), batch))
                        sources = json.loads(row["sources"])
                        self.store.learning.count(str(row["scope"]), "jargon:failed", 1, sources[0]["message_id"], sources[-1]["message_id"])
                    if row and row["status"] == "succeeded":
                        self.store.db.execute("UPDATE learning_batches SET error_detail=NULL WHERE id=?", (batch,))
                    if row:
                        self.store.db.execute("UPDATE learning_batches SET latency_ms=latency_ms+? WHERE id=?", (int((time.monotonic() - started) * 1000), batch))
