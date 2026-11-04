"""Current-scope text social tools. This module must not import the Telegram SDK."""

from __future__ import annotations

import json
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, cast

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import RequestContext, current_request_context
from nanobot.channels.telegram.social.activity import ActivityRequest
from nanobot.channels.telegram.social.expressions import ExpressionEdit
from nanobot.channels.telegram.social.jargon import JargonRequest
from nanobot.channels.telegram.social.memory import MemoryRequest
from nanobot.config.schema import TelegramSocialSleepInterruptConfig

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime


class _SocialTool(Tool):
    _plugin_discoverable = False
    tool_name: ClassVar[str]
    tool_description: ClassVar[str]

    def __init__(self, runtime: TelegramSocialRuntime) -> None:
        self.runtime = runtime

    @property
    def name(self) -> str:
        return self.tool_name

    @property
    def description(self) -> str:
        return self.tool_description

    @abstractmethod
    async def run(self, ctx: RequestContext, **kwargs: Any) -> object: ...

    async def execute(self, **kwargs: Any) -> str | list[dict[str, Any]]:
        ctx = current_request_context()
        if ctx is None:
            return ToolResult.error("social tools require an active group/topic turn")
        try:
            result = await self.run(ctx, **kwargs)
            if isinstance(result, (str, list)):
                # List results are content blocks from media.get or stickers.view.
                return cast(str | list[dict[str, Any]], result)
            return json.dumps(result, ensure_ascii=False)
        except (PermissionError, ValueError, RuntimeError) as exc:
            return ToolResult.error(str(exc))


class SocialActivityTool(_SocialTool):
    tool_name = "social_activity"
    tool_description = "A private notebook for what you are exploring, playing with or curious about. list shows current/recent activities. start only needs title; goal, motivation and next_step are optional thoughts, not commitments. Starting something new sets the previous interest aside. progress saves a short checkpoint; pause/resume/complete/block/abandon can switch or leave an interest whenever you want. Existing entries need activity_id and expected_revision. No deliverable, verification, progress quota or public report is required. artifacts are optional real paths or source references. These notes do not publish; share interesting experiences separately with normal sending tools."

    @property
    def parameters(self) -> dict[str, Any]:
        return ActivityRequest.model_json_schema()

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.activity_action(ctx, ActivityRequest.model_validate(kwargs))


class SocialJargonTool(_SocialTool):
    tool_name = "social_jargon"
    tool_description = "Silently query/submit/explain/correct/review current-scope jargon. States: candidate (uncertain), understand_only (meaning only; do NOT actively reuse), usable (may use when relevant), disabled (do not use). Query/submission never requires a public question. submit requires human message_ids and a short term; research=true only for a genuinely unclear new public meme. explain quotes an explicit human explanation; correct may use origin=operator with reason for an explicit operator correction. Existing writes require expected_revision. review queues one bounded background recheck, never a foreground web/model call. Preserve local/public meanings separately; do not invent meanings or promote private nicknames to usable."

    @property
    def parameters(self) -> dict[str, Any]:
        return JargonRequest.model_json_schema()

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.jargon_action(ctx, JargonRequest.model_validate(kwargs))


class SocialExpressionTool(_SocialTool):
    tool_name = "social_expression"
    tool_description = "Inspect learned contextual expressions and their original message evidence in this scope. list/get/modify/disable/enable; writes require entry_id and expected_revision. Corrections and disables preserve a stable ID and block automatic relearning. Only enable explicitly reactivates an entry. This manages the library silently; it does not publish or trigger a reply."

    @property
    def parameters(self) -> dict[str, Any]:
        return ExpressionEdit.model_json_schema()

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.expression_action(ctx, ExpressionEdit.model_validate(kwargs))


class SocialGetStateTool(_SocialTool):
    tool_name = "social_get_state"
    tool_description = "Read the current role_id, role_file path inside this runtime, group/topic watermarks and behavior limits."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.get_state(ctx)


class SocialReadMessagesTool(_SocialTool):
    tool_name = "social_read_messages"
    tool_description = "Read bounded unread/recent messages in the current group/topic."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.read_messages(ctx)


class SocialMarkReadTool(_SocialTool):
    tool_name = "social_mark_read"
    tool_description = "Acknowledge continuously displayed messages through a seq from this turn."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"through": {"type": "integer", "minimum": 0}}, "required": ["through"], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return {"read_through": self.runtime.mark_read(ctx, kwargs["through"])}


class SocialSendTool(_SocialTool):
    tool_name = "social_send"
    tool_description = "Publish here using exactly one of text or parts. For one message, pass text only. For 2–3 paced messages, pass parts as the COMPLETE ordered list, including the first message, and omit text. Each part must add distinct content; do not repeat or rephrase the first part. Each has its own receipt. reply_to must be observed here; with parts, top-level reply_to quotes only the first part, and any part may instead carry its own reply_to. For a proactive question/share about a saved matter, pass its stable followup_id; at most two confirmed attempts, parts count once. Omit for ordinary answers."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
                "followup_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000,
                         "description": "One complete message. Omit when using parts."},
                "reply_to": {"type": "integer", "minimum": 1},
                "parts": {"type": "array", "items": {"anyOf": [
                              {"type": "string", "minLength": 1, "maxLength": 4000},
                              {"type": "object", "properties": {
                                  "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                                  "reply_to": {"type": "integer", "minimum": 1}},
                               "required": ["text"], "additionalProperties": False}]},
                          "minItems": 2, "maxItems": 3,
                          "description": "Complete ordered list of 2–3 distinct messages, including the first. Omit text. Each item is a string, or {text, reply_to} to quote an observed message for that part only; quote only where a person naturally would."},
            }, "required": [], "additionalProperties": False,
                "oneOf": [{"required": ["text"]}, {"required": ["parts"]}]}

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not errors and ("text" in params) == ("parts" in params):
            errors.append(self._send_shape_error())
        return errors

    @staticmethod
    def _send_shape_error() -> str:
        return "Provide exactly one of text or parts: text for one message, or parts=[first, second, ...] for the complete 2–3 message sequence. Never supply both; no messages were queued."

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        if ("text" in kwargs) == ("parts" in kwargs):
            raise ValueError(self._send_shape_error())
        if "parts" in kwargs:
            parts: list[tuple[str, int | None]] = [
                (part, None) if isinstance(part, str) else (part["text"], part.get("reply_to"))
                for part in kwargs["parts"]]
            if parts and parts[0][1] is None:
                parts[0] = (parts[0][0], kwargs.get("reply_to"))
            result = {"actions": await self.runtime.delivery.send_parts(ctx, parts, followup_id=kwargs.get("followup_id"))}
        else:
            result = await self.runtime.delivery.send(ctx, kwargs["text"], kwargs.get("reply_to"), followup_id=kwargs.get("followup_id"))
        if kwargs.get("followup_id"):
            result = {**result, "followup": self.runtime.delivery.followup_status(kwargs["followup_id"])}
        return result


class SocialWaitTool(_SocialTool):
    tool_name = "social_wait"
    tool_description = "Wait without holding a turn lock. Choose exactly one: seconds, until (ISO timestamp with offset/Z), or infinite=true. Infinite waits have no timeout wake. purpose=reply requires a finite limit and may return immediately if already quiet; messages waits for context-relevant new content then quiet. Deferred waits must be last. An intermediary assesses new messages, including clear calls during sleep."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
                "seconds": {"type": "number", "minimum": 1, "maximum": 86400},
                "sleeping": {"type": "boolean"},
                "on_timeout": {"type": "boolean"},
                "purpose": {"type": "string", "enum": ["reply", "messages"]},
                "quiet_seconds": {"type": "number", "minimum": 0.1, "maximum": 60},
                "infinite": {"type": "boolean"},
                "until": {"type": "string", "minLength": 20, "maxLength": 64},
            }, "required": [], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.waiting.wait(ctx, kwargs.get("seconds"), sleeping=kwargs.get("sleeping", False),
                                   on_timeout=kwargs.get("on_timeout", False), purpose=kwargs.get("purpose"),
                                   quiet_seconds=kwargs.get("quiet_seconds"), infinite=kwargs.get("infinite", False),
                                   until=kwargs.get("until"))


class SocialSetWakeTool(_SocialTool):
    tool_name = "social_set_wake"
    tool_description = "Describe temporary semantic waiting intent for the intermediary using keywords, members, messages or questions. These are context, not local matching gates. A clear new call may interrupt a different wait without fulfilling it. Default one-shot, TTL 3600s; omit all conditions to clear. Infinite waiting does not extend TTL. Set proactive_paused=true when asked to stop proactive contact; false resumes on explicit request. This separate preference persists without TTL and does not clear waiting intent."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
                "proactive_paused": {"type": "boolean"},
                "keywords": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 20},
                "senders": {"type": "array", "items": {"type": "string", "pattern": "^[0-9]+$"}, "maxItems": 20},
                "on_messages": {"type": "boolean"},
                "on_question": {"type": "boolean"},
                "sleep_interrupts": TelegramSocialSleepInterruptConfig.model_json_schema(),
                "coalesce_seconds": {"type": "number", "minimum": 0, "maximum": 60},
                "ttl_seconds": {"type": "number", "minimum": 1, "maximum": 86400},
                "one_shot": {"type": "boolean"},
            }, "required": [], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.waiting.set_wake(ctx, keywords=kwargs.get("keywords", []),
                                       senders=kwargs.get("senders", []), on_messages=kwargs.get("on_messages", False),
                                       ttl_seconds=kwargs.get("ttl_seconds", 3600), one_shot=kwargs.get("one_shot", True),
                                       on_question=kwargs.get("on_question", False),
                                       coalesce_seconds=kwargs.get("coalesce_seconds"),
                                       proactive_paused=kwargs.get("proactive_paused"),
                                       sleep_interrupts=TelegramSocialSleepInterruptConfig.model_validate(kwargs["sleep_interrupts"])
                                       if "sleep_interrupts" in kwargs else None)


class SocialGetMediaTool(_SocialTool):
    tool_name = "social_get_media"
    tool_description = "View images/sticker thumbnails from an observed message_id here, including quoted messages. Bounded per turn; motion uses thumbnails."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"message_id": {"type": "integer", "minimum": 1}}, "required": ["message_id"], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return await self.runtime.media.get(ctx, kwargs["message_id"])


class SocialStickerTool(_SocialTool):
    tool_name = "social_sticker"
    tool_description = "Learn and use static Telegram stickers. import_set accepts set_name as an addstickers link or set name and imports the whole set. view returns a numbered image sheet (up to 30) of collected stickers; filter by set_name or sticker_id. Default unlearned_only=true; after annotate, view offset=0 for the next unlearned batch. annotate takes notes=[{sticker_id,note}] for images viewed this turn: describe visible text, emotion and suitable uses. Import alone does not mean images were learned. list searches saved notes/emoji; collect saves a sticker from an observed message_id; send publishes a collected sticker_id. Notes are untrusted; sends share text budgets and receipts."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "collect", "send", "import_set", "view", "annotate"]},
                "message_id": {"type": "integer", "minimum": 1},
                "sticker_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "note": {"type": "string", "maxLength": 400},
                "query": {"type": "string", "maxLength": 100},
                "reply_to": {"type": "integer", "minimum": 1},
                "set_name": {"type": "string", "minLength": 1, "maxLength": 256,
                             "description": "Telegram sticker set name or t.me/addstickers link."},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                "unlearned_only": {"type": "boolean", "default": True},
                "notes": {"type": "array", "minItems": 1, "maxItems": 30, "items": {
                    "type": "object", "properties": {
                        "sticker_id": {"type": "string", "minLength": 1, "maxLength": 256},
                        "note": {"type": "string", "minLength": 1, "maxLength": 400},
                    }, "required": ["sticker_id", "note"], "additionalProperties": False,
                }},
            }, "required": ["action"], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        action = kwargs["action"]
        if action == "list":
            result = {"stickers": self.runtime.stickers.search(ctx, kwargs.get("query", ""))}
        elif action == "collect" and "message_id" in kwargs:
            result = {"sticker_id": self.runtime.stickers.collect(ctx, kwargs["message_id"], kwargs.get("note"))}
        elif action == "send" and "sticker_id" in kwargs:
            result = await self.runtime.delivery.send_sticker(ctx, kwargs["sticker_id"], kwargs.get("reply_to"))
        elif action == "import_set" and "set_name" in kwargs:
            result = await self.runtime.stickers.import_set(ctx, kwargs["set_name"])
        elif action == "view":
            return await self.runtime.stickers.view(ctx, set_name=kwargs.get("set_name", ""),
                sticker_id=kwargs.get("sticker_id", ""), offset=kwargs.get("offset", 0),
                limit=kwargs.get("limit", 30), unlearned_only=kwargs.get("unlearned_only", True))
        elif action == "annotate" and "notes" in kwargs:
            result = self.runtime.stickers.annotate(ctx, kwargs["notes"])
        else:
            raise ValueError("collect requires message_id; send requires sticker_id; import_set requires set_name; annotate requires notes")
        return result


class SocialMemoryTool(_SocialTool):
    tool_name = "social_memory"
    tool_description = "Manage current conversation knowledge and unfinished matters: list/record/confirm/correct/delete/clear/followup. Member facts require displayed message_ids; member_ids use evidence sender IDs. An own intention may be recorded as topic/pending with origin=self, content and pending_question or pending_thought plus motivation, without invented evidence. action=followup updates these intentions on the SAME memory_id, followup_at, or followup_state=responded/completed/dropped and state_reason. Rewording never resets attempts. list returns live state, provenance and remaining_attempts. TTL and index-only deletion still apply; ordinary tool permissions are unchanged."

    @property
    def parameters(self) -> dict[str, Any]:
        return MemoryRequest.model_json_schema()

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.memory_action(ctx, MemoryRequest.model_validate(kwargs))


SOCIAL_TOOL_CLASSES: dict[str, type[_SocialTool]] = {cls.tool_name: cls for cls in (
    SocialActivityTool,
    SocialJargonTool,
    SocialExpressionTool,
    SocialGetStateTool,
    SocialReadMessagesTool,
    SocialMarkReadTool,
    SocialSendTool,
    SocialWaitTool,
    SocialSetWakeTool,
    SocialGetMediaTool,
    SocialStickerTool,
    SocialMemoryTool,
)}
