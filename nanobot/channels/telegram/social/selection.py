"""Local optional expression/jargon/sticker references, without model calls."""
from __future__ import annotations

import json
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nanobot.channels.telegram.social.db.views import complete_view_ids
from nanobot.channels.telegram.social.expressions import ExpressionLibrary
from nanobot.channels.telegram.social.jargon import JargonLibrary
from nanobot.channels.telegram.social.models import MediaReference
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import TelegramSocialConfig


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
    def __init__(self, store: SocialStore, expressions: ExpressionLibrary, jargon: JargonLibrary,
                 config: Callable[[], TelegramSocialConfig], now: Callable[[], float]) -> None:
        self.store = store
        self.expressions, self.jargon = expressions, jargon
        self.config = config
        self.now = now
        self.cache: OrderedDict[tuple[str, str], TurnSelection] = OrderedDict()

    def input(self, scope: str, turn: str, model_text: str = "") -> list[TurnMessage]:
        rows = self.store.views.turn_bodies(scope, turn)
        ids = complete_view_ids(model_text)
        for row in rows:
            if model_text and row["id"] not in ids:
                continue
            if not model_text and row["turn_id"] != turn:
                continue
            messages = SelectionInput.model_validate_json(row["body"]).messages[-8:]
            for message in messages:
                # Already learned notes for the exact displayed sticker provide
                # visual context without another model/image call. Never borrow
                # another scope's notes or describe an unseen/unknown image.
                notes = self.store.messages.sticker_notes(scope, message.message_id, message.revision)
                message.known_sticker_notes = [str(note[0])[:400] for note in notes]
            return messages
        return []

    def candidates(self, scope: str, messages: list[TurnMessage]) -> list[Candidate]:
        config = self.config().learning
        text = " ".join(m.text + " " + " ".join(m.known_sticker_notes) + " " + json.dumps(m.reply or {}, ensure_ascii=False)
                        for m in messages[-4:] if not m.is_self).casefold()[-2400:]
        terms = retrieval_terms(text)
        scenes = [scene for scene, hints in SCENE_HINTS.items() if any(w in text for w in hints.split())]
        result: list[Candidate] = []
        specs: list[tuple[Literal["expression", "jargon", "sticker"], int]] = [
            ("expression", config.expression_candidates if config.selection_expressions else 0),
            ("jargon", config.jargon_candidates if config.selection_jargon else 0),
            ("sticker", config.selection_sticker_candidates if config.selection_stickers else 0),
        ]
        for kind, limit in specs:
            if not limit:
                continue
            needles = terms + (scenes if kind != "sticker" else [w for s in scenes for w in SCENE_HINTS[s].split()])
            if kind == "expression":
                rows = self.expressions.candidates(scope, needles, limit)
            elif kind == "jargon":
                rows = self.jargon.candidates(scope, needles, self.now(), limit)
            else:
                rows = self.store.stickers.candidates(scope, needles, limit)
            result.extend(Candidate(kind, str(row["id"]), int(row["revision"]),
                {name: str(row[name])[:400] for name in row.keys() if name not in {"id", "revision", "relevance", "variety"}}) for row in rows if row["relevance"] != 0)
        return result

    def current(self, scope: str, candidate: Candidate) -> bool:
        row = {"expression": self.expressions, "jargon": self.jargon, "sticker": self.store.stickers}[candidate.kind].row(scope, candidate.entry_id)
        if row is None:
            return False
        if candidate.kind != "sticker" and (row["revision"] != candidate.revision or row["status"] not in {"usable", "available", "understand_only"}):
            return False
        if candidate.kind == "jargon" and row["expires"] is not None and row["expires"] <= self.now():
            return False
        return all(str(row[name])[:400] == value for name, value in candidate.data.items())

    def forget(self, scope: str, turn: str) -> None:
        self.cache.pop((scope, turn), None)

    def select(self, scope: str, turn: str, model_text: str = "") -> list[Candidate]:
        key = (scope, turn)
        if (previous := self.cache.get(key)) is not None:
            return previous.selected
        if self.store.learning.has_selection(*key):
            return []
        started = time.monotonic()
        source = self.input(scope, turn, model_text)
        cached = TurnSelection()
        self.cache[key] = cached
        while len(self.cache) > 100:
            self.cache.popitem(last=False)
        pool: list[Candidate] = []
        config = self.config().learning
        if not config.enabled or not source:
            cached.status = "disabled_or_unavailable"
        else:
            pool = self.candidates(scope, source)
            selected: list[Candidate] = []
            for kind, limit in [("expression", config.selection_expressions),
                                ("jargon", config.selection_jargon),
                                ("sticker", config.selection_stickers)]:
                choices = [c for c in pool if c.kind == kind and self.current(scope, c)]
                selected.extend(choices[:limit])
            bounded = self.context(selected)
            included = {str(item["id"]) for entries in bounded.values() for item in entries}
            cached.selected = [item for item in selected if item.entry_id in included]
            cached.status = "retrieved" if cached.selected else "empty"
        self.store.learning.record_selection(scope, turn, target_message_id=next((m.message_id for m in reversed(source) if not m.is_self), None),
            candidates=len(pool), selected=[(c.kind, c.entry_id, c.revision) for c in cached.selected],
            status=cached.status, calls=0, input_tokens=0, output_tokens=0,
            latency_ms=int((time.monotonic() - started) * 1000),
            explanation={"input_messages": [[m.message_id, m.revision] for m in source],
                         "candidates": [{"kind": c.kind, "id": c.entry_id, "selected": c in cached.selected} for c in pool]})
        return cached.selected

    def context(self, selected: list[Candidate]) -> dict[str, list[dict[str, str | int]]]:
        budget = self.config().learning.selection_chars
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
        # Only sent receipts from the unified send journal;
        # unconfirmed/queued/unknown and generated-but-unsent drafts never qualify.
        rows = self.store.deliveries.recent_usage(scope)
        turns: dict[str, UsageTurn] = {}
        for row in rows:
            key = str(row["turn_id"] or row["id"])
            if key not in turns and len(turns) >= self.config().learning.recent_usage_turns:
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
                       for word in self.config().learning.catchphrases}
        frames: Counter[str] = Counter()
        stickers: Counter[str] = Counter()
        for item in turns.values():
            # Count a repeated opening/ending or sticker at most once per round.
            frames.update({frame for text in item.texts if len(text) >= 6
                           for frame in (text[:8] + "…", "…" + text[-8:])})
            stickers.update(set(item.stickers))
        return {"turns": [{"turn_id": key, "texts": item.texts, "stickers": item.stickers} for key, item in turns.items()], "catchphrases": {k: v for k, v in catchphrases.items() if v},
                "repeated_sentence_frames": {k: v for k, v in frames.items() if v > 1},
                "sticker_counts": dict(stickers)}

    def confirm_sticker(self, scope: str, turn: str | None, delivery: str, sticker_id: str) -> None:
        if turn is None:
            return
        row = self.store.learning.selected_sticker(scope, turn, sticker_id)
        if row:
            self.store.learning.confirm_use(scope, turn, "sticker", sticker_id, int(row[0]), delivery)
