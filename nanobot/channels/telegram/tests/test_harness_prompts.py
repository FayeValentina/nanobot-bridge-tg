"""Offline prompt/wire regression. Canned decisions do NOT measure LLM accuracy."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.channels.telegram.social.judge import SocialJudge
from nanobot.channels.telegram.social.judge_models import (
    JudgeFollowup,
    JudgeMessage,
    JudgeReply,
    JudgeSnapshot,
)
from nanobot.channels.telegram.social.models import ObservedMessage
from nanobot.channels.telegram.social.runtime import SOCIAL_PROTOCOL
from nanobot.channels.telegram.tests.test_social_runtime import A
from nanobot.channels.telegram.tests.test_social_scheduler import observed, social
from nanobot.providers.base import LLMProvider, LLMResponse

__all__ = ["social"]
REPO = Path(__file__).resolve().parents[4]
PERSONA = REPO / "docs/harness/groupmate.md"
CASES = json.loads((Path(__file__).with_name("fixtures") / "social_harness.json").read_text())


@pytest.mark.parametrize("sample", CASES, ids=lambda sample: sample["name"])
async def test_reviewed_scenarios_assemble_and_validate_with_canned_decisions(social, sample):
    now = social.now()
    role = PERSONA.read_text()
    messages = [JudgeMessage(seq=index, message_id=index, sender_id=sender, name=sender,
                             timestamp=now-10, text=text, is_self=sender == "bot")
                for index, (sender, text) in enumerate(sample["history"], 1)]
    seq = len(messages)+1
    messages.append(JudgeMessage(seq=seq, message_id=seq, sender_id="member", name="Member",
        timestamp=now-sample.get("age_seconds", 0), text=sample["text"],
        mentions_bot=sample.get("mentions_bot", False), media=("photo",) if sample.get("media") else (),
        reply=JudgeReply(kind="bot", message_id=seq-1) if sample.get("reply_bot") else None))
    for message in messages:
        social.store.observe(ObservedMessage(A, message.seq, message.message_id, message.sender_id,
            message.name, message.timestamp, message.text, is_self=message.is_self), 500)
    with social.store.db:
        social.store.db.execute("UPDATE judge_state SET assessed_through=? WHERE scope=?", (seq-1, A.session_key))
    followups = (JudgeFollowup(id="stable-matter", content="旅行计划", pending_question="和谁去？",
        pending_thought="", motivation="关心近况", followup_state="active", confirmed_attempts=1),) if sample.get("followup") else ()
    snapshot = JudgeSnapshot(scope=A.session_key, version=social.store.state(A.session_key)["version"],
        after_seq=seq-1, through_seq=seq, new_seqs=(seq,), messages=tuple(messages), created=now,
        expires=now+120, bot_name="肥鱼", bot_username="Deep_Fat_Fish_bot", nicknames=("肥鱼", "肥魚"),
        role_summary=role[:600], followups=followups, history_truncated=sample.get("history_truncated", False))
    provider = AsyncMock(spec=LLMProvider)
    provider.chat.return_value = LLMResponse(content=json.dumps({
        "decision": sample["expected"], "through_seq": seq,
        "source_seqs": [seq] if sample["expected"] == "wake" else [],
        "reason": sample["reason"], "fulfills_intent": False,
    }, ensure_ascii=False))
    media = [{"type": "text", "text": "Image unavailable in this offline fixture; content unknown."}]
    loader = AsyncMock(return_value=media if sample.get("media") else [])
    judge = SocialJudge(social.store, social.config.judge, provider, media_loader=loader)
    result = await judge.assess(snapshot)
    assert result.status == "decided" and result.decision.decision == sample["expected"]
    request = provider.chat.call_args.kwargs
    content = request["messages"][1]["content"]
    payload = json.loads(content[0]["text"] if isinstance(content, list) else content)
    assert payload["role_summary"] == role[:600]
    assert payload["history_truncated"] == sample.get("history_truncated", False)
    assert request["tools"] is None and request["tool_choice"] == "none"
    assert request["reasoning_effort"] == "low" and request["temperature"] == 0
    assert social.store.state(A.session_key)["read_through"] == 0
    assert not social.store.db.execute("SELECT 1 FROM actions").fetchone()


async def test_actual_summary_truncation_contains_identity_independence_and_silence(social):
    (social.roles / "guest.md").write_text(PERSONA.read_text())
    await social.observe(observed(social, text="有人聊天吗"), directed=False)
    snapshot = social.judging._snapshot(A)
    assert snapshot.role_summary == PERSONA.read_text()[:600]
    for essential in ("@Deep_Fat_Fish_bot", "AI 群友", "没被点名", "自己的兴趣", "安静听着"):
        assert essential in snapshot.role_summary


def test_conversation_persona_does_not_leak_into_ordinary_mode(tmp_path):
    builder = ContextBuilder(tmp_path)
    ordinary = builder.build_system_prompt(channel="telegram", include_skills=False)
    social_prompt = builder.build_system_prompt(channel="telegram", include_skills=False,
        trusted_persona=SOCIAL_PROTOCOL+"\n# Operator persona\n"+PERSONA.read_text())
    assert "肥鱼" not in ordinary and "鲸鱼娘" not in ordinary
    assert PERSONA.read_text() in social_prompt
    assert "In ordinary conversations" in ordinary
    assert "Social delivery rules override" in social_prompt
