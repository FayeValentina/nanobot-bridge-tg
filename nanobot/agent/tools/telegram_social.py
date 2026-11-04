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
from nanobot.channels.telegram.social.models import SendPart

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
    tool_description = "Private notebook for what you explore or play with in free time. list shows activities; start needs only a title (goal, motivation, next_step optional). progress saves a checkpoint; pause/resume/complete/block/abandon switch or leave an interest. Existing entries need activity_id and expected_revision. Nothing here is published and no deliverable is required."

    @property
    def parameters(self) -> dict[str, Any]:
        return ActivityRequest.model_json_schema()

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.activity_action(ctx, ActivityRequest.model_validate(kwargs))


class SocialJargonTool(_SocialTool):
    tool_name = "social_jargon"
    tool_description = "Look up and maintain this group's jargon silently: list/get/submit/explain/correct/disable/set_status/review. States: candidate (uncertain), understand_only (understand, never reuse), usable, disabled. submit needs human message_ids and a short term; explain quotes a human explanation verbatim; writes need expected_revision. review queues a background recheck."

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


class SocialStateTool(_SocialTool):
    tool_name = "social_state"
    tool_description = "action=get: role_id, role_file, mode, waiting state, proactive_paused and remaining budgets. action=read: bounded unread or recent messages of this group/topic. action=set_proactive with paused=true/false: stop or resume your own proactive contact when a member asks; replies to members are unaffected."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["get", "read", "set_proactive"]},
                "paused": {"type": "boolean"},
            }, "required": ["action"], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        if kwargs["action"] == "get":
            return self.runtime.get_state(ctx)
        if kwargs["action"] == "read":
            return self.runtime.read_messages(ctx)
        if "paused" not in kwargs:
            raise ValueError("set_proactive requires paused")
        return self.runtime.waiting.set_proactive(ctx, kwargs["paused"])


class SocialSendTool(_SocialTool):
    tool_name = "social_send"
    tool_description = "Publish in this group/topic. Pass exactly one of text (one message), sticker_id (one collected static sticker) or parts (the complete ordered list of 2–3 messages, including the first; each a string, {text, reply_to} or {sticker_id, reply_to}). Omit reply_to unless the target would otherwise be unclear. Pass followup_id only for a proactive follow-up on a saved matter."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
                "followup_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                "sticker_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "reply_to": {"type": "integer", "minimum": 1},
                "parts": {"type": "array", "items": {"anyOf": [
                              {"type": "string", "minLength": 1, "maxLength": 4000},
                              {"type": "object", "properties": {
                                  "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                                  "reply_to": {"type": "integer", "minimum": 1}},
                               "required": ["text"], "additionalProperties": False},
                              {"type": "object", "properties": {
                                  "sticker_id": {"type": "string", "minLength": 1, "maxLength": 256},
                                  "reply_to": {"type": "integer", "minimum": 1}},
                               "required": ["sticker_id"], "additionalProperties": False}]},
                          "minItems": 2, "maxItems": 3},
            }, "required": [], "additionalProperties": False,
                "oneOf": [{"required": ["text"]}, {"required": ["sticker_id"]}, {"required": ["parts"]}]}

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not errors and sum(key in params for key in ("text", "sticker_id", "parts")) != 1:
            errors.append(self._send_shape_error())
        return errors

    @staticmethod
    def _send_shape_error() -> str:
        return "Provide exactly one of text, sticker_id or parts: text for one message, sticker_id for one sticker, or parts=[first, second, ...] for the complete 2–3 message sequence. No messages were queued."

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        if sum(key in kwargs for key in ("text", "sticker_id", "parts")) != 1:
            raise ValueError(self._send_shape_error())
        if "parts" in kwargs:
            parts = [
                SendPart(part, None, None) if isinstance(part, str)
                else SendPart(part.get("text"), part.get("sticker_id"), part.get("reply_to"))
                for part in kwargs["parts"]]
            if parts and parts[0].reply_to is None:
                parts[0] = parts[0]._replace(reply_to=kwargs.get("reply_to"))
            result = {"actions": await self.runtime.delivery.send_parts(ctx, parts, followup_id=kwargs.get("followup_id"))}
        elif "sticker_id" in kwargs:
            result = await self.runtime.delivery.send_sticker(ctx, kwargs["sticker_id"], kwargs.get("reply_to"), followup_id=kwargs.get("followup_id"))
        else:
            result = await self.runtime.delivery.send(ctx, kwargs["text"], kwargs.get("reply_to"), followup_id=kwargs.get("followup_id"))
        if kwargs.get("followup_id"):
            result = {**result, "followup": self.runtime.delivery.followup_status(kwargs["followup_id"])}
        return result


class SocialWaitTool(_SocialTool):
    tool_name = "social_wait"
    tool_description = "End this turn and wait. Choose one duration: seconds (total), until (ISO time with offset/Z) or infinite=true. purpose=messages waits for new content, then quiet; purpose=reply waits for quiet on an unfinished request and may return at once. sleeping=true sleeps. on_timeout=true wakes once if nothing arrives. until_condition says who or what you are waiting for."

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
                "until_condition": {"type": "object", "properties": {
                    "members": {"type": "array", "maxItems": 20, "items": {"type": "string", "pattern": "^[0-9]+$"}},
                    "topics": {"type": "array", "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 80}},
                    "questions": {"type": "boolean"},
                    "one_shot": {"type": "boolean"},
                    "ttl_seconds": {"type": "number", "minimum": 1, "maximum": 86400},
                }, "additionalProperties": False},
            }, "required": [], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        conditions = None
        if "until_condition" in kwargs:
            condition = kwargs["until_condition"]
            conditions = self.runtime.waiting.set_conditions(ctx, members=condition.get("members", []),
                topics=condition.get("topics", []), questions=condition.get("questions", False),
                one_shot=condition.get("one_shot", True), ttl_seconds=condition.get("ttl_seconds", 3600))
        result = self.runtime.waiting.wait(ctx, kwargs.get("seconds"), sleeping=kwargs.get("sleeping", False),
                                   on_timeout=kwargs.get("on_timeout", False), purpose=kwargs.get("purpose"),
                                   quiet_seconds=kwargs.get("quiet_seconds"), infinite=kwargs.get("infinite", False),
                                   until=kwargs.get("until"))
        if conditions is not None:
            result["until_condition"] = conditions
        return result


class SocialMediaTool(_SocialTool):
    tool_name = "social_media"
    tool_description = "View images/sticker thumbnails from an observed message_id here, including quoted messages. Bounded per turn; motion uses thumbnails."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"message_id": {"type": "integer", "minimum": 1}}, "required": ["message_id"], "additionalProperties": False}

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return await self.runtime.media.get(ctx, kwargs["message_id"])


class SocialStickerTool(_SocialTool):
    tool_name = "social_sticker"
    tool_description = "Manage the static sticker library; send with social_send. import_set takes set_name (addstickers link or name). view shows numbered sheets of up to 30 (unlearned_only=true by default; after annotate, view offset=0 for the next batch). annotate takes notes=[{sticker_id,note}] for stickers viewed this turn: visible text, emotion, uses. list searches notes; collect saves one from a message_id."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "collect", "import_set", "view", "annotate"]},
                "message_id": {"type": "integer", "minimum": 1},
                "sticker_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "note": {"type": "string", "maxLength": 400},
                "query": {"type": "string", "maxLength": 100},
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
        elif action == "import_set" and "set_name" in kwargs:
            result = await self.runtime.stickers.import_set(ctx, kwargs["set_name"])
        elif action == "view":
            return await self.runtime.stickers.view(ctx, set_name=kwargs.get("set_name", ""),
                sticker_id=kwargs.get("sticker_id", ""), offset=kwargs.get("offset", 0),
                limit=kwargs.get("limit", 30), unlearned_only=kwargs.get("unlearned_only", True))
        elif action == "annotate" and "notes" in kwargs:
            result = self.runtime.stickers.annotate(ctx, kwargs["notes"])
        else:
            raise ValueError("collect requires message_id; import_set requires set_name; annotate requires notes")
        return result


class SocialMemoryTool(_SocialTool):
    tool_name = "social_memory"
    tool_description = "Manage conversation knowledge and unfinished matters: list/record/confirm/correct/delete/clear/followup. Member facts need message_ids displayed this turn; member_ids come from their senders. Own intentions use origin=self with content, pending_question or pending_thought, and motivation. followup edits the SAME memory_id (followup_at, followup_state, state_reason). list shows remaining_attempts."

    @property
    def parameters(self) -> dict[str, Any]:
        return MemoryRequest.model_json_schema()

    async def run(self, ctx: RequestContext, **kwargs: Any) -> object:
        return self.runtime.memory_action(ctx, MemoryRequest.model_validate(kwargs))


SOCIAL_TOOL_CLASSES: dict[str, type[_SocialTool]] = {cls.tool_name: cls for cls in (
    SocialActivityTool,
    SocialJargonTool,
    SocialExpressionTool,
    SocialStateTool,
    SocialSendTool,
    SocialWaitTool,
    SocialMediaTool,
    SocialStickerTool,
    SocialMemoryTool,
)}
