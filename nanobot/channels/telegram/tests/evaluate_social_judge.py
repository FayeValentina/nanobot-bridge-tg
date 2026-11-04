"""Opt-in real-model evaluation, synthetic input and temporary SQLite only.

Run with python -m nanobot.channels.telegram.tests.evaluate_social_judge
--oauth-dir <independent Bot OAuth directory> --output <report.json>.
Never starts a Telegram receiver, sends public messages or changes live read state.
"""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from loguru import logger

from nanobot.agent.tools.context import RequestContext
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.judge import (
    JUDGE_PROMPT_VERSION,
    JudgeInput,
    JudgeVerdict,
    make_judge_provider,
)
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    ObservedMessage,
    QuotedMessage,
)
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.config.schema import (
    Config,
    TelegramSocialConfig,
    TelegramSocialJudgeConfig,
    TelegramSocialScopeConfig,
)
from nanobot.utils.prompt_templates import render_template


async def evaluate(args):
    settings = TelegramSocialJudgeConfig(oauth_storage_dir=args.oauth_dir)
    provider, version = make_judge_provider(Config(), settings)
    if args.snapshot:
        # Historical model-input replay only: never hand an expired batch to the
        # scheduler or change the live assessment/read watermarks.
        snapshot = JudgeInput.model_validate_json(args.snapshot.read_text())
        results = []
        identity = {"bot_username": snapshot.bot_username, "bot_name": snapshot.bot_name, "nicknames": snapshot.nicknames}
        prompt = args.system_prompt.read_text() if args.system_prompt else render_template("telegram/social_judge.md") + "\nRuntime target bot identity: " + json.dumps(identity, ensure_ascii=False)
        for index in range(args.repeat):
            response = await provider.chat(
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": snapshot.prompt_json()}],
                tools=None, model=settings.model, max_tokens=settings.max_tokens,
                temperature=0, reasoning_effort=settings.reasoning_effort, tool_choice="none")
            if response.tool_calls or response.finish_reason != "stop" or response.content is None:
                raise ValueError("historical replay did not return a final decision")
            decision = JudgeVerdict.model_validate_json(response.content)
            result = {"run": index+1, "decision": decision.model_dump(mode="json"),
                      "matched": args.expected is None or decision.decision == args.expected}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        args.output.write_text(json.dumps({"model": settings.model, "historical_input_replay": True,
            "messages": len(snapshot.context) + len(snapshot.new), "results": results}, ensure_ascii=False, indent=2))
        args.output.chmod(0o600)
        if not all(result["matched"] for result in results):
            raise SystemExit("Historical model-input replay has mismatches")
        return
    cases = json.loads(args.cases.read_text())
    results = []
    for case in cases:
        if args.case and case["name"] not in args.case:
            continue
        with tempfile.TemporaryDirectory(prefix="social-judge-eval-") as directory:
            root = Path(directory)
            (root / "roles").mkdir()
            (root / "roles/friend.md").write_text(args.persona.read_text() if args.persona else "你是肥鱼，作为群友自然地参与群聊。")
            scope = ConversationScope("telegram", -10001, 53)
            config = TelegramSocialConfig(enabled=True, coalesce_seconds=0, cooldown_seconds=0, judge=settings,
                scopes=[TelegramSocialScopeConfig(chat_id=scope.chat_id, thread_id=53, role_id="friend",
                    nicknames=["肥鱼", "肥魚"], wake_on_question=case.get("standing_questions", False))])
            config.sleep_observation.enabled = case.get("observe_sleep", False)
            runtime = TelegramSocialRuntime(config, root, MessageBus(), judge_provider=provider, judge_provider_version=version)
            try:
                for seq, (sender, text) in enumerate(case["history"], 1):
                    runtime.store.observe(ObservedMessage(scope, seq, seq, sender, sender, time.time()-10, text,
                                                          is_self=sender == "bot"), 500)
                if case.get("wait") or case.get("observe_sleep"):
                    job = uuid4().hex
                    now = runtime.now()
                    with runtime.store.db:
                        runtime.store.db.execute(
                            "INSERT INTO wakes(id,scope,version,trigger_seq,status,created,expires,due,requested) "
                            "VALUES(?,?,?,1,'queued',?,?,?,?)",
                            (job, scope.session_key, runtime.store.scopes.state(scope.session_key)["version"], now, now + 120, now, now),
                        )
                        runtime.store.db.execute("INSERT INTO requests VALUES(?,?)", (scope.session_key, now))
                    ctx = RequestContext(channel="telegram", chat_id=str(scope.chat_id), session_key=scope.session_key,
                        turn_id="setup", metadata={"message_thread_id": 53, "_social_job": job},
                        attributes={"context_version": runtime.store.scopes.state(scope.session_key)["version"]})
                    runtime.waiting.admit_request(ctx)
                    if case.get("wait"):
                        runtime.waiting.set_wake(ctx, keywords=["到家"], senders=["456"], on_messages=False, ttl_seconds=60)
                        runtime.waiting.wait(ctx, infinite=True, purpose="messages", quiet_seconds=0.1)
                    else:
                        runtime.waiting.wait(ctx, 30, sleeping=True)
                    runtime.store.wakes.finish("setup", "completed")
                elif case.get("sleeping"):
                    with runtime.store.db:
                        runtime.store.db.execute("UPDATE scopes SET mode='sleeping',wait_infinite=1 WHERE key=?", (scope.session_key,))
                seq = len(case["history"]) + 1
                quote = QuotedMessage(message_id=53, sender_id="creator", name="Creator", timestamp=time.time()-1000,
                                      text="", is_topic_root=True) if case.get("topic_root") else None
                message = ObservedMessage(scope, seq, seq, case.get("sender", "123"), "Member", time.time(), case["text"],
                    reply_to=53 if quote else None, reply=quote, bot_username="fish_bot")
                started = time.monotonic()
                await runtime.observe(message, directed=False)
                await asyncio.sleep(0.15)  # Explicit message-wait coalescing, never a held agent lock.
                await runtime.run_due()
                await asyncio.gather(*list(runtime.judging.tasks.values()))
                await runtime.run_due()
                row = runtime.store.db.execute("SELECT admission,condition_config FROM wakes WHERE admission IS NOT NULL ORDER BY created DESC LIMIT 1").fetchone()
                admission = json.loads(row["admission"]) if row else None
                decision = {"decision": "wake" if admission else "observe",
                            "fulfills_intent": bool(row and row["condition_config"] != "{}")}
                result = {"case": case["name"], "expected": case["expected"], "decision": decision,
                    "admission": admission,
                    "matched": decision["decision"] == case["expected"] and decision["fulfills_intent"] == case.get("fulfills", False),
                    "elapsed_ms": round((time.monotonic()-started)*1000),
                    "main_candidates": runtime.bus.inbound.qsize()}
                assert runtime.store.scopes.state(scope.session_key)["read_through"] == 0
                assert runtime.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
                # A setup wake is historical; it must not be delivered again.
                assert result["main_candidates"] == int(decision.get("decision") == "wake")
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            finally:
                runtime.close()
    report = {"model": settings.model, "reasoning_effort": settings.reasoning_effort,
              "prompt_version": JUDGE_PROMPT_VERSION, "results": results}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    if not results or not all(result["matched"] for result in results):
        raise SystemExit("Semantic evaluation has mismatches; inspect report")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oauth-dir", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("fixtures") / "social_judge_v3c.json")
    parser.add_argument("--case", action="append")
    parser.add_argument("--persona", type=Path, help="Use the reviewed role file in the actual 600-character summary")
    parser.add_argument("--snapshot", type=Path, help="Replay an exported JudgeInput without scheduling/delivery")
    parser.add_argument("--system-prompt", type=Path, help="Replay a saved baseline system prompt with --snapshot")
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 11))
    parser.add_argument("--expected", choices=["wake", "observe"])
    logger.remove()
    asyncio.run(evaluate(parser.parse_args()))
