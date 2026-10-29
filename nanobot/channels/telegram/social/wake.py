"""Local question/address checks and explicit waiting deadlines; no model calls."""

from __future__ import annotations

import math
import re
from datetime import datetime

from nanobot.channels.telegram.social.models import ObservedMessage

_QUESTION = re.compile(
    r"[?？]|(?:吗|么|呢)[。！!\s]*$|"
    r"什么|怎么|怎样|为何|为什么|哪[个里种位些]|多少|能否|是否|能不能|会不会|敢不敢|凭什么|"
    r"你觉得|你怎么看|你认为|解释|证明|说说|回答|不信|不对|不成立|错了|"
    r"\b(?:what|why|how|can you|could you|prove|explain|really)\b",
    re.I,
)


def looks_like_question(text: str) -> bool:
    return _QUESTION.search(text[:4000]) is not None


def is_bot_question(message: ObservedMessage, nicknames: list[str], *, reply_to_self: bool = False) -> bool:
    """Require a current vocative or an authoritative reply to this bot.

    A quoted nickname, third-party reply, substring of a longer name, or bare
    question mark is not an address. Uncertain text stays with existing triggers.
    """
    if message.reply is not None and not message.reply.is_bot:
        return False
    text = "\n".join(line for line in message.text.splitlines() if not line.lstrip().startswith(">"))[:4000].strip()
    if not text or text.startswith(("```", "\"", "'", "“", "「", "『")) or not _QUESTION.search(text):
        return False
    if reply_to_self or (message.reply is not None and message.reply.is_bot):
        return True
    aliases = [*nicknames, *([message.bot_username] if message.bot_username else [])]
    for alias in aliases:
        name = re.escape(alias.strip().lstrip("@"))
        if not name:
            continue
        # Chinese names must be followed by a vocative delimiter or an addressing
        # word. ASCII names additionally use word boundaries (bot != botanical).
        boundary = r"(?![A-Za-z0-9_])" if alias.isascii() else r"(?=$|[\s,，:：!！?？、。]|你|您|能|会|可|怎|为|是|请|帮|说|来|给|什|哪|几|多|有|敢|凭)"
        prefix = rf"^(?:(?:嘿|喂|请问|hey|hi|hello)[\s,，:：]*)?@?{name}{boundary}"
        suffix = rf"[\s,，:：]@?{name}[?？!！。\s]*$"
        if re.search(prefix, text, re.I) or re.search(suffix, text, re.I):
            return True
    return False


def wait_duration(seconds: float | None, *, infinite: bool, until: str | None,
                  on_timeout: bool, now: float, maximum: float) -> tuple[float | None, float | None]:
    """Return duration and an optional absolute deadline, with exactly one mode."""
    if sum((seconds is not None, infinite, until is not None)) != 1:
        raise ValueError("choose exactly one of seconds, infinite=true, or until")
    if infinite:
        if on_timeout:
            raise ValueError("an infinite wait cannot request a timeout wake")
        return None, None
    if until is not None:
        try:
            date = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if "T" not in until or date.tzinfo is None or date.utcoffset() is None:
                raise ValueError("until requires an ISO 8601 timestamp with an explicit timezone offset or Z")
            deadline = date.timestamp()
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("until requires an ISO 8601 timestamp with an explicit timezone offset or Z") from exc
        duration = deadline - now
        if not math.isfinite(duration) or not 0 < duration <= maximum:
            raise ValueError("absolute wait deadline must be future and within the operator limit")
        return duration, deadline
    assert seconds is not None
    if not math.isfinite(seconds) or not 1 <= seconds <= maximum:
        raise ValueError("wait duration exceeds the operator limit")
    return seconds, None
