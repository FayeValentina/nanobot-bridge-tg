"""Legacy waiting/tool import contracts; V3 model-gate tests replace retired triggers."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from nanobot.agent.tools.context import request_context
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.channels.telegram.social.store import SocialStore
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
    remove_v4_columns,
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
    assert view["wake_reason"] == "judge"


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
    assert view["wake_reason"] == "judge"
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
    restored = TelegramSocialRuntime(config, root, bus, now=now)
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
    remove_v4_columns(db)
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
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 12
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
            probability=0, on_question=True, sleep_interrupts={"question": True}, coalesce_seconds=1,
        ))
        assert wake["probability"] == 0 and wake["on_question"]
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
