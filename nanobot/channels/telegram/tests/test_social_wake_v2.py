"""V2-B local targeting, durable sampling, sleep interruption and wait contracts."""

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from nanobot.agent.tools.context import request_context
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.channels.telegram.social.models import QuotedMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.social.wake import is_bot_question
from nanobot.channels.telegram.tests.test_social_dialogue import admit, expose, take
from nanobot.channels.telegram.tests.test_social_runtime import (
    A,
    B,
    agent,
    channel,
    telegram_update,
)
from nanobot.channels.telegram.tests.test_social_scheduler import (
    clock,
    context,
    finish,
    observed,
    social,
)
from nanobot.config.schema import TelegramSocialConfig, TelegramSocialSleepInterruptConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest

__all__ = ["social"]


@pytest.fixture
def v2b(social):
    social.config.coalesce_seconds = 0
    social.config.cooldown_seconds = 0
    social.config.max_requests_per_hour = 120
    binding = social.config.scopes[0]
    binding.wake_on_question = True
    binding.wake_probability = 0.25
    binding.sleep_interrupts = TelegramSocialSleepInterruptConfig(
        keywords=True, senders=True, question=True, probability=True, messages=True,
    )
    social.random_source = Mock(return_value=0.9)
    social.configure(social.config)
    return social


def event(runtime, seq=1, text="ordinary chat", **kwargs):
    return replace(observed(runtime, seq, text=text), bot_username="test_bot", **kwargs)


def until(runtime, seconds):
    return datetime.fromtimestamp(runtime.now() + seconds, timezone.utc).isoformat()


@pytest.mark.parametrize("text,expected", [
    ("test_bot，你怎么看？", True), ("hey test_bot, what do you think?", True),
    ("肥鱼你怎么看", True), ("请问肥鱼，能不能解释一下", True),
    ("肥鱼，证明给我看", True), ("肥鱼，我不信", True),
    ("你怎么看，肥鱼？", True), ("TEST_BOT: explain this", True),
    ("test_botany, why?", False), ("肥鱼王你怎么看？", False),
    ("小明你怎么看？", False), ("？", False), ("谁知道答案？", False),
    ("有人提到肥鱼，他会怎么想？", False), ("肥鱼，早上好", False),
    ("> 肥鱼你怎么看？", False), ("“肥鱼你怎么看？”", False),
    ("```\n肥鱼你怎么看？\n```", False), ("小明说：肥鱼你怎么看？", False),
])
def test_question_needs_a_current_unambiguous_address(v2b, text, expected):
    assert is_bot_question(event(v2b, text=text), ["肥鱼"]) is expected


def test_quote_content_and_other_members_questions_do_not_address_the_bot(v2b):
    quote = QuotedMessage(message_id=99, sender_id="456", name="Other", timestamp=v2b.now(),
                          text="test_bot，你怎么看？", is_bot=False)
    assert not is_bot_question(event(v2b, text="你怎么看？", reply=quote, reply_to=99), [])
    assert not is_bot_question(event(v2b, text="test_bot，你怎么看？", reply=quote, reply_to=99), [])
    own = quote.model_copy(update={"is_bot": True, "sender_id": "bot"})
    assert is_bot_question(event(v2b, text="你怎么看？", reply=own, reply_to=99), [])


async def test_question_is_an_independent_candidate_without_a_nickname_trigger(v2b):
    await v2b.observe(event(v2b, text="test_bot，你怎么看？"), directed=False)
    _, view = take(v2b)
    assert view["wake_reason"] == "question"
    v2b.random_source.assert_not_called()


async def test_telegram_adapter_supplies_the_real_bot_username(v2b):
    tg = channel(v2b)
    # Plain username, without @: Telegram's explicit mention path is not used.
    await tg._observe_social(telegram_update(text="bot，你怎么看？"))
    _, view = take(v2b)
    assert view["wake_reason"] == "question"


@pytest.mark.parametrize("text", ["肥鱼王你怎么看？", "小明说：肥鱼你怎么看？", "> 肥鱼你怎么看？", "有人提到肥鱼，他会怎么想？"])
async def test_question_false_positives_are_not_reintroduced_by_nickname_fallback(v2b, text):
    v2b.config.scopes[0].nicknames = ["肥鱼"]
    v2b.config.scopes[0].wake_probability = 0
    await v2b.observe(event(v2b, text=text), directed=False)
    assert v2b.bus.inbound.empty()


async def test_basic_nickname_remains_reachable_while_sleeping_with_question_interrupt_off(v2b):
    v2b.config.scopes[0].nicknames = ["肥鱼"]
    v2b.config.scopes[0].sleep_interrupts = TelegramSocialSleepInterruptConfig()
    ctx = context(v2b)
    v2b.wait(ctx, infinite=True, sleeping=True)
    finish(v2b, ctx)
    await v2b.observe(event(v2b, text="肥鱼你怎么看？"), directed=False)
    _, view = take(v2b)
    assert view["wake_reason"] == "nickname"


async def test_verified_direct_address_has_priority_over_question_and_probability(v2b):
    await v2b.observe(event(v2b, text="test_bot，你怎么看？"), directed=True)
    _, view = take(v2b)
    assert view["wake_reason"] == "directed"
    v2b.random_source.assert_not_called()


@pytest.mark.parametrize("probability,value,expected", [(0, 0, False), (1, 0.999, True), (0.25, 0.249, True), (0.25, 0.25, False)])
async def test_probability_boundaries_are_deterministic(v2b, probability, value, expected):
    v2b.config.scopes[0].wake_probability = probability
    v2b.random_source = Mock(return_value=value)
    await v2b.observe(event(v2b), directed=False)
    assert (not v2b.bus.inbound.empty()) is expected
    assert v2b.random_source.call_count == int(probability > 0)
    rows = v2b.store.db.execute("SELECT * FROM wake_samples").fetchall()
    assert len(rows) == int(probability > 0)
    if rows:
        assert (rows[0]["seq"], rows[0]["probability"], bool(rows[0]["selected"])) == (1, probability, expected)


async def test_duplicate_and_edited_updates_never_roll_again(v2b):
    v2b.random_source = Mock(return_value=0.1)
    original = event(v2b)
    await v2b.observe(original, directed=False)
    await v2b.observe(original, directed=False)
    await v2b.observe(replace(original, update_id=99, text="edited question?", is_edit=True), directed=True)
    assert v2b.random_source.call_count == 1
    assert v2b.bus.inbound.qsize() == 1
    assert v2b.store.db.execute("SELECT COUNT(*) FROM wake_samples").fetchone()[0] == 1


@pytest.mark.parametrize("excluded", ["self", "bot", "stale", "future", "paused", "unauthorized", "sleeping", "edit"])
async def test_sampling_excludes_ineligible_observations(v2b, excluded):
    msg = event(v2b)
    if excluded == "self":
        msg = replace(msg, is_self=True)
    elif excluded == "bot":
        msg = replace(msg, is_bot=True)
    elif excluded == "stale":
        msg = replace(msg, timestamp=v2b.now() - 121)
    elif excluded == "future":
        msg = replace(msg, timestamp=v2b.now() + 1)
    elif excluded == "paused":
        v2b.config.scopes[0].paused = True
    elif excluded == "unauthorized":
        v2b.config.scopes[0].observe_from = ["456"]
    elif excluded == "edit":
        msg = replace(msg, is_edit=True)
    else:
        v2b.config.scopes[0].sleep_interrupts = TelegramSocialSleepInterruptConfig()
        ctx = context(v2b)
        v2b.wait(ctx, 300, sleeping=True)
        finish(v2b, ctx)
    await v2b.observe(msg, directed=False)
    v2b.random_source.assert_not_called()
    assert v2b.bus.inbound.empty()


async def test_persisted_sample_survives_restart_and_budget_delay(v2b):
    v2b.config.cooldown_seconds = 10
    ctx = context(v2b)
    finish(v2b, ctx)
    v2b.random_source = Mock(return_value=0.1)
    await v2b.observe(event(v2b), directed=False)
    assert v2b.bus.inbound.empty()
    root, config, now, bus = v2b.root, v2b.config, v2b.now, v2b.bus
    v2b.close()
    random_source = Mock(side_effect=AssertionError("must not draw again"))
    restored = TelegramSocialRuntime(config, root, bus, now=now, random_source=random_source)
    try:
        restored.store.recovery_jobs()
        await restored.observe(event(restored), directed=False)
        now.advance(10)
        await restored.run_due()
        _, view = take(restored)
        assert view["wake_reason"] == "probability"
        random_source.assert_not_called()
        assert restored.store.db.execute("SELECT COUNT(*) FROM wake_samples").fetchone()[0] == 1
    finally:
        restored.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1])
async def test_invalid_random_result_rolls_back_observation_atomically(v2b, value):
    v2b.random_source = Mock(return_value=value)
    with pytest.raises(ValueError, match="random source"):
        await v2b.observe(event(v2b), directed=False)
    assert v2b.store.state(A.session_key)["next_seq"] == 1
    assert v2b.store.db.execute("SELECT COUNT(*) FROM wake_samples").fetchone()[0] == 0
    assert v2b.store.db.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == 0


@pytest.mark.parametrize("queued", [False, True])
async def test_direct_address_replaces_random_candidate_before_model_admission(v2b, queued):
    v2b.config.coalesce_seconds = 0 if queued else 2
    v2b.random_source = Mock(return_value=0.1)
    await v2b.observe(event(v2b), directed=False)
    old = v2b.bus.inbound.get_nowait() if queued else None
    await v2b.observe(event(v2b, 2, text="@bot answer this"), directed=True)
    clock(v2b).advance(2)
    await v2b.run_due()
    if old is not None:
        with pytest.raises(PermissionError):
            admit(v2b, old)
    msg, view = take(v2b)
    assert view["wake_reason"] == "directed"
    assert v2b.bus.inbound.empty()
    admit(v2b, msg)
    assert v2b.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


async def test_random_does_not_create_second_turn_behind_an_unclaimed_mention(v2b):
    await v2b.observe(event(v2b, text="@bot hello"), directed=True)
    v2b.random_source = Mock(return_value=0.1)
    await v2b.observe(event(v2b, 2), directed=False)
    assert v2b.bus.inbound.qsize() == 1
    assert not v2b.store.db.execute("SELECT 1 FROM wakes WHERE status='scheduled'").fetchone()


async def test_random_source_is_preserved_when_later_chatter_extends_quiet(v2b):
    v2b.config.dynamic_wait.enabled = True
    v2b.random_source = Mock(side_effect=[0.1, 0.9])
    await v2b.observe(event(v2b, text="first ordinary message"), directed=False)
    clock(v2b).advance(1)
    await v2b.observe(event(v2b, 2, text="second ordinary message"), directed=False)
    clock(v2b).advance(2)
    await v2b.run_due()
    msg, _ = take(v2b)
    row = v2b.store.db.execute("SELECT trigger_seq,source_seq FROM wakes WHERE id=?", (msg.metadata["_social_job"],)).fetchone()
    assert tuple(row) == (2, 1)
    admit(v2b, msg)


@pytest.mark.parametrize("missing", ["sample", "message", "selection"])
async def test_probability_source_is_revalidated_before_model_admission(v2b, missing):
    v2b.random_source = Mock(return_value=0.1)
    await v2b.observe(event(v2b), directed=False)
    msg, _ = take(v2b)
    if missing == "sample":
        v2b.store.db.execute("DELETE FROM wake_samples")
    elif missing == "message":
        v2b.store.db.execute("DELETE FROM messages")
    else:
        v2b.store.db.execute("UPDATE wake_samples SET selected=0")
    v2b.store.db.commit()
    with pytest.raises(PermissionError):
        admit(v2b, msg)


async def test_failed_sample_journal_write_rolls_back_the_message_and_candidate(v2b):
    v2b.store.db.execute("CREATE TEMP TRIGGER reject_sample BEFORE INSERT ON wake_samples "
                         "BEGIN SELECT RAISE(ABORT, 'sample write failure'); END")
    v2b.random_source = Mock(return_value=0.1)
    with pytest.raises(sqlite3.IntegrityError):
        await v2b.observe(event(v2b), directed=False)
    assert v2b.store.state(A.session_key)["next_seq"] == 1
    assert not v2b.store.db.execute("SELECT 1 FROM messages").fetchone()
    assert not v2b.store.db.execute("SELECT 1 FROM wakes").fetchone()


async def test_scope_conditions_and_sampling_do_not_leak_into_other_topics(v2b):
    v2b.random_source = Mock(return_value=0)
    await v2b.observe(replace(event(v2b), scope=B), directed=False)
    assert v2b.bus.inbound.empty()
    v2b.random_source.assert_not_called()
    assert not v2b.store.db.execute("SELECT 1 FROM wake_samples").fetchone()


async def test_temporary_probability_candidate_expires_before_admission(v2b):
    ctx = context(v2b)
    v2b.set_wake(ctx, keywords=[], senders=[], on_messages=False, probability=1, ttl_seconds=5)
    finish(v2b, ctx)
    await v2b.observe(event(v2b), directed=False)
    msg, _ = take(v2b)
    clock(v2b).advance(6)
    with pytest.raises(PermissionError):
        admit(v2b, msg)


async def test_timeout_and_direct_address_share_one_unclaimed_opportunity(v2b):
    ctx = expose(v2b)
    v2b.wait(ctx, 5, on_timeout=True)
    finish(v2b, ctx)
    clock(v2b).advance(5)
    await v2b.run_due()
    stale, _ = take(v2b)
    await v2b.observe(event(v2b, 2, text="@bot hello"), directed=True)
    msg, view = take(v2b)
    with pytest.raises(PermissionError):
        admit(v2b, stale)
    assert view["wake_reason"] == "directed"
    admit(v2b, msg)
    assert v2b.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2  # setup plus one actual candidate


@pytest.mark.parametrize("condition", ["keywords", "senders", "question", "probability", "messages"])
@pytest.mark.parametrize("allowed", [True, False])
async def test_sleep_interrupts_require_the_specific_operator_policy(v2b, condition, allowed):
    binding = v2b.config.scopes[0]
    binding.wake_probability = 1 if condition == "probability" else 0
    binding.wake_keywords = ["出发"] if condition == "keywords" else []
    binding.wake_senders = ["123"] if condition == "senders" else []
    binding.wake_on_messages = condition == "messages"
    binding.sleep_interrupts = TelegramSocialSleepInterruptConfig(**{condition: allowed})
    ctx = context(v2b)
    v2b.wait(ctx, 300, sleeping=True)
    finish(v2b, ctx)
    text = "test_bot，你怎么看？" if condition == "question" else "出发" if condition == "keywords" else "ordinary"
    await v2b.observe(event(v2b, text=text), directed=False)
    assert (not v2b.bus.inbound.empty()) is allowed
    assert v2b.random_source.call_count == int(condition == "probability" and allowed)


def test_model_can_narrow_but_cannot_expand_sleep_interrupt_permissions(v2b):
    v2b.config.scopes[0].sleep_interrupts = TelegramSocialSleepInterruptConfig(keywords=True)
    ctx = context(v2b)
    with pytest.raises(PermissionError, match="operator policy"):
        v2b.set_wake(ctx, keywords=["出发"], senders=[], on_messages=False,
                     sleep_interrupts=TelegramSocialSleepInterruptConfig(probability=True))
    result = v2b.set_wake(ctx, keywords=["出发"], senders=[], on_messages=False,
                          sleep_interrupts=TelegramSocialSleepInterruptConfig(keywords=False))
    assert result["sleep_interrupts"]["keywords"] is False


async def test_temporary_keyword_does_not_allow_probability_to_bypass_selective_wait(v2b):
    ctx = context(v2b)
    v2b.set_wake(ctx, keywords=["出发"], senders=[], on_messages=False)
    v2b.wait(ctx, 60)
    finish(v2b, ctx)
    v2b.random_source = Mock(return_value=0)
    await v2b.observe(event(v2b, text="unrelated"), directed=False)
    assert v2b.bus.inbound.empty()
    v2b.random_source.assert_not_called()


@pytest.mark.parametrize("condition", ["question", "probability"])
async def test_one_shot_new_conditions_are_consumed_at_admission(v2b, condition):
    ctx = context(v2b)
    v2b.set_wake(ctx, keywords=[], senders=[], on_messages=False,
                 on_question=condition == "question", probability=1 if condition == "probability" else 0)
    v2b.wait(ctx, 60)
    finish(v2b, ctx)
    await v2b.observe(event(v2b, text="test_bot，你怎么看？" if condition == "question" else "ordinary"), directed=False)
    msg, view = take(v2b)
    assert view["wake_reason"] == condition
    assert v2b.store.state(A.session_key)["wake_config"] != "{}"
    admit(v2b, msg)
    assert v2b.store.state(A.session_key)["wake_config"] == "{}"


async def test_busy_time_candidate_consumes_one_shot_only_when_shown_to_model(tmp_path, v2b):
    loop = agent(tmp_path, v2b, [])
    calls = 0
    async def response(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="condition", name="social_set_wake", arguments={"keywords": ["go"]},
            )])
        if calls == 2:
            await v2b.observe(event(v2b, 2, text="go"), directed=False)
            assert json.loads(v2b.store.state(A.session_key)["wake_config"])["keywords"] == ["go"]
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(id="read", name="social_read_messages", arguments={})])
        return LLMResponse(content="")
    loop.provider.chat_stream_with_retry.side_effect = response
    await v2b.observe(event(v2b, text="@bot hello"), directed=True)
    await loop._dispatch(v2b.bus.inbound.get_nowait())
    assert calls == 3
    assert v2b.store.state(A.session_key)["wake_config"] == "{}"
    assert not v2b.store.db.execute("SELECT 1 FROM wakes WHERE status IN ('scheduled','queued','running')").fetchone()


async def test_replacement_condition_is_not_consumed_by_an_old_running_candidate(tmp_path, v2b):
    ctx = context(v2b)
    v2b.set_wake(ctx, keywords=["go"], senders=[], on_messages=False)
    finish(v2b, ctx)
    loop = agent(tmp_path, v2b, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="next", name="social_set_wake", arguments={"keywords": ["next"]},
        )]),
        LLMResponse(content=""),
    ])
    await v2b.observe(event(v2b, text="go"), directed=False)
    await loop._dispatch(v2b.bus.inbound.get_nowait())
    assert json.loads(v2b.store.state(A.session_key)["wake_config"])["keywords"] == ["next"]


async def test_replacing_temporary_conditions_cancels_the_old_queued_candidate(v2b):
    ctx = context(v2b)
    v2b.set_wake(ctx, keywords=["go"], senders=[], on_messages=False)
    finish(v2b, ctx)
    await v2b.observe(event(v2b, text="go"), directed=False)
    msg, _ = take(v2b)
    v2b.store.set_wake_config(A.session_key, json.dumps({"keywords": ["new"], "expires_at": v2b.now() + 60}))
    with pytest.raises(PermissionError):
        admit(v2b, msg)
    assert v2b.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1  # setup only


async def test_infinite_wait_has_no_timer_or_lock_and_direct_address_interrupts(tmp_path, v2b):
    loop = agent(tmp_path, v2b, [LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="wait", name="social_wait", arguments={"infinite": True, "sleeping": True}),
    ])])
    await v2b.observe(event(v2b, text="@bot hello"), directed=True)
    await loop._dispatch(v2b.bus.inbound.get_nowait())
    state = v2b.store.state(A.session_key)
    assert state["wait_infinite"] and state["wake_at"] is None and state["mode"] == "sleeping"
    lock = loop._session_locks.get(A.session_key)
    assert lock is None or not lock.locked()
    assert loop.provider.chat_stream_with_retry.await_count == 1
    await v2b.observe(event(v2b, 2, text="@bot wake"), directed=True)
    msg, _ = take(v2b)
    admit(v2b, msg)
    assert not v2b.store.state(A.session_key)["wait_infinite"]


async def test_infinite_temporary_wait_expires_back_to_administrator_triggers(v2b):
    v2b.config.scopes[0].wake_keywords = ["base"]
    ctx = context(v2b)
    v2b.set_wake(ctx, keywords=["temporary"], senders=[], on_messages=False, ttl_seconds=5)
    v2b.wait(ctx, infinite=True, sleeping=True)
    finish(v2b, ctx)
    assert v2b.store.next_due() == v2b.now() + 5
    clock(v2b).advance(5)
    await v2b.run_due()
    assert v2b.store.state(A.session_key)["wake_config"] == "{}"
    assert v2b.store.state(A.session_key)["mode"] == "observing"
    await v2b.observe(event(v2b, text="base"), directed=False)
    _, view = take(v2b)
    assert view["wake_reason"] == "keyword"


async def test_infinite_messages_ttl_clears_both_wait_intent_and_infinite_flag(v2b):
    ctx = expose(v2b)
    v2b.set_wake(ctx, keywords=["temporary"], senders=[], on_messages=False, ttl_seconds=5)
    v2b.wait(ctx, infinite=True, purpose="messages")
    finish(v2b, ctx)
    clock(v2b).advance(5)
    await v2b.run_due()
    state = v2b.store.state(A.session_key)
    assert state["mode"] == "observing" and not state["wait_infinite"]
    assert v2b.store.dialogue_status(A.session_key)["wait"] is None
    assert v2b.bus.inbound.empty()


async def test_infinite_message_wait_starts_a_bounded_burst_on_first_new_message(v2b):
    ctx = expose(v2b)
    v2b.wait(ctx, infinite=True, purpose="messages", quiet_seconds=3)
    finish(v2b, ctx)
    clock(v2b).advance(1000)
    await v2b.run_due()
    assert v2b.bus.inbound.empty()
    assert v2b.store.dialogue_status(A.session_key)["wait"]["total_due"] is None
    await v2b.observe(event(v2b, 2), directed=False)
    first = v2b.now()
    for seq in range(3, 15):
        clock(v2b).advance(1)
        await v2b.observe(event(v2b, seq), directed=False)
        await v2b.run_due()
    _, view = take(v2b)
    assert view["wake_reason"] == "wait_messages"
    assert view["dialogue"]["total_due"] == first + v2b.config.dynamic_wait.max_seconds
    assert view["dialogue"]["quiet"] is False


async def test_infinite_sleep_observation_completes_locally(v2b):
    v2b.config.sleep_observation.enabled = True
    v2b.configure(v2b.config)
    ctx = expose(v2b)
    result = v2b.wait(ctx, infinite=True, sleeping=True)
    assert result["purpose"] == "observe"
    finish(v2b, ctx)
    clock(v2b).advance(300)
    await v2b.run_due()
    state = v2b.store.state(A.session_key)
    assert state["mode"] == "sleeping" and state["wait_infinite"] and state["wake_at"] is None
    assert v2b.bus.inbound.empty()


async def test_absolute_wait_does_not_move_its_deadline_for_sleep_observation(v2b):
    v2b.config.sleep_observation.enabled = True
    v2b.configure(v2b.config)
    ctx = expose(v2b)
    end = v2b.now() + 30
    result = v2b.wait(ctx, until=until(v2b, 30), sleeping=True, on_timeout=True)
    assert result["total_due"] == pytest.approx(end)
    finish(v2b, ctx)
    clock(v2b).advance(31)
    await v2b.run_due()
    _, view = take(v2b)
    assert view["wake_reason"] == "timeout"
    assert v2b.store.state(A.session_key)["wake_at"] is None


@pytest.mark.parametrize("args", [
    {}, {"seconds": 10, "infinite": True}, {"seconds": 10, "until": "2030-01-01T12:00:00Z"},
    {"infinite": True, "until": "2030-01-01T12:00:00Z"}, {"infinite": True, "on_timeout": True},
    {"infinite": True, "purpose": "reply"}, {"until": "2026-10-29"},
    {"until": "2026-10-29T12:00:00"}, {"until": "tomorrow"}, {"until": "2020-01-01T00:00:00Z"},
    {"until": "2099-01-01T00:00:00+00:00"},
])
def test_invalid_or_ambiguous_wait_modes_are_rejected_before_yielding(v2b, args):
    ctx = context(v2b)
    with pytest.raises(ValueError):
        v2b.wait(ctx, **args)
    assert v2b.store.state(A.session_key)["mode"] == "active"
    assert not ctx.attributes.get("social_deferred")


def test_absolute_deadline_accepts_explicit_non_utc_offset(v2b):
    from datetime import timedelta
    ctx = context(v2b)
    end = datetime.fromtimestamp(v2b.now() + 30, timezone(timedelta(hours=8))).isoformat()
    result = v2b.wait(ctx, until=end)
    assert result["wake_at"] == pytest.approx(v2b.now() + 30)


async def test_new_wait_modes_and_sampling_recover_without_redrawing(v2b):
    ctx = context(v2b)
    v2b.wait(ctx, infinite=True, sleeping=True)
    root, config, now, bus = v2b.root, v2b.config, v2b.now, v2b.bus
    v2b.close()
    restored = TelegramSocialRuntime(config, root, bus, now=now, random_source=Mock(return_value=0.9))
    try:
        restored.store.recovery_jobs()
        await restored.run_due()
        state = restored.store.state(A.session_key)
        assert state["mode"] == "sleeping" and state["wait_infinite"] and state["wake_at"] is None
        assert bus.inbound.empty()
        restored.config.scopes[0].paused = True
        restored.configure(restored.config)
        assert restored.store.state(A.session_key)["mode"] == "paused"
    finally:
        restored.close()


def test_schema_seven_upgrade_preserves_an_existing_wait_and_watermarks(v2b):
    ctx = expose(v2b)
    v2b.mark_read(ctx, 1)
    v2b.wait(ctx, 60, purpose="messages", quiet_seconds=3)
    finish(v2b, ctx)
    root = v2b.root
    db = v2b.store.db
    db.execute("DROP TABLE wake_samples")
    db.execute("ALTER TABLE scopes DROP COLUMN wait_infinite")
    db.execute("ALTER TABLE wakes DROP COLUMN source_seq")
    db.execute("ALTER TABLE wakes DROP COLUMN condition_config")
    db.executescript("""
        CREATE TABLE dialogue_waits_v7 (
            scope TEXT PRIMARY KEY REFERENCES scopes(key), purpose TEXT NOT NULL,
            started REAL NOT NULL, quiet_seconds REAL NOT NULL, quiet_due REAL,
            total_due REAL NOT NULL, after_seq INTEGER NOT NULL, source_seq INTEGER NOT NULL,
            on_timeout INTEGER NOT NULL, sleep_seconds REAL, condition_expires REAL
        );
        INSERT INTO dialogue_waits_v7 SELECT scope,purpose,started,quiet_seconds,quiet_due,total_due,
            after_seq,source_seq,on_timeout,sleep_seconds,condition_expires FROM dialogue_waits;
        DROP TABLE dialogue_waits;
        ALTER TABLE dialogue_waits_v7 RENAME TO dialogue_waits;
        PRAGMA user_version=7;
    """)
    db.commit()
    v2b.close()
    upgraded = SocialStore(root, now=v2b.now)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert upgraded.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        state = upgraded.state(A.session_key)
        assert state["mode"] == "waiting" and not state["wait_infinite"]
        assert state["read_through"] == state["seen_through"] == 1
        wait = upgraded.dialogue_status(A.session_key)["wait"]
        assert wait["purpose"] == "messages" and wait["total_due"] == v2b.now() + 60
        assert wait["quiet_seconds"] == 3 and wait["sleep_until"] is None
        assert upgraded.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        upgraded.close()


async def test_scope_coalescing_and_model_override_obey_operator_ceiling(v2b):
    v2b.config.scopes[0].coalesce_seconds = 3
    v2b.configure(v2b.config)
    ctx = context(v2b)
    with pytest.raises(ValueError, match="operator limit"):
        v2b.set_wake(ctx, keywords=["go"], senders=[], on_messages=False, coalesce_seconds=11)
    v2b.set_wake(ctx, keywords=["go"], senders=[], on_messages=False, coalesce_seconds=2)
    finish(v2b, ctx)
    await v2b.observe(event(v2b, text="go"), directed=False)
    assert v2b.bus.inbound.empty()
    clock(v2b).advance(2)
    await v2b.run_due()
    assert v2b.bus.inbound.qsize() == 1


async def test_tools_accept_infinite_wait_and_new_temporary_conditions(v2b):
    ctx = context(v2b)
    with request_context(ctx):
        wake = json.loads(await TelegramSocialTool(v2b, "social_set_wake").execute(
            probability=0.1, on_question=True, sleep_interrupts={"question": True}, coalesce_seconds=1,
        ))
        assert wake["probability"] == 0.1 and wake["on_question"]
        wait = json.loads(await TelegramSocialTool(v2b, "social_wait").execute(infinite=True, sleeping=True))
        assert wait["infinite"] and wait["wake_at"] is None


@pytest.mark.parametrize("section", [
    {"scopes": [{"chatId": -1001, "roleId": "guest", "wakeProbability": -0.1}]},
    {"scopes": [{"chatId": -1001, "roleId": "guest", "wakeProbability": float("nan")}]},
    {"scopes": [{"chatId": -1001, "roleId": "guest", "sleepInterrupts": {"unknown": True}}]},
    {"scopes": [{"chatId": -1001, "roleId": "guest", "coalesceSeconds": 11}]},
    {"maxCoalesceSeconds": 1},
])
def test_invalid_policy_configuration_is_rejected(section):
    with pytest.raises(ValidationError):
        TelegramSocialConfig.model_validate(section)


def test_new_policy_defaults_preserve_legacy_sleeping_and_sampling():
    config = TelegramSocialConfig.model_validate({"scopes": [{"chatId": -1001, "roleId": "guest"}]})
    binding = config.scopes[0]
    assert not binding.wake_on_question and binding.wake_probability == 0
    assert not any(binding.sleep_interrupts.model_dump().values())
    assert binding.coalesce_seconds is None
    assert TelegramSocialConfig.model_validate(config.model_dump(by_alias=True)) == config
