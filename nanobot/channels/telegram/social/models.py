"""Typed records at the Telegram social boundary (no Telegram SDK dependency)."""

from dataclasses import dataclass
from typing import Literal, TypedDict


class SocialState(TypedDict):
    key: str
    channel: str
    chat_id: int
    thread_id: int
    version: str
    next_seq: int
    read_through: int
    seen_through: int
    mode: str
    wake_at: float | None
    wait_after_seq: int
    wake_on_timeout: bool
    wake_config: str
    event_seq: int
    timeout_through: int


class SnapshotMessage(TypedDict):
    seq: int
    message_id: int
    sender_id: str
    name: str
    timestamp: float
    age_seconds: int
    text: str
    reply_to: int | None
    media: list[str]
    is_self: bool


class UncertainSend(TypedDict):
    action_id: str
    text: str
    status: Literal["unknown"]


class Snapshot(TypedDict):
    view_id: str
    now: float
    read_through: int
    history_gap: list[int] | None
    messages: list[SnapshotMessage]
    wake_job: str | None
    uncertain_send: UncertainSend | None
    mode: str
    wake_reason: str | None


class WakeJob(TypedDict):
    id: str
    scope: str
    version: str
    trigger_seq: int
    status: str
    created: float
    expires: float
    turn_id: str | None
    due: float
    reason: str


class DeliveryResult(TypedDict, total=False):
    action_id: str
    status: Literal["queued", "sent", "failed", "unknown"]
    message_id: int
    retry: bool
    retry_after: float


@dataclass(frozen=True, slots=True)
class ConversationScope:
    channel: str
    chat_id: int
    thread_id: int = 0

    @property
    def session_key(self) -> str:
        suffix = f":topic:{self.thread_id}" if self.thread_id else ""
        return f"{self.channel}:{self.chat_id}{suffix}"

    @classmethod
    def from_session_key(cls, key: str) -> "ConversationScope | None":
        try:
            if ":topic:" in key:
                prefix, thread = key.rsplit(":topic:", 1)
                channel, chat = prefix.rsplit(":", 1)
                scope = cls(channel, int(chat), int(thread))
            else:
                channel, chat = key.rsplit(":", 1)
                scope = cls(channel, int(chat))
        except ValueError:
            return None
        return scope if scope.chat_id < 0 and scope.thread_id >= 0 and scope.session_key == key else None


@dataclass(frozen=True, slots=True)
class ObservedMessage:
    scope: ConversationScope
    update_id: int
    message_id: int
    sender_id: str
    display_name: str
    timestamp: float
    text: str
    reply_to: int | None = None
    media: tuple[str, ...] = ()
    is_self: bool = False
    is_bot: bool = False


class SocialDeliveryUnknownError(RuntimeError):
    """The platform may have accepted the send; do not retry it blindly."""


class SocialRateLimitError(RuntimeError):
    """Explicit platform rejection; a bounded retry is safe before expiry."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("platform rate limit")
        self.retry_after = retry_after
