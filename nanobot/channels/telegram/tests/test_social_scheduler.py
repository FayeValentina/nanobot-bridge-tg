"""M2 acceptance with a manual clock, real SQLite and offline channel/agent fakes."""

import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from telegram.error import RetryAfter

from nanobot.agent.tools.context import RequestContext
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.models import (
    SocialDeliveryUnknownError,
    SocialRateLimitError,
)
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_runtime import A, B, agent, channel, message
from nanobot.config.schema import TelegramSocialConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest


class Clock:
    def __init__(self):
        self.value = time.time()

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def social(tmp_path):
    root = tmp_path / "social"
    (root / "roles").mkdir(parents=True)
    (root / "roles" / "guest.md").write_text("A friendly guest", encoding="utf-8")
    config = TelegramSocialConfig.model_validate({
        "enabled": True,
        "repairUnsentFinal": False,
        "scopes": [
            {"chatId": A.chat_id, "threadId": A.thread_id, "roleId": "guest"},
            {"chatId": B.chat_id, "threadId": B.thread_id, "roleId": "guest"},
        ],
    })
    runtime = TelegramSocialRuntime(config, root, MessageBus(), now=Clock())
    yield runtime
    runtime.close()


def clock(social):
    assert isinstance(social.now, Clock)
    return social.now


def observed(social, seq=1, *, scope=A, **kwargs):
    return replace(message(scope, seq=seq, **kwargs), timestamp=social.now())


def context(social, *, scope=A, turn="test-turn"):
    _, version = social._persona(scope)
    job = social.store.wake(scope.session_key, 1, 100, now=social.now())
    ctx = RequestContext(
        channel=scope.channel, chat_id=str(scope.chat_id), session_key=scope.session_key,
        turn_id=turn, metadata={"message_thread_id": scope.thread_id, "_social_job": job},
        attributes={"context_version": version},
    )
    social.admit_request(ctx)
    return ctx


def finish(social, ctx):
    social.store.finish(ctx.turn_id, "completed")
    social.store.release_pending(ctx.session_key)


async def test_short_window_coalesces_direct_addresses_and_includes_plain_messages(social):
    await social.observe(observed(social, text="@bot first"), directed=True)
    await social.observe(observed(social, 2, text="second part"), directed=False)
    await social.observe(observed(social, 3, text="@bot third"), directed=True)
    assert social.bus.inbound.empty()
    clock(social).advance(2)
    await social.run_due()
    assert social.bus.inbound.qsize() == 1
    inbound = social.bus.inbound.get_nowait()
    view = json.loads(inbound.content.split("\n", 1)[1])
    assert [m["seq"] for m in view["messages"]] == [1, 2, 3]
    assert view["wake_reason"] == "directed"
    await social.run_due()
    assert social.bus.inbound.empty()
    assert social.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


@pytest.mark.parametrize("kind,text,sender,reason", [
    ("nicknames", "hello guest", "123", "nickname"),
    ("wake_keywords", "Python is fun", "123", "keyword"),
    ("wake_senders", "hello", "456", "sender"),
    ("wake_on_messages", "ordinary chat", "123", "message"),
])
async def test_operator_trigger_conditions(social, kind, text, sender, reason):
    binding = social.config.scopes[0]
    setattr(binding, kind, {"nicknames": ["Guest"], "wake_keywords": ["python"],
                           "wake_senders": ["456"], "wake_on_messages": True}[kind])
    social.configure(social.config)
    msg = replace(observed(social, text=text), sender_id=sender)
    await social.observe(msg, directed=False)
    clock(social).advance(2)
    await social.run_due()
    view = json.loads(social.bus.inbound.get_nowait().content.split("\n", 1)[1])
    assert view["wake_reason"] == reason


async def test_directed_trigger_upgrades_keyword_without_another_job(social):
    social.config.scopes[0].wake_keywords = ["topic"]
    await social.observe(observed(social, text="topic"), directed=False)
    await social.observe(observed(social, 2), directed=True)
    rows = social.store.db.execute("SELECT * FROM wakes").fetchall()
    assert len(rows) == 1 and rows[0]["reason"] == "directed" and rows[0]["trigger_seq"] == 2


async def test_plain_messages_and_other_bots_never_create_idle_model_loops(social):
    social.config.scopes[0].wake_on_messages = True
    await social.observe(replace(observed(social), is_bot=True), directed=True)
    social.config.scopes[0].wake_on_messages = False
    await social.observe(observed(social, 2), directed=False)
    for _ in range(3):
        clock(social).advance(60)
        await social.run_due()
    assert social.bus.inbound.empty()
    assert social.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


async def test_wait_tool_ends_agent_turn_without_an_extra_model_request(tmp_path, social):
    loop = agent(tmp_path, social, [LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="wait", name="social_wait", arguments={"seconds": 300}),
    ])])
    await social.observe(observed(social), directed=True)
    clock(social).advance(2)
    await social.run_due()
    await loop._dispatch(social.bus.inbound.get_nowait())
    assert loop.provider.chat_stream_with_retry.await_count == 1
    assert social.store.state(A.session_key)["mode"] == "waiting"
    assert social.bus.outbound.empty()
    lock = loop._session_locks.get(A.session_key)
    assert lock is None or not lock.locked()
    await social.observe(observed(social, 2, text="continuation"), directed=False)
    clock(social).advance(11)
    await social.run_due()
    assert social.bus.inbound.qsize() == 1


async def test_sleep_ignores_ambient_but_direct_address_interrupts(social):
    social.config.scopes[0].wake_on_messages = True
    ctx = context(social)
    social.wait(ctx, 300, sleeping=True)
    finish(social, ctx)
    await social.observe(observed(social), directed=False)
    clock(social).advance(15)
    await social.run_due()
    assert social.bus.inbound.empty()
    await social.observe(observed(social, 2), directed=True)
    clock(social).advance(2)
    await social.run_due()
    inbound = social.bus.inbound.get_nowait()
    new_ctx = RequestContext(channel="telegram", chat_id=str(A.chat_id), session_key=A.session_key,
                             turn_id="interrupt", metadata=inbound.metadata,
                             attributes={"context_version": social.store.state(A.session_key)["version"]})
    social.admit_request(new_ctx)
    assert social.store.state(A.session_key)["mode"] == "active"
    assert social.store.state(A.session_key)["wake_at"] is None


@pytest.mark.parametrize("sleeping", [False, True])
@pytest.mark.parametrize("on_timeout", [False, True])
async def test_wait_and_sleep_deadlines_are_one_shot(social, sleeping, on_timeout):
    ctx = context(social)
    social.wait(ctx, 30, sleeping=sleeping, on_timeout=on_timeout)
    finish(social, ctx)
    clock(social).advance(31)
    await social.run_due()
    assert social.bus.inbound.qsize() == int(on_timeout)
    assert social.store.state(A.session_key)["wake_at"] is None
    for _ in range(3):
        clock(social).advance(60)
        await social.run_due()
    assert social.bus.inbound.qsize() == int(on_timeout)


async def test_rearming_timeout_without_new_human_messages_does_not_poll_model(social):
    ctx = context(social)
    social.wait(ctx, 30, on_timeout=True)
    finish(social, ctx)
    clock(social).advance(31)
    await social.run_due()
    inbound = social.bus.inbound.get_nowait()
    followup = RequestContext(channel="telegram", chat_id=str(A.chat_id), session_key=A.session_key,
                              turn_id="followup", metadata=inbound.metadata,
                              attributes={"context_version": social.store.state(A.session_key)["version"]})
    social.admit_request(followup)
    social.wait(followup, 30, on_timeout=True)
    finish(social, followup)
    await social.observe(replace(observed(social), is_bot=True), directed=False)
    clock(social).advance(31)
    await social.run_due()
    assert social.bus.inbound.empty()
    assert social.store.state(A.session_key)["wake_at"] is None


async def test_missed_wait_deadline_does_not_create_a_fresh_stale_wake(social):
    ctx = context(social)
    social.wait(ctx, 30, on_timeout=True)
    finish(social, ctx)
    clock(social).advance(300)
    await social.run_due()
    assert social.bus.inbound.empty()
    assert social.store.state(A.session_key)["mode"] == "observing"
    assert social.store.state(A.session_key)["wake_at"] is None


@pytest.mark.parametrize("seconds", [0, -1, 3601, float("nan"), float("inf")])
def test_wait_cannot_exceed_operator_limits(social, seconds):
    ctx = context(social)
    with pytest.raises(ValueError):
        social.wait(ctx, seconds)


async def test_wait_blocks_later_sends_in_the_same_turn(social):
    ctx = context(social)
    social.wait(ctx, 30)
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    with pytest.raises(PermissionError, match="yielded"):
        await social.send(ctx, "late send", None)
    sender.assert_not_awaited()


async def test_model_conditions_are_scoped_and_operator_can_forbid_them(social):
    ctx = context(social)
    social.set_wake(ctx, keywords=["python"], senders=[], on_messages=False)
    finish(social, ctx)
    await social.observe(observed(social, scope=B, text="python"), directed=False)
    assert social.bus.inbound.empty()
    await social.observe(observed(social, 2, text="python"), directed=False)
    clock(social).advance(11)
    await social.run_due()
    assert social.bus.inbound.qsize() == 1
    social.config.scopes[1].allow_ambient_wake = False
    other = context(social, scope=B, turn="other")
    with pytest.raises(PermissionError):
        social.set_wake(other, keywords=[], senders=[], on_messages=True)


@pytest.mark.parametrize("keywords,senders", [([""], []), (["x" * 81], []), ([], ["*"]), (["x"] * 21, [])])
def test_model_wake_configuration_is_validated(social, keywords, senders):
    with pytest.raises(ValidationError):
        social.set_wake(context(social), keywords=keywords, senders=senders, on_messages=False)


async def test_busy_addresses_merge_and_survive_a_simultaneous_sleep(social):
    ctx = context(social)
    await social.observe(observed(social), directed=True)
    await social.observe(observed(social, 2), directed=True)
    clock(social).advance(2)
    await social.run_due()
    assert social.bus.inbound.empty()
    social.wait(ctx, 300, sleeping=True)
    finish(social, ctx)
    clock(social).advance(10)
    await social.run_due()
    assert social.bus.inbound.qsize() == 1
    assert social.store.state(A.session_key)["read_through"] == 0
    job = social.store.db.execute("SELECT * FROM wakes WHERE status='queued'").fetchone()
    assert job["trigger_seq"] == 2


async def test_busy_trigger_read_by_the_current_model_does_not_run_another_turn(tmp_path, social):
    loop = agent(tmp_path, social, [])
    calls = 0

    async def respond(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await social.observe(observed(social, 2, text="new direct address"), directed=True)
            return LLMResponse(content=None, tool_calls=[
                ToolCallRequest(id="read", name="social_read_messages", arguments={}),
            ])
        return LLMResponse(content="")

    loop.provider.chat_stream_with_retry = AsyncMock(side_effect=respond)
    await social.observe(observed(social), directed=True)
    clock(social).advance(2)
    await social.run_due()
    await loop._dispatch(social.bus.inbound.get_nowait())
    clock(social).advance(20)
    await social.run_due()
    assert calls == 2 and social.bus.inbound.empty()
    assert social.store.db.execute("SELECT COUNT(*) FROM wakes WHERE status='completed'").fetchone()[0] == 2


async def test_sleep_discards_an_unseen_ambient_trigger_but_preserves_direct_address(social):
    social.config.scopes[0].wake_on_messages = True
    ctx = context(social)
    await social.observe(observed(social), directed=False)
    social.wait(ctx, 300, sleeping=True)
    finish(social, ctx)
    clock(social).advance(20)
    await social.run_due()
    assert social.bus.inbound.empty()


async def test_cooldown_and_hourly_limit_delay_a_live_address(social):
    social.config.max_requests_per_hour = 1
    ctx = context(social)
    finish(social, ctx)
    clock(social).advance(3590)
    await social.observe(observed(social), directed=True)
    clock(social).advance(2)
    await social.run_due()
    assert social.bus.inbound.empty()
    clock(social).advance(9)
    await social.run_due()
    assert social.bus.inbound.qsize() == 1
    assert social.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


async def test_expired_address_is_not_replayed_after_budget_delay(social):
    social.config.max_requests_per_hour = 1
    ctx = context(social)
    finish(social, ctx)
    await social.observe(observed(social), directed=True)
    clock(social).advance(121)
    await social.run_due()
    clock(social).advance(3600)
    await social.run_due()
    assert social.bus.inbound.empty()
    assert social.store.state(A.session_key)["next_seq"] == 2


async def test_expired_execution_lease_does_not_leave_a_permanently_busy_scope(social):
    old = context(social)
    clock(social).advance(121)
    await social.observe(observed(social), directed=True)
    clock(social).advance(2)
    await social.run_due()
    assert social.bus.inbound.qsize() == 1
    with pytest.raises(PermissionError):
        social.validate_context(old)


async def test_pause_invalidates_running_tools_timers_and_pending_addresses(social):
    ctx = context(social)
    social.wait(ctx, 30, on_timeout=True)
    await social.observe(observed(social), directed=True)
    social.config.scopes[0].paused = True
    social.configure(social.config)
    clock(social).advance(40)
    await social.run_due()
    assert social.bus.inbound.empty()
    assert social.store.state(A.session_key)["mode"] == "paused"
    with pytest.raises(PermissionError):
        social.validate_context(ctx)
    social.config.scopes[0].paused = False
    social.configure(social.config)
    await social.observe(observed(social, 2), directed=True)
    clock(social).advance(2)
    await social.run_due()
    assert social.bus.inbound.qsize() == 1


async def test_restart_restores_wait_deadline_once_without_replaying_provider_state(social):
    ctx = context(social)
    social.wait(ctx, 30, on_timeout=True)
    finish(social, ctx)
    config, root, now = social.config, social.root, social.now
    social.close()
    restored = TelegramSocialRuntime(config, root, MessageBus(), now=now)
    try:
        assert restored.store.state(A.session_key)["mode"] == "waiting"
        clock(restored).advance(31)
        await restored.recover()
        assert restored.bus.inbound.qsize() == 1
        await restored.recover()
        assert restored.bus.inbound.qsize() == 1
    finally:
        await restored.scheduler.stop()
        restored.close()


async def test_restart_after_wait_decision_does_not_replay_the_unfinished_old_turn(social):
    ctx = context(social)
    social.wait(ctx, 30, on_timeout=True)
    # Crash before finish(): the persisted decision must still yield the old run.
    config, root, now = social.config, social.root, social.now
    social.close()
    restored = TelegramSocialRuntime(config, root, MessageBus(), now=now)
    try:
        await restored.recover()
        assert restored.bus.inbound.empty()
        assert restored.store.state(A.session_key)["mode"] == "waiting"
        clock(restored).advance(31)
        await restored.run_due()
        assert restored.bus.inbound.qsize() == 1
    finally:
        await restored.scheduler.stop()
        restored.close()


async def test_observation_dedup_and_wake_creation_commit_together(social):
    social.store.db.execute("CREATE TRIGGER reject_wake BEFORE INSERT ON wakes "
                            "BEGIN SELECT RAISE(ABORT,'simulated disk error'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await social.observe(observed(social), directed=True)
    assert social.store.state(A.session_key)["next_seq"] == 1
    assert social.store.db.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == 0
    assert social.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    social.store.db.execute("DROP TRIGGER reject_wake")
    await social.observe(observed(social), directed=True)
    clock(social).advance(2)
    await social.run_due()
    assert social.bus.inbound.qsize() == 1


async def test_same_turn_repeated_send_returns_the_same_receipt(social):
    ctx = context(social)
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    first = await social.send(ctx, "hello", None)
    second = await social.send(ctx, "hello", None)
    assert first["action_id"] == second["action_id"] and second["message_id"] == 501
    sender.assert_awaited_once()
    with pytest.raises(PermissionError, match="cooldown"):
        await social.send(ctx, "another message", None)


async def test_platform_429_retry_uses_same_action_without_an_agent_or_new_model_request(social):
    tg = channel(social)
    tg._app.bot.send_message = AsyncMock(side_effect=[RetryAfter(5), SimpleNamespace(message_id=501)])
    await social.observe(observed(social), directed=False)
    result = await social.send(context(social), "public text", 1)
    assert result["status"] == "queued"
    clock(social).advance(4)
    await social.run_due()
    assert tg._app.bot.send_message.await_count == 1
    clock(social).advance(2)
    await social.run_due()
    assert tg._app.bot.send_message.await_count == 2
    assert social.bus.inbound.empty() and social.bus.outbound.empty()
    row = social.store.db.execute("SELECT * FROM actions").fetchone()
    assert row["id"] == result["action_id"] and row["status"] == "sent" and row["attempts"] == 2
    assert tg._app.bot.send_message.await_args.kwargs["message_thread_id"] == A.thread_id
    assert tg._app.bot.send_message.await_args.kwargs["reply_parameters"].message_id == 1


async def test_burst_sends_paced_parts_with_individual_receipts_and_no_replay(social):
    sender = AsyncMock(side_effect=[501, 502, 503])
    social.set_sender(sender)
    ctx = context(social)
    queued = await social.send_parts(ctx, ["first", "second", "third"], None)
    assert [row["status"] for row in queued] == ["queued"] * 3
    await social.run_due()
    assert sender.await_count == 1
    for _ in range(2):
        clock(social).advance(2)
        await social.run_due()
    assert [call.args[1] for call in sender.await_args_list] == ["first", "second", "third"]
    repeated = await social.send_parts(ctx, ["first", "second", "third"], None)
    assert [row["action_id"] for row in repeated] == [row["action_id"] for row in queued]
    assert [row["message_id"] for row in repeated] == [501, 502, 503]
    await social.run_due()
    assert sender.await_count == 3


@pytest.mark.parametrize("error,status", [(ValueError("rejected"), "failed"), (TimeoutError(), "unknown")])
async def test_partial_burst_failure_never_replays_the_confirmed_part(social, error, status):
    sender = AsyncMock(side_effect=[501, error])
    social.set_sender(sender)
    ctx = context(social)
    await social.send_parts(ctx, ["first", "second", "third"], None)
    await social.run_due()
    clock(social).advance(2)
    await social.run_due()
    rows = await social.send_parts(ctx, ["first", "second", "third"], None)
    assert [row["status"] for row in rows] == ["sent", status, "failed"]
    clock(social).advance(10)
    await social.run_due()
    assert sender.await_count == 2


async def test_rate_limited_burst_cannot_send_later_parts_first(social):
    sender = AsyncMock(side_effect=[SocialRateLimitError(5), 501, 502])
    social.set_sender(sender)
    await social.send_parts(context(social), ["first", "second"], None)
    await social.run_due()
    clock(social).advance(3)
    await social.run_due()
    assert sender.await_count == 1
    clock(social).advance(3)
    await social.run_due()
    assert sender.await_count == 2
    clock(social).advance(2)
    await social.run_due()
    assert [call.args[1] for call in sender.await_args_list] == ["first", "first", "second"]


@pytest.mark.parametrize("texts", [["one"], ["x"] * 4, ["repeat", "repeat"], ["valid", ""]])
async def test_invalid_burst_is_rejected_before_enqueue(social, texts):
    social.set_sender(AsyncMock(return_value=501))
    with pytest.raises(ValueError):
        await social.send_parts(context(social), texts, None)
    assert social.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


async def test_burst_cannot_reserve_more_than_remaining_budget(social):
    social.config.max_sends_per_hour = 1
    social.set_sender(AsyncMock(return_value=501))
    with pytest.raises(PermissionError, match="all parts"):
        await social.send_parts(context(social), ["one", "two"], None)
    assert social.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


@pytest.mark.parametrize("limit,after,expected_calls", [(6, 500, 1), (1, 2, 1), (6, 2, 3)])
async def test_platform_retries_are_bounded_by_expiry_budget_and_attempts(social, limit, after, expected_calls):
    social.config.max_sends_per_hour = limit
    sender = AsyncMock(side_effect=SocialRateLimitError(after))
    social.set_sender(sender)
    await social.send(context(social), "public", None)
    for _ in range(4):
        clock(social).advance(3)
        await social.run_due()
    assert sender.await_count == expected_calls
    assert social.store.db.execute("SELECT status FROM actions").fetchone()[0] == "failed"


async def test_send_retry_attempts_still_count_after_the_original_action_ages_out(social):
    social.config.max_sends_per_hour = 2
    sender = AsyncMock(side_effect=[SocialRateLimitError(100), 501, 502])
    social.set_sender(sender)
    await social.send(context(social), "first", None)
    clock(social).advance(100)
    await social.run_due()
    clock(social).advance(3501)
    new_ctx = context(social, turn="new-hour")
    assert social.get_state(new_ctx)["remaining_send_attempts"] == 1
    assert (await social.send(new_ctx, "second", None))["status"] == "sent"
    clock(social).advance(2)
    with pytest.raises(PermissionError, match="budget"):
        await social.send(new_ctx, "third", None)
    assert sender.await_count == 3


@pytest.mark.parametrize("change", ["paused", "binding", "revoke"])
async def test_configuration_change_cancels_a_platform_retry(social, change):
    sender = AsyncMock(side_effect=SocialRateLimitError(5))
    social.set_sender(sender)
    await social.send(context(social), "public", None)
    if change == "paused":
        social.config.scopes[0].paused = True
    elif change == "binding":
        (social.roles / "other.md").write_text("other persona", encoding="utf-8")
        social.config.scopes[0].role_id = "other"
    else:
        social.config.scopes = []
    social.configure(social.config)
    clock(social).advance(6)
    await social.run_due()
    sender.assert_awaited_once()
    assert social.store.db.execute("SELECT status FROM actions").fetchone()[0] == "failed"


async def test_unknown_platform_result_never_retries(social):
    sender = AsyncMock(side_effect=SocialDeliveryUnknownError())
    social.set_sender(sender)
    result = await social.send(context(social), "possibly delivered", None)
    assert result["status"] == "unknown"
    clock(social).advance(10)
    await social.run_due()
    sender.assert_awaited_once()


async def test_restart_never_retries_interrupted_platform_actions(social):
    sender = AsyncMock(side_effect=SocialRateLimitError(5))
    social.set_sender(sender)
    await social.send(context(social), "public", None)
    await social.recover()
    clock(social).advance(6)
    await social.run_due()
    sender.assert_awaited_once()
    assert social.store.db.execute("SELECT status FROM actions").fetchone()[0] == "unknown"


async def test_worker_starts_once_and_can_be_stopped_without_leaking_timers(social):
    await social.recover()
    task = social.scheduler.task
    await social.recover()
    assert social.scheduler.task is task
    await social.scheduler.stop()
    assert task.done() and social.scheduler.task is None


def test_schema_v1_migration_preserves_watermarks_and_delivery_journal(tmp_path):
    root = tmp_path / "v1"
    root.mkdir()
    db = sqlite3.connect(root / "social.sqlite3")
    db.executescript("""
        CREATE TABLE scopes(key TEXT PRIMARY KEY,channel TEXT,chat_id INTEGER,thread_id INTEGER,
            version TEXT,next_seq INTEGER,read_through INTEGER,seen_through INTEGER);
        CREATE TABLE wakes(id TEXT PRIMARY KEY,scope TEXT,version TEXT,trigger_seq INTEGER,status TEXT,
            created REAL,expires REAL,turn_id TEXT);
        CREATE TABLE actions(id TEXT PRIMARY KEY,scope TEXT,version TEXT,content TEXT,status TEXT,
            created REAL,message_id INTEGER,error TEXT);
        CREATE TABLE messages(scope TEXT,seq INTEGER,update_id INTEGER,message_id INTEGER,
            sender_id TEXT,display_name TEXT,at REAL,text TEXT,reply_to INTEGER,media TEXT,is_self INTEGER);
        INSERT INTO scopes VALUES('telegram:-1001:topic:42','telegram',-1001,42,'v1',7,4,5);
        INSERT INTO actions VALUES('old','telegram:-1001:topic:42','v1','hello','sent',1,501,NULL);
        PRAGMA user_version=1;
    """)
    db.close()
    store = SocialStore(root)
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 6
        state = store.state(A.session_key)
        assert (state["next_seq"], state["read_through"], state["seen_through"]) == (7, 4, 5)
        assert state["mode"] == "observing"
        assert store.db.execute("SELECT content,message_id FROM actions").fetchone()[:] == ("hello", 501)
    finally:
        store.close()


@pytest.mark.parametrize("values", [
    {"coalesceSeconds": -1}, {"cooldownSeconds": -1}, {"maxWaitSeconds": 86401},
    {"scopes": [{"chatId": -1001, "roleId": "guest", "wakeKeywords": [""]}]},
])
def test_new_configuration_fields_reject_invalid_values(values):
    with pytest.raises(ValidationError):
        TelegramSocialConfig.model_validate(values)
