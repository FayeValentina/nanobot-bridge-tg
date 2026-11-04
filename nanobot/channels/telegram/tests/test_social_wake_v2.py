"""Legacy waiting/tool import contracts; V3 model-gate tests replace retired triggers."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from nanobot.agent.tools.context import request_context
from nanobot.agent.tools.telegram_social import SOCIAL_TOOL_CLASSES
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.channels.telegram.tests.test_social_dialogue import admit, expose, take
from nanobot.channels.telegram.tests.test_social_runtime import (
    A,
    agent,
)
from nanobot.channels.telegram.tests.test_social_scheduler import (
    clock,
    context,
    finish,
    observed,
    social,
)
from nanobot.config.schema import TelegramSocialConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest

__all__ = ["social"]


@pytest.fixture
def v2b(social):
    social.config.coalesce_seconds = 0
    social.config.cooldown_seconds = 0
    social.config.max_requests_per_hour = 120
    social.random_source = Mock(return_value=0.9)
    social.configure(social.config)
    return social


def event(runtime, seq=1, text="ordinary chat", **kwargs):
    return replace(observed(runtime, seq, text=text), bot_username="test_bot", **kwargs)


def until(runtime, seconds):
    return datetime.fromtimestamp(runtime.now() + seconds, timezone.utc).isoformat()
























































async def test_infinite_wait_has_no_timer_or_lock_and_direct_address_interrupts(tmp_path, v2b):
    loop = agent(tmp_path, v2b, [LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="wait", name="social_wait", arguments={"infinite": True, "sleeping": True}),
    ])])
    await v2b.observe(event(v2b, text="@bot hello"), directed=True)
    await loop._dispatch(v2b.bus.inbound.get_nowait())
    state = v2b.store.scopes.state(A.session_key)
    assert state["wait_infinite"] and state["wake_at"] is None and state["mode"] == "sleeping"
    lock = loop._session_locks.get(A.session_key)
    assert lock is None or not lock.locked()
    assert loop.provider.chat_stream_with_retry.await_count == 1
    await v2b.observe(event(v2b, 2, text="@bot wake"), directed=True)
    msg, _ = take(v2b)
    admit(v2b, msg)
    assert not v2b.store.scopes.state(A.session_key)["wait_infinite"]


async def test_infinite_temporary_wait_expires_back_to_broad_admission(v2b):
    ctx = context(v2b)
    v2b.waiting.set_conditions(ctx, topics=["temporary"], members=[], questions=False, one_shot=True, ttl_seconds=5)
    v2b.waiting.wait(ctx, infinite=True, sleeping=True)
    finish(v2b, ctx)
    assert v2b.store.wakes.next_due() == v2b.now() + 5
    clock(v2b).advance(5)
    await v2b.run_due()
    assert v2b.store.scopes.state(A.session_key)["wake_config"] == "{}"
    assert v2b.store.scopes.state(A.session_key)["mode"] == "observing"
    await v2b.observe(event(v2b, text="base"), directed=False)
    _, view = take(v2b)
    assert view["wake_reason"] == "judge"


async def test_infinite_messages_ttl_clears_both_wait_intent_and_infinite_flag(v2b):
    ctx = expose(v2b)
    v2b.waiting.set_conditions(ctx, topics=["temporary"], members=[], questions=False, one_shot=True, ttl_seconds=5)
    v2b.waiting.wait(ctx, infinite=True, purpose="messages")
    finish(v2b, ctx)
    clock(v2b).advance(5)
    await v2b.run_due()
    state = v2b.store.scopes.state(A.session_key)
    assert state["mode"] == "observing" and not state["wait_infinite"]
    assert v2b.store.wakes.dialogue_status(A.session_key)["wait"] is None
    assert v2b.bus.inbound.empty()


async def test_infinite_message_wait_starts_a_bounded_burst_on_first_new_message(v2b):
    ctx = expose(v2b)
    v2b.waiting.wait(ctx, infinite=True, purpose="messages", quiet_seconds=3)
    finish(v2b, ctx)
    clock(v2b).advance(1000)
    await v2b.run_due()
    assert v2b.bus.inbound.empty()
    assert v2b.store.wakes.dialogue_status(A.session_key)["wait"]["total_due"] is None
    await v2b.observe(event(v2b, 2), directed=False)
    first = v2b.now()
    for seq in range(3, 15):
        clock(v2b).advance(1)
        await v2b.observe(event(v2b, seq), directed=False)
        await v2b.run_due()
    _, view = take(v2b)
    assert view["wake_reason"] == "judge"
    assert view["dialogue"]["total_due"] == first + v2b.config.dynamic_wait.max_seconds
    assert view["dialogue"]["quiet"] is False


async def test_infinite_sleep_observation_completes_locally(v2b):
    v2b.config.sleep_observation.enabled = True
    v2b.configure(v2b.config)
    ctx = expose(v2b)
    result = v2b.waiting.wait(ctx, infinite=True, sleeping=True)
    assert result["purpose"] == "observe"
    finish(v2b, ctx)
    clock(v2b).advance(300)
    await v2b.run_due()
    state = v2b.store.scopes.state(A.session_key)
    assert state["mode"] == "sleeping" and state["wait_infinite"] and state["wake_at"] is None
    assert v2b.bus.inbound.empty()


async def test_absolute_wait_does_not_move_its_deadline_for_sleep_observation(v2b):
    v2b.config.sleep_observation.enabled = True
    v2b.configure(v2b.config)
    ctx = expose(v2b)
    end = v2b.now() + 30
    result = v2b.waiting.wait(ctx, until=until(v2b, 30), sleeping=True, on_timeout=True)
    assert result["total_due"] == pytest.approx(end)
    finish(v2b, ctx)
    clock(v2b).advance(31)
    await v2b.run_due()
    _, view = take(v2b)
    assert view["wake_reason"] == "timeout"
    assert v2b.store.scopes.state(A.session_key)["wake_at"] is None


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
        v2b.waiting.wait(ctx, **args)
    assert v2b.store.scopes.state(A.session_key)["mode"] == "active"
    assert not ctx.attributes.get("social_deferred")


def test_absolute_deadline_accepts_explicit_non_utc_offset(v2b):
    from datetime import timedelta
    ctx = context(v2b)
    end = datetime.fromtimestamp(v2b.now() + 30, timezone(timedelta(hours=8))).isoformat()
    result = v2b.waiting.wait(ctx, until=end)
    assert result["wake_at"] == pytest.approx(v2b.now() + 30)


async def test_new_wait_modes_and_sampling_recover_without_redrawing(v2b):
    ctx = context(v2b)
    v2b.waiting.wait(ctx, infinite=True, sleeping=True)
    root, config, now, bus = v2b.root, v2b.config, v2b.now, v2b.bus
    v2b.close()
    restored = TelegramSocialRuntime(config, root, bus, now=now)
    try:
        restored.store.recover()
        await restored.run_due()
        state = restored.store.scopes.state(A.session_key)
        assert state["mode"] == "sleeping" and state["wait_infinite"] and state["wake_at"] is None
        assert bus.inbound.empty()
        restored.config.scopes[0].paused = True
        restored.configure(restored.config)
        assert restored.store.scopes.state(A.session_key)["mode"] == "paused"
    finally:
        restored.close()


async def test_scope_coalescing_obeys_operator_setting(v2b):
    v2b.config.scopes[0].coalesce_seconds = 3
    v2b.configure(v2b.config)
    ctx = context(v2b)
    finish(v2b, ctx)
    await v2b.observe(event(v2b, text="go"), directed=False)
    assert v2b.bus.inbound.empty()
    clock(v2b).advance(3)
    await v2b.run_due()
    assert v2b.bus.inbound.qsize() == 1


async def test_tools_accept_infinite_wait_and_new_temporary_conditions(v2b):
    ctx = context(v2b)
    with request_context(ctx):
        wait = json.loads(await SOCIAL_TOOL_CLASSES["social_wait"](v2b).execute(
            infinite=True, sleeping=True, until_condition={"questions": True},
        ))
        assert wait["until_condition"]["questions"]
        assert wait["infinite"] and wait["wake_at"] is None


@pytest.mark.parametrize("section", [
    {"scopes": [{"chatId": -1001, "roleId": "guest", "coalesceSeconds": 11}]},
    {"maxCoalesceSeconds": 1},
])
def test_invalid_policy_configuration_is_rejected(section):
    with pytest.raises(ValidationError):
        TelegramSocialConfig.model_validate(section)


def test_policy_defaults_round_trip():
    config = TelegramSocialConfig.model_validate({"scopes": [{"chatId": -1001, "roleId": "guest"}]})
    binding = config.scopes[0]
    assert binding.coalesce_seconds is None
    assert TelegramSocialConfig.model_validate(config.model_dump(by_alias=True)) == config


def test_stored_wake_conditions_ignore_retired_keys(v2b):
    raw = json.dumps({
        "keywords": ["到家"], "senders": ["456"], "on_question": True,
        "one_shot": False, "expires_at": v2b.now() + 60,
        "probability": 0.05, "sleep_interrupts": {"question": True},
        "coalesce_seconds": 9, "on_messages": True,
    })
    v2b.store.wakes.set_wake_config(A.session_key, raw)
    conditions, dynamic = v2b.waiting.conditions(A.session_key)
    assert dynamic
    assert conditions.model_dump() == {
        "keywords": ["到家"], "senders": ["456"], "on_question": True,
        "one_shot": False, "expires_at": v2b.now() + 60,
    }
    assert v2b.store.scopes.state(A.session_key)["wake_config"] == raw


def test_deployed_social_config_validates_after_retired_keys_are_removed():
    config = TelegramSocialConfig.model_validate({
        "enabled": True,
        "scopes": [{
            "chatId": -1003760551468, "roleId": "groupmate", "allTopics": True,
            "nicknames": ["肥鱼", "肥魚"],
            "proactive": {
                "enabled": True, "minIntervalSeconds": 1800, "maxIntervalSeconds": 5400,
                "probability": 0.6, "timezone": "Asia/Tokyo", "nightMultiplier": 1.0,
                "activityEnabled": True, "activityIntervalSeconds": 3600,
                "activityMaxIterations": 4,
            },
            "quietHours": {"enabled": False, "timezone": "Asia/Tokyo"},
        }],
        "maxRequestsPerHour": 120, "maxSendsPerHour": 60, "repairUnsentFinal": True,
        "dynamicWait": {"quietSeconds": 10, "maxSeconds": 30},
        "sleepObservation": {"enabled": True, "windowSeconds": 300},
        "coalesceSeconds": 10,
        "learning": {"catchphrases": ["本大爷", "白饭", "Token", "算力"]},
    })
    assert config.enabled
    assert config.dynamic_wait.quiet_seconds == 10
    assert config.dynamic_wait.max_seconds == 30
    assert config.scopes[0].nicknames == ["肥鱼", "肥魚"]


def test_wake_probability_must_be_removed_before_deployment():
    with pytest.raises(ValidationError) as exc:
        TelegramSocialConfig.model_validate({
            "scopes": [{"chatId": -1001, "roleId": "guest", "wakeProbability": 0.05}],
        })
    assert [(error["loc"], error["type"]) for error in exc.value.errors()] == [
        (("scopes", 0, "wakeProbability"), "extra_forbidden"),
    ]


async def test_wait_sets_member_condition_and_empty_condition_clears_it(v2b):
    ctx = context(v2b)
    tool = SOCIAL_TOOL_CLASSES["social_wait"](v2b)
    with request_context(ctx):
        result = json.loads(await tool.execute(
            seconds=30, purpose="messages", until_condition={"members": ["456"]},
        ))
    assert result["deferred"]
    assert result["until_condition"] == {
        "members": ["456"], "topics": [], "questions": False, "one_shot": True,
        "expires_at": v2b.now() + 3600,
    }
    conditions, dynamic = v2b.waiting.conditions(A.session_key)
    assert dynamic and conditions.senders == ["456"]
    wait = v2b.store.wakes.dialogue_status(A.session_key)["wait"]
    assert wait["purpose"] == "messages" and wait["total_due"] == v2b.now() + 30
    finish(v2b, ctx)
    next_ctx = context(v2b, turn="clear-condition")
    with request_context(next_ctx):
        cleared = json.loads(await tool.execute(infinite=True, until_condition={}))
    assert cleared["until_condition"] == {"cleared": True}
    assert v2b.store.scopes.state(A.session_key)["wake_config"] == "{}"
    assert cleared["infinite"]
