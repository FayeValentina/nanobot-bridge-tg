"""Pure conversion between OpenAI-style messages and Gemini native payloads."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from nanobot.providers.base import (
    LLMResponse,
    LLMUsage,
    ToolCallRequest,
    tool_arguments_object_for_replay,
)

SKIP_SIGNATURE = "skip_thought_signature_validator"


def _content_parts(content: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict[str, Any]] = []
    for block in content:
        if block["type"] == "text":
            parts.append({"text": block["text"]})
        elif block["type"] == "image_url":
            url = block["image_url"]["url"]
            if url.startswith("data:"):
                header, data = url.split(",", 1)
                parts.append({"inlineData": {"mimeType": header[5:].split(";", 1)[0], "data": data}})
            else:
                parts.append({"text": f"[image: {url}]"})
    return parts


def build_request(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    *,
    max_output_tokens: int,
    temperature: float | None,
    thinking_level: str | None,
) -> dict[str, Any]:
    """Convert sanitized chat history and tool declarations to a Gemini request."""
    contents: list[dict[str, Any]] = []
    system_texts: list[str] = []
    tool_names: dict[str, str] = {}
    for message in messages:
        role = message["role"]
        parts = _content_parts(message.get("content"))
        if role == "system":
            system_texts.extend(part["text"] for part in parts if "text" in part)
            continue
        if role == "assistant":
            role = "model"
            for tc in message.get("tool_calls", []):
                fn = tc["function"]
                tool_names[tc["id"]] = fn["name"]
                signature = tc.get("extra_content", {}).get("google", {}).get("thought_signature")
                parts.append({
                    "functionCall": {
                        "name": fn["name"],
                        "args": tool_arguments_object_for_replay(fn.get("arguments")),
                        "id": tc["id"],
                    },
                    "thoughtSignature": signature or SKIP_SIGNATURE,
                })
            if not parts:
                continue
        elif role == "tool":
            role = "user"
            call_id = message["tool_call_id"]
            function_response: dict[str, Any] = {
                "name": tool_names[call_id],
                "id": call_id,
                "response": {"output": "\n".join(part["text"] for part in parts if "text" in part)},
            }
            images = [part for part in parts if "inlineData" in part]
            if images:
                function_response["parts"] = images
            parts = [{"functionResponse": function_response}]
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    request: dict[str, Any] = {"contents": contents}
    if system_texts:
        request["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}
    if tools:
        declarations: list[dict[str, Any]] = []
        for tool in tools:
            fn = tool["function"]
            declaration = {"name": fn["name"]}
            if "description" in fn:
                declaration["description"] = fn["description"]
            if "parameters" in fn:
                declaration["parametersJsonSchema"] = fn["parameters"]
            declarations.append(declaration)
        request["tools"] = [{"functionDeclarations": declarations}]
    if tool_choice is not None:
        calling_config: dict[str, Any]
        if isinstance(tool_choice, str):
            calling_config = {"mode": {"auto": "AUTO", "required": "ANY", "none": "NONE"}[tool_choice]}
        else:
            calling_config = {"mode": "ANY", "allowedFunctionNames": [tool_choice["function"]["name"]]}
        request["toolConfig"] = {"functionCallingConfig": calling_config}
    generation: dict[str, Any] = {"maxOutputTokens": max_output_tokens}
    if temperature is not None:
        generation["temperature"] = temperature
    if thinking_level is not None:
        generation["thinkingConfig"] = {"thinkingLevel": thinking_level, "includeThoughts": True}
    request["generationConfig"] = generation
    return request


class GeminiResponseAccumulator:
    """Accumulate native response events for both streaming and single responses."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._tool_calls: list[ToolCallRequest] = []
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] | None = None

    def feed(self, event: dict[str, Any]) -> list[tuple[Literal["content", "thinking"], str]]:
        candidates = event.get("candidates", [])
        candidate: dict[str, Any] = candidates[0] if candidates else {}
        deltas: list[tuple[Literal["content", "thinking"], str]] = []
        for part in candidate.get("content", {}).get("parts", []):
            if "text" in part:
                text = part["text"]
                if part.get("thought"):
                    self._reasoning.append(text)
                    deltas.append(("thinking", text))
                elif text:
                    self._content.append(text)
                    deltas.append(("content", text))
            if "functionCall" in part:
                fc = part["functionCall"]
                sig = part.get("thoughtSignature")
                self._tool_calls.append(ToolCallRequest(
                    id=fc.get("id") or f"call_{uuid4().hex[:24]}",
                    name=fc["name"],
                    arguments=fc.get("args") or {},
                    extra_content={"google": {"thought_signature": sig}} if sig else None,
                ))
        if "finishReason" in candidate:
            self._finish_reason = candidate["finishReason"]
        if "usageMetadata" in event:
            self._usage = event["usageMetadata"]
        return deltas

    def response(self) -> LLMResponse:
        finish_reason = "stop"
        if self._finish_reason == "MAX_TOKENS":
            finish_reason = "length"
        elif self._finish_reason in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}:
            finish_reason = "content_filter"
        usage = None
        if self._usage is not None:
            usage = LLMUsage.reported(
                input_tokens=self._usage.get("promptTokenCount", 0),
                output_tokens=self._usage.get("candidatesTokenCount", 0) + self._usage.get("thoughtsTokenCount", 0),
                total_tokens=self._usage.get("totalTokenCount", 0),
                cache_read_tokens=self._usage.get("cachedContentTokenCount"),
            )
        return LLMResponse(
            content="".join(self._content) or None,
            tool_calls=self._tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content="".join(self._reasoning) or None,
        )
