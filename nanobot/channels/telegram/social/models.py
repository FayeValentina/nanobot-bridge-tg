"""Typed records at the Telegram social boundary (no Telegram SDK dependency)."""

from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class MediaReference(BaseModel):
    """Telegram-owned identifiers; never constructed from model tool parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["photo", "sticker", "animation", "video", "document"]
    file_id: str = Field(min_length=1, max_length=512)
    unique_id: str = Field(min_length=1, max_length=256)
    size: int | None = Field(default=None, ge=0)
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    emoji: str = Field(default="", max_length=64)
    sticker_set: str = Field(default="", max_length=128)
    animated: bool = False
    video: bool = False
    thumbnail_id: str | None = Field(default=None, max_length=512)
    mime: str = Field(default="", max_length=128)

    def summary(self) -> str:
        variant = "video" if self.video else "animated" if self.animated else "static"
        return f"{self.kind} {variant} {self.emoji}".strip()


class StickerSet(BaseModel):
    """A Telegram-owned set resolved by the channel adapter, never arbitrary file IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=64)
    title: str = Field(max_length=128)
    stickers: tuple[MediaReference, ...] = Field(max_length=1000)


class QuotedMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    message_id: int = Field(gt=0)
    sender_id: str = Field(max_length=64)
    name: str = Field(max_length=120)
    timestamp: float
    text: str = Field(max_length=1000)
    is_bot: bool = False
    is_topic_root: bool = False
    media_refs: list[MediaReference] = Field(default_factory=list, max_length=4)

    def summary(self) -> dict[str, object]:
        return {**self.model_dump(exclude={"media_refs"}),
                "media": [ref.summary() for ref in self.media_refs]}


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
    wait_infinite: bool


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
    media_details: list[str]
    is_self: bool
    reply: dict[str, object] | None
    album_id: str | None


class UncertainSend(TypedDict):
    action_id: str
    text: str
    status: Literal["unknown"]


class DialogueContext(TypedDict):
    purpose: str
    quiet: bool
    quiet_due: float
    total_due: float
    source_seq: int
    after_seq: int


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
    opportunity: dict[str, object] | None
    dialogue: DialogueContext | None
    judgement: list[dict[str, object]]


class ProactiveCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["memory", "unread", "configured", "idle"]
    seq: int = Field(ge=0)
    expires_at: float
    memory_id: str | None = None
    revision: int | None = None
    context: str = Field(default="", max_length=1000)
    opportunity_id: int | None = None


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
    media_refs: tuple[MediaReference, ...] = ()
    reply: QuotedMessage | None = None
    album_id: str | None = None
    bot_username: str | None = None
    is_edit: bool = False


class SocialDeliveryUnknownError(RuntimeError):
    """The platform may have accepted the send; do not retry it blindly."""


class SocialRateLimitError(RuntimeError):
    """Explicit platform rejection; a bounded retry is safe before expiry."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("platform rate limit")
        self.retry_after = retry_after
