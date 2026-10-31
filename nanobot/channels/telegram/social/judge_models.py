"""Frozen V3-A intermediary wire contracts; independent of main-model read state."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nanobot.channels.telegram.social.models import ConversationScope

JUDGE_PROMPT_VERSION = "social-judge-v8"


class JudgeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class JudgeReply(JudgeDTO):
    kind: Literal["bot", "member", "topic_root", "unknown"]
    message_id: int = Field(gt=0)
    sender_id: str = Field(default="", max_length=64)
    text: str = Field(default="", max_length=1000)


class JudgeMessage(JudgeDTO):
    seq: int = Field(gt=0)
    message_id: int = Field(gt=0)
    sender_id: str = Field(min_length=1, max_length=64)
    name: str = Field(max_length=120)
    timestamp: float = Field(ge=0, allow_inf_nan=False)
    text: str = Field(max_length=4000)
    is_self: bool = False
    mentions_bot: bool = False
    reply: JudgeReply | None = None
    media: tuple[str, ...] = Field(default=(), max_length=8)
    revision: int = Field(default=1, ge=1)


class JudgeIntent(JudgeDTO):
    """Meaning of an explicit wait, not a second local matching engine."""

    purpose: Literal["reply", "messages", "observe"] | None = None
    description: str = Field(default="", max_length=1000)
    senders: tuple[str, ...] = Field(default=(), max_length=20)
    keywords: tuple[str, ...] = Field(default=(), max_length=20)
    until: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    infinite: bool = False
    condition_expires: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    one_shot: bool = True
    interrupt_policy: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_deadline(self) -> JudgeIntent:
        if self.infinite and (self.until is not None or self.purpose == "reply"):
            raise ValueError("infinite wait cannot have a deadline or first-reply purpose")
        return self


class JudgeParticipation(JudgeDTO):
    """Standing preferences are context, never an explicit selective wait."""

    keywords: tuple[str, ...] = Field(default=(), max_length=20)
    senders: tuple[str, ...] = Field(default=(), max_length=20)
    on_messages: bool = False
    on_question: bool = False


class JudgeFollowup(JudgeDTO):
    id: str
    content: str = Field(max_length=120)
    pending_question: str = Field(max_length=160)
    pending_thought: str = Field(max_length=160)
    motivation: str = Field(max_length=80)
    followup_state: Literal["active", "responded"]
    confirmed_attempts: int = Field(ge=0, le=2)
    responded_at: float | None = None


class JudgeSnapshot(JudgeDTO):
    batch_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=64)
    scope: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=128)
    after_seq: int = Field(ge=0)
    through_seq: int = Field(ge=0)
    new_seqs: tuple[int, ...] = Field(max_length=100)
    messages: tuple[JudgeMessage, ...] = Field(max_length=100)
    created: float = Field(ge=0, allow_inf_nan=False)
    expires: float = Field(ge=0, allow_inf_nan=False)
    bot_name: str = Field(min_length=1, max_length=120)
    bot_username: str = Field(default="", max_length=120)
    nicknames: tuple[str, ...] = Field(default=(), max_length=20)
    role_summary: str = Field(default="", max_length=1000)
    background: str = Field(default="", max_length=4096)
    mode: Literal["observing", "waiting", "sleeping"] = "observing"
    main_running: bool = False
    intent: JudgeIntent | None = None
    participation: JudgeParticipation = Field(default_factory=JudgeParticipation)
    history_truncated: bool = False
    opportunity: str | None = Field(default=None, max_length=2000)
    followups: tuple[JudgeFollowup, ...] = Field(default=(), max_length=4)
    quiet_due: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    total_due: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_batch(self) -> JudgeSnapshot:
        if ConversationScope.from_session_key(self.scope) is None or not self.scope.startswith("telegram:"):
            raise ValueError("judge snapshot requires a Telegram group/topic scope")
        seqs = tuple(message.seq for message in self.messages)
        if seqs != tuple(sorted(set(seqs))) or self.new_seqs != tuple(sorted(set(self.new_seqs))):
            raise ValueError("message and new-message seqs must be sorted and unique")
        if not self.new_seqs and self.opportunity is None:
            raise ValueError("a message batch needs new messages")
        if self.new_seqs and (not self.after_seq < self.through_seq or self.through_seq != self.new_seqs[-1]):
            raise ValueError("through_seq must be the last new seq, after after_seq")
        if any(seq not in seqs or not self.after_seq < seq <= self.through_seq for seq in self.new_seqs):
            raise ValueError("new seqs must be displayed inside the batch range")
        if any(message.seq > self.through_seq and not message.is_self for message in self.messages) or self.expires <= self.created or self.through_seq < self.after_seq:
            raise ValueError("future context or invalid batch expiry")
        if any(message.is_self and message.seq in self.new_seqs for message in self.messages):
            raise ValueError("own messages provide context, not new judgement events")
        return self

    def prompt_json(self) -> str:
        """Only conversational facts belong in the model request."""
        return self.model_dump_json(exclude={
            "batch_id": True, "scope": True, "version": True, "expires": True,
            "quiet_due": True, "total_due": True,
            "messages": {"__all__": {"revision"}},
        })


class JudgeDecision(JudgeDTO):
    decision: Literal["wake", "observe"]
    through_seq: int = Field(ge=0)
    source_seqs: tuple[int, ...] = Field(max_length=100)
    reason: str = Field(min_length=1, max_length=240)
    fulfills_intent: bool = False

    def validate_for(self, snapshot: JudgeSnapshot) -> None:
        if self.through_seq != snapshot.through_seq:
            raise ValueError("decision must cover exactly the submitted batch")
        displayed = {message.seq for message in snapshot.messages if not message.is_self}
        if tuple(sorted(set(self.source_seqs))) != self.source_seqs or not set(self.source_seqs) <= displayed:
            raise ValueError("decision sources must be unique displayed member seqs")
        if self.decision == "wake" and not self.source_seqs and (snapshot.opportunity is None or displayed):
            raise ValueError("wake requires at least one displayed source")
        if self.decision == "observe" and self.fulfills_intent:
            raise ValueError("observing cannot fulfill an explicit wait")
        if self.decision == "observe" and self.source_seqs:
            raise ValueError("observe must not request wake sources")
        if not self.reason.strip():
            raise ValueError("decision reason must not be blank")


class JudgeRecord(JudgeDTO):
    id: str
    scope: str
    version: str
    judge_revision: int
    model: str
    prompt_version: str
    after_seq: int
    through_seq: int
    created: float
    expires: float
    due: float
    status: Literal["pending", "running", "decided", "fallback", "expired", "cancelled"]
    attempts: int
    decision: JudgeDecision | None
    error_code: str | None
    fallback_at: float | None
