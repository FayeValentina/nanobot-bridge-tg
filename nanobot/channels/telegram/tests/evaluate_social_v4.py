"""Opt-in V4-C evaluation: real scheduling, main runner/models, synthetic chats, fake sends.

python -m nanobot.channels.telegram.tests.evaluate_social_v4 --config <config.json>
--output <report.json> [--case <name>] [--live-web]
Never starts a Telegram receiver or sends to real chats. Reports contain synthetic data only.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.agent.context_governance import ContextCompactionState, ContextGovernor
from nanobot.agent.hook import AgentTurnHookContext
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import DeliveryReceipt, InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.judge import make_judge_provider
from nanobot.channels.telegram.social.memory import MemoryKind, MemoryRequest
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime, encode_view
from nanobot.config.loader import load_config
from nanobot.config.schema import Config, TelegramSocialConfig, TelegramSocialScopeConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.factory import make_provider
from nanobot.utils.llm_runtime import LLMRuntime

CASES = (
    "idle_empty", "idle_pending_question", "idle_pending_thought", "old_story",
    "ordinary_statement", "short_reply", "no_reply", "two_unanswered", "explicit_end",
    "silence", "interest_search", "search_failed", "search_disabled", "restart_pending", "compacted_pending",
    "chat_invitation", "unanswered_invitation",
)
SCOPE = ConversationScope("telegram", -10001, 53)


class Clock:
    def __init__(self) -> None:
        self.value = time.time()

    def __call__(self) -> float:
        return self.value


class MeasuredProvider(LLMProvider):
    """Count actual raw calls, including retries, without replacing model output."""

    def __init__(self, delegate: LLMProvider) -> None:
        super().__init__(api_key=None, provider_name=delegate.provider_name)
        self.delegate = delegate
        self.generation = delegate.generation
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return self.delegate.get_default_model()

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        started = time.monotonic()
        response = await self.delegate.chat(messages=messages, **kwargs)
        self.calls.append({"elapsed_ms": round((time.monotonic()-started)*1000),
            "finish_reason": response.finish_reason,
            "usage": {key: getattr(response.usage, key) for key in ("input_tokens", "output_tokens", "total_tokens")} if response.usage else None})
        return response


class EvaluationWeb(Tool):
    """Only the evaluation fixture limits network exploration; production tools are untouched."""

    def __init__(self, delegate: Tool, trace: list[dict[str, Any]], failed: bool) -> None:
        self.delegate, self.trace, self.failed = delegate, trace, failed

    @property
    def name(self) -> str:
        return self.delegate.name

    @property
    def description(self) -> str:
        return self.delegate.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self.delegate.parameters

    async def execute(self, **kwargs: Any) -> Any:
        if len(self.trace) >= 3:
            output = ToolResult.error("This evaluation opportunity has exhausted its three web lookups; finish without further web calls.")
            self.trace.append({"name": self.name, "arguments": kwargs, "budget_exhausted": True, "result": str(output)})
            return output
        started = time.monotonic()
        try:
            async with asyncio.timeout(25):
                output = ToolResult.error("Web service unavailable; no content was retrieved.") if self.failed else await self.delegate.execute(**kwargs)
        except TimeoutError:
            output = ToolResult.error("Web lookup timed out; no content was retrieved.")
        self.trace.append({"name": self.name, "arguments": kwargs, "simulated_failure": self.failed,
            "elapsed_ms": round((time.monotonic()-started)*1000), "result": str(output)[:7000]})
        return output


async def pump(runtime: TelegramSocialRuntime, clock: Clock) -> None:
    for _ in range(4):
        await runtime.run_due()
        tasks = list(runtime.judging.tasks.values())
        if tasks:
            await asyncio.gather(*tasks)
        await runtime.run_due()
        pending = runtime.store.db.execute("SELECT MIN(pending_due) FROM judge_state WHERE pending_due IS NOT NULL").fetchone()[0]
        if pending is None or not runtime.bus.inbound.empty():
            break
        clock.value = max(clock.value, float(pending))


def seed_message(runtime: TelegramSocialRuntime, text: str, *, own: bool = False) -> int:
    seq = int(runtime.store.state(SCOPE.session_key)["next_seq"])
    runtime.store.observe(ObservedMessage(SCOPE, -seq, seq, "bot" if own else "123", "肥鱼" if own else "林林",
        runtime.now()-7200, text, is_self=own), 500)
    return seq


def seed_fact(runtime: TelegramSocialRuntime, text: str, *, kind: MemoryKind = "topic") -> str:
    seq = seed_message(runtime, text)
    view = runtime.store.view(SCOPE.session_key, count=30, chars=12000, turn_id="seed")
    runtime.store.note_seen(SCOPE.session_key, "seed", encode_view(view))
    memory = runtime.memory.apply(SCOPE.session_key, "seed", MemoryRequest.model_validate({
        "action": "record", "kind": kind, "content": text, "message_ids": [seq], "basis": "direct"}))["memory"]
    return str(memory["id"])


def seed_intention(runtime: TelegramSocialRuntime, *, thought: bool = False) -> str:
    data = {"action": "record", "kind": "pending", "origin": "self",
        "content": "想和林林聊聊轻装出门" if thought else "想了解林林的郊游安排",
        "pending_thought": "轻装出门可以先减掉不必要的东西，想听林林会带哪三样。" if thought else None,
        "pending_question": None if thought else "明天几点出发、和谁一起去郊游？",
        "motivation": "分享一个减少负担的想法，也好奇对方的选择" if thought else None}
    return str(runtime.memory.apply(SCOPE.session_key, "seed", MemoryRequest.model_validate(data))["memory"]["id"])


def seed_attempt(runtime: TelegramSocialRuntime, memory_id: str, index: int) -> None:
    action = runtime.store.reserve_send(SCOPE.session_key, runtime.store.state(SCOPE.session_key)["version"],
        "明天几点出发、和谁一起去郊游？", 60, turn=f"prior-{index}", followup_id=memory_id)
    runtime.store.delivery(action, "sent", message_id=300+index)
    seed_message(runtime, "明天几点出发、和谁一起去郊游？", own=True)


async def evaluate_case(name: str, args: argparse.Namespace, config: Config, provider: MeasuredProvider) -> dict[str, Any]:
    case_started = time.monotonic()
    settings = TelegramSocialConfig.model_validate(json.loads(args.config.read_text())["channels"]["telegram"]["social"]).judge
    if settings.oauth_storage_dir is None and settings.provider == "openai_codex":
        settings.oauth_storage_dir = str(args.config.parent / "telegram-social/oauth")
    settings.min_interval_seconds = 0
    judge, judge_version = make_judge_provider(config, settings)
    clock = Clock()
    with tempfile.TemporaryDirectory(prefix="social-v4c-") as directory:
        root = Path(directory)
        (root / "roles").mkdir()
        (root / "roles/friend.md").write_text(args.persona.read_text() if args.persona else "你是肥鱼，作为群友用简短自然的中文交流，具体关心对方近况。")
        social = TelegramSocialConfig(enabled=True, coalesce_seconds=0, cooldown_seconds=0, send_cooldown_seconds=0,
            judge=settings, scopes=[TelegramSocialScopeConfig(chat_id=SCOPE.chat_id, thread_id=SCOPE.thread_id,
                role_id="friend", nicknames=["肥鱼"])])
        runtime = TelegramSocialRuntime(social, root, MessageBus(), now=clock, judge_provider=judge, judge_provider_version=judge_version)
        publications: list[str] = []
        web_trace: list[dict[str, Any]] = []
        turns: list[dict[str, Any]] = []
        initial_calls = len(provider.calls)
        memory_id: str | None = None
        input_text: str | None = None
        no_reply_calls: int | None = None
        try:
            if name in {"idle_pending_question", "short_reply", "no_reply", "two_unanswered", "explicit_end"}:
                memory_id = seed_intention(runtime)
            elif name in {"idle_pending_thought", "restart_pending", "compacted_pending"}:
                memory_id = seed_intention(runtime, thought=True)
            elif name == "old_story":
                memory_id = seed_fact(runtime, "林林昨天第一次去上陶艺体验课，约好回来聊做出来的东西。")
                runtime.memory.apply(SCOPE.session_key, "seed", MemoryRequest(action="followup", memory_id=memory_id,
                    pending_question="第一次陶艺体验感觉怎么样，最后做出了什么？"))
            elif name in {"interest_search", "search_failed", "search_disabled"}:
                seed_fact(runtime, "林林喜欢 NASA 官方天文新闻；只想聊有官方来源可核实的近期发现，消息来源可从 https://www.nasa.gov/news/ 查看。", kind="preference")
            if name in {"short_reply", "explicit_end", "no_reply"}:
                assert memory_id is not None
                seed_attempt(runtime, memory_id, 0)
            elif name == "two_unanswered":
                assert memory_id is not None
                for index in range(2):
                    seed_attempt(runtime, memory_id, index)
            if name == "ordinary_statement":
                seed_message(runtime, "最近有什么打算呀？", own=True)
                input_text = "明天要出去玩。"
            elif name == "short_reply":
                input_text = "下午三点，跟朋友一起。"
            elif name == "explicit_end":
                input_text = "郊游的事先不聊了，我去忙，不用追问。"
            elif name == "silence":
                input_text = "肥鱼，我想安静一会儿，这条也别回复了。"
            elif name in {"chat_invitation", "unanswered_invitation"}:
                seed_message(runtime, "我明天准备去郊游。")
                seed_message(runtime, "带点水，出发前看一下交通。", own=True)
                if name == "unanswered_invitation":
                    seed_message(runtime, "没人说话")
                input_text = "没人说话" if name == "chat_invitation" else "？"
            # All historical evidence has already been read/considered. Idle runs
            # must be produced by the timer, not by an artificial inbound snapshot.
            last = int(runtime.store.state(SCOPE.session_key)["next_seq"])-1
            with runtime.store.db:
                runtime.store.db.execute("UPDATE scopes SET read_through=?,seen_through=? WHERE key=?", (last, last, SCOPE.session_key))
                runtime.store.db.execute("UPDATE judge_state SET assessed_through=? WHERE scope=?", (last, SCOPE.session_key))
            runtime.proactive.random_value = lambda: 0.0
            social.scopes[0].proactive.enabled = True
            runtime.configure(social)
            if name == "restart_pending":
                runtime.close()
                runtime = TelegramSocialRuntime(social, root, MessageBus(), now=clock, judge_provider=judge, judge_provider_version=judge_version)
                runtime.proactive.random_value = lambda: 0.0

            async def sender(scope: ConversationScope, text: str, reply_to: int | None) -> int:
                publications.append(text)
                return 500+len(publications)

            runtime.set_sender(sender)
            registry = ToolRegistry()
            for key in ("social_memory", "social_send", "social_wait", "social_set_wake", "social_get_state", "social_read_messages", "social_mark_read"):
                registry.register(TelegramSocialTool(runtime, key))

            async def ordinary_send(message: OutboundMessage) -> DeliveryReceipt:
                ctx = registry_context[0]
                assert ctx is not None
                matter = message.metadata.get("_social_followup_id")
                delivery = runtime.record_message_receipt(SCOPE, DeliveryReceipt("queued"), ctx.turn_id, [], followup_id=matter)
                sent = await sender(SCOPE, message.content, None)
                receipt = DeliveryReceipt("sent", (sent,))
                runtime.record_message_receipt(SCOPE, receipt, ctx.turn_id, [(sent, message.content)], delivery)
                return receipt

            registry_context: list[RequestContext | None] = [None]
            registry.register(MessageTool(send_callback=runtime.bus.publish_outbound, delivery_callback=ordinary_send))
            if name == "search_failed" or (name == "interest_search" and args.live_web):
                web = config.tools.web
                registry.register(EvaluationWeb(WebSearchTool(config=web.search, proxy=web.proxy, user_agent=web.user_agent), web_trace, name == "search_failed"))
                registry.register(EvaluationWeb(WebFetchTool(config=web.fetch, proxy=web.proxy, user_agent=web.user_agent, max_chars=6000), web_trace, name == "search_failed"))
            preset = config.resolve_preset()
            llm_runtime = LLMRuntime.capture(provider, preset.model, context_window_tokens=32000).with_generation_overrides(max_tokens=4096)

            async def run_main(inbound: InboundMessage) -> None:
                ctx = RequestContext(channel="telegram", chat_id=str(SCOPE.chat_id), session_key=SCOPE.session_key,
                    turn_id=f"v4c-{name}-{len(turns)}", metadata=dict(inbound.metadata), runtime=llm_runtime)
                registry_context[0] = ctx
                runtime.admit_request(ctx)
                persona, _ = runtime._persona(SCOPE)
                hook = runtime.hook_factory(AgentTurnHookContext(session_key=SCOPE.session_key))
                started = time.monotonic()
                before = len(provider.calls)
                builder = ContextBuilder(root)
                transcript = TranscriptInput(history=[], current_message=inbound.content)
                messages = builder.build_transcript(transcript, trusted_persona=persona, include_skills=False)
                if name == "compacted_pending":
                    async def summarized(*_args: Any, **_kwargs: Any) -> str:
                        return "此前聊过日常；未完成事项以本轮实时记忆为准。"

                    _, compaction = ContextCompactionState.from_transcript(
                        transcript,
                        partial(builder.build_transcript, trusted_persona=persona, include_skills=False), summarized, None)
                    assert compaction is not None
                    messages = ContextGovernor._summary_transcript(compaction, await summarized()) + [messages[-1]]
                with request_context(ctx):
                    result = await AgentRunner().run(AgentRunSpec(
                        initial_messages=messages,
                        tools=registry, runtime=llm_runtime, max_iterations=6, max_tool_result_chars=7000,
                        hook=hook, workspace=root, session_key=SCOPE.session_key))
                await runtime.run_due()  # Flush paced sends and persist each fake platform receipt.
                turns.append({"input": inbound.content, "elapsed_ms": round((time.monotonic()-started)*1000),
                    "model_calls": len(provider.calls)-before, "stop_reason": result.stop_reason, "error": result.error,
                    "tools": result.tools_used, "tool_calls": [call for message in result.messages for call in message.get("tool_calls", [])],
                    "final_content": result.final_content})

            if name == "no_reply":
                # Start from an actually confirmed fake-platform send, then exercise
                # the real automatic reply wait. Silence in a generated first turn
                # must not accidentally count as evidence of reply-wait behavior.
                runtime.keep_reply_intent(SCOPE, "prior-0")
                before = len(provider.calls)
                wait = runtime.store.db.execute("SELECT total_due FROM dialogue_waits WHERE scope=?", (SCOPE.session_key,)).fetchone()
                assert wait is not None
                clock.value = max(clock.value+31, float(wait[0])+1 if wait and wait[0] else clock.value)
                await pump(runtime, clock)
                no_reply_calls = len(provider.calls)-before
                assert runtime.bus.inbound.empty(), "No-reply wait must not create a model wake"
                next_check = runtime.proactive.status(SCOPE.session_key)["next_check"]
                assert next_check is not None
                # A later independent period must still be reachable.
                clock.value = float(next_check)
                await pump(runtime, clock)
                if not runtime.bus.inbound.empty():
                    await run_main(runtime.bus.inbound.get_nowait())
            else:
                if input_text is not None:
                    await runtime.observe(ObservedMessage(SCOPE, 11, 11, "123", "林林", clock(), input_text), directed=False)
                    clock.value += 2
                else:
                    clock.value = float(runtime.proactive.status(SCOPE.session_key)["next_check"])
                await pump(runtime, clock)
                if not runtime.bus.inbound.empty():
                    await run_main(runtime.bus.inbound.get_nowait())
            judgement = [dict(row) for row in runtime.store.db.execute("SELECT status,decision,attempts FROM judge_batches")]
            judgement_metrics = [dict(row) for row in runtime.store.db.execute("SELECT latency_ms,input_tokens,output_tokens,total_tokens,error_code FROM judge_attempts")]
            followup = runtime.store.followups.describe(memory_id).model_dump() if memory_id else None
            question = any("?" in text or "？" in text for text in publications)
            checks = {"main_completed": bool(turns) and all(not turn["error"] for turn in turns),
                "no_exact_repeat": not any(text in publications[:index] for index, text in enumerate(publications))}
            if name in {"idle_pending_question", "ordinary_statement", "old_story"}:
                checks["public_question"] = bool(publications) and question
            elif name in {"chat_invitation", "unanswered_invitation"}:
                checks["responded_to_chat_invitation"] = bool(publications)
            elif name in {"idle_pending_thought", "restart_pending", "compacted_pending"}:
                checks["published_thought"] = bool(publications)
            if name in {"idle_pending_question", "idle_pending_thought", "old_story", "restart_pending", "compacted_pending"}:
                checks["linked_attempt"] = followup is not None and followup["confirmed_attempts"] == 1
            if name == "short_reply":
                checks["responded_without_extra_attempt"] = bool(publications) and followup is not None and followup["confirmed_attempts"] == 1 and followup["followup_state"] in {"responded", "completed"}
            if name in {"short_reply", "ordinary_statement"}:
                checks["no_unsupported_weather"] = not any(word in text for text in publications for word in ("不晒", "最晒", "天气看着", "天气不错"))
            if name == "two_unanswered":
                checks["limit_preserved"] = followup is not None and followup["confirmed_attempts"] == 2 and followup["followup_state"] == "dropped"
            if name == "explicit_end":
                checks["dropped_without_question"] = followup is not None and followup["followup_state"] in {"dropped", "completed"} and not question
            if name == "silence":
                checks["no_public_reply"] = not publications
            if name == "no_reply":
                checks["timeout_zero_calls"] = no_reply_calls == 0
                checks["later_idle_reachable"] = len(turns) == 1
            if name == "interest_search":
                checks["bounded_live_web_and_publication"] = args.live_web and 1 <= len(web_trace) <= 3 and bool(publications)
            if name == "search_failed":
                checks["bounded_failed_search"] = 1 <= len(web_trace) <= 3
            if name in {"search_failed", "search_disabled"}:
                checks["unavailable_web_stays_quiet"] = not publications
            if input_text is None:
                checks["idle_bypassed_judge"] = not judgement
            result = {"case": name, "input_text": input_text, "automated_checks": checks,
                "elapsed_ms": round((time.monotonic()-case_started)*1000),
                "matched": all(checks.values()), "needs_semantic_review": True, "publications": publications,
                "has_question_mark": question, "turns": turns, "followup": followup,
                "no_reply_calls": no_reply_calls, "web": web_trace, "judge": judgement, "judge_metrics": judgement_metrics,
                "main_calls": provider.calls[initial_calls:], "proactive": runtime.proactive.status(SCOPE.session_key),
                "actions": [dict(row) for row in runtime.store.db.execute("SELECT status,message_id,group_id FROM actions WHERE message_id>=500")],
                "ordinary_deliveries": [dict(row) for row in runtime.store.db.execute("SELECT status,message_ids FROM message_deliveries")]}
            return result
        finally:
            runtime.close()


async def evaluate(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    config.agents.defaults.fallback_models = []
    preset = config.resolve_preset()
    provider = MeasuredProvider(make_provider(config, preset=preset))
    instance = TelegramSocialConfig.model_validate(json.loads(args.config.read_text())["channels"]["telegram"]["social"])
    report: dict[str, Any] = {"runtime_module": inspect.getfile(TelegramSocialRuntime), "main_model": preset.model,
        "temperature": provider.generation.temperature, "reasoning_effort": provider.generation.reasoning_effort,
        "max_tokens": 4096, "controlled_timer_sample": 0.0, "web_lookup_guidance": 3,
        "judge_model": instance.judge.model, "judge_reasoning_effort": instance.judge.reasoning_effort,
        "instance_policy": {"max_requests_per_hour": instance.max_requests_per_hour,
            "max_sends_per_hour": instance.max_sends_per_hour,
            "scopes": [{"proactive": scope.proactive.model_dump(), "quiet_hours": scope.quiet_hours.model_dump()} for scope in instance.scopes]},
        "results": []}
    for name in args.case or CASES:
        try:
            result = await evaluate_case(name, args, config, provider)
        except Exception as exc:
            result = {"case": name, "matched": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}
        report["results"].append(result)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        args.output.chmod(0o600)
        print(json.dumps({key: value for key, value in result.items() if key in {"case", "matched", "automated_checks", "publications", "error_type", "error"}}, ensure_ascii=False), flush=True)
    main_calls = [call for result in report["results"] for call in result.get("main_calls", [])]
    judge_calls = [call for result in report["results"] for call in result.get("judge_metrics", [])]
    report["summary"] = {"cases": len(report["results"]), "automated_passes": sum(result["matched"] for result in report["results"]),
        "main_calls": len(main_calls), "judge_calls": len(judge_calls),
        "main_tokens": sum((call.get("usage") or {}).get("total_tokens", 0) for call in main_calls),
        "judge_tokens": sum(call.get("total_tokens") or 0 for call in judge_calls),
        "main_turn_elapsed_ms": [turn["elapsed_ms"] for result in report["results"] for turn in result.get("turns", [])],
        "judge_elapsed_ms": [call["latency_ms"] for call in judge_calls],
        "web_calls": sum(len(result.get("web", [])) for result in report["results"]),
        "semantic_review": "Review all publications and retrieved sources; automated pass is not a naturalness judgement."}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    if not all(result["matched"] for result in report["results"]):
        raise SystemExit("V4-C structural checks failed; inspect report and review semantic quality separately")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--live-web", action="store_true", help="Use existing configured web tools for interest_search")
    parser.add_argument("--persona", type=Path, help="Use the reviewed role file for judge and main turns")
    logger.remove()
    asyncio.run(evaluate(parser.parse_args()))
