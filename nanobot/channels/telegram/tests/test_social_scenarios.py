"""Black-box refactor contracts: change the harness, never storage-aware scenarios."""

import pytest

from nanobot.channels.telegram.tests.social_harness import SocialHarness


@pytest.fixture
async def make_harness(tmp_path):
    instances = []

    def make(**config):
        instance = SocialHarness(tmp_path, **config)
        instances.append(instance)
        return instance

    yield make
    for instance in instances:
        await instance.close()


async def test_ordinary_chat_and_unmentioned_followup_reach_main_model(make_harness):
    chat = make_harness()
    chat.script_model(chat.tool("social_send", text="一起喝茶吧"))
    await chat.member_says("刚泡好一壶茶")
    await chat.tick()
    assert chat.wakes()
    assert [item["text"] for item in chat.sent()] == ["一起喝茶吧"]
    calls = chat.model_calls()
    chat.advance(5)
    await chat.member_says("你想喝哪种？")
    chat.advance(0.1)
    await chat.tick()
    assert chat.model_calls() > calls
    assert chat.model_saw("你想喝哪种？")
    assert len(chat.wakes()) == 2


@pytest.mark.parametrize("error", [TimeoutError("judge timeout"), RuntimeError("judge failed")],
                         ids=["timeout", "exception"])
async def test_judge_failure_falls_back_to_main_model(make_harness, error):
    chat = make_harness()
    chat.fail_judge(error)
    await chat.member_says("普通新消息")
    await chat.tick()
    assert chat.judge_calls() == 1
    assert len(chat.wakes()) == 1
    assert chat.model_calls() == 1
    assert chat.model_saw("普通新消息")


async def test_selective_wait_ignores_others_then_wakes_for_selected_sender(make_harness):
    chat = make_harness()
    chat.script_model(chat.tool("social_set_wake", senders=["456"], on_messages=False))
    await chat.member_says("等小王回来", directed=True)
    await chat.tick()
    assert "error" not in chat.last_tool_result("social_set_wake")
    calls = chat.model_calls()
    wakes = len(chat.wakes())
    await chat.member_says("我去倒杯水", sender="789")
    await chat.tick()
    assert chat.model_calls() == calls
    assert len(chat.wakes()) == wakes
    await chat.member_says("我回来了", sender="456")
    await chat.tick()
    assert chat.model_calls() > calls
    assert len(chat.wakes()) == wakes + 1
    assert chat.model_saw("我回来了")


async def test_direct_mention_interrupts_selective_wait(make_harness):
    chat = make_harness()
    chat.script_model(chat.tool("social_set_wake", senders=["456"], on_messages=False))
    await chat.member_says("等小王", directed=True)
    await chat.tick()
    calls = chat.model_calls()
    await chat.member_says("@bot 来聊聊", sender="789", directed=True)
    await chat.tick()
    assert chat.model_calls() > calls
    assert chat.model_saw("@bot 来聊聊")


async def test_multipart_rate_limit_retries_in_order_without_duplicates(make_harness):
    chat = make_harness()
    chat.sender_outcome(2, "rate_limit", retry_after=2)
    chat.script_model(chat.tool("social_send", parts=["第一段", "第二段", "第三段"]))
    await chat.member_says("说三段吧")
    await chat.tick()
    assert [item["text"] for item in chat.sent()] == ["第一段"]
    chat.advance(1)
    await chat.tick()
    assert [item["text"] for item in chat.sent()] == ["第一段"]
    for seconds in [1, 2, 2]:
        chat.advance(seconds)
        await chat.tick()
    assert [item["text"] for item in chat.sent()] == ["第一段", "第二段", "第三段"]
    calls = chat.model_calls()
    chat.advance(10)
    await chat.tick()
    assert chat.model_calls() == calls
    assert len(chat.sent()) == 3


async def test_unknown_delivery_cannot_be_repeated_in_a_later_turn(make_harness):
    chat = make_harness()
    chat.sender_outcome(1, "unknown")
    chat.script_model(chat.tool("social_send", text="只发这一次"))
    await chat.member_says("发一句")
    await chat.tick()
    assert chat.last_tool_result("social_send")["status"] == "unknown"
    chat.advance(1)
    chat.script_model(chat.tool("social_send", text="只发这一次"))
    await chat.member_says("再发同样的", directed=True)
    await chat.tick()
    assert "error" in chat.last_tool_result("social_send")
    assert [item["text"] for item in chat.sent()] == ["只发这一次"]


async def test_send_cooldown_rejects_a_second_message(make_harness):
    chat = make_harness(sendCooldownSeconds=10)
    chat.script_model(chat.tool("social_send", text="第一句"),
                      chat.tool("social_send", text="第二句"))
    await chat.member_says("说点什么")
    await chat.tick()
    assert "cooldown" in chat.last_tool_result("social_send")["error"]
    assert [item["text"] for item in chat.sent()] == ["第一句"]


async def test_sleeping_still_sends_new_messages_through_judge(make_harness):
    chat = make_harness()
    chat.script_model(chat.tool("social_wait", sleeping=True, infinite=True))
    await chat.member_says("先休息吧")
    await chat.tick()
    assert "error" not in chat.last_tool_result("social_wait")
    calls = chat.model_calls()
    judged = chat.judge_calls()
    await chat.member_says("@bot 醒醒", directed=True)
    await chat.tick()
    assert chat.judge_calls() == judged + 1
    assert chat.model_calls() > calls
    assert chat.model_saw("@bot 醒醒")


async def test_new_conversation_supersedes_unclaimed_idle_opportunity(make_harness):
    chat = make_harness()
    chat.set_proactive()
    chat.advance(61)
    await chat.queue_due()
    assert chat.wakes()[-1]["kind"] == "idle"
    assert chat.model_calls() == 0
    await chat.member_says("大家来看这个新话题")
    await chat.tick()
    assert chat.wakes()[-1]["reason"] == "judge"
    assert chat.wakes()[-1]["kind"] is None
    assert chat.model_calls() == 1
    assert chat.model_saw("大家来看这个新话题")


async def test_idle_opportunity_remains_available_when_activity_is_enabled(make_harness):
    chat = make_harness()
    chat.set_proactive(activityEnabled=True)
    chat.advance(61)
    await chat.tick()
    assert chat.model_calls() == 1
    assert any(wake["kind"] == "idle" for wake in chat.wakes())


async def test_activity_runs_on_its_own_interval_without_accelerating_idle(make_harness):
    chat = make_harness()
    chat.set_proactive(activityEnabled=True, minIntervalSeconds=3600, maxIntervalSeconds=3600,
                       activityIntervalSeconds=60, probability=1)
    chat.advance(61)
    await chat.tick()
    assert any(wake["kind"] == "activity" for wake in chat.wakes())
    assert not any(wake["kind"] == "idle" for wake in chat.wakes())
    chat.advance(3540)
    await chat.tick()
    assert any(wake["kind"] == "idle" for wake in chat.wakes())


async def test_activity_still_runs_when_idle_probability_misses(make_harness):
    chat = make_harness()
    chat.set_proactive(activityEnabled=True, probability=0, minIntervalSeconds=60,
                       maxIntervalSeconds=60, activityIntervalSeconds=60)
    chat.advance(61)
    await chat.tick()
    assert any(wake["kind"] == "activity" for wake in chat.wakes())


async def test_pending_matter_allows_only_two_proactive_followups(make_harness):
    chat = make_harness()
    chat.set_proactive()
    chat.script_model(chat.tool(
        "social_memory", action="record", kind="pending", origin="self",
        content="想聊聊乌龙茶", pending_thought="想听大家喜欢哪种茶", motivation="好奇口味",
    ))
    await chat.member_says("随便聊聊")
    await chat.tick()
    matter = chat.last_tool_result("social_memory")["memory"]["id"]
    for text in ["你们喜欢哪种茶？", "有没有偏爱的焙火程度？"]:
        chat.advance(61)
        chat.script_model(chat.tool("social_send", text=text, followup_id=matter))
        await chat.tick()
        assert chat.last_tool_result("social_send")["status"] == "sent"
        assert chat.wakes()[-1]["kind"] == "idle"
    chat.advance(61)
    chat.script_model(chat.tool("social_send", text="再问一次茶的事情", followup_id=matter))
    await chat.tick()
    assert "error" in chat.last_tool_result("social_send")
    assert [item["text"] for item in chat.sent()] == ["你们喜欢哪种茶？", "有没有偏爱的焙火程度？"]


async def test_role_edit_applies_new_persona_on_next_turn(make_harness):
    chat = make_harness()
    await chat.member_says("你好")
    await chat.tick()
    assert chat.model_saw("PERSONA ALPHA")
    chat.edit_role("PERSONA BETA")
    await chat.member_says("又见面啦")
    await chat.tick()
    assert chat.model_saw("PERSONA BETA")


async def test_role_edit_keeps_earlier_group_history_in_context(make_harness):
    chat = make_harness(snapshotMessages=2)
    chat.script_model(chat.tool("social_mark_read", through=1))
    await chat.member_says("上次群聊约定的暗号是青瓷")
    await chat.tick()
    assert "error" not in chat.last_tool_result("social_mark_read")
    # Move the old message outside the fresh snapshot; only conversation history
    # can retain it. Prove that history is present before editing the role.
    await chat.member_says("今天的话题一")
    await chat.member_says("今天的话题二")
    await chat.tick()
    assert chat.model_saw("上次群聊约定的暗号是青瓷")
    chat.edit_role("PERSONA BETA")
    await chat.member_says("换个话题继续聊", directed=True)
    await chat.tick()
    assert chat.model_saw("PERSONA BETA")
    assert chat.model_saw("上次群聊约定的暗号是青瓷")


async def test_unrelated_config_change_keeps_waiting_and_history(make_harness):
    chat = make_harness(snapshotMessages=2)
    chat.script_model(chat.tool("social_mark_read", through=1))
    await chat.member_says("配置修改前的暗号是青瓷")
    await chat.tick()
    assert "error" not in chat.last_tool_result("social_mark_read")
    await chat.member_says("今天的话题一")
    await chat.member_says("今天的话题二")
    await chat.tick()
    chat.script_model(chat.tool("social_wait", purpose="messages", seconds=30))
    await chat.member_says("等下一条消息")
    await chat.tick()
    assert chat.last_tool_result("social_wait")["deferred"]
    calls = chat.model_calls()
    chat.reconfigure(judge={"timeoutSeconds": 25, "minIntervalSeconds": 0, "maxRetries": 0})
    chat.advance(5)
    await chat.tick()
    assert chat.model_calls() == calls
    await chat.member_says("继续刚才的话题")
    await chat.tick()
    assert chat.model_calls() == calls
    chat.advance(0.1)
    await chat.tick()
    assert chat.model_calls() > calls
    assert chat.model_saw("继续刚才的话题")
    assert chat.model_saw("配置修改前的暗号是青瓷")


async def test_restart_processes_a_wake_that_was_queued_but_unclaimed(make_harness):
    chat = make_harness()
    await chat.member_says("重启之后还要接着聊")
    await chat.queue_due()
    assert len(chat.wakes()) == 1
    assert chat.model_calls() == 0
    await chat.restart()
    chat.script_model(chat.tool("social_send", text="接着聊呀"))
    await chat.tick()
    assert chat.model_calls() > 0
    assert chat.model_saw("重启之后还要接着聊")
    assert [item["text"] for item in chat.sent()] == ["接着聊呀"]


async def test_restart_does_not_retry_an_interrupted_platform_send(make_harness):
    chat = make_harness()
    await chat.interrupt_send("回执丢失的一句")
    await chat.restart()
    chat.advance(10)
    await chat.tick()
    assert [item["text"] for item in chat.sent()] == ["回执丢失的一句"]
    chat.script_model(chat.tool("social_send", text="回执丢失的一句"))
    await chat.member_says("请再发一遍", directed=True)
    await chat.tick()
    assert chat.model_saw("回执丢失的一句")
    assert "unknown" in chat.last_tool_result("social_send")["error"]
    assert len(chat.sent()) == 1


async def test_sticker_pack_can_be_viewed_annotated_searched_and_sent(make_harness):
    chat = make_harness()
    chat.script_model(
        chat.tool("social_sticker", action="import_set", set_name="Scenario_Pack"),
        chat.tool("social_sticker", action="view", set_name="Scenario_Pack"),
        chat.tool("social_sticker", action="annotate",
                  notes=[{"sticker_id": "scenario-sticker", "note": "开心问好"}]),
        chat.tool("social_sticker", action="list", query="问好"),
    )
    await chat.member_says("学一下这个贴纸包")
    await chat.tick()
    found = chat.last_tool_result("social_sticker")["stickers"]
    assert len(found) == 1
    sticker = found[0]["sticker_id"]
    chat.script_model(chat.tool("social_sticker", action="send", sticker_id=sticker))
    await chat.member_says("打个招呼吧")
    await chat.tick()
    assert chat.last_tool_result("social_sticker")["status"] == "sent"
    assert [(item["kind"], item["sticker"]) for item in chat.sent()] == [
        ("sticker", "scenario-sticker-file"),
    ]


async def test_ordinary_message_tool_shares_social_hourly_send_quota(make_harness):
    chat = make_harness(maxSendsPerHour=2)
    chat.script_model(chat.tool("message", content="普通工具第一句"),
                      chat.tool("message", content="普通工具第二句"),
                      chat.tool("social_send", text="不能越过同一个配额"))
    await chat.member_says("发两句")
    await chat.tick()
    assert "error" not in chat.last_tool_result("message")
    assert "budget" in chat.last_tool_result("social_send")["error"]
    assert [item["text"] for item in chat.sent()] == ["普通工具第一句", "普通工具第二句"]
