"""Channel-owned message batching around the single intermediary component."""

# These two channel-owned objects share their private lifecycle, not an external API.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.channels.telegram.social.judge import SocialJudge, make_judge_provider
from nanobot.channels.telegram.social.judge_models import (
    JudgeIntent,
    JudgeMessage,
    JudgeReply,
    JudgeSnapshot,
)
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    ProactiveCandidate,
    QuotedMessage,
)
from nanobot.config.schema import Config
from nanobot.providers.base import LLMProvider

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime


class JudgeCoordinator:
    def __init__(self, runtime: TelegramSocialRuntime, provider: LLMProvider | None = None,
                 provider_version: str = "") -> None:
        self.runtime = runtime
        self.store = runtime.store
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.provider = provider
        self.provider_version = provider_version
        self.configure()

    def configure(self) -> None:
        if self.provider is None:
            self.provider, self.provider_version = make_judge_provider(Config(), self.runtime.config.judge)
        self.judge = SocialJudge(self.store, self.runtime.config.judge, self.provider,
                                 provider_version=self.provider_version)

    def set_provider(self, provider: LLMProvider, version: str) -> None:
        previous = self.judge.signature
        self.provider, self.provider_version = provider, version
        self.configure()
        if self.judge.signature != previous:
            with self.store.db:
                self.store.db.execute("UPDATE judge_batches SET status='cancelled',error_code='provider_changed' "
                                      "WHERE status IN ('pending','running') AND signature<>?", (self.judge.signature,))
        self.runtime.scheduler.notify()

    def cancel(self) -> None:
        for task in self.tasks.values():
            task.cancel()

    def coalesce_delay(self, scope: ConversationScope) -> float:
        binding = self.runtime.binding(scope)
        assert binding is not None
        conditions, _ = self.runtime._wake_conditions(scope.session_key)
        delay = conditions.coalesce_seconds if conditions.coalesce_seconds is not None else binding.coalesce_seconds
        delay = self.runtime.config.coalesce_seconds if delay is None else delay
        wait = self.store.db.execute("SELECT purpose,quiet_seconds FROM dialogue_waits WHERE scope=?", (scope.session_key,)).fetchone()
        if wait and wait["purpose"] in {"reply", "messages"}:
            delay = float(wait["quiet_seconds"])
        return delay

    def enqueue(self, scope: ConversationScope, seq: int, opportunity: str | None = None) -> None:
        binding = self.runtime.binding(scope)
        if binding is None or binding.paused:
            return
        delay = self.coalesce_delay(scope)
        self.store.queue_judgement(scope.session_key, seq, delay=delay,
                                    maximum=self.runtime.config.dynamic_wait.max_seconds, opportunity=opportunity)

    def _intent(self, scope: ConversationScope) -> JudgeIntent | None:
        binding = self.runtime.binding(scope)
        assert binding is not None
        state = self.store.state(scope.session_key)
        conditions, dynamic = self.runtime._wake_conditions(scope.session_key)
        wait = self.store.db.execute("SELECT * FROM dialogue_waits WHERE scope=?", (scope.session_key,)).fetchone()
        keywords = conditions.keywords if dynamic else binding.wake_keywords
        senders = conditions.senders if dynamic else binding.wake_senders
        if not (wait or dynamic or keywords or senders or binding.wake_on_messages or binding.wake_on_question or state["mode"] in {"waiting", "sleeping"}):
            return None
        return JudgeIntent(
            purpose=wait["purpose"] if wait else None,
            description="An explicit wait is selective; ordinary unrelated messages need not interrupt it. Clear calls remain reachable. "
                        + json.dumps({"waiting_for_messages": conditions.on_messages if dynamic else binding.wake_on_messages,
                                      "waiting_for_questions": conditions.on_question if dynamic else binding.wake_on_question}),
            keywords=tuple(keywords), senders=tuple(senders),
            until=float(wait["total_due"]) if wait and wait["total_due"] is not None else state["wake_at"],
            infinite=bool(state["wait_infinite"]),
            condition_expires=conditions.expires_at if dynamic else None,
            one_shot=conditions.one_shot if dynamic else False,
            interrupt_policy=json.dumps({"operator": binding.sleep_interrupts.model_dump(),
                                          "temporary": conditions.sleep_interrupts.model_dump() if conditions.sleep_interrupts else None}),
        )

    def _message(self, row: object) -> JudgeMessage:
        # sqlite3.Row is typed at the dynamic storage boundary.
        assert isinstance(row, sqlite3.Row)
        facts = json.loads(row["judge_facts"])
        quoted = QuotedMessage.model_validate_json(row["reply_context"]) if row["reply_context"] else None
        reply = JudgeReply(kind="topic_root" if quoted.is_topic_root else "bot" if quoted.is_bot else "member",
                           message_id=quoted.message_id, sender_id=quoted.sender_id, text=quoted.text) if quoted else None
        if reply is None and row["reply_to"] is not None:
            target = self.store.db.execute("SELECT * FROM messages WHERE scope=? AND message_id=?",
                                           (row["scope"], row["reply_to"])).fetchone()
            if target is not None:
                reply = JudgeReply(kind="bot" if target["is_self"] else "member", message_id=int(target["message_id"]),
                                   sender_id=str(target["sender_id"]), text=str(target["text"])[:1000])
        text = str(row["text"])
        return JudgeMessage(seq=int(row["seq"]), message_id=int(row["message_id"]), sender_id=str(row["sender_id"]),
                            name=str(row["display_name"]), timestamp=float(row["at"]), text=text,
                            is_self=bool(row["is_self"]), mentions_bot=bool(facts.get("directed")), reply=reply,
                            media=tuple(json.loads(row["media"])), content_hash=hashlib.sha256(text.encode()).hexdigest())

    def _restore(self, batch: str) -> JudgeSnapshot:
        row = self.store.db.execute("SELECT input_meta FROM judge_batches WHERE id=?", (batch,)).fetchone()
        metadata = json.loads(row[0])
        messages: list[JudgeMessage] = []
        for excerpt in metadata.pop("excerpts"):
            source = self.store.db.execute("SELECT * FROM messages WHERE scope=? AND seq=?", (metadata["scope"], excerpt["seq"])).fetchone()
            if source is None:
                raise ValueError("judge source no longer retained")
            message = self._message(source)
            if excerpt["content_hash"] != message.content_hash:
                raise ValueError("judge source changed")
            reply = message.reply.model_copy(update={"text": message.reply.text[:excerpt["reply_length"]]}) if message.reply else None
            messages.append(message.model_copy(update={"text": message.text[:excerpt["length"]], "reply": reply}))
        return JudgeSnapshot.model_validate_json(json.dumps({**metadata, "messages": [message.model_dump(mode="json") for message in messages]}))

    def _snapshot(self, scope: ConversationScope) -> JudgeSnapshot | None:
        key = scope.session_key
        pending = self.store.db.execute("SELECT * FROM judge_state WHERE scope=?", (key,)).fetchone()
        after = self.store.judge_through(key)
        rows = self.store.db.execute("SELECT * FROM messages WHERE scope=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                                    (key, after, pending["pending_seq"], self.runtime.config.judge.context_messages)).fetchall()
        # Leading non-events can be passed locally; this is assessment, never main read.
        while rows:
            facts = json.loads(rows[0]["judge_facts"])
            if facts.get("eligible") and float(facts.get("received_at",rows[0]["at"])) + self.runtime.config.judge.batch_ttl_seconds > self.runtime.now():
                break
            after = int(rows.pop(0)["seq"])
        with self.store.db:
            self.store.db.execute("UPDATE judge_state SET assessed_through=? WHERE scope=?", (after, key))
        if not rows and pending["opportunity"] is None:
            return None
        binding = self.runtime.binding(scope)
        assert binding is not None
        state = self.store.state(key)
        new = [self._message(row) for row in rows]
        through = new[-1].seq if new else after
        while new and not json.loads(rows[len(new)-1]["judge_facts"]).get("eligible"):
            new.pop()
        through = new[-1].seq if new else after
        if not new and pending["opportunity"] is None:
            return None
        history = [self._message(row) for row in reversed(self.store.db.execute(
            "SELECT * FROM messages WHERE scope=? AND seq<=? ORDER BY seq DESC LIMIT ?",
            (key, after, max(0, self.runtime.config.judge.context_messages-len(new))),
        ).fetchall())]
        confirmed_after = [self._message(row) for row in self.store.db.execute(
            "SELECT * FROM messages WHERE scope=? AND seq>? AND is_self=1 ORDER BY seq LIMIT 3", (key,through),
        )]
        while history and len(history+new+confirmed_after) > self.runtime.config.judge.context_messages:
            history.pop(0)
        confirmed_after = confirmed_after[:max(0,self.runtime.config.judge.context_messages-len(history)-len(new))]
        now = self.runtime.now()
        arrival_times = [float(json.loads(row["judge_facts"]).get("received_at",row["at"])) for row in rows
                         if int(row["seq"]) <= through and json.loads(row["judge_facts"]).get("eligible")]
        created = min(arrival_times) if arrival_times else float(pending["pending_first"]) if pending["pending_first"] is not None else now
        persona, _ = self.runtime._persona(scope)
        expires = created+self.runtime.config.judge.batch_ttl_seconds
        if pending["opportunity"] and not new:
            expires = min(expires, ProactiveCandidate.model_validate_json(pending["opportunity"]).expires_at)
        selected_new = tuple(int(row["seq"]) for row in rows if int(row["seq"]) <= through and json.loads(row["judge_facts"]).get("eligible"))
        base: dict[str, object] = dict(scope=key, version=state["version"], after_seq=after, through_seq=through,
                    new_seqs=selected_new,
                    created=created, expires=expires,
                    bot_name=binding.nicknames[0] if binding.nicknames else "Bot", nicknames=tuple(binding.nicknames),
                    bot_username=next((json.loads(row["judge_facts"]).get("bot_username") for row in reversed(rows)
                                       if json.loads(row["judge_facts"]).get("bot_username")), ""),
                    role_summary=persona.split("# Operator persona\n",1)[-1][:600], background=binding.background[:1000],
                    mode=state["mode"] if state["mode"] in {"waiting","sleeping"} else "observing",
                    main_running=state["mode"]=="active", intent=self._intent(scope), opportunity=pending["opportunity"],
                    quiet_due=max(arrival_times)+self.coalesce_delay(scope) if arrival_times else now,
                    total_due=created+self.runtime.config.dynamic_wait.max_seconds if arrival_times else now)
        truncated = False
        while True:
            snapshot = JudgeSnapshot.model_validate({**base, "messages": tuple(history+new+confirmed_after), "history_truncated": truncated})
            if len(snapshot.model_dump_json()) <= self.runtime.config.judge.context_chars:
                return snapshot
            truncated = True
            if history:
                history.pop(0)
            elif confirmed_after and len(new)==1 and len(new[0].text)<=64:
                confirmed_after.pop(0)
            elif len(new) > 1:
                new.pop()
                while new and new[-1].seq not in selected_new:
                    new.pop()
                if not new:
                    raise ValueError("no eligible message fits judge context")
                base["through_seq"] = new[-1].seq
                base["new_seqs"] = tuple(seq for seq in selected_new if seq <= new[-1].seq)
                if not base["new_seqs"]:
                    raise ValueError("no eligible message fits judge context")
            elif new and len(new[0].text) > 64:
                new[0] = new[0].model_copy(update={"text": new[0].text[:len(new[0].text)//2]})
            else:
                raise ValueError("judge context budget cannot hold required facts")

    async def start_due(self) -> None:
        now = self.runtime.now()
        rows = self.store.db.execute("SELECT scope,batch_id FROM judge_state WHERE inflight=0 AND pending_due<=?", (now,)).fetchall()
        for row in rows:
            key = str(row["scope"])
            if key in self.tasks:
                continue
            scope = ConversationScope.from_session_key(key)
            if scope is None or self.runtime.binding(scope) is None or self.store.state(key)["mode"] == "paused":
                continue
            with self.store.db:
                self.store.db.execute("UPDATE judge_state SET inflight=1 WHERE scope=?", (key,))
            self.tasks[key] = asyncio.create_task(self._run(scope, row["batch_id"]), name="social-judge")
        if rows:
            await asyncio.sleep(0)  # Yield to independent model work; never await its network latency.

    async def _run(self, scope: ConversationScope, batch: str | None) -> None:
        key = scope.session_key
        snapshot = None
        result = None
        preparation_failed = False
        try:
            snapshot = self._restore(batch) if batch else self._snapshot(scope)
            if snapshot is None:
                return
            if batch is None:
                result = self.store.open_judge_batch(snapshot, self.judge.signature, self.judge.settings.model)
                batch = result.id
                self.store.freeze_judge_input(batch, snapshot)
                with self.store.db:
                    self.store.db.execute("UPDATE judge_state SET batch_id=?,pending_first=NULL,pending_due=NULL,opportunity=NULL WHERE scope=?", (batch, key))
            result = await self.judge.assess(snapshot)
        except asyncio.CancelledError:
            raise
        except sqlite3.Error:
            # A failed decision/handoff transaction must remain recoverable, not
            # advance assessment as if the input had been handled.
            if batch and not self.store.closed:
                with self.store.db:
                    self.store.db.execute("UPDATE judge_attempts SET finished=?,error_code='commit_failed' "
                                          "WHERE batch_id=? AND finished IS NULL", (self.runtime.now(),batch))
                    self.store.db.execute("UPDATE judge_batches SET status='pending',error_code='commit_failed',due=? "
                                          "WHERE id=? AND status='running'", (self.runtime.now()+1,batch))
                result = self.store.judge_record(batch)
            preparation_failed = result is None
        except Exception:
            if batch and not self.store.closed:
                with self.store.db:
                    self.store.db.execute("UPDATE judge_batches SET status='cancelled',error_code='invalid_input' WHERE id=? AND status IN ('pending','running')", (batch,))
            preparation_failed = snapshot is None
            logger.warning("Social intermediary input expired/changed or could not be prepared")
        finally:
            self.tasks.pop(key, None)
            if not self.store.closed:
                with self.store.db:
                    state = self.store.db.execute("SELECT * FROM judge_state WHERE scope=?", (key,)).fetchone()
                    if result is not None and result.status == "pending" and result.expires > self.runtime.now():
                        self.store.db.execute("UPDATE judge_state SET inflight=0,batch_id=?,pending_due=? WHERE scope=?",
                                              (result.id, min(result.due,result.expires), key))
                    else:
                        if snapshot is not None and (result is None or result.error_code != "provider_changed"):
                            self.store.db.execute("UPDATE judge_state SET assessed_through=MAX(assessed_through,?) WHERE scope=?",
                                                  (snapshot.through_seq,key))
                        remaining = self.store.db.execute("SELECT 1 FROM messages WHERE scope=? AND seq>? AND json_extract(judge_facts,'$.eligible')=1",
                                                          (key,self.store.judge_through(key))).fetchone()
                        due = self.runtime.now() if remaining or state["opportunity"] is not None else None
                        if remaining and state["pending_first"] is not None:
                            due = min(float(state["pending_last"])+self.coalesce_delay(scope),float(state["pending_max"]))
                        if preparation_failed:
                            end = float(state["pending_first"] or self.runtime.now()) + self.runtime.config.judge.batch_ttl_seconds
                            due = min(self.runtime.now()+5,end)
                            if self.runtime.now() >= end:
                                self.store.db.execute("UPDATE judge_state SET assessed_through=MAX(assessed_through,pending_seq),opportunity=NULL WHERE scope=?", (key,))
                                due = None
                        if self.store.state(key)["mode"] == "paused" or self.runtime.binding(scope) is None:
                            due = None
                        self.store.db.execute("UPDATE judge_state SET inflight=0,batch_id=NULL,pending_due=? WHERE scope=?", (due,key))
                self.runtime.scheduler.notify()
