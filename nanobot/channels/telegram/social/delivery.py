"""Public social delivery, send journals and confirmed-send effects."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections.abc import Awaitable, Callable, Sequence

from nanobot.agent.tools.context import RequestContext
from nanobot.bus.events import DeliveryReceipt
from nanobot.channels.telegram.social.bindings import ScopeBindings
from nanobot.channels.telegram.social.context import snapshot_text
from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    DeliveryResult,
    MediaReference,
    ObservedMessage,
    SocialDeliveryUnknownError,
    SocialRateLimitError,
)
from nanobot.channels.telegram.social.proactive import SocialProactive
from nanobot.channels.telegram.social.selection import ContextSelector
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.social.waiting import SocialWaiting
from nanobot.config.schema import TelegramSocialConfig
from nanobot.utils.helpers import strip_think

SocialSender = Callable[[ConversationScope, str, int | None], Awaitable[int]]

NEW_MESSAGES_NOTICE = "NOT sent: new messages arrived after the snapshot you answered. Read them below and reply once to everything together (one send, text or parts); do not answer the earlier messages separately. If nothing changes your reply, send it again."


class SocialDelivery:
    """Every public send: permission check, journal, Telegram delivery, retries and confirmed-send effects."""

    def __init__(self, store: SocialStore, bindings: ScopeBindings, proactive: SocialProactive,
                 waiting: SocialWaiting, selector: ContextSelector, notify: Callable[[], None],
                 config: Callable[[], TelegramSocialConfig], now: Callable[[], float]) -> None:
        self.store = store
        self.bindings = bindings
        self.proactive = proactive
        self.waiting = waiting
        self.selector = selector
        self.notify = notify
        self.config = config
        self.now = now
        self.sender: SocialSender | None = None
        self.sticker_sender: SocialSender | None = None
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._send_rechecked: dict[str, str] = {}

    def followup_status(self, followup_id: str) -> dict[str, object]:
        return self.store.followups.describe(followup_id).model_dump()

    async def send_sticker(self, ctx: RequestContext, sticker_id: str, reply_to: int | None) -> DeliveryResult:
        scope = self.bindings.validate_context(ctx)
        ref = self.store.stickers.get(scope.session_key, sticker_id)
        if ref.animated or ref.video or ref.kind != "sticker":
            raise ValueError("only static stickers can be sent in this release")
        return await self.send(ctx, f"[sticker:{ref.unique_id}]", reply_to, sticker=ref)

    def _collect_new_messages(self, scope: ConversationScope, turn: str | None) -> None:
        """Before a new send, show member messages that arrived after this turn's latest exposed snapshot.

        Runs at most once per turn, so a busy group cannot bounce sends forever."""
        key = scope.session_key
        if turn is None:
            return
        if self._send_rechecked.get(key) == turn:
            return
        shown = self.store.views.shown_sequences(key, turn)
        if not shown:
            return
        fresh = [row for row in self.store.messages.after(key, max(shown), self.config().snapshot_messages) if not row["is_self"]]
        if not fresh:
            return
        self._send_rechecked[key] = turn
        raise PermissionError(NEW_MESSAGES_NOTICE + "\n" + snapshot_text(self.store, self.config(), scope, turn))

    def send_lock(self, scope: ConversationScope) -> asyncio.Lock:
        """All publishing tools serialize against the same scope's send journal."""
        return self._send_locks.setdefault(scope.session_key, asyncio.Lock())

    def _check_send_allowed(self, scope: ConversationScope, turn: str | None, version: str,
                            contents: Sequence[str], units: int) -> None:
        """Every new send passes here once, inside the scope's send lock."""
        key = scope.session_key
        now = self.now()
        if self.store.scopes.state(key)["mode"] == "paused":
            raise PermissionError("social conversation is paused")
        self.proactive.check_send(scope, turn, self.bindings.binding(scope))
        previous = self.store.deliveries.last_send(key)
        if previous is not None and now < previous + self.config().send_cooldown_seconds:
            raise PermissionError("send cooldown is active")
        if self.store.deliveries.queued(key):
            raise PermissionError("an earlier send is awaiting delivery")
        for content in contents:
            if self.store.deliveries.unknown_content(key, version, content, now - 86400):
                raise PermissionError("delivery of this text is unknown; do not blindly repeat it")
        if self.store.deliveries.attempt_count(key, now - 3600) + units > self.config().max_sends_per_hour:
            raise PermissionError("hourly send budget exhausted")

    def begin_message(self, scope: ConversationScope, content: str, turn: str | None, *, units: int,
                      followup_id: str | None) -> str | None:
        """Reserve one ordinary envelope; None when the scope has no binding."""
        binding = self.bindings.binding(scope)
        if binding is None:
            return None
        version = self.store.scopes.state(scope.session_key)["version"]
        if turn is not None and self.store.deliveries.attempted_content(scope.session_key, version, turn, content):
            raise PermissionError("delivery already attempted in this turn; do not repeat it")
        self._check_send_allowed(scope, turn, version, [content], units)
        return self.store.reserve_send(scope.session_key, version, content, kind="message", turn=turn, units=units, followup_id=followup_id)

    async def send(self, ctx: RequestContext, text: str, reply_to: int | None,
                   *, sticker: MediaReference | None = None, followup_id: str | None = None) -> DeliveryResult:
        scope = self.bindings.validate_context(ctx)
        text = self._public_text(text)
        if reply_to is not None and not self.store.messages.has_message(scope.session_key, reply_to):
            raise PermissionError("reply target was not observed in this group/topic")
        if (self.sticker_sender if sticker else self.sender) is None:
            raise RuntimeError("Telegram sender is unavailable")
        lock = self.send_lock(scope)
        async with lock:
            self.bindings.validate_context(ctx)
            version = self.store.scopes.state(scope.session_key)["version"]
            previous_action = self.store.deliveries.latest_content(scope.session_key, version, ctx.turn_id, "sticker" if sticker else "text", text)
            if previous_action is not None:
                return self._action_result(previous_action)
            self._collect_new_messages(scope, ctx.turn_id)
            self._check_send_allowed(scope, ctx.turn_id, version, [text], 1)
            action = self.store.reserve_send(scope.session_key, version, text,
                                             kind="sticker" if sticker else "text", turn=ctx.turn_id, sticker=sticker, followup_id=followup_id)
            with transaction(self.store.db):
                self.store.deliveries.set_reply_expiry(reply_to, ctx.turn_id, action)
            self.bindings.validate_context(ctx)
            return await self._deliver(scope, action, version, text, reply_to)

    def _public_text(self, text: str) -> str:
        text = strip_think(text)
        if not text.strip() or len(text) > self.config().max_text_chars or len(text.encode("utf-16-le")) // 2 > 4000:
            raise ValueError("message is empty or exceeds the configured text limit")
        return text

    @staticmethod
    def _action_result(row: sqlite3.Row) -> DeliveryResult:
        result: DeliveryResult = {"action_id": str(row["id"]), "status": "unknown", "retry": False}
        status = str(row["status"])
        if status in ("queued", "sent", "failed", "unknown"):
            result["status"] = status
            result["retry"] = status == "queued"
        message_ids = json.loads(row["message_ids"])
        if message_ids:
            result["message_id"] = int(message_ids[0])
        return result

    async def send_parts(self, ctx: RequestContext, parts: list[tuple[str, int | None]],
                         *, followup_id: str | None = None) -> list[DeliveryResult]:
        scope = self.bindings.validate_context(ctx)
        if not 2 <= len(parts) <= 3:
            raise ValueError("a burst must contain two or three short parts")
        parts = [(self._public_text(text), reply_to) for text, reply_to in parts]
        texts = [text for text, _ in parts]
        if len(set(texts)) != len(texts):
            raise ValueError("duplicate parts are not allowed")
        for _, reply_to in parts:
            if reply_to is not None and not self.store.messages.has_message(scope.session_key, reply_to):
                raise PermissionError("reply target was not observed in this group/topic")
        if self.sender is None:
            raise RuntimeError("Telegram sender is unavailable")
        assert ctx.turn_id is not None
        lock = self.send_lock(scope)
        async with lock:
            self.bindings.validate_context(ctx)
            version = self.store.scopes.state(scope.session_key)["version"]
            # Repeated identical calls return each child's receipt, including partial failures.
            for group in self.store.deliveries.turn_groups(scope.session_key, version, ctx.turn_id):
                rows = self.store.deliveries.group_parts(group[0])
                if [(str(row["content"]), row["reply_to"]) for row in rows] == parts:
                    return [self._action_result(row) for row in rows]
            self._collect_new_messages(scope, ctx.turn_id)
            self._check_send_allowed(scope, ctx.turn_id, version, texts, len(texts))
            rows = self.store.enqueue_parts(scope.session_key, version, ctx.turn_id, parts,
                                            followup_id=followup_id)
            self.notify()
            return [self._action_result(row) for row in rows]

    async def _deliver(self, scope: ConversationScope, action: str, version: str,
                       text: str, reply_to: int | None) -> DeliveryResult:
        row = self.store.deliveries.action_media(action)
        sticker = MediaReference.model_validate_json(row["media_ref"]) if row["kind"] == "sticker" else None
        try:
            if version != self.store.scopes.state(scope.session_key)["version"] or self.store.scopes.state(scope.session_key)["mode"] == "paused":
                raise PermissionError("send authorization changed")
            self.proactive.check_send(scope, str(row["turn_id"]) if row["turn_id"] else None, self.bindings.binding(scope))
            self.store.followups.check_delivery(action)
            if reply_to is not None and not self.store.messages.has_message(scope.session_key, reply_to):
                raise PermissionError("reply target is no longer observed here")
            sender = self.sticker_sender if sticker else self.sender
            if sender is None:
                raise RuntimeError("Telegram sender is unavailable")
            message_id = await sender(scope, sticker.file_id if sticker else text, reply_to)
        except SocialRateLimitError as exc:
            after = max(exc.retry_after, self.config().send_cooldown_seconds)
            queued = math.isfinite(after) and self.store.deliveries.queue_retry(action, after, reply_to)
            if not queued:
                self.store.delivery(action, "failed", error="rate limit exceeds retry bounds")
            self.notify()
            return {"action_id": action, "status": "queued" if queued else "failed",
                    "retry": queued, "retry_after": after if math.isfinite(after) else 0}
        except asyncio.CancelledError:
            self.store.delivery(action, "unknown", error="interrupted send")
            raise
        except (SocialDeliveryUnknownError, TimeoutError, ConnectionError):
            self.store.delivery(action, "unknown", error="platform outcome unknown")
            return {"action_id": action, "status": "unknown", "retry": False}
        except Exception:
            self.store.delivery(action, "failed", error="platform rejected send")
            return {"action_id": action, "status": "failed"}
        self.store.delivery(action, "sent", (message_id,))
        self.store.observe(ObservedMessage(
            scope=scope, update_id=-message_id, message_id=message_id,
            sender_id="bot", display_name="Bot", timestamp=self.now(),
            text=f"[sticker {sticker.emoji}]" if sticker else text,
            reply_to=reply_to, is_self=True, media=("sticker",) if sticker else (),
            media_refs=(sticker,) if sticker else (),
        ), self.config().retention_messages)
        # Every confirmed public send resets observation. A paced group establishes
        # Confirm the grouped delivery only after its final child is confirmed.
        child = self.store.deliveries.part_position(action)
        final = child["group_id"] is None or not self.store.deliveries.later_part(child["group_id"], child["ordinal"])
        if self.store.scopes.state(scope.session_key)["version"] == version:
            self._confirmed_send(scope, action, str(row["turn_id"]) if row["turn_id"] else None, text, final=final)
        return {"action_id": action, "status": "sent", "message_id": message_id}

    def _confirmed_send(self, scope: ConversationScope, action: str, turn: str | None,
                        text: str, *, final: bool = True) -> None:
        binding = self.bindings.binding(scope)
        if binding is None or binding.paused:
            return
        # Recent confirmed messages are dialogue facts. They do not arm a second wake route.
        self.store.wakes.note_sent(scope.session_key)
        sticker = self.store.deliveries.sent_sticker(action, scope.session_key)
        if sticker and sticker[0]:
            self.selector.confirm_sticker(scope.session_key, turn, action,
                                          MediaReference.model_validate_json(sticker[0]).unique_id)
        self.notify()
        if turn is not None and self.store.scopes.state(scope.session_key)["mode"] == "observing":
            self.waiting.keep_reply_intent(scope, turn)

    async def retry_due(self) -> None:
        for row in self.store.deliveries.ready_sends():
            scope = ConversationScope.from_session_key(str(row["scope"]))
            if scope is None or self.bindings.binding(scope) is None:
                self.store.delivery(str(row["id"]), "failed", error="scope revoked")
                continue
            lock = self.send_lock(scope)
            async with lock:
                action = str(row["id"])
                try:
                    self.proactive.check_send(scope, str(row["turn_id"]) if row["turn_id"] else None, self.bindings.binding(scope))
                except PermissionError:
                    self.store.delivery(action, "failed", error="proactive opportunity no longer valid")
                    continue
                predecessor = self.store.deliveries.send_predecessor(action)
                if predecessor is not None:
                    if predecessor["status"] in ("failed", "unknown"):
                        self.store.delivery(action, "failed", error="earlier part was not confirmed")
                    else:
                        self.store.deliveries.postpone_send(action, max(self.now() + 1, float(predecessor["due"] or 0)))
                    continue
                if row["expires"] is not None and float(row["expires"]) <= self.now():
                    self.store.delivery(action, "failed", error="send expired")
                    continue
                previous = self.store.deliveries.last_send(scope.session_key)
                if int(row["attempts"]) == 0 and previous is not None and self.now() < previous + self.config().send_cooldown_seconds:
                    self.store.deliveries.postpone_send(action, previous + self.config().send_cooldown_seconds)
                    continue
                if not self.store.claim_retry(str(row["id"]), self.config().max_sends_per_hour):
                    continue
                await self._deliver(scope, str(row["id"]), str(row["version"]), str(row["content"]),
                                    int(row["reply_to"]) if row["reply_to"] is not None else None)

    def finish_message(self, scope: ConversationScope, delivery_id: str, receipt: DeliveryReceipt,
                       texts: list[tuple[int, str]]) -> None:
        """Record the envelope's final receipt once, then confirmed-send side effects."""
        row = self.store.deliveries.action_media(delivery_id)
        changed = self.store.delivery(delivery_id, receipt.status, receipt.message_ids, receipt.detail)
        for message_id, text in texts:
            self.store.observe(ObservedMessage(scope, -message_id, message_id, "bot", "Bot", self.now(),
                                               text, is_self=True, is_bot=True), self.config().retention_messages)
        if row["version"] == self.store.scopes.state(scope.session_key)["version"] and changed and (receipt.status == "sent" or texts):
            self._confirmed_send(scope, delivery_id, row["turn_id"], "\n".join(text for _, text in texts), final=receipt.status == "sent")

    def followup_note(self, followup_id: str) -> str:
        info = self.store.followups.describe(followup_id)
        return f"remaining_attempts={info.remaining_attempts}; unresolved_send={info.unresolved_send}"

