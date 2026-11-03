"""Channel-owned message batching around the single intermediary component."""

# These two channel-owned objects share their private lifecycle, not an external API.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.judge import (
    JudgeProviderSettings,
    SocialJudge,
    make_judge_provider,
)
from nanobot.channels.telegram.social.judge_models import (
    JudgeFollowup,
    JudgeIntent,
    JudgeMessage,
    JudgeParticipation,
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
                 provider_version: JudgeProviderSettings = "") -> None:
        self.runtime = runtime
        self.store = runtime.store
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.provider = provider
        self.provider_version = provider_version
        self.configure()

    def configure(self, *, provider_changed: bool = False) -> None:
        if self.provider is None:
            self.provider, self.provider_version = make_judge_provider(Config(), self.runtime.config.judge)
        self.judge = SocialJudge(self.store, self.runtime.config.judge, self.provider,
                                 provider_changed=provider_changed, media_loader=self._media_content)
        # A restart can change the prompt as well as the provider. Reassess live
        # pending content with fresh facts instead of restoring an old wait intent.
        with transaction(self.store.db):
            self.store.judge.cancel_other_revisions(self.judge.revision)
            self.store.judge.requeue_provider_changes(self.runtime.now())

    def set_provider(self, provider: LLMProvider, version: JudgeProviderSettings) -> None:
        changed = version != self.provider_version
        self.provider, self.provider_version = provider, version
        self.configure(provider_changed=changed)
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
        wait = self.store.wakes.wait(scope.session_key)
        if wait and wait["purpose"] in {"reply", "messages"}:
            delay = float(wait["quiet_seconds"])
        return delay

    def enqueue(self, scope: ConversationScope, seq: int, opportunity: str | None = None) -> None:
        binding = self.runtime.binding(scope)
        if binding is None or binding.paused:
            return
        delay = self.coalesce_delay(scope)
        self.store.judge.queue(scope.session_key, seq, delay=delay,
                                    maximum=self.runtime.config.dynamic_wait.max_seconds, opportunity=opportunity)

    def intent(self, scope: ConversationScope) -> JudgeIntent | None:
        binding = self.runtime.binding(scope)
        assert binding is not None
        conditions, dynamic = self.runtime._wake_conditions(scope.session_key)
        state = self.store.scopes.state(scope.session_key)  # Expiring conditions can end an infinite wait.
        wait = self.store.wakes.wait(scope.session_key)
        dynamic = dynamic and bool(conditions.keywords or conditions.senders or conditions.on_messages or conditions.on_question)
        if not (wait or dynamic or state["mode"] in {"waiting", "sleeping"}):
            return None
        # Standing participation preferences must not become selective criteria
        # merely because a normal reply/messages wait is present.
        keywords = conditions.keywords if dynamic else []
        senders = conditions.senders if dynamic else []
        observing = wait is not None and wait["purpose"] == "observe"
        selective = dynamic and not observing
        return JudgeIntent(
            purpose=wait["purpose"] if wait else None,
            description=("Background observation before sleep, not a selective wait for questions. " if observing else
                         "An explicit wait is selective; ordinary unrelated messages need not interrupt it. Clear calls remain reachable. " if selective else
                         "Waiting for a relevant conversation continuation, including photos and short answers; not only questions. ")
                        + json.dumps({"waiting_for_messages": conditions.on_messages if selective else not observing,
                                      "waiting_for_questions": conditions.on_question if selective else False}),
            keywords=tuple(keywords), senders=tuple(senders),
            until=float(wait["total_due"]) if wait and wait["total_due"] is not None else state["wake_at"],
            infinite=bool(state["wait_infinite"]),
            condition_expires=conditions.expires_at if dynamic else None,
            one_shot=conditions.one_shot if dynamic else False,
            interrupt_policy=json.dumps({"operator": binding.sleep_interrupts.model_dump(),
                                          "temporary": conditions.sleep_interrupts.model_dump() if conditions.sleep_interrupts else None}),
        )

    async def _media_content(self, snapshot: JudgeSnapshot) -> list[dict[str, Any]]:
        scope = ConversationScope.from_session_key(snapshot.scope)
        assert scope is not None  # JudgeSnapshot validates the scope.

        def validate() -> None:
            if self.runtime.binding(scope) is None or not self.store.judge.current(snapshot):
                raise PermissionError("judge image batch is no longer current")

        validate()
        fresh = [message for message in snapshot.messages if message.seq in snapshot.new_seqs]
        targets = [(message.message_id, f"Message {message.message_id} (seq {message.seq})") for message in fresh if message.media]
        targets += [(message.reply.message_id, f"Quoted message {message.reply.message_id}, replied to by seq {message.seq}")
                    for message in fresh if message.reply is not None and message.reply.kind != "topic_root"]
        if not targets:
            return []
        blocks: list[dict[str, Any]] = []
        used: set[tuple[int, str]] = set()
        settings = self.runtime.config.judge
        limit = min(settings.media_max_images, self.runtime.config.media_max_images_per_turn)
        try:
            # Bound all image I/O together, leaving time for the actual decision.
            async with asyncio.timeout(min(settings.media_timeout_seconds, settings.timeout_seconds / 2)):
                async with self.runtime._media_lock:
                    for message_id, label in targets:
                        validate()
                        for ref in self.store.messages.media_for_message(snapshot.scope, message_id):
                            image_label = label
                            identity = (message_id, ref.unique_id)
                            if identity in used:
                                continue
                            if len(used) >= limit:
                                blocks.append({"type": "text", "text": "Other batch images omitted at the image limit; their content is unknown."})
                                return blocks
                            used.add(identity)
                            if ref.animated or ref.video or ref.kind in {"video", "animation"}:
                                if not ref.thumbnail_id:
                                    blocks.append({"type": "text", "text": f"{label}: no thumbnail; motion not decoded."})
                                    continue
                                ref = ref.model_copy(update={"file_id": ref.thumbnail_id, "size": None, "width": 0, "height": 0})
                                image_label += "; thumbnail only, motion not decoded"
                            images = await self.runtime.prepare_media(scope, ref, image_label, validate=validate,
                                                                      max_edge=settings.media_max_edge)
                            blocks.extend({key: value for key, value in block.items() if key != "_meta"} for block in images)
        except TimeoutError:
            blocks.append({"type": "text", "text": "Remaining images unavailable: image preparation timed out; do not infer their content."})
        validate()
        return blocks

    def _message(self, row: object) -> JudgeMessage:
        # sqlite3.Row is typed at the dynamic storage boundary.
        assert isinstance(row, sqlite3.Row)
        facts = json.loads(row["judge_facts"])
        quoted = QuotedMessage.model_validate_json(row["reply_context"]) if row["reply_context"] else None
        reply = JudgeReply(kind="topic_root" if quoted.is_topic_root else "bot" if quoted.is_bot else "member",
                           message_id=quoted.message_id, sender_id=quoted.sender_id, text=quoted.text) if quoted else None
        if reply is None and row["reply_to"] is not None:
            target = self.store.messages.by_message_id(row["scope"], row["reply_to"])
            if target is not None:
                reply = JudgeReply(kind="bot" if target["is_self"] else "member", message_id=int(target["message_id"]),
                                   sender_id=str(target["sender_id"]), text=str(target["text"])[:1000])
        text = str(row["text"])
        return JudgeMessage(seq=int(row["seq"]), message_id=int(row["message_id"]), sender_id=str(row["sender_id"]),
                            name=str(row["display_name"]), timestamp=float(row["at"]), text=text,
                            is_self=bool(row["is_self"]), mentions_bot=bool(facts.get("directed")), reply=reply,
                            media=tuple(json.loads(row["media"])), revision=int(row["revision"]))

    def _restore(self, batch: str) -> JudgeSnapshot:
        row = self.store.judge.input_metadata(batch)
        # The coordinator restores the persisted batch selected from judge_state.
        metadata = json.loads(cast(sqlite3.Row, row)[0])
        messages: list[JudgeMessage] = []
        for excerpt in metadata.pop("excerpts"):
            source = self.store.messages.by_seq(metadata["scope"], excerpt["seq"])
            if source is None:
                raise ValueError("judge source no longer retained")
            message = self._message(source)
            if excerpt["revision"] != message.revision:
                raise ValueError("judge source changed")
            reply = message.reply.model_copy(update={"text": message.reply.text[:excerpt["reply_length"]]}) if message.reply else None
            messages.append(message.model_copy(update={"text": message.text[:excerpt["length"]], "reply": reply}))
        return JudgeSnapshot.model_validate_json(json.dumps({**metadata, "messages": [message.model_dump(mode="json") for message in messages]}))

    def _snapshot(self, scope: ConversationScope) -> JudgeSnapshot | None:
        key = scope.session_key
        pending = self.store.judge.state(key)
        after = self.store.judge.through(key)
        rows = self.store.messages.range(key, after, pending["pending_seq"], self.runtime.config.judge.context_messages)
        # Leading non-events can be passed locally; this is assessment, never main read.
        while rows:
            facts = json.loads(rows[0]["judge_facts"])
            if facts.get("eligible") and float(facts.get("received_at",rows[0]["at"])) + self.runtime.config.judge.batch_ttl_seconds > self.runtime.now():
                break
            after = int(rows.pop(0)["seq"])
        with transaction(self.store.db):
            self.store.judge.set_through(after, key)
        if not rows and pending["opportunity"] is None:
            return None
        binding = self.runtime.binding(scope)
        assert binding is not None
        state = self.store.scopes.state(key)
        new = [self._message(row) for row in rows]
        through = new[-1].seq if new else after
        while new and not json.loads(rows[len(new)-1]["judge_facts"]).get("eligible"):
            new.pop()
        through = new[-1].seq if new else after
        if not new and pending["opportunity"] is None:
            return None
        history = [self._message(row) for row in reversed(self.store.messages.history_through(key, after, max(0, self.runtime.config.judge.context_messages-len(new))))]
        confirmed_after = [self._message(row) for row in self.store.messages.self_after(key, through)]
        while history and len(history+new+confirmed_after) > self.runtime.config.judge.context_messages:
            history.pop(0)
        confirmed_after = confirmed_after[:max(0,self.runtime.config.judge.context_messages-len(history)-len(new))]
        now = self.runtime.now()
        arrival_times = [float(json.loads(row["judge_facts"]).get("received_at",row["at"])) for row in rows
                         if int(row["seq"]) <= through and json.loads(row["judge_facts"]).get("eligible")]
        created = min(arrival_times) if arrival_times else float(pending["pending_first"]) if pending["pending_first"] is not None else now
        persona = self.runtime.bindings.resolve(scope).persona
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
                    main_running=state["mode"]=="active", intent=self.intent(scope), opportunity=pending["opportunity"],
                    followups=tuple(JudgeFollowup.model_validate(item) for item in self.store.followups.summary(key)),
                    participation=JudgeParticipation(keywords=tuple(binding.wake_keywords), senders=tuple(binding.wake_senders),
                                                     on_messages=binding.wake_on_messages, on_question=binding.wake_on_question),
                    quiet_due=max(arrival_times)+self.coalesce_delay(scope) if arrival_times else now,
                    total_due=created+self.runtime.config.dynamic_wait.max_seconds if arrival_times else now)
        truncated = False
        while True:
            snapshot = JudgeSnapshot.model_validate({**base, "messages": tuple(history+new+confirmed_after), "history_truncated": truncated})
            if len(snapshot.prompt_json()) <= self.runtime.config.judge.context_chars:
                return snapshot
            truncated = True
            if history:
                history.pop(0)
            elif base["followups"]:
                followups = snapshot.followups
                base["followups"] = followups[:-1]
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
        rows = self.store.judge.due_scopes(now)
        for row in rows:
            key = str(row["scope"])
            if key in self.tasks:
                continue
            scope = ConversationScope.from_session_key(key)
            if scope is None or self.runtime.binding(scope) is None or self.store.scopes.state(key)["mode"] == "paused":
                continue
            with transaction(self.store.db):
                self.store.judge.claim_scope(key)
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
                result = self.store.judge.open_batch(snapshot, self.judge.revision, self.judge.settings.model)
                batch = result.id
                with transaction(self.store.db):
                    self.store.judge.attach_batch(batch, key)
            result = await self.judge.assess(snapshot)
        except asyncio.CancelledError:
            raise
        except sqlite3.Error:
            # A failed decision/handoff transaction must remain recoverable, not
            # advance assessment as if the input had been handled.
            if batch and not self.store.closed:
                with transaction(self.store.db):
                    self.store.judge.finish_failed_commit(self.runtime.now(), batch)
                    self.store.judge.retry_failed_commit(self.runtime.now()+1, batch)
                result = self.store.judge.record(batch)
            preparation_failed = result is None
        except Exception:
            if batch and not self.store.closed:
                with transaction(self.store.db):
                    self.store.judge.cancel_invalid_input(batch)
            preparation_failed = snapshot is None
            logger.warning("Social intermediary input expired/changed or could not be prepared")
        finally:
            self.tasks.pop(key, None)
            if not self.store.closed:
                with transaction(self.store.db):
                    state = self.store.judge.state(key)
                    if result is not None and result.status == "pending" and result.expires > self.runtime.now():
                        self.store.judge.reschedule_batch(result.id, min(result.due,result.expires), key)
                    else:
                        if snapshot is not None and (result is None or result.error_code != "provider_changed"):
                            self.store.judge.advance_through(snapshot.through_seq, key)
                        remaining = self.store.messages.eligible_after(key, self.store.judge.through(key))
                        due = self.runtime.now() if remaining or state["opportunity"] is not None else None
                        if remaining and state["pending_first"] is not None:
                            due = min(float(state["pending_last"])+self.coalesce_delay(scope),float(state["pending_max"]))
                        if preparation_failed:
                            end = float(state["pending_first"] or self.runtime.now()) + self.runtime.config.judge.batch_ttl_seconds
                            due = min(self.runtime.now()+5,end)
                            if self.runtime.now() >= end:
                                self.store.judge.discard_pending(key)
                                due = None
                        if self.store.scopes.state(key)["mode"] == "paused" or self.runtime.binding(scope) is None:
                            due = None
                        self.store.judge.release_scope(due, key)
                self.runtime.scheduler.notify()
