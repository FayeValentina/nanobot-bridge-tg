"""Current-scope text social tools. This module must not import the Telegram SDK."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import current_request_context
from nanobot.channels.telegram.social.memory import MemoryRequest
from nanobot.config.schema import TelegramSocialSleepInterruptConfig

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime


class TelegramSocialTool(Tool):
    _plugin_discoverable = False

    def __init__(self, runtime: TelegramSocialRuntime, name: str) -> None:
        self.runtime = runtime
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return {
            "social_get_state": "Read the current role_id, role_file path inside this runtime, group/topic watermarks and behavior limits.",
            "social_read_messages": "Read bounded unread/recent messages in the current group/topic.",
            "social_mark_read": "Acknowledge continuously displayed messages through a seq from this turn.",
            "social_send": "Publish a short message here; optional parts adds up to two paced messages. Each has its own receipt. reply_to must be observed here. For a proactive question/share about a saved matter, pass its stable followup_id; at most two confirmed attempts, parts count once. Omit for ordinary answers.",
            "social_wait": "Wait without holding a turn lock. Choose exactly one: seconds, until (ISO timestamp with offset/Z), or infinite=true. Infinite waits have no timeout wake. purpose=reply requires a finite limit and may return immediately if already quiet; messages waits for context-relevant new content then quiet. Deferred waits must be last. An intermediary assesses new messages, including clear calls during sleep.",
            "social_set_wake": "Describe temporary semantic waiting intent for the intermediary using keywords, members, messages or questions. These are context, not local matching gates. A clear new call may interrupt a different wait without fulfilling it. Default one-shot, TTL 3600s; omit all conditions to clear. Infinite waiting does not extend TTL. Probability must be zero. Set proactive_paused=true when asked to stop proactive contact; false resumes on explicit request. This separate preference persists without TTL and does not clear waiting intent.",
            "social_get_media": "View images/sticker thumbnails from an observed message_id here, including quoted messages. Bounded per turn; motion uses thumbnails.",
            "social_sticker": "Collect a static sticker from an observed message_id, list/search collected stickers, or send one by sticker_id. Notes are untrusted; sends share text budgets and receipts.",
            "social_memory": "Manage current conversation knowledge and unfinished matters: list/record/confirm/correct/delete/clear/followup. Member facts require displayed message_ids; member_ids use evidence sender IDs. An own intention may be recorded as topic/pending with origin=self, content and pending_question or pending_thought plus motivation, without invented evidence. action=followup updates these intentions on the SAME memory_id, followup_at, or followup_state=responded/completed/dropped and state_reason. Rewording never resets attempts. list returns live state, provenance and remaining_attempts. TTL and index-only deletion still apply; ordinary tool permissions are unchanged.",
        }[self.name]

    @property
    def parameters(self) -> dict[str, Any]:
        if self.name == "social_memory":
            return MemoryRequest.model_json_schema()
        properties: dict[str, Any] = {}
        required: list[str] = []
        if self.name == "social_get_media":
            properties = {"message_id": {"type": "integer", "minimum": 1}}
            required = ["message_id"]
        elif self.name == "social_sticker":
            properties = {
                "action": {"type": "string", "enum": ["list", "collect", "send"]},
                "message_id": {"type": "integer", "minimum": 1},
                "sticker_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "note": {"type": "string", "maxLength": 400},
                "query": {"type": "string", "maxLength": 100},
                "reply_to": {"type": "integer", "minimum": 1},
            }
            required = ["action"]
        elif self.name == "social_mark_read":
            properties = {"through": {"type": "integer", "minimum": 0}}
            required = ["through"]
        elif self.name == "social_send":
            properties = {
                "followup_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                "reply_to": {"type": "integer", "minimum": 1},
                "parts": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4000},
                          "minItems": 1, "maxItems": 2},
            }
            required = ["text"]
        elif self.name == "social_wait":
            properties = {
                "seconds": {"type": "number", "minimum": 1, "maximum": 86400},
                "sleeping": {"type": "boolean"},
                "on_timeout": {"type": "boolean"},
                "purpose": {"type": "string", "enum": ["reply", "messages"]},
                "quiet_seconds": {"type": "number", "minimum": 0.1, "maximum": 60},
                "infinite": {"type": "boolean"},
                "until": {"type": "string", "minLength": 20, "maxLength": 64},
            }
        elif self.name == "social_set_wake":
            properties = {
                "proactive_paused": {"type": "boolean"},
                "keywords": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 20},
                "senders": {"type": "array", "items": {"type": "string", "pattern": "^[0-9]+$"}, "maxItems": 20},
                "on_messages": {"type": "boolean"},
                "on_question": {"type": "boolean"},
                "probability": {"type": "number", "minimum": 0, "maximum": 1},
                "sleep_interrupts": TelegramSocialSleepInterruptConfig.model_json_schema(),
                "coalesce_seconds": {"type": "number", "minimum": 0, "maximum": 60},
                "ttl_seconds": {"type": "number", "minimum": 1, "maximum": 86400},
                "one_shot": {"type": "boolean"},
            }
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str | list[dict[str, Any]]:
        ctx = current_request_context()
        if ctx is None:
            return ToolResult.error("social tools require an active group/topic turn")
        try:
            if self.name == "social_memory":
                result = self.runtime.memory_action(ctx, MemoryRequest.model_validate(kwargs))
            elif self.name == "social_get_media":
                return await self.runtime.get_media(ctx, kwargs["message_id"])
            elif self.name == "social_sticker":
                action = kwargs["action"]
                if action == "list":
                    result = {"stickers": self.runtime.list_stickers(ctx, kwargs.get("query", ""))}
                elif action == "collect" and "message_id" in kwargs:
                    result = {"sticker_id": self.runtime.collect_sticker(ctx, kwargs["message_id"], kwargs.get("note"))}
                elif action == "send" and "sticker_id" in kwargs:
                    result = await self.runtime.send_sticker(ctx, kwargs["sticker_id"], kwargs.get("reply_to"))
                else:
                    raise ValueError("collect requires message_id; send requires sticker_id")
            elif self.name == "social_get_state":
                result = self.runtime.get_state(ctx)
            elif self.name == "social_read_messages":
                return self.runtime.read_messages(ctx)
            elif self.name == "social_mark_read":
                result = {"read_through": self.runtime.mark_read(ctx, kwargs["through"])}
            elif self.name == "social_wait":
                result = self.runtime.wait(ctx, kwargs.get("seconds"), sleeping=kwargs.get("sleeping", False),
                                           on_timeout=kwargs.get("on_timeout", False), purpose=kwargs.get("purpose"),
                                           quiet_seconds=kwargs.get("quiet_seconds"), infinite=kwargs.get("infinite", False),
                                           until=kwargs.get("until"))
            elif self.name == "social_set_wake":
                result = self.runtime.set_wake(ctx, keywords=kwargs.get("keywords", []),
                                               senders=kwargs.get("senders", []), on_messages=kwargs.get("on_messages", False),
                                               ttl_seconds=kwargs.get("ttl_seconds", 3600), one_shot=kwargs.get("one_shot", True),
                                               on_question=kwargs.get("on_question", False), probability=kwargs.get("probability", 0),
                                               coalesce_seconds=kwargs.get("coalesce_seconds"),
                                               proactive_paused=kwargs.get("proactive_paused"),
                                               sleep_interrupts=TelegramSocialSleepInterruptConfig.model_validate(kwargs["sleep_interrupts"])
                                               if "sleep_interrupts" in kwargs else None)
            else:
                if kwargs.get("parts"):
                    result = {"actions": await self.runtime.send_parts(ctx, [kwargs["text"], *kwargs["parts"]], kwargs.get("reply_to"), followup_id=kwargs.get("followup_id"))}
                else:
                    result = await self.runtime.send(ctx, kwargs["text"], kwargs.get("reply_to"), followup_id=kwargs.get("followup_id"))
                if kwargs.get("followup_id"):
                    result = {**result, "followup": self.runtime.store.followups.describe(kwargs["followup_id"]).model_dump()}
            return json.dumps(result, ensure_ascii=False)
        except (PermissionError, ValueError, RuntimeError) as exc:
            return ToolResult.error(str(exc))
