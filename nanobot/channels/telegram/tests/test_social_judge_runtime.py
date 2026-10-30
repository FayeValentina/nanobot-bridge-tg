"""V3-B message-pool integration: one semantic authority, durable bounded scheduling."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from telegram import Update

from nanobot.channels.telegram.social.judge_models import JudgeSnapshot
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.channels.telegram.tests.test_social_runtime import A, B, channel, telegram_update
from nanobot.channels.telegram.tests.test_social_scheduler import (
    clock,
    context,
    finish,
    observed,
    social,
)
from nanobot.providers.base import LLMProvider, LLMResponse

__all__ = ["social"]


def decide(runtime, decision="wake", *, fulfills=False, error=False):
    provider = AsyncMock(spec=LLMProvider)

    async def respond(**kwargs):
        snapshot = JudgeSnapshot.model_validate_json(kwargs["messages"][1]["content"])
        if error:
            return LLMResponse(content=None, finish_reason="error")
        source = snapshot.new_seqs[-1:] or tuple(message.seq for message in snapshot.messages if not message.is_self)[-1:]
        return LLMResponse(content=json.dumps({"decision":decision,"through_seq":snapshot.through_seq,
                                              "source_seqs":list(source) if decision=="wake" else [],
                                              "reason":"canned integration outcome", "fulfills_intent":fulfills}))

    provider.chat.side_effect = respond
    runtime.judging.set_provider(provider, "integration-provider")
    return provider


async def due(runtime, seconds=2):
    clock(runtime).advance(seconds)
    await runtime.run_due()


@pytest.mark.parametrize("text,directed", [("肥鱼呢",False),("你猜猜我给你准备了什么",False),("@bot hello",True),("ordinary chatter",False)])
async def test_every_new_batch_reaches_model_even_with_legacy_probability_zero(social,text,directed):
    social.config.scopes[0].wake_probability=0
    provider=decide(social)
    await social.observe(observed(social,text=text),directed=directed)
    await due(social)
    provider.chat.assert_awaited_once()
    inbound=social.bus.inbound.get_nowait()
    view=json.loads(inbound.content.split("\n",1)[1])
    assert view["wake_reason"]=="judge" and view["judgement"][0]["decision"]["decision"]=="wake"
    assert social.store.state(A.session_key)["seen_through"]==0
    assert social.store.db.execute("SELECT COUNT(*) FROM wake_samples").fetchone()[0]==0


@pytest.mark.parametrize("directed", [False,True])
async def test_even_explicit_addresses_have_no_parallel_local_wake_path(social,directed):
    social.config.scopes[0].nicknames=["肥鱼"]
    social.config.scopes[0].wake_probability=1
    provider=decide(social,"observe")
    await social.observe(observed(social,text="肥鱼呢"),directed=directed)
    await due(social)
    provider.chat.assert_awaited_once()
    assert social.bus.inbound.empty()
    assert social.store.judge_through(A.session_key)==1
    assert social.store.state(A.session_key)["read_through"]==0


@pytest.mark.parametrize("excluded",["duplicate","edit","self","bot","old","future","paused"])
async def test_no_new_model_call_for_ineligible_events(social,excluded):
    provider=decide(social,"observe")
    message=observed(social)
    if excluded=="duplicate":
        await social.observe(message,directed=True)
        await due(social)
        provider.chat.reset_mock()
    elif excluded=="edit":
        message=replace(message,is_edit=True)
    elif excluded=="self":
        message=replace(message,is_self=True)
    elif excluded=="bot":
        message=replace(message,is_bot=True)
    elif excluded=="old":
        message=replace(message,timestamp=social.now()-121)
    elif excluded=="future":
        message=replace(message,timestamp=social.now()+2)
    else:
        social.config.scopes[0].paused=True
        social.configure(social.config)
    await social.observe(message,directed=True)
    await due(social)
    provider.chat.assert_not_awaited()


async def test_continuous_messages_are_batched_with_a_fixed_total_cap(social):
    provider=decide(social)
    social.config.dynamic_wait.max_seconds=4
    for seq in range(1,6):
        await social.observe(observed(social,seq,text=f"fragment {seq}"),directed=False)
        if seq<5:
            await due(social,1)
    await social.run_due()
    provider.chat.assert_awaited_once()
    snapshot=JudgeSnapshot.model_validate_json(provider.chat.call_args.kwargs["messages"][1]["content"])
    assert snapshot.new_seqs==(1,2,3,4)
    assert social.store.state(A.session_key)["next_seq"]==6
    assert social.bus.inbound.qsize()==1


@pytest.mark.parametrize("creator", [456, 999])
async def test_topic_root_is_not_presented_as_a_member_reply(social, creator):
    provider=decide(social)
    tg=channel(social)
    data=telegram_update(text="肥鱼呢",reply=True).to_dict()
    data["message"]["date"]=int(social.now())
    quote=data["message"]["reply_to_message"]
    quote.update(message_id=A.thread_id,text="",from_user=None)
    quote["from"]={"id":creator,"is_bot":creator==999,"first_name":"Creator"}
    await tg._observe_social(Update.de_json(data,None))
    await due(social)
    snapshot=JudgeSnapshot.model_validate_json(provider.chat.call_args.kwargs["messages"][1]["content"])
    assert snapshot.messages[-1].reply.kind=="topic_root"
    assert not snapshot.messages[-1].mentions_bot


async def test_judge_latency_does_not_hold_receiver_or_cycle_lock(social):
    provider=decide(social)
    entered,release=asyncio.Event(),asyncio.Event()
    original=provider.chat.side_effect

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    provider.chat.side_effect=delayed
    await social.observe(observed(social),directed=True)
    await due(social)
    await entered.wait()
    assert not social._cycle_lock.locked()
    await social.observe(observed(social,2,text="more"),directed=False)
    assert social.store.state(A.session_key)["next_seq"]==3
    release.set()
    await asyncio.gather(*list(social.judging.tasks.values()))
    assert social.store.judge_through(A.session_key)==1
    assert social.store.state(A.session_key)["read_through"]==0


async def test_network_failures_make_one_bounded_fallback_without_legacy_triggers(social):
    social.config.judge.retry_delay_seconds=0
    provider=decide(social,error=True)
    await social.observe(observed(social,text="plain input"),directed=False)
    await due(social)
    await social.run_due()
    await social.run_due()
    assert provider.chat.await_count==2
    assert social.bus.inbound.qsize()==1
    row=social.store.db.execute("SELECT status,attempts,handoff_job FROM judge_batches").fetchone()
    assert tuple(row)[:2]==("fallback",2) and row[2]
    await social.run_due()
    assert provider.chat.await_count==2 and social.bus.inbound.qsize()==1


async def test_edit_during_judgement_invalidates_even_unchanged_text(social):
    provider = decide(social)
    original = provider.chat.side_effect
    release = asyncio.Event()

    async def delayed(**kwargs):
        await release.wait()
        return await original(**kwargs)

    provider.chat.side_effect = delayed
    message = observed(social, text="hello")
    await social.observe(message, directed=True)
    await due(social)
    await social.observe(replace(message, update_id=99, is_edit=True), directed=True)
    release.set()
    await asyncio.gather(*list(social.judging.tasks.values()))
    await social.run_due()
    assert social.bus.inbound.empty()
    assert social.store.db.execute("SELECT status FROM judge_batches").fetchone()[0] == "cancelled"
    assert social.store.state(A.session_key)["read_through"] == 0


async def test_decision_and_main_handoff_commit_in_one_transaction(social):
    social.config.judge.retry_delay_seconds=0
    provider=decide(social)
    social.store.db.execute("CREATE TRIGGER fail_handoff BEFORE INSERT ON wakes BEGIN SELECT RAISE(ABORT,'test'); END")
    social.store.db.commit()
    await social.observe(observed(social),directed=True)
    await due(social)
    assert social.store.judge_through(A.session_key)==0
    assert social.store.db.execute("SELECT status FROM judge_batches").fetchone()[0]=="pending"
    assert social.bus.inbound.empty()
    social.store.db.execute("DROP TRIGGER fail_handoff")
    social.store.db.commit()
    await due(social,1)
    assert social.bus.inbound.qsize()==1 and provider.chat.await_count==2


async def test_two_topics_keep_separate_context_and_main_routes(social):
    provider=decide(social)
    await social.observe(observed(social,1,scope=A,text="first topic"),directed=False)
    await social.observe(observed(social,2,scope=B,text="second topic"),directed=False)
    await due(social)
    assert provider.chat.await_count==2
    views=[json.loads(social.bus.inbound.get_nowait().content.split("\n",1)[1]) for _ in range(2)]
    assert {view["messages"][0]["text"] for view in views}=={"first topic","second topic"}


async def test_no_new_content_means_no_intermediary_polling(social):
    provider=decide(social,"observe")
    for _ in range(3):
        await due(social,60)
    provider.chat.assert_not_awaited()
    await social.observe(observed(social),directed=False)
    await due(social)
    for _ in range(3):
        await due(social,60)
    provider.chat.assert_awaited_once()


async def test_restart_preserves_frozen_retry_and_does_not_reset_attempt_budget(social):
    social.config.judge.retry_delay_seconds=3
    provider=decide(social,error=True)
    await social.observe(observed(social),directed=True)
    await due(social)
    batch=social.store.db.execute("SELECT id FROM judge_batches").fetchone()[0]
    root,config,bus,now=social.root,social.config,social.bus,social.now
    social.close()
    restored=TelegramSocialRuntime(config,root,bus,now=now,judge_provider=provider,judge_provider_version="integration-provider")
    try:
        await restored.recover()
        clock(restored).advance(3)
        await restored.run_due()
        assert restored.store.judge_record(batch).attempts==2
        assert restored.store.judge_record(batch).status=="fallback"
        assert bus.inbound.qsize()==1
    finally:
        restored.close()


async def test_pause_cancels_pending_judge_without_an_expired_deadline_spin(social):
    provider=decide(social)
    await social.observe(observed(social),directed=True)
    social.store.pause(A.session_key)
    for _ in range(3):
        await due(social)
    provider.chat.assert_not_awaited()
    assert social.store.next_due() is None


@pytest.mark.parametrize("mode", ["observing", "active"])
async def test_standing_preferences_do_not_invent_a_selective_wait(social, mode):
    binding = social.config.scopes[0]
    binding.wake_on_question = True
    binding.wake_keywords = ["旅行"]
    binding.wake_senders = ["456"]
    provider = decide(social)
    if mode == "active":
        context(social)
    await social.observe(observed(social, text="你别用繁体字"), directed=False)
    await due(social)
    snapshot = JudgeSnapshot.model_validate_json(provider.chat.call_args.kwargs["messages"][1]["content"])
    assert snapshot.intent is None
    assert snapshot.participation.on_question
    assert snapshot.participation.keywords == ("旅行",)
    assert snapshot.participation.senders == ("456",)
    assert snapshot.main_running == (mode == "active")
    assert snapshot.mode == "observing"
    if mode == "observing":
        assert social.bus.inbound.qsize() == 1
        row = social.store.db.execute("SELECT through_seq,handoff_job FROM judge_batches").fetchone()
        assert row["through_seq"] == 1 and row["handoff_job"]
    assert social.store.state(A.session_key)["read_through"] == 0


@pytest.mark.parametrize("seconds,infinite", [(30, False), (None, True)])
async def test_real_selective_wait_keeps_conditions_deadline_and_one_shot(social, seconds, infinite):
    social.config.cooldown_seconds = 0
    ctx = context(social)
    social.set_wake(ctx, keywords=["到家"], senders=["456"], on_messages=False, ttl_seconds=60)
    social.wait(ctx, seconds, purpose="messages", infinite=infinite)
    finish(social, ctx)
    provider = decide(social, "observe")
    await social.observe(observed(social, text="今晚吃面"), directed=False)
    await due(social)
    snapshot = JudgeSnapshot.model_validate_json(provider.chat.call_args.kwargs["messages"][1]["content"])
    assert snapshot.intent.purpose == "messages"
    assert snapshot.intent.keywords == ("到家",) and snapshot.intent.senders == ("456",)
    assert snapshot.intent.infinite == infinite
    assert snapshot.intent.until == (None if infinite else snapshot.intent.condition_expires - 30)
    assert snapshot.intent.one_shot and snapshot.intent.condition_expires > social.now()
    assert social.bus.inbound.empty()
    assert social.store.state(A.session_key)["wake_config"] != "{}"

    provider = decide(social, fulfills=True)
    await social.observe(observed(social, 2, text="到家了"), directed=False)
    await due(social)
    inbound = social.bus.inbound.get_nowait()
    from nanobot.agent.tools.context import RequestContext

    resumed = RequestContext(channel="telegram", chat_id=str(A.chat_id), session_key=A.session_key,
                             turn_id="resumed", metadata=inbound.metadata,
                             attributes={"context_version": social.store.state(A.session_key)["version"]})
    social.admit_request(resumed)
    assert social.store.state(A.session_key)["wake_config"] == "{}"


async def test_expired_temporary_wait_does_not_fall_back_to_question_only_observation(social):
    social.config.cooldown_seconds = 0
    social.config.scopes[0].wake_on_question = True
    ctx = context(social)
    social.set_wake(ctx, keywords=["到家"], senders=[], on_messages=False, ttl_seconds=1)
    social.wait(ctx, infinite=True, purpose="messages")
    finish(social, ctx)
    clock(social).advance(2)
    provider = decide(social)
    await social.observe(observed(social, text="你别用繁体字"), directed=False)
    await due(social)
    snapshot = JudgeSnapshot.model_validate_json(provider.chat.call_args.kwargs["messages"][1]["content"])
    assert snapshot.intent is None and snapshot.mode == "observing"
    assert social.bus.inbound.qsize() == 1


async def test_sleep_observation_is_not_a_selective_question_wait(social):
    social.config.scopes[0].wake_on_question = True
    social.config.sleep_observation.enabled = True
    ctx = context(social)
    social.wait(ctx, 30, sleeping=True)
    finish(social, ctx)
    provider = decide(social)
    await social.observe(observed(social, text="你别用繁体字"), directed=False)
    await due(social)
    snapshot = JudgeSnapshot.model_validate_json(provider.chat.call_args.kwargs["messages"][1]["content"])
    assert snapshot.intent.purpose == "observe"
    assert '"waiting_for_questions": false' in snapshot.intent.description


def test_state_distinguishes_retired_configuration_and_effective_preferences(social):
    social.config.scopes[0].wake_on_question = True
    social.config.scopes[0].wake_probability = 0.05
    social.config.reply_attention.enabled = True
    state = social.get_state(context(social))
    assert state["judge"]["intent"] is None
    assert state["judge"]["participation"]["on_question"]
    assert "probability" not in state["base_wake"]
    assert "reply_attention" not in state["dialogue_config"]
    assert "unfinished_seconds" not in state["dialogue_config"]["dynamic_wait"]
    assert "wakeProbability" in state["retired_config"]


async def test_restart_with_changed_judge_rebuilds_pending_input_without_reusing_old_intent(social):
    social.config.judge.retry_delay_seconds = 3
    provider = decide(social, error=True)
    await social.observe(observed(social, text="你别用繁体字"), directed=False)
    await due(social)
    batch = social.store.db.execute("SELECT id FROM judge_batches").fetchone()[0]
    root, config, bus, now = social.root, social.config, social.bus, social.now
    social.close()
    restored = TelegramSocialRuntime(config, root, bus, now=now, judge_provider=provider,
                                     judge_provider_version="changed-prompt-provider")
    try:
        ready = decide(restored)
        restored.judging.set_provider(ready, "changed-prompt-provider")
        await restored.recover()
        assert restored.store.judge_record(batch).status == "cancelled"
        assert restored.store.judge_record(batch).error_code == "provider_changed"
        assert bus.inbound.qsize() == 1
        assert restored.store.state(A.session_key)["read_through"] == 0
    finally:
        restored.close()
