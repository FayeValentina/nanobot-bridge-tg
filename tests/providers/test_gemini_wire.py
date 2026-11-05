"""Gemini native conversion tests with locally constructed wire events."""

from copy import deepcopy

import pytest

from nanobot.providers.base import LLMUsage, ToolCallRequest
from nanobot.providers.gemini_wire import (
    SKIP_SIGNATURE,
    GeminiResponseAccumulator,
    build_request,
)


def _build(messages, tools=None, tool_choice=None, **kwargs):
    return build_request(
        messages, tools, tool_choice,
        max_output_tokens=kwargs.get("max_output_tokens", 1024),
        temperature=kwargs.get("temperature"),
        thinking_level=kwargs.get("thinking_level"),
    )


def test_system_messages_collected_in_order_and_images_converted() -> None:
    messages = [
        {"role": "system", "content": "first"},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
            {"type": "audio", "data": "ignored"},
        ]},
        {"role": "system", "content": [{"type": "text", "text": "middle"}]},
        {"role": "assistant", "content": "seen", "reasoning_content": "private reasoning",
         "thinking_blocks": [{"text": "private block"}]},
        {"role": "system", "content": "last"},
    ]
    original = deepcopy(messages)
    request = _build(messages)
    assert request == {
        "systemInstruction": {"parts": [{"text": "first\n\nmiddle\n\nlast"}]},
        "contents": [
            {"role": "user", "parts": [
                {"text": "look"},
                {"inlineData": {"mimeType": "image/png", "data": "YWJj"}},
                {"text": "[image: https://example.com/image.png]"},
            ]},
            {"role": "model", "parts": [{"text": "seen"}]},
        ],
        "generationConfig": {"maxOutputTokens": 1024},
    }
    assert messages == original


def test_assistant_call_signatures_and_arguments_are_per_call() -> None:
    messages = [{"role": "assistant", "content": "working", "tool_calls": [
        {"id": "signed", "function": {"name": "search", "arguments": '{"query": "cats"}'},
         "extra_content": {"google": {"thought_signature": "real-signature"}}},
        {"id": "unsigned", "function": {"name": "read", "arguments": {"path": "cat.png"}}},
        {"id": "empty-signature", "function": {"name": "noop", "arguments": "{}"},
         "extra_content": {"google": {"thought_signature": ""}}},
    ]}]
    assert _build(messages)["contents"] == [{"role": "model", "parts": [
        {"text": "working"},
        {"functionCall": {"name": "search", "args": {"query": "cats"}, "id": "signed"},
         "thoughtSignature": "real-signature"},
        {"functionCall": {"name": "read", "args": {"path": "cat.png"}, "id": "unsigned"},
         "thoughtSignature": SKIP_SIGNATURE},
        {"functionCall": {"name": "noop", "args": {}, "id": "empty-signature"},
         "thoughtSignature": SKIP_SIGNATURE},
    ]}]


def test_tool_images_and_text_followed_by_user_share_one_content() -> None:
    request = _build([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "first", "function": {"name": "read_image", "arguments": "{}"}},
            {"id": "second", "function": {"name": "search", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "second", "content": "found"},
        {"role": "tool", "tool_call_id": "first", "content": [
            {"type": "text", "text": "caption"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,eHl6"}},
            {"type": "text", "text": "details"},
        ]},
        {"role": "user", "content": "what do you think?"},
    ])
    assert len(request["contents"]) == 2
    assert request["contents"][1] == {"role": "user", "parts": [
        {"functionResponse": {"name": "search", "id": "second", "response": {"output": "found"}}},
        {"functionResponse": {
            "name": "read_image", "id": "first", "response": {"output": "caption\ndetails"},
            "parts": [{"inlineData": {"mimeType": "image/jpeg", "data": "eHl6"}}],
        }},
        {"text": "what do you think?"},
    ]}


def test_adjacent_roles_merge_and_empty_assistant_is_omitted() -> None:
    request = _build([
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": None, "reasoning_content": "hidden"},
        {"role": "assistant", "content": []},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "three"},
        {"role": "assistant", "content": [{"type": "image_url", "image_url": {
            "url": "data:image/webp;base64,YQ==",
        }}]},
    ])
    assert request["contents"] == [
        {"role": "user", "parts": [{"text": "one"}, {"text": "two"}]},
        {"role": "model", "parts": [
            {"text": "three"}, {"inlineData": {"mimeType": "image/webp", "data": "YQ=="}},
        ]},
    ]
    assert "systemInstruction" not in request


def test_full_tool_json_schema_passed_through_unchanged() -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {"type": ["string", "null"]},
            "choice": {"oneOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "string"}]},
            "node": {"$ref": "#/$defs/node"},
        },
        "$defs": {"node": {"anyOf": [
            {"type": "null"},
            {"type": "object", "properties": {"next": {"$ref": "#/$defs/node"}}},
        ]}},
        "additionalProperties": False,
    }
    original = deepcopy(schema)
    tools = [
        {"type": "function", "function": {"name": "complex", "description": "full schema", "parameters": schema}},
        {"type": "function", "function": {"name": "bare"}},
    ]
    declarations = _build([], tools=tools)["tools"][0]["functionDeclarations"]
    assert declarations == [
        {"name": "complex", "description": "full schema", "parametersJsonSchema": original},
        {"name": "bare"},
    ]
    assert declarations[0]["parametersJsonSchema"] is schema
    assert schema == original
    assert "tools" not in _build([], tools=[])
    assert "tools" not in _build([])


@pytest.mark.parametrize(("choice", "expected"), [
    ("auto", {"mode": "AUTO"}),
    ("required", {"mode": "ANY"}),
    ("none", {"mode": "NONE"}),
    ({"type": "function", "function": {"name": "search"}}, {"mode": "ANY", "allowedFunctionNames": ["search"]}),
])
def test_tool_choice_mapping(choice, expected) -> None:
    assert _build([], tool_choice=choice)["toolConfig"] == {"functionCallingConfig": expected}
    assert "toolConfig" not in _build([])


@pytest.mark.parametrize("temperature", [None, 0.0, 0.7])
@pytest.mark.parametrize("thinking_level", [None, "medium"])
def test_generation_config_optional_fields(temperature, thinking_level) -> None:
    expected = {"maxOutputTokens": 2048}
    if temperature is not None:
        expected["temperature"] = temperature
    if thinking_level is not None:
        expected["thinkingConfig"] = {"thinkingLevel": thinking_level, "includeThoughts": True}
    assert _build([], max_output_tokens=2048, temperature=temperature, thinking_level=thinking_level) == {
        "contents": [], "generationConfig": expected,
    }


def test_accumulator_stream_deltas_calls_finish_and_latest_usage() -> None:
    accumulator = GeminiResponseAccumulator()
    assert accumulator.feed({
        "candidates": [{"content": {"parts": [
            {"text": "thinking ", "thought": True},
            {"text": "hello "},
            {"text": "more thought", "thought": True},
            {"text": "", "thoughtSignature": "standalone-signature"},
        ]}}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
    }) == [("thinking", "thinking "), ("content", "hello "), ("thinking", "more thought")]
    assert accumulator.feed({"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "search", "args": {"query": "cats"}, "id": "call-real"},
         "thoughtSignature": "call-signature"},
        {"text": "world"},
        {"functionCall": {"name": "noop"}},
    ]}, "finishReason": "STOP"}]}) == [("content", "world")]
    assert accumulator.feed({"usageMetadata": {
        "promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30,
        "totalTokenCount": 150, "cachedContentTokenCount": 40,
    }}) == []
    response = accumulator.response()
    assert response.content == "hello world"
    assert response.reasoning_content == "thinking more thought"
    assert response.finish_reason == "stop"
    assert response.usage == LLMUsage.reported(
        input_tokens=100, output_tokens=50, total_tokens=150, cache_read_tokens=40,
    )
    assert response.tool_calls[0] == ToolCallRequest(
        id="call-real", name="search", arguments={"query": "cats"},
        extra_content={"google": {"thought_signature": "call-signature"}},
    )
    generated = response.tool_calls[1]
    assert generated.id.startswith("call_") and len(generated.id) == 29
    assert int(generated.id[5:], 16) >= 0
    assert generated.name == "noop"
    assert generated.arguments == {}
    assert generated.extra_content is None


@pytest.mark.parametrize(("reason", "expected"), [
    ("MAX_TOKENS", "length"), ("SAFETY", "content_filter"), ("RECITATION", "content_filter"),
    ("PROHIBITED_CONTENT", "content_filter"), ("BLOCKLIST", "content_filter"),
    ("SPII", "content_filter"), ("STOP", "stop"), ("OTHER", "stop"),
])
def test_accumulator_finish_mapping_without_usage(reason: str, expected: str) -> None:
    accumulator = GeminiResponseAccumulator()
    assert accumulator.feed({"candidates": [{"finishReason": reason}]}) == []
    response = accumulator.response()
    assert response.finish_reason == expected
    assert response.usage is None
    assert response.content is None
    assert response.reasoning_content is None


def test_empty_events_and_missing_usage_counts() -> None:
    accumulator = GeminiResponseAccumulator()
    assert accumulator.feed({}) == []
    assert accumulator.feed({"candidates": []}) == []
    assert accumulator.response().usage is None
    assert accumulator.response().finish_reason == "stop"
    accumulator.feed({"usageMetadata": {}})
    assert accumulator.response().usage == LLMUsage.reported(input_tokens=0, output_tokens=0, total_tokens=0)
    accumulator.feed({"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": 7}})
    assert accumulator.response().usage == LLMUsage.reported(input_tokens=5, output_tokens=2, total_tokens=7)


def test_tool_call_round_trip_preserves_id_arguments_and_signature() -> None:
    accumulator = GeminiResponseAccumulator()
    accumulator.feed({"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "read", "args": {"path": "image.png"}, "id": "round-trip"},
         "thoughtSignature": "opaque-signature"},
    ]}, "finishReason": "STOP"}]})
    response = accumulator.response()
    request = _build([{
        "role": "assistant", "content": response.content,
        "tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
    }])
    assert request["contents"] == [{"role": "model", "parts": [{
        "functionCall": {"name": "read", "args": {"path": "image.png"}, "id": "round-trip"},
        "thoughtSignature": "opaque-signature",
    }]}]
