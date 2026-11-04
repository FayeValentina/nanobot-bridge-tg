"""Jargon discovery/research on the V5 shared worker, batch and budget lifecycle."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections.abc import Callable
from typing import cast
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.channels.telegram.social.bindings import ScopeBindings
from nanobot.channels.telegram.social.db.connection import transaction
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
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import TelegramSocialLearningModelConfig
from nanobot.utils.prompt_templates import render_template


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

    def __init__(self, store: SocialStore, bindings: ScopeBindings, judge_busy: Callable[[], bool]) -> None:
        super().__init__(store, bindings, judge_busy)
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
            entries = self.jargon.requested_reviews(scope)
            if not entries or self.store.learning.active_jargon_batch(scope):
                continue
            sources: dict[int, dict[str, object]] = {}
            for entry in entries:
                for row in self.jargon.review_evidence(entry[0]):
                    sources[row["message_id"]] = {"message_id": row["message_id"], "revision": row["revision"], "sender_id": row["sender_id"],
                        "seq": row["seq"], "at": row["at"], "source": row["source"], "text": row["excerpt"], "context": row["context"]}
            if not sources:
                continue
            with transaction(self.store.db):
                self.store.learning.ensure_jargon_cursor(scope)
                after = self.store.learning.jargon_cursor(scope)[0]
                self.store.learning.prepare_jargon_review(uuid4().hex, scope, self.store.scopes.state(scope)["version"], self.store.learning.config_revision, after, after, json.dumps(list(sources.values()), ensure_ascii=False), self.store.now(), self.store.now(), self.store.now(), len(sources), json.dumps([e[0] for e in entries]))

    async def ask(self, batch: str, prompt: str, payload: object, settings: TelegramSocialLearningModelConfig) -> str:
        if self.provider is None:
            raise ValueError("jargon provider unavailable")
        with transaction(self.store.db):
            self.store.learning.charge_jargon_call(batch)
        async with asyncio.timeout(settings.timeout_seconds):
            response = await self.provider.chat(messages=[{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                tools=None, tool_choice="none", model=self.model, temperature=0, max_tokens=settings.max_tokens)
        with transaction(self.store.db):
            if response.usage:
                self.store.learning.record_jargon_usage(response.usage.input_tokens, response.usage.output_tokens, batch)
        if response.finish_reason != "stop" or response.tool_calls or response.content is None or len(response.content) > 32000:
            raise ValueError("invalid jargon response")
        return response.content

    async def read_public(self, batch: str, terms: list[str], settings: TelegramSocialLearningModelConfig) -> list[ReadPage]:
        config = self.store.learning.config
        row = self.store.learning.batch_documents(batch)
        pages = [ReadPage.model_validate(p) for p in json.loads(row[0])]
        if self.tools is None or self.tools.get("web_search") is None or self.tools.get("web_fetch") is None:
            return pages
        for term in terms:
            if any(normalized(term) in normalized(page.text) for page in pages):
                continue
            # run() retains this active batch throughout its bounded research.
            budget = cast(sqlite3.Row, self.store.learning.batch(batch))
            if budget["search_calls"] >= config.research_max_searches or budget["fetch_calls"] >= config.research_max_fetches or budget["research_chars"] >= config.research_max_chars:
                break
            # Charge BEFORE every call; retries/restart cannot reset network budgets.
            with transaction(self.store.db):
                self.store.learning.charge_jargon_search(batch)
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
                budget = cast(sqlite3.Row, self.store.learning.batch(batch))
                remaining = config.research_max_chars - int(budget["research_chars"])
                if budget["fetch_calls"] >= config.research_max_fetches or remaining <= 0:
                    break
                with transaction(self.store.db):
                    self.store.learning.charge_jargon_fetch(batch)
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
                    with transaction(self.store.db):
                        self.store.learning.record_jargon_documents(json.dumps([p.model_dump() for p in pages], ensure_ascii=False), len(page.text), batch)
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
            row = self.store.learning.batch(batch)
            if row is None or self.provider is None:
                return
            scope = ConversationScope.from_session_key(str(row["scope"]))
            settings = self.store.learning.config.jargon.model_copy(deep=True)
            if scope is None or (binding := self.bindings.binding(scope)) is None or binding.paused or self.busy() or row["attempts"] >= 1 + settings.max_retries:
                return
            sources = [ExpressionSource.model_validate(s) for s in json.loads(row["sources"])]
            with transaction(self.store.db):
                self.jargon.expire(scope.session_key)
            generation = self.jargon.generation(scope.session_key)
            self.store.learning.start(batch)
            with transaction(self.store.db):
                self.store.learning.start_jargon_call(self.store.now() + self.store.learning.config.interval_seconds, self.model, batch)
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
            binding = self.bindings.binding(scope)
            if binding is None or binding.paused or self.jargon.generation(scope.session_key) != generation:
                raise ValueError("jargon/scope changed during extraction")
            with transaction(self.store.db):
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
                        self.jargon.finish_research(self.store.now(), entry_id)
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
                with transaction(self.store.db):
                    row = self.store.learning.batch_status(batch)
                    if row and row["status"] == "running" and error:
                        self.store.learning.fail_batch(error, error_detail, self.store.now(), batch)
                        sources = json.loads(row["sources"])
                        self.store.learning.count(str(row["scope"]), "jargon:failed", 1, sources[0]["message_id"], sources[-1]["message_id"])
                    if row and row["status"] == "succeeded":
                        self.store.learning.clear_batch_error(batch)
                    if row:
                        self.store.learning.record_jargon_latency(int((time.monotonic() - started) * 1000), batch)
