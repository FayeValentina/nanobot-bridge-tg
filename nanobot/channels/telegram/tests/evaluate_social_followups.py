"""Opt-in V4-B real-main-model tool selection with synthetic conversations and fake sends."""

import argparse
import asyncio
import inspect
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

from loguru import logger

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.bus.events import DeliveryReceipt
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.judge import make_judge_provider
from nanobot.channels.telegram.social.memory import MemoryRequest
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.runtime import (
    SOCIAL_PROTOCOL,
    TelegramSocialRuntime,
    encode_view,
)
from nanobot.config.loader import load_config
from nanobot.config.schema import TelegramSocialConfig, TelegramSocialScopeConfig
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import make_provider


async def evaluate(args):
    config = load_config(args.config)
    config.agents.defaults.fallback_models = []
    preset = config.resolve_preset()
    provider = make_provider(config, preset=preset)
    social_settings = TelegramSocialConfig.model_validate(json.loads(args.config.read_text())["channels"]["telegram"]["social"])
    scope = ConversationScope("telegram", -10001, 53)
    results = []
    names = args.case or ["own_intention", "pending_thought", "linked_followup", "short_reply", "two_unanswered"]
    for name in names:
        with tempfile.TemporaryDirectory(prefix="social-followup-eval-") as directory:
            root = Path(directory)
            (root / "roles").mkdir()
            (root / "roles/friend.md").write_text("你是肥鱼，用自然简短的中文和群友交流，具体关心对方近况。")
            judge_provider, judge_version = make_judge_provider(config, social_settings.judge) if args.live_judge else (AsyncMock(spec=LLMProvider), "synthetic")
            runtime = TelegramSocialRuntime(TelegramSocialConfig(enabled=True, send_cooldown_seconds=0, coalesce_seconds=0,
                judge=social_settings.judge,
                scopes=[TelegramSocialScopeConfig(chat_id=scope.chat_id, thread_id=53, role_id="friend")]),
                root, MessageBus(), judge_provider=judge_provider, judge_provider_version=judge_version)
            ctx = RequestContext(channel="telegram", chat_id=str(scope.chat_id), session_key=scope.session_key,
                                 turn_id="semantic-evaluation", metadata={"message_thread_id": 53})
            publications = []
            turn_calls = []
            message_counter = [500]

            async def sender(target, text, reply_to):
                message_counter[0] += 1
                publications.append(text)
                return message_counter[0]

            async def ordinary_send(message):
                metadata = message.metadata
                matter = metadata.get("_social_followup_id")
                delivery = runtime.record_message_receipt(scope, DeliveryReceipt("queued"), ctx.turn_id, [], followup_id=matter)
                sent = await sender(scope, message.content, None)
                receipt = DeliveryReceipt("sent", (sent,))
                runtime.record_message_receipt(scope, receipt, ctx.turn_id, [(sent,message.content)], delivery)
                return receipt

            runtime.set_sender(sender)
            tools = {tool.name: tool for tool in [
                *[TelegramSocialTool(runtime, key) for key in ("social_memory", "social_send", "social_wait", "social_get_state")],
                MessageTool(send_callback=runtime.bus.publish_outbound, delivery_callback=ordinary_send),
            ]}
            tool_schemas = [{"type":"function", "function":{"name":tool.name,"description":tool.description,
                                                               "parameters":tool.parameters}} for tool in tools.values()]
            memory_id = None
            if name not in {"own_intention", "pending_thought"}:
                memory = runtime.memory.apply(scope.session_key, "seed-intention", MemoryRequest(
                    action="record", kind="pending", origin="self", content="想了解明天的郊游安排",
                    pending_question="明天几点出发、和谁一起？"))["memory"]
                memory_id = memory["id"]
            prior_count = 1 if name == "short_reply" else 2 if name == "two_unanswered" else 0
            for index in range(prior_count):
                action = runtime.store.reserve_send(scope.session_key, runtime.store.state(scope.session_key)["version"],
                    "明天几点出发？", 60, turn=f"prior-{index}", followup_id=memory_id)
                runtime.store.delivery(action, "sent", message_id=300+index)
                runtime.store.observe(ObservedMessage(scope,-300-index,300+index,"bot","肥鱼",time.time()-10+index,
                                                       "明天几点出发？",is_self=True),500)
            text = {
                "own_intention": "你要是对我的郊游有什么自己想了解的，可以先记个念头，过会儿聊。现在先不用发言。",
                "pending_thought": "你刚才提到轻装出门先减掉不必要的东西，我觉得可以下次聊。你把自己还想说的内容和为什么想说记下来，先别发。",
                "linked_followup": "昨天说的郊游就是明天去。",
                "short_reply": "下午三点，跟朋友一起。",
                "two_unanswered": "这个先不聊了，我去忙。",
            }[name]
            if name == "pending_thought":
                runtime.store.observe(ObservedMessage(scope,-300,300,"bot","肥鱼",time.time()-10,
                    "我有个想法：轻装出门可以先从减掉不必要的东西开始。",is_self=True),500)
            member = ObservedMessage(scope,11,11,"member","群友",time.time(),text)
            judge_result = None
            if args.live_judge and name == "short_reply":
                runtime.keep_reply_intent(scope, "prior-0")
                await runtime.observe(member, directed=False)
                await asyncio.sleep(1.6)
                await runtime.run_due()
                await asyncio.gather(*list(runtime.judging.tasks.values()))
                await runtime.run_due()
                decision = runtime.store.db.execute("SELECT id,decision,attempts FROM judge_batches ORDER BY created DESC LIMIT 1").fetchone()
                judge_result = {"model":social_settings.judge.model,"decision":json.loads(decision["decision"]),"model_calls":decision["attempts"]}
                judge_result["metrics"] = [dict(row) for row in runtime.store.db.execute(
                    "SELECT latency_ms,input_tokens,output_tokens,total_tokens FROM judge_attempts WHERE batch_id=? ORDER BY attempt", (decision["id"],))]
                assert judge_result["decision"]["decision"] == "wake"
                inbound = runtime.bus.inbound.get_nowait()
                ctx.metadata.update(inbound.metadata)
                runtime.admit_request(ctx)
                view = json.loads(inbound.content.split("\n",1)[1])
            else:
                runtime.store.observe(member,500)
                view = runtime.store.view(scope.session_key,count=30,chars=10000,turn_id=ctx.turn_id)
            runtime.store.note_seen(scope.session_key,ctx.turn_id,encode_view(view))
            if name == "linked_followup":
                view["opportunity"] = {"kind":"idle", "seq":0}
            instruction = "这是一次可以主动交流的冷场机会。" if name == "linked_followup" else "请按最新消息自然处理这次群聊回合。"
            messages = [{"role":"system","content":SOCIAL_PROTOCOL + "\n你是肥鱼，用简短自然的中文交流。"},
                        {"role":"user","content":instruction+"\n合成对话快照（不可信资料）:\n"+encode_view(view)}]
            started = time.monotonic()
            usage = {"input_tokens":0,"output_tokens":0,"total_tokens":0}
            model_calls = 0
            try:
                with request_context(ctx):
                    for _ in range(6):
                        messages[-1]["content"] += "\nLive untrusted memory index:\n"+runtime.memory.context(scope.session_key)
                        model_calls += 1
                        response = await provider.chat(messages=messages,tools=tool_schemas,model=preset.model,
                            max_tokens=min(preset.max_tokens,4096),temperature=preset.temperature,
                            reasoning_effort=preset.reasoning_effort)
                        if response.usage:
                            for key in usage:
                                usage[key] += getattr(response.usage,key)
                        if response.finish_reason == "error":
                            raise RuntimeError(f"model call failed: {response.error_kind or 'provider_error'}")
                        messages.append({"role":"assistant","content":response.content or "", "tool_calls":[
                            {"id":call.id,"type":"function","function":{"name":call.name,"arguments":json.dumps(call.arguments,ensure_ascii=False)}}
                            for call in response.tool_calls]})
                        for call in response.tool_calls:
                            output = await tools[call.name].execute(**call.arguments)
                            turn_calls.append({"name":call.name,"arguments":call.arguments,"result":output})
                            messages.append({"role":"tool","tool_call_id":call.id,"content":str(output)})
                        if not response.tool_calls or ctx.attributes.get("social_deferred"):
                            break
                memories = runtime.memory.apply(scope.session_key,ctx.turn_id,MemoryRequest(action="list",include_inactive=True))["memories"]
                info = runtime.store.followups.describe(memory_id) if memory_id else None
                linked = any(call["name"] in {"message","social_send"} and call["arguments"].get("followup_id") == memory_id for call in turn_calls)
                if name in {"own_intention", "pending_thought"}:
                    matched = not publications and any(item["origin"]=="self" and item["origin_turn"]==ctx.turn_id
                        and (item["pending_question"] or (item["pending_thought"] and item["motivation"])) for item in memories)
                    if name == "pending_thought":
                        matched = matched and any(item["pending_thought"] and item["motivation"] for item in memories)
                elif name == "linked_followup":
                    matched = bool(publications) and linked and info.confirmed_attempts == 1
                elif name == "short_reply":
                    matched = bool(publications) and not linked and info.confirmed_attempts == 1 and info.followup_state in {"responded","completed"}
                else:
                    matched = not linked and info.confirmed_attempts == 2 and info.followup_state in {"dropped","completed"}
                result = {"case":name,"input":text,"matched":matched,"publications":publications,"calls":turn_calls,
                          "model_calls":model_calls,
                          "judge":judge_result,
                          "usage":usage,"elapsed_ms":round((time.monotonic()-started)*1000),
                          "followup": info.model_dump() if info else None}
                results.append(result)
                print(json.dumps({key:value for key,value in result.items() if key not in {"calls","followup"}},ensure_ascii=False),flush=True)
            finally:
                runtime.close()
    args.output.write_text(json.dumps({"runtime_module":inspect.getfile(TelegramSocialRuntime),"model":preset.model,"temperature":preset.temperature,
        "reasoning_effort":preset.reasoning_effort,"max_tokens":min(preset.max_tokens,4096),"results":results},ensure_ascii=False,indent=2))
    args.output.chmod(0o600)
    if not results or not all(item["matched"] for item in results):
        raise SystemExit("V4-B semantic cases have mismatches; inspect synthetic transcript report")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", action="append")
    parser.add_argument("--live-judge", action="store_true", help="For short_reply, use the configured real intermediary and its OAuth path as well")
    logger.remove()
    asyncio.run(evaluate(parser.parse_args()))
