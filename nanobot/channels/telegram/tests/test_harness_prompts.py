"""Offline prompt/wire regression. Canned decisions do NOT measure LLM accuracy."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.channels.telegram.social.judge import JudgeInput, JudgeMessage, JudgeReply, SocialJudge
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
    snapshot = JudgeInput(context=tuple(messages[:-1]), new=(messages[-1],),
        bot_name="肥鱼", bot_username="Deep_Fat_Fish_bot", nicknames=("肥鱼", "肥魚"))
    provider = AsyncMock(spec=LLMProvider)
    provider.provider_name = "offline-test-provider"
    provider.chat.return_value = LLMResponse(content=json.dumps({
        "decision": sample["expected"],
        "reason": sample["reason"], "fulfills_intent": False,
    }, ensure_ascii=False))
    judge = SocialJudge(social.config.judge, provider, "offline")
    result = await judge.assess(snapshot)
    assert result.decision == sample["expected"]
    request = provider.chat.call_args.kwargs
    content = request["messages"][1]["content"]
    payload = json.loads(content[0]["text"] if isinstance(content, list) else content)
    assert payload["new"][-1]["text"] == sample["text"]
    assert set(payload) == {"bot_name", "bot_username", "nicknames", "intent", "context", "new"}
    assert request["tools"] is None and request["tool_choice"] == "none"
    assert request["reasoning_effort"] == "low" and request["temperature"] == 0
    assert social.store.scopes.state(A.session_key)["read_through"] == 0
    assert not social.store.db.execute("SELECT 1 FROM actions").fetchone()


async def test_judge_does_not_receive_persona(social):
    from nanobot.channels.telegram.tests.test_social_judge import decide
    from nanobot.channels.telegram.tests.test_social_scheduler import clock

    (social.roles / "guest.md").write_text(PERSONA.read_text())
    provider = decide(social)
    await social.observe(observed(social, text="有人聊天吗", is_bot=True), directed=False)
    clock(social).advance(2)
    await social.run_due()
    payload = json.loads(provider.chat.call_args.kwargs["messages"][1]["content"])
    assert "role_summary" not in payload and "background" not in payload


def test_conversation_persona_does_not_leak_into_ordinary_mode(tmp_path):
    builder = ContextBuilder(tmp_path)
    ordinary = builder.build_system_prompt(channel="telegram")
    social_prompt = builder.build_system_prompt(channel="telegram",
        trusted_persona=SOCIAL_PROTOCOL+"\n# Operator persona\n"+PERSONA.read_text())
    assert "肥鱼" not in ordinary and "鲸鱼娘" not in ordinary
    assert PERSONA.read_text() in social_prompt
    assert "In ordinary conversations" in ordinary
    assert "Social delivery rules override" in social_prompt
