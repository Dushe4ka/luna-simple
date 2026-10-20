import json

import pytest
from luna_core.messages import Message, Text, ToolCall, ToolResult
from luna_core.providers.anthropic import AnthropicMessages
from luna_core.providers.base import ModelRequest, StreamAccumulator
from luna_core.providers.gigachat import GigaChat
from luna_core.providers.google import GoogleGenAI
from luna_core.providers.openai_chat import OpenAIChat
from luna_core.providers.openai_responses import OpenAIResponses
from luna_core.tools.base import ToolSpec
from sse import Recorder, json_response, stream_response

LS = ToolSpec(
    "ls", "List.", {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}
)


def openai_chat_tool():
    return [
        (
            None,
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "ls", "arguments": '{"path": "."}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ),
        (None, "[DONE]"),
    ]


def anthropic_tool():
    return [
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "c1", "name": "ls"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"path": "."}'},
            },
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 1},
            },
        ),
    ]


def responses_tool():
    return [
        (
            None,
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "function_call", "call_id": "c1", "name": "ls"},
            },
        ),
        (
            None,
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": '{"path": "."}',
            },
        ),
        (None, {"type": "response.completed", "response": {"status": "completed", "usage": {}}}),
    ]


def google_tool():
    return [
        (
            None,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"id": "c1", "name": "ls", "args": {"path": "."}}}
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )
    ]


def gigachat_tool():
    return [
        (
            None,
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"function_call": {"name": "ls", "arguments": {"path": "."}}},
                        "finish_reason": "function_call",
                    }
                ]
            },
        ),
        (None, "[DONE]"),
    ]


CASES = [
    ("openai_chat", lambda c: OpenAIChat(api_key="k", client=c), openai_chat_tool),
    ("anthropic", lambda c: AnthropicMessages(api_key="k", client=c), anthropic_tool),
    ("openai_responses", lambda c: OpenAIResponses(api_key="k", client=c), responses_tool),
    ("google", lambda c: GoogleGenAI(api_key="k", client=c), google_tool),
    ("gigachat", lambda c: GigaChat(credentials="k", client=c, clock=lambda: 0), gigachat_tool),
]


@pytest.mark.parametrize(("name", "make", "script"), CASES, ids=[c[0] for c in CASES])
async def test_tool_call_normalises_identically(name, make, script):
    responses = [stream_response(script())]
    if name == "gigachat":
        responses.insert(0, json_response(200, {"access_token": "t", "expires_at": 10**13}))
    rec = Recorder(*responses)
    provider = make(rec.client())
    acc = StreamAccumulator(provider.name)
    history = [Message.user("list files")]
    async for ev in provider.stream(
        ModelRequest(model="m", system="s", messages=history, tools=[LS])
    ):
        acc.feed(ev)
    msg = acc.message()
    assert [(c.name, c.args) for c in msg.tool_calls] == [("ls", {"path": "."})]
    assert acc.stop_reason == "tool_use"
    # The produced history converts back without error and keeps the call/result pairing.
    call = msg.tool_calls[0]
    follow = history + [
        msg,
        Message.tool_results([ToolResult(call.id, (Text("a.py"),), name="ls")]),
    ]
    payload = provider.build_payload(
        ModelRequest(model="m", system="s", messages=follow, tools=[LS])
    )
    assert "a.py" in json.dumps(payload, ensure_ascii=False)
    assert ToolCall  # imported for readers: the normalised type under test
