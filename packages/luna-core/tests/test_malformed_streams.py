"""Malformed provider streams surface as ProviderError, never KeyError/TypeError/..."""

import pytest
from luna_core.errors import ProviderError
from luna_core.events import ToolCallDelta, ToolCallStart
from luna_core.messages import Message
from luna_core.providers.anthropic import AnthropicMessages
from luna_core.providers.base import ModelRequest, RetryPolicy, StreamAccumulator
from luna_core.providers.gigachat import GigaChat
from luna_core.providers.google import GoogleGenAI
from luna_core.providers.openai_chat import OpenAIChat
from luna_core.providers.openai_responses import OpenAIResponses
from sse import Recorder, json_response, stream_response

ONCE = RetryPolicy(attempts=1)
MAKERS = {
    "openai_chat": lambda c: OpenAIChat(api_key="k", client=c, retry=ONCE),
    "anthropic": lambda c: AnthropicMessages(api_key="k", client=c, retry=ONCE),
    "openai_responses": lambda c: OpenAIResponses(api_key="k", client=c, retry=ONCE),
    "google": lambda c: GoogleGenAI(api_key="k", client=c, retry=ONCE),
    "gigachat": lambda c: GigaChat(credentials="k", client=c, clock=lambda: 0, retry=ONCE),
}
COMMON = ["not json", "[1, 2]", "null", '"text"', "42"]
SPECIFIC = {
    "openai_chat": [
        {"choices": "x"},
        {"choices": [{"delta": "x"}]},
        {"choices": [{"delta": {"content": 5}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": "x"}]}}]},
        {"choices": [{"delta": {"tool_calls": "x"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 7}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": "x"}]}}]},
        {"usage": "x"},
    ],
    "anthropic": [
        {"type": "content_block_start"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use"}},
        {"type": "content_block_start", "index": 0, "content_block": "x"},
        {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": None}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "a"}},
        {"type": "message_start", "message": {"usage": {"input_tokens": "lots"}}},
        {"type": "message_start", "message": "x"},
        {"type": "message_delta", "delta": "x"},
    ],
    "openai_responses": [
        {"type": "response.output_item.added", "item": {"type": "function_call"}},
        {"type": "response.output_item.added", "item": "x"},
        {"type": "response.function_call_arguments.delta", "output_index": 4, "delta": "{}"},
        {"type": "response.function_call_arguments.delta", "delta": "{}"},
        {"type": "response.output_text.delta", "delta": None},
        {"type": "response.completed", "response": {"usage": "x"}},
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "encrypted_content": 1},
        },
    ],
    "google": [
        {"candidates": "x"},
        {"candidates": [{"content": {"parts": [{"functionCall": "x"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": 5}]}}]},
        {"candidates": [{"content": "x"}]},
        {"usageMetadata": {"promptTokenCount": "x"}},
        {"usageMetadata": "x"},
    ],
    "gigachat": [
        {"choices": [{"delta": {"function_call": "x"}}]},
        {"choices": [{"delta": {"content": ["a"]}}]},
        {"choices": "x"},
        {"usage": {"prompt_tokens": "x"}},
        {"usage": "x"},
    ],
}
CASES = [(name, data) for name in MAKERS for data in [*COMMON, *SPECIFIC[name]]]


async def drain(name, data):
    responses = [stream_response([(None, data)])]
    if name == "gigachat":
        responses.insert(0, json_response(200, {"access_token": "t", "expires_at": 10**13}))
    provider = MAKERS[name](Recorder(*responses).client())
    acc = StreamAccumulator(provider.name)
    request = ModelRequest(model="m", system="s", messages=[Message.user("hi")])
    async for event in provider.stream(request):
        acc.feed(event)
    return acc.message()


@pytest.mark.parametrize(
    ("name", "data"), CASES, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CASES)]
)
async def test_a_malformed_event_is_a_provider_error(name, data):
    with pytest.raises(ProviderError, match="malformed"):
        await drain(name, data)


def test_accumulator_rejects_a_delta_for_a_call_that_never_started():
    acc = StreamAccumulator("p")
    acc.feed(ToolCallStart(0, "c1", "ls"))
    acc.feed(ToolCallDelta(0, "{}"))
    with pytest.raises(ProviderError, match="malformed"):
        acc.feed(ToolCallDelta(1, "{}"))
