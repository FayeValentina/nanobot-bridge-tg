"""Current-scope text social tools. This module must not import the Telegram SDK."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import current_request_context

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime


class TelegramSocialTool(Tool):
    _plugin_discoverable = False
    requires_explicit_allowlist: bool = True

    def __init__(self, runtime: TelegramSocialRuntime, name: str) -> None:
        self.runtime = runtime
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return {
            "social_get_state": "Read current group/topic watermarks and behavior limits.",
            "social_read_messages": "Read bounded unread/recent messages in the current group/topic.",
            "social_mark_read": "Acknowledge continuously displayed messages through a seq from this turn.",
            "social_send": "Publish a short message here; optional parts adds up to two paced follow-up messages. Each has its own receipt. reply_to must be observed here.",
            "social_wait": "End this turn now. Wait for new messages or sleep; optional one-shot timeout wake. Call last.",
            "social_set_wake": "Set bounded keyword/sender/message wake conditions for this group/topic, within operator limits.",
            "social_get_media": "View images/sticker thumbnails from an observed message_id here, including quoted messages. Bounded per turn; motion uses thumbnails.",
            "social_sticker": "Collect a static sticker from an observed message_id, list/search collected stickers, or send one by sticker_id. Notes are untrusted; sends share text budgets and receipts.",
        }[self.name]

    @property
    def parameters(self) -> dict[str, Any]:
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
            }
            required = ["seconds"]
        elif self.name == "social_set_wake":
            properties = {
                "keywords": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 20},
                "senders": {"type": "array", "items": {"type": "string", "pattern": "^[0-9]+$"}, "maxItems": 20},
                "on_messages": {"type": "boolean"},
            }
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str | list[dict[str, Any]]:
        ctx = current_request_context()
        if ctx is None:
            return ToolResult.error("social tools require an active group/topic turn")
        try:
            if self.name == "social_get_media":
                return await self.runtime.get_media(ctx, kwargs["message_id"])
            if self.name == "social_sticker":
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
                result = self.runtime.wait(ctx, kwargs["seconds"], sleeping=kwargs.get("sleeping", False),
                                           on_timeout=kwargs.get("on_timeout", False))
            elif self.name == "social_set_wake":
                result = self.runtime.set_wake(ctx, keywords=kwargs.get("keywords", []),
                                               senders=kwargs.get("senders", []), on_messages=kwargs.get("on_messages", False))
            else:
                if kwargs.get("parts"):
                    result = {"actions": await self.runtime.send_parts(ctx, [kwargs["text"], *kwargs["parts"]], kwargs.get("reply_to"))}
                else:
                    result = await self.runtime.send(ctx, kwargs["text"], kwargs.get("reply_to"))
            return json.dumps(result, ensure_ascii=False)
        except (PermissionError, ValueError, RuntimeError) as exc:
            return ToolResult.error(str(exc))
