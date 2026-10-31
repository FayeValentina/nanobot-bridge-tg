"""Silent, bounded expression extraction and revision-aware scope-local storage."""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from collections.abc import Callable
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from nanobot.channels.telegram.social.models import ConversationScope
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import Config, ModelPresetConfig, TelegramSocialLearningModelConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.factory import make_provider
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime

Intent = Literal["irony", "playful_exaggeration", "self_mockery", "ellipsis", "closing", "teasing", "playful_challenge", "reaction"]
Scene = Literal["banter", "sharing", "complaint", "correction", "praise", "disagreement", "embarrassment", "ending", "guessing", "surprise"]


class ExpressionSource(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    message_id: int
    revision: int
    seq: int
    sender_id: str
    at: float
    source: Literal["human_text"]
    text: str = Field(min_length=1, max_length=4000)
    context: str


class ExpressionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message_id: int = Field(gt=0)
    revision: int = Field(gt=0)
    sender_id: str = Field(min_length=1, max_length=100)
    excerpt: str = Field(min_length=1, max_length=240)
    situation: str = Field(min_length=8, max_length=240)
    expression: str = Field(min_length=2, max_length=160)
    tone: str = Field(min_length=1, max_length=60)
    intent_category: Intent
    scene_tags: list[Scene] = Field(min_length=1, max_length=4)
    suitable: str = Field(min_length=4, max_length=200)
    unsuitable: str = Field(min_length=4, max_length=200)
    confidence: Literal["clear", "uncertain"]
    reusable: bool
    concrete_scene: bool
    private_or_named: bool
    abusive_or_mechanical: bool
    top_level: bool
    merge_id: str | None = Field(default=None, max_length=64)


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expressions: list[ExpressionCandidate] = Field(max_length=10)


class ExpressionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["list", "get", "modify", "disable", "enable"]
    entry_id: str | None = Field(default=None, max_length=64)
    expected_revision: int | None = Field(default=None, gt=0)
    situation: str | None = Field(default=None, min_length=8, max_length=240)
    expression: str | None = Field(default=None, min_length=2, max_length=160)
    tone: str | None = Field(default=None, min_length=1, max_length=60)
    intent_category: Intent | None = None
    scene_tags: list[Scene] | None = Field(default=None, min_length=1, max_length=4)
    suitable: str | None = Field(default=None, min_length=4, max_length=200)
    unsuitable: str | None = Field(default=None, min_length=4, max_length=200)
    reason: str = Field(default="operator revision", min_length=1, max_length=200)


def normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def similar(left: str, right: str) -> bool:
    a, b = normalized(left), normalized(right)
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b).ratio() >= .78
                             or (min(len(a), len(b)) >= 2 and min(len(a), len(b)) / max(len(a), len(b)) >= .6 and (a in b or b in a)))


def forbidden(text: str) -> bool:
    value = normalized(text)
    return (any(word in value for word in ("本大爷", "白饭", "算力", "token"))
            or value in {"加油", "你真棒", "真棒", "好棒", "努力", "相信自己", "你可以的", "奥利给"}
            or bool(re.search(r"https?://|@\w|\d{5,}|傻[逼比]|煞笔|死全家|操你|滚你", text, re.I)))


class ExpressionLibrary:
    def __init__(self, store: SocialStore) -> None:
        self.store = store
        self.db = store.db

    def list(self, scope: str) -> list[dict[str, object]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM expressions WHERE scope=? ORDER BY created,id", (scope,))]

    def get(self, scope: str, entry_id: str) -> dict[str, object]:
        row = self.db.execute("SELECT * FROM expressions WHERE scope=? AND id=?", (scope, entry_id)).fetchone()
        if row is None:
            raise ValueError("expression does not exist in this scope")
        return {**dict(row), "evidence": [dict(e) for e in self.db.execute(
            "SELECT * FROM expression_evidence WHERE expression_id=? ORDER BY seq", (entry_id,))]}

    def change(self, scope: str, request: ExpressionEdit) -> dict[str, object]:
        if request.action == "list":
            return {"expressions": self.list(scope)}
        if not request.entry_id:
            raise ValueError("entry_id is required")
        with self.db:
            current = self.get(scope, request.entry_id)
            if request.action == "get":
                return current
            if request.expected_revision != current["revision"]:
                raise ValueError("expected_revision must match the current expression revision")
            changes = request.model_dump(exclude_none=True, exclude={"action", "entry_id", "expected_revision", "reason"})
            if request.action != "modify" and changes:
                raise ValueError("only modify accepts expression fields")
            if request.action == "modify" and not changes:
                raise ValueError("modify requires at least one expression field")
            if any(forbidden(str(value)) for value in changes.values()):
                raise ValueError("expression contains excluded learning content")
            revision = int(str(current["revision"])) + 1
            # Keep the OLD wording too. A corrected expression must not be recreated
            # under a new ID from old evidence, synonyms or an in-flight response.
            self.db.execute("INSERT INTO expression_guards(scope,entry_id,revision,situation,expression,intent_category,scene_tags,created) VALUES(?,?,?,?,?,?,?,?)",
                            (scope, request.entry_id, revision, current["situation"], current["expression"], current["intent_category"], current["scene_tags"], self.store.now()))
            for field, value in changes.items():
                self.db.execute(f"UPDATE expressions SET {field}=? WHERE id=?", (json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value, request.entry_id))
            status = "disabled" if request.action == "disable" else "available" if request.action == "enable" else current["status"]
            self.db.execute("UPDATE expressions SET status=?,revision=?,updated=? WHERE id=?", (status, revision, self.store.now(), request.entry_id))
            self.db.execute("INSERT INTO learning_changes(scope,kind,entry_id,revision,action,origin,reason,at) VALUES(?,'expression',?,?,?,'operator',?,?)",
                            (scope, request.entry_id, revision, request.action, request.reason, self.store.now()))
            return self.get(scope, request.entry_id)

    def generation(self, scope: str) -> int:
        return int(self.db.execute("SELECT COALESCE(MAX(id),0) FROM learning_changes WHERE scope=? AND kind='expression'", (scope,)).fetchone()[0])

    def payload(self, scope: str) -> dict[str, object]:
        # All protected wording is included, even when inactive. No hash/content keys.
        entries = [dict(row) for row in self.db.execute("SELECT id,revision,situation,expression,intent_category,scene_tags,status FROM expressions WHERE scope=?", (scope,))]
        guards = [dict(row) for row in self.db.execute("SELECT entry_id,revision,situation,expression,intent_category,scene_tags FROM expression_guards WHERE scope=?", (scope,))]
        return {"existing": entries, "protected_wording": guards}

    def commit(self, batch: str, result: ExtractionResult, generation: int) -> list[str]:
        with self.db:
            row = self.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
            if row is None or row["kind"] != "expression" or row["status"] != "running":
                raise ValueError("expression batch is no longer running")
            scope = str(row["scope"])
            if self.generation(scope) != generation:
                raise ValueError("expression library changed during extraction")
            sources = {s.message_id: s for s in (ExpressionSource.model_validate(s) for s in json.loads(row["sources"]))}
            if len(result.expressions) > self.store.learning.config.expression_candidates:
                raise ValueError("expression candidate budget exceeded")
            ids: list[str] = []
            for candidate in result.expressions:
                source = sources.get(candidate.message_id)
                if (source is None or source.revision != candidate.revision or source.sender_id != candidate.sender_id
                        or candidate.excerpt not in source.text or candidate.expression not in candidate.excerpt):
                    # A fabricated/context-only ID or quote invalidates the WHOLE batch.
                    raise ValueError("expression evidence does not match a human top-level source")
                if not self.accept(candidate, source):
                    continue
                # Prior operator evidence is a permanent source suppression, including
                # a later edit of the same message. Only explicit enable can restore it.
                if self.db.execute("SELECT 1 FROM expression_evidence e JOIN learning_changes c ON c.entry_id=e.expression_id AND c.kind='expression' AND c.origin='operator' WHERE c.scope=? AND e.message_id=? LIMIT 1", (scope, source.message_id)).fetchone():
                    continue
                guards = self.db.execute("SELECT * FROM expression_guards WHERE scope=?", (scope,)).fetchall()
                if any(similar(candidate.expression, g["expression"]) for g in guards):
                    continue
                existing = self.db.execute("SELECT * FROM expressions WHERE scope=? ORDER BY created,id", (scope,)).fetchall()
                target = next((e for e in existing if e["id"] == candidate.merge_id), None)
                if candidate.merge_id and target is None:
                    raise ValueError("merge_id is outside this scope or unknown")
                if target and (target["revision"] > 1 or target["status"] not in {"candidate", "available", "usable"}):
                    continue
                # Retired entries win over active similarities so new IDs cannot revive them.
                matches = [e for e in existing if similar(candidate.expression, e["expression"])
                           and (e["intent_category"] == candidate.intent_category or e["revision"] > 1
                                or e["status"] not in {"candidate", "available", "usable"})]
                if any(e["status"] not in {"candidate", "available", "usable"} or e["revision"] > 1 for e in matches):
                    continue
                target = matches[0] if matches else target
                if target and (target["revision"] > 1 or target["status"] not in {"candidate", "available", "usable"}):
                    continue
                if target:
                    entry_id = str(target["id"])
                else:
                    entry_id = uuid4().hex
                    self.db.execute("INSERT INTO expressions(id,scope,situation,expression,tone,intent_category,scene_tags,suitable,unsuitable,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (entry_id, scope, candidate.situation, candidate.expression, candidate.tone, candidate.intent_category,
                         json.dumps(candidate.scene_tags, ensure_ascii=False), candidate.suitable, candidate.unsuitable,
                         "available" if candidate.confidence == "clear" else "candidate", self.store.now(), self.store.now()))
                inserted = self.db.execute("INSERT OR IGNORE INTO expression_evidence VALUES(?,?,?,?,?,?,?,?,?)",
                    (entry_id, source.message_id, source.seq, source.sender_id, source.at, candidate.excerpt,
                     source.revision, source.source, source.context)).rowcount
                if inserted:
                    self.db.execute("UPDATE expressions SET occurrences=occurrences+1,updated=? WHERE id=?", (self.store.now(), entry_id))
                    if target and target["status"] == "candidate" and candidate.confidence == "clear":
                        self.db.execute("UPDATE expressions SET status='available' WHERE id=?", (entry_id,))
                    ids.append(entry_id)
            self.store.learning.finish(batch, success=True, result_count=len(ids))
            return ids

    @staticmethod
    def accept(candidate: ExpressionCandidate, source: ExpressionSource) -> bool:
        if (not candidate.reusable or not candidate.concrete_scene or not candidate.top_level
                or candidate.private_or_named or candidate.abusive_or_mechanical):
            return False
        if any(forbidden(value) for value in (candidate.expression, candidate.excerpt, candidate.situation, candidate.suitable)):
            return False
        if normalized(candidate.situation) in {"聊天时可以这样说", "群友在聊天的时候", "有人说话的时候"}:
            return False
        # Telegram quote entities are excluded at ingestion; also reject obvious
        # manually pasted quotes/code so they cannot masquerade as top-level writing.
        if "```" in source.text or re.search(r"(?m)^\s*>|\[tool|system prompt", source.text, re.I):
            return False
        for quoted in re.findall(r'[“「『"]([^”」』"\n]+)[”」』"]', source.text):
            if candidate.expression in quoted:
                return False
        return True


def make_expression_provider(config: Config, settings: TelegramSocialLearningModelConfig) -> tuple[LLMProvider, str]:
    isolated = config.model_copy(deep=True)
    isolated.agents.defaults.fallback_models = []
    default = config.resolve_preset()
    preset = ModelPresetConfig(model=settings.model or default.model, provider=settings.provider or default.provider,
                              max_tokens=settings.max_tokens, temperature=0, reasoning_effort=default.reasoning_effort)
    return make_provider(isolated, preset=preset), preset.model


class ExpressionLearner:
    def __init__(self, runtime: TelegramSocialRuntime) -> None:
        self.runtime = runtime
        self.store = runtime.store
        self.library = ExpressionLibrary(self.store)
        self.provider: LLMProvider | None = None
        self.model = ""
        self.task: asyncio.Task[None] | None = None
        self.main_busy: Callable[[], bool] = lambda: False
        self.prompt = render_template("telegram/social_expression_learning.md")

    def configure_provider(self, config: Config) -> None:
        try:
            provider, model = make_expression_provider(config, self.store.learning.config.expression)
        except (ValueError, KeyError):
            self.cancel()
            self.provider, self.model = None, ""
            return
        self.set_provider(provider, model)

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.cancel()
        self.provider, self.model = provider, model

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()

    def busy(self) -> bool:
        return bool(self.main_busy() or self.runtime.judging.tasks or self.store.db.execute(
            "SELECT 1 FROM scopes WHERE mode='active' UNION ALL SELECT 1 FROM wakes WHERE status IN ('scheduled','queued','running') UNION ALL SELECT 1 FROM judge_state WHERE inflight=1 OR pending_due IS NOT NULL LIMIT 1").fetchone())

    def start_due(self, scopes: list[str]) -> None:
        if (self.provider is None or not scopes or not self.store.learning.config.enabled
                or (self.task is not None and not self.task.done()) or self.busy()):
            return
        # Durable global start interval also covers failed/cancelled requests and restarts.
        last = self.store.db.execute("SELECT MAX(due) FROM learning_batches WHERE kind='expression' AND attempts>0").fetchone()[0]
        if last is not None and self.store.now() < last:
            return
        for row in self.store.db.execute("SELECT id,scope,attempts FROM learning_batches WHERE kind='expression' AND status IN ('ready','failed') ORDER BY created,id"):
            if row["scope"] in scopes and row["attempts"] < 1 + self.store.learning.config.expression.max_retries:
                self.task = asyncio.create_task(self.run(str(row["id"])), name="social-expression-learning")
                return

    async def run(self, batch: str) -> None:
        response: LLMResponse | None = None
        started = time.monotonic()
        error = ""
        try:
            row = self.store.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
            if row is None or self.provider is None:
                return
            scope = ConversationScope.from_session_key(str(row["scope"]))
            if scope is None or (binding := self.runtime.binding(scope)) is None or binding.paused or self.busy():
                return
            settings = self.store.learning.config.expression.model_copy(deep=True)
            if row["attempts"] >= 1 + settings.max_retries:
                return
            generation = self.library.generation(scope.session_key)
            payload = {"sources": json.loads(row["sources"]), "library": self.library.payload(scope.session_key),
                       "max_candidates": self.store.learning.config.expression_candidates,
                       "schema": ExtractionResult.model_json_schema()}
            # Refuse oversized library context rather than truncate tombstones.
            content = json.dumps(payload, ensure_ascii=False)
            if len(content) > 180000:
                with self.store.db:
                    self.store.db.execute("UPDATE learning_batches SET reason='library_context_budget' WHERE id=?", (batch,))
                return
            self.store.learning.start(batch)
            with self.store.db:
                self.store.db.execute("UPDATE learning_batches SET due=?,model=? WHERE id=?", (self.store.now() + self.store.learning.config.interval_seconds, self.model, batch))
            async with asyncio.timeout(settings.timeout_seconds):
                response = await self.provider.chat(messages=[{"role": "system", "content": self.prompt}, {"role": "user", "content": content}],
                    tools=None, tool_choice="none", model=self.model, max_tokens=settings.max_tokens, temperature=0)
            if response.finish_reason != "stop" or response.tool_calls or response.content is None or len(response.content) > 24000:
                raise ValueError("invalid extraction response")
            result = ExtractionResult.model_validate_json(response.content)
            binding = self.runtime.binding(scope)
            if binding is None or binding.paused:
                raise ValueError("scope revoked or paused")
            self.library.commit(batch, result, generation)
        except asyncio.CancelledError:
            error = "interrupted"
            raise
        except TimeoutError:
            error = "timeout"
        except ValueError:
            error = "invalid_or_changed_input"
        except Exception:
            error = "provider_exception"
        finally:
            if not self.store.closed:
                with self.store.db:
                    current = self.store.db.execute("SELECT status,scope,sources FROM learning_batches WHERE id=?", (batch,)).fetchone()
                    if current and current[0] == "running" and error:
                        self.store.db.execute("UPDATE learning_batches SET status='failed',reason=?,updated=? WHERE id=?", (error, self.store.now(), batch))
                        sources = json.loads(current["sources"])
                        self.store.learning.count(str(current["scope"]), "expression:failed", 1, sources[0]["message_id"], sources[-1]["message_id"])
                    if current and current[0] in {"running", "succeeded", "failed", "cancelled"}:
                        usage = response.usage if response else None
                        self.store.db.execute("UPDATE learning_batches SET input_tokens=input_tokens+?,output_tokens=output_tokens+?,latency_ms=latency_ms+? WHERE id=?",
                            (usage.input_tokens if usage else 0, usage.output_tokens if usage else 0, int((time.monotonic() - started) * 1000), batch))
