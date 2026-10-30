"""V3-B message-pool integration: one semantic authority, durable bounded scheduling."""

import asyncio
import base64
import io
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from telegram import Update

from nanobot.channels.telegram.social.judge_models import JudgeSnapshot
from nanobot.channels.telegram.social.models import QuotedMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.channels.telegram.tests.test_social_media import picture, reference
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
        content = kwargs["messages"][1]["content"]
        snapshot = json.loads(content[0]["text"] if isinstance(content, list) else content)
        if error:
            return LLMResponse(content=None, finish_reason="error")
        source = snapshot["new_seqs"][-1:] or tuple(message["seq"] for message in snapshot["messages"] if not message["is_self"])[-1:]
        return LLMResponse(content=json.dumps({"decision":decision,"through_seq":snapshot["through_seq"],
                                              "source_seqs":list(source) if decision=="wake" else [],
                                              "reason":"canned integration outcome", "fulfills_intent":fulfills}))

    provider.chat.side_effect = respond
    runtime.judging.set_provider(provider, "integration-provider")
    return provider


def sent_snapshot(provider):
    content = provider.chat.call_args.kwargs["messages"][1]["content"]
    data = json.loads(content[0]["text"] if isinstance(content, list) else content)
    return JudgeSnapshot.model_validate_json(json.dumps({**data, "scope": A.session_key,
        "version": "test", "expires": data["created"] + 120}))


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


@pytest.mark.parametrize("excluded",["duplicate","edit","self","old","future","paused"])
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


@pytest.mark.parametrize("text,reply,directed", [
    ("早，@bot 今天吃什么？", False, True),
    ("我觉得可以拿 token 拌饭。", True, True),
    ("早，其他人今天吃什么？", False, False),
])
async def test_peer_bot_messages_use_existing_judge_without_address_gate(social, text, reply, directed):
    provider = decide(social)
    tg = channel(social)
    update = telegram_update(text=text, reply=reply, is_bot=True, user=456, timestamp=social.now())
    assert await tg._observe_social(update)
    await due(social)
    provider.chat.assert_awaited_once()
    assert sent_snapshot(provider).messages[-1].mentions_bot is directed
    assert social.bus.inbound.qsize() == 1


async def test_unaddressed_peer_bot_can_be_observed_without_forcing_a_reply(social):
    provider = decide(social, "observe")
    tg = channel(social)
    await tg._observe_social(telegram_update(text="这个表情存个档，不用接话。", is_bot=True, user=456, timestamp=social.now()))
    await due(social)
    provider.chat.assert_awaited_once()
    assert social.bus.inbound.empty()


async def test_successive_explicit_peer_bot_calls_have_no_extra_gate(social):
    provider = decide(social, "observe")
    tg = channel(social)
    for seq in (1, 2):
        await tg._observe_social(telegram_update(seq=seq, text="@bot 你怎么看？", is_bot=True, user=456, timestamp=social.now()))
        await due(social)
    assert provider.chat.await_count == 2
    assert social.bus.inbound.empty()  # V3 still decides; address never forces a public reply.


async def test_continuous_messages_are_batched_with_a_fixed_total_cap(social):
    provider=decide(social)
    social.config.dynamic_wait.max_seconds=4
    for seq in range(1,6):
        await social.observe(observed(social,seq,text=f"fragment {seq}"),directed=False)
        if seq<5:
            await due(social,1)
    await social.run_due()
    provider.chat.assert_awaited_once()
    snapshot=sent_snapshot(provider)
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
    snapshot=sent_snapshot(provider)
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
    snapshot = sent_snapshot(provider)
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
    snapshot = sent_snapshot(provider)
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
    snapshot = sent_snapshot(provider)
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
    snapshot = sent_snapshot(provider)
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


async def settle_images(social):
    await due(social)
    await asyncio.gather(*list(social.judging.tasks.values()))
    await social.run_due()


@pytest.mark.parametrize("kind,caption", [("photo", "[non-text message]"), ("photo", "今天的晚饭"),
                                         ("document", "[non-text message]")])
async def test_judge_sees_native_image_before_deciding_and_can_wake_main(social, kind, caption):
    provider = decide(social)
    loader = AsyncMock(return_value=picture((1800, 900)))
    social.set_media_services(loader, AsyncMock())
    ref = reference(kind=kind)
    await social.observe(observed(social, text=caption, media=(kind,), media_refs=(ref,)), directed=False)
    await settle_images(social)
    content = provider.chat.call_args.kwargs["messages"][1]["content"]
    images = [block for block in content if block["type"] == "image_url"]
    assert len(images) == 1
    with Image.open(io.BytesIO(base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]))) as image:
        assert image.size == (768, 384)
    assert "Message 1 (seq 1)" in content[-1]["text"]
    assert "_meta" not in images[0] and str(social.root) not in json.dumps(content)
    loader.assert_awaited_once()
    assert social.bus.inbound.qsize() == 1
    assert social.store.state(A.session_key)["read_through"] == 0
    metadata = social.store.db.execute("SELECT input_meta FROM judge_batches").fetchone()[0]
    assert "base64" not in metadata and "download-file" not in metadata


async def test_album_image_budget_and_topic_isolation(social):
    social.config.judge.media_max_images = 2
    provider = decide(social)
    loader = AsyncMock(return_value=picture())
    social.set_media_services(loader, AsyncMock())
    # An unrelated old image in another topic must never be downloaded.
    social.store.observe(observed(social, 99, scope=B, media=("photo",),
                                 media_refs=(reference(file_id="other-topic", unique_id="other"),)), 500)
    for seq in range(1, 5):
        await social.observe(observed(social, seq, text="[non-text message]", media=("photo",),
            media_refs=(reference(file_id=f"file-{seq}", unique_id=f"unique-{seq}"),), album_id="album"), directed=False)
    await settle_images(social)
    blocks = provider.chat.call_args.kwargs["messages"][1]["content"]
    assert sum(block["type"] == "image_url" for block in blocks) == 2
    assert "image limit" in blocks[-1]["text"]
    assert [call.args[0].file_id for call in loader.call_args_list] == ["file-1", "file-2"]


async def test_new_text_reply_can_show_its_quoted_image(social):
    provider = decide(social)
    loader = AsyncMock(return_value=picture())
    social.set_media_services(loader, AsyncMock())
    quote = QuotedMessage(message_id=77, sender_id="member", name="Member", timestamp=social.now()-100, text="",
                          media_refs=[reference()])
    await social.observe(observed(social, text="这个怎么看？", reply_to=77, reply=quote), directed=False)
    await settle_images(social)
    blocks = provider.chat.call_args.kwargs["messages"][1]["content"]
    assert sum(block["type"] == "image_url" for block in blocks) == 1
    assert "Quoted message 77, replied to by seq 1" in blocks[-1]["text"]


@pytest.mark.parametrize("failure", ["download", "invalid", "oversize", "timeout"])
async def test_unavailable_judge_image_is_explicit_without_extra_model_calls(social, failure):
    provider = decide(social, "observe")
    loader = AsyncMock(return_value=b"broken" if failure == "invalid" else picture())
    if failure == "download":
        loader.side_effect = RuntimeError("offline")
    if failure == "timeout":
        social.config.judge.media_timeout_seconds = 0.1

        async def blocked(*args):
            await asyncio.Event().wait()

        loader.side_effect = blocked
    social.set_media_services(loader, AsyncMock())
    ref = reference(size=social.config.media_max_bytes+1) if failure == "oversize" else reference()
    await social.observe(observed(social, media=("photo",), media_refs=(ref,)), directed=False)
    await settle_images(social)
    provider.chat.assert_awaited_once()
    blocks = provider.chat.call_args.kwargs["messages"][1]["content"]
    assert not any(block["type"] == "image_url" for block in blocks)
    assert "unavailable" in blocks[-1]["text"]
    assert social.bus.inbound.empty()
    assert social.store.judge_through(A.session_key) == 1


@pytest.mark.parametrize("change", ["pause", "edit"])
async def test_invalidated_judge_image_download_never_calls_model(social, change):
    provider = decide(social)
    message = observed(social, media=("photo",), media_refs=(reference(),))

    async def pause(*args):
        if change == "pause":
            social.store.pause(A.session_key)
        else:
            await social.observe(replace(message, update_id=99, is_edit=True), directed=False)
        return picture()

    social.set_media_services(AsyncMock(side_effect=pause), AsyncMock())
    await social.observe(message, directed=False)
    await settle_images(social)
    provider.chat.assert_not_awaited()
    assert social.bus.inbound.empty()
    assert not list(social.media.root.iterdir())


async def test_bounded_judge_retry_reuses_downloaded_original(social):
    social.config.judge.retry_delay_seconds = 0
    provider = decide(social, error=True)
    loader = AsyncMock(return_value=picture())
    social.set_media_services(loader, AsyncMock())
    await social.observe(observed(social, media=("photo",), media_refs=(reference(),)), directed=False)
    await settle_images(social)
    await asyncio.gather(*list(social.judging.tasks.values()))
    await social.run_due()
    assert provider.chat.await_count == 2
    loader.assert_awaited_once()
    assert social.store.db.execute("SELECT attempts,status FROM judge_batches").fetchone()["status"] == "fallback"


async def test_exhausted_judge_budget_does_not_download_images(social):
    social.config.judge.max_requests_per_hour = 1
    decide(social, "observe")
    await social.observe(observed(social), directed=False)
    await due(social)
    loader = AsyncMock(return_value=picture())
    social.set_media_services(loader, AsyncMock())
    await social.observe(observed(social, 2, media=("photo",), media_refs=(reference(),)), directed=False)
    await settle_images(social)
    loader.assert_not_awaited()


@pytest.mark.parametrize("purpose", ["reply", "messages"])
async def test_general_reply_wait_never_inherits_standing_question_only_preference(social, purpose):
    social.config.scopes[0].wake_on_question = True
    social.config.scopes[0].wake_keywords = ["旅行"]
    social.config.scopes[0].wake_senders = ["456"]
    social.store.observe(observed(social), 500)
    ctx = context(social)
    social.store.note_seen(A.session_key, ctx.turn_id, social.read_messages(ctx))
    social.wait(ctx, 30, purpose=purpose)
    finish(social, ctx)
    intent = social.judging.intent(A)
    assert intent.purpose == purpose
    assert intent.keywords == () and intent.senders == ()
    assert '"waiting_for_questions": false' in intent.description
    assert '"waiting_for_messages": true' in intent.description
    assert "not only questions" in intent.description
