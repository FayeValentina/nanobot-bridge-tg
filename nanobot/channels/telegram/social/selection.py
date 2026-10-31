"""One bounded, optional expression/jargon/sticker selection before generation."""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from nanobot.channels.telegram.social.expressions import make_expression_provider
from nanobot.channels.telegram.social.models import MediaReference
from nanobot.config.schema import Config
from nanobot.providers.base import LLMProvider
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime


class TurnMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message_id: int
    revision: int
    text: str
    is_self: bool = False
    reply_to: int | None = None
    reply: dict[str, object] | None = None
    media: list[str] = Field(default_factory=list)
    known_sticker_notes: list[str] = Field(default_factory=list)
    sender_id: str


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    messages: list[TurnMessage] = Field(default_factory=list)


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expressions: list[str] = Field(default_factory=list, max_length=3)
    jargon: list[str] = Field(default_factory=list, max_length=5)
    stickers: list[str] = Field(default_factory=list, max_length=5)
    reason: str = Field(default="", max_length=240)


@dataclass(frozen=True)
class Candidate:
    kind: Literal["expression", "jargon", "sticker"]
    entry_id: str
    revision: int
    data: dict[str, str | int]


@dataclass
class UsageTurn:
    texts: list[str] = field(default_factory=list)
    stickers: list[str] = field(default_factory=list)


@dataclass
class TurnSelection:
    source: list[TurnMessage]
    version: str
    config_revision: int
    selected: list[Candidate] = field(default_factory=list)
    status: str = "empty"


# Retrieval hints only: they expand SQLite recall, never decide intent or force a meme.
SCENE_HINTS = {
    "banter": "哈哈 笑 绷 乐 调侃 玩笑 嘴硬",
    "sharing": "分享 今天 吃 下班 摸鱼",
    "complaint": "累 烦 气 委屈 难受",
    "correction": "纠正 不是 其实",
    "praise": "厉害 牛 棒 赢",
    "disagreement": "不对 反对 不服",
    "embarrassment": "尴尬 丢脸 社死",
    "ending": "晚安 拜拜 再见",
    "guessing": "猜猜 猜",
    "surprise": "居然 竟然 啊 震惊",
}


def retrieval_terms(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_]{2,32}|[\u4e00-\u9fff]{2,}", text.casefold())
    terms: list[str] = []
    for word in reversed(words):
        parts = [word] if len(word) < 5 or word.isascii() else [word[i:i + 2] for i in range(len(word) - 1)]
        for part in parts:
            if part not in terms:
                terms.append(part)
    return terms[:32]


class ContextSelector:
    def __init__(self, runtime: TelegramSocialRuntime) -> None:
        self.runtime = runtime
        self.provider: LLMProvider | None = None
        self.model = ""
        self.prompt = render_template("telegram/social_expression_selection.md")
        self.cache: OrderedDict[tuple[str, str], TurnSelection] = OrderedDict()
        self.locks: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task[object]] = set()

    def configure_provider(self, config: Config) -> None:
        try:
            self.provider, self.model = make_expression_provider(config, self.runtime.config.learning.selection)
        except Exception:
            self.provider, self.model = None, ""
        self.cache.clear()

    def cancel(self) -> None:
        for task in self.tasks:
            task.cancel()

    def input(self, scope: str, turn: str, model_text: str = "") -> list[TurnMessage]:
        rows = self.runtime.store.db.execute(
            "SELECT body,turn_id FROM views WHERE scope=? AND (turn_id=? OR turn_id IS NULL) "
            "ORDER BY created DESC,rowid DESC LIMIT 128", (scope, turn),
        ).fetchall()
        for row in rows:
            if model_text and row[0] not in model_text:
                continue
            if not model_text and row[1] != turn:
                continue
            messages = SelectionInput.model_validate_json(row[0]).messages[-8:]
            for message in messages:
                # Already learned notes for the exact displayed sticker provide
                # visual context without another model/image call. Never borrow
                # another scope's notes or describe an unseen/unknown image.
                notes = self.runtime.store.db.execute(
                    "SELECT s.note FROM messages m JOIN json_each(m.media_refs) r "
                    "JOIN stickers s ON s.scope=m.scope AND s.id=json_extract(r.value,'$.unique_id') "
                    "WHERE m.scope=? AND m.message_id=? AND m.revision=? "
                    "AND json_extract(r.value,'$.kind')='sticker' AND trim(s.note)<>'' LIMIT 2",
                    (scope, message.message_id, message.revision),
                ).fetchall()
                message.known_sticker_notes = [str(note[0])[:400] for note in notes]
            return messages
        return []

    def candidates(self, scope: str, messages: list[TurnMessage]) -> list[Candidate]:
        config = self.runtime.config.learning
        text = " ".join(m.text + " " + " ".join(m.known_sticker_notes) + " " + json.dumps(m.reply or {}, ensure_ascii=False)
                        for m in messages[-4:] if not m.is_self).casefold()[-2400:]
        terms = retrieval_terms(text)
        scenes = [scene for scene, hints in SCENE_HINTS.items() if any(w in text for w in hints.split())]
        result: list[Candidate] = []
        specs: list[tuple[Literal["expression", "jargon", "sticker"], str, str, int, list[str]]] = [
            ("expression", "expressions", "status IN ('available','usable')", config.expression_candidates if config.selection_expressions else 0,
             ["situation", "expression", "tone", "suitable", "unsuitable", "scene_tags", "intent_category"]),
            ("jargon", "jargon", "status IN ('understand_only','usable') AND (expires IS NULL OR expires>?)", config.jargon_candidates if config.selection_jargon else 0,
             ["term", "aliases", "local_meaning", "public_meaning", "usage", "unsuitable", "status", "scene_tags", "intent_category"]),
            ("sticker", "stickers", "trim(note)<>''", config.selection_sticker_candidates if config.selection_stickers else 0,
             ["note"]),
        ]
        for kind, table, condition, limit, fields in specs:
            if not limit:
                continue
            # Scope/status indexes bound the search domain. SQL ranks keyword and
            # stored scene matches; a diverse fallback allows semantic matches with
            # no shared literal words. Never use global popularity as relevance.
            haystack = "lower(" + "||' '||".join(fields) + ")"
            needles = terms + (scenes if kind != "sticker" else [w for s in scenes for w in SCENE_HINTS[s].split()])
            score = "+".join(f"(instr({haystack},?)>0)" for _ in needles) or "0"
            partition = "intent_category" if kind != "sticker" else "substr(note,1,6)"
            revision = "revision" if kind != "sticker" else "0 AS revision"
            args: list[str | int | float] = [*needles, scope]
            if kind == "jargon":
                args.append(self.runtime.now())
            args.append(limit)
            rows = self.runtime.store.db.execute(
                f"SELECT * FROM (SELECT id,{revision},{','.join(fields)},({score}) AS relevance,"
                f"ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY updated DESC,id) AS variety "
                f"FROM {table} WHERE scope=? AND {condition}) ORDER BY relevance DESC,variety,id LIMIT ?", args,
            ).fetchall()
            for row in rows:
                result.append(Candidate(kind, str(row["id"]), int(row["revision"]),
                    {name: str(row[name])[:400] for name in fields}))
        return result

    def current(self, scope: str, candidate: Candidate) -> bool:
        table = {"expression": "expressions", "jargon": "jargon", "sticker": "stickers"}[candidate.kind]
        row = self.runtime.store.db.execute(f"SELECT * FROM {table} WHERE scope=? AND id=?", (scope, candidate.entry_id)).fetchone()
        if row is None:
            return False
        if candidate.kind != "sticker" and (row["revision"] != candidate.revision or row["status"] not in {"usable", "available", "understand_only"}):
            return False
        if candidate.kind == "jargon" and row["expires"] is not None and row["expires"] <= self.runtime.now():
            return False
        return all(str(row[name])[:400] == value for name, value in candidate.data.items())

    def messages_current(self, scope: str, messages: list[TurnMessage]) -> bool:
        return all(self.runtime.store.db.execute(
            "SELECT 1 FROM messages WHERE scope=? AND message_id=? AND revision=?",
            (scope, m.message_id, m.revision),
        ).fetchone() is not None for m in messages)

    async def select(self, scope: str, turn: str, model_text: str = "") -> list[Candidate]:
        # Every hook iteration reuses this direct ID/revision/value comparison.
        # A changed input in the SAME natural turn returns empty, preserving the
        # one-call budget; a new turn can select against the new input.
        async with self.locks.setdefault(scope, asyncio.Lock()):
            source = self.input(scope, turn, model_text)
            store = self.runtime.store
            version = store.state(scope)["version"]
            key = (scope, turn)
            previous = self.cache.get(key)
            if previous is not None:
                if (previous.source == source and previous.version == version
                        and previous.config_revision == store.learning.config_revision
                        and self.messages_current(scope, source)
                        and all(self.current(scope, c) for c in previous.selected)):
                    return previous.selected
                for detail in reversed(store.learning.recent.get(scope, [])):
                    if detail.get("turn_id") == turn:
                        detail["reuse_status"] = "changed_input_or_entry_returned_empty"
                        break
                return []
            cached = TurnSelection(source, version, store.learning.config_revision)
            self.cache[key] = cached
            while len(self.cache) > 100:
                self.cache.popitem(last=False)
            if store.db.execute("SELECT 1 FROM expression_turns WHERE scope=? AND turn_id=?", key).fetchone():
                return []  # Restart/eviction never buys a second call in this turn.
            started = time.monotonic()
            pool: list[Candidate] = []
            calls = input_tokens = output_tokens = 0
            reason = ""
            task = None
            try:
                if not self.runtime.config.learning.enabled or self.provider is None or not source:
                    cached.status = "disabled_or_unavailable"
                else:
                    pool = self.candidates(scope, source)
                    if pool:
                        calls = 1
                        references = {f"{c.kind[0]}{i}": c for i, c in enumerate(pool)}
                        payload = json.dumps({"conversation": [m.model_dump() for m in source],
                            "candidates": [{"kind": c.kind, "id": ref, **c.data} for ref, c in references.items()]}, ensure_ascii=False)
                        # asyncio.wait returns at the deadline even if an adapter
                        # swallows cancellation. Late results have no store/cache writes.
                        task = asyncio.create_task(self.provider.chat(
                            messages=[{"role": "system", "content": self.prompt}, {"role": "user", "content": payload}],
                            tools=None, tool_choice="none", model=self.model, temperature=0,
                            reasoning_effort="none",
                            max_tokens=min(768, self.runtime.config.learning.selection.max_tokens)))
                        self.tasks.add(task)
                        def finished(done: asyncio.Task[object]) -> None:
                            self.tasks.discard(done)
                            if not done.cancelled():
                                done.exception()
                        task.add_done_callback(finished)
                        remaining = self.runtime.config.learning.selection_timeout_seconds - (time.monotonic() - started)
                        done, _ = await asyncio.wait({task}, timeout=max(0, remaining))
                        if not done:
                            task.cancel()
                            raise TimeoutError
                        response = task.result()
                        input_tokens = response.usage.input_tokens if response.usage else 0
                        output_tokens = response.usage.output_tokens if response.usage else 0
                        if response.finish_reason != "stop" or response.tool_calls or not response.content or len(response.content) > 8000:
                            raise ValueError("invalid selection")
                        choice = Choice.model_validate_json(response.content)
                        reason = choice.reason
                        selected: list[Candidate] = []
                        for kind, ids, limit in [("expression", choice.expressions, self.runtime.config.learning.selection_expressions),
                                                 ("jargon", choice.jargon, self.runtime.config.learning.selection_jargon),
                                                 ("sticker", choice.stickers, self.runtime.config.learning.selection_stickers)]:
                            choices: list[Candidate] = []
                            for identity in dict.fromkeys(ids):
                                item = references.get(identity)
                                if item is None or item.kind != kind or not self.current(scope, item):
                                    raise ValueError("unknown or stale candidate")
                                choices.append(item)
                            selected.extend(choices[:limit])
                        if (self.input(scope, turn, model_text) != source or not self.messages_current(scope, source)
                                or store.state(scope)["version"] != version or store.learning.config_revision != cached.config_revision):
                            raise ValueError("input changed")
                        bounded = self.context(selected)
                        included = {str(item["id"]) for entries in bounded.values() for item in entries}
                        cached.selected = [item for item in selected if item.entry_id in included]
                        cached.status = "selected" if selected else "empty"
            except asyncio.CancelledError:
                if task is not None:
                    task.cancel()
                raise
            except TimeoutError:
                cached.status = "timeout"
            except Exception:
                cached.status = "error"
            try:
                store.learning.record_selection(scope, turn, target_message_id=next((m.message_id for m in reversed(source) if not m.is_self), None),
                    candidates=len(pool), selected=[(c.kind, c.entry_id, c.revision) for c in cached.selected],
                    status=cached.status, calls=calls, input_tokens=input_tokens, output_tokens=output_tokens,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    explanation={"reason": reason, "model": self.model, "input_tokens": input_tokens,
                                 "output_tokens": output_tokens, "input_messages": [[m.message_id, m.revision] for m in source],
                                 "candidates": [{"kind": c.kind, "id": c.entry_id, "selected": c in cached.selected} for c in pool]})
            except Exception:
                cached.selected = []
                cached.status = "error"
            return cached.selected

    def context(self, selected: list[Candidate]) -> dict[str, list[dict[str, str | int]]]:
        budget = self.runtime.config.learning.selection_chars
        groups: dict[str, list[dict[str, str | int]]] = {"expressions": [], "jargon": [], "stickers": []}
        for item in selected:
            group = {"expression": "expressions", "jargon": "jargon", "sticker": "stickers"}[item.kind]
            fields = {"expression": ["expression", "situation", "unsuitable"],
                      "jargon": ["term", "local_meaning", "public_meaning", "usage", "status"],
                      "sticker": ["note"]}[item.kind]
            detail = {"id": item.entry_id, "revision": item.revision, **{k: item.data[k] for k in fields}}
            trial = {**groups, group: [*groups[group], detail]}
            if len(json.dumps(trial, ensure_ascii=False)) <= budget:
                groups[group].append(detail)
        return groups

    def recent_usage(self, scope: str) -> dict[str, object]:
        # Only sent receipts. message IDs join to the observed SELF text/media;
        # unconfirmed/queued/unknown and generated-but-unsent drafts never qualify.
        rows = self.runtime.store.db.execute(
            "SELECT * FROM (SELECT id,turn_id,created,CASE WHEN kind='sticker' THEN '' ELSE content END AS text,CASE WHEN kind='sticker' THEN media_ref ELSE NULL END AS sticker "
            "FROM actions WHERE scope=? AND status='sent' UNION ALL "
            "SELECT d.id,d.turn_id,d.created,m.text AS text,m.media_refs AS sticker FROM message_deliveries d "
            "JOIN json_each(d.message_ids) j JOIN messages m ON m.scope=d.scope AND m.message_id=j.value AND m.is_self=1 "
            "WHERE d.scope=? AND d.status='sent') ORDER BY created DESC,id DESC LIMIT 80", (scope, scope),
        ).fetchall()
        turns: dict[str, UsageTurn] = {}
        for row in rows:
            key = str(row["turn_id"] or row["id"])
            if key not in turns and len(turns) >= self.runtime.config.learning.recent_usage_turns:
                continue
            item = turns.setdefault(key, UsageTurn())
            text_list, sticker_list = item.texts, item.stickers
            if row["sticker"]:
                raw = str(row["sticker"])
                refs = json.loads(raw) if raw.startswith("[") else [json.loads(raw)]
                for value in refs:
                    ref = MediaReference.model_validate(value)
                    if ref.kind == "sticker" and ref.unique_id not in sticker_list:
                        sticker_list.append(ref.unique_id)
            if row["text"] and not str(row["text"]).startswith("[sticker "):
                text = str(row["text"])[:240]
                if text not in text_list:
                    text_list.append(text)
        catchphrases = {word: sum(any(word.casefold() in text.casefold() for text in item.texts) for item in turns.values())
                       for word in ("本大爷", "白饭", "Token", "算力")}
        frames: Counter[str] = Counter()
        stickers: Counter[str] = Counter()
        for item in turns.values():
            # Count a repeated opening/ending or sticker at most once per round.
            frames.update({frame for text in item.texts if len(text) >= 6
                           for frame in (text[:8] + "…", "…" + text[-8:])})
            stickers.update(set(item.stickers))
        return {"turns": [{"turn_id": key, "texts": item.texts, "stickers": item.stickers} for key, item in turns.items()], "catchphrases": {k: v for k, v in catchphrases.items() if v},
                "repeated_sentence_frames": {k: v for k, v in frames.items() if v > 1},
                "sticker_counts": dict(stickers),
                "note": "这些是最近已确认发送的原话/句式和贴纸。无关话题请换种自然说法；这是软参考，不是禁词。正在讨论、引用或解释 Token/白饭等时正常接话。"}

    def confirm_sticker(self, scope: str, turn: str | None, delivery: str, sticker_id: str) -> None:
        if turn is None:
            return
        row = self.runtime.store.db.execute("SELECT revision FROM expression_selections WHERE scope=? AND turn_id=? AND kind='sticker' AND entry_id=?", (scope, turn, sticker_id)).fetchone()
        if row:
            self.runtime.store.learning.confirm_use(scope, turn, "sticker", sticker_id, int(row[0]), delivery)
