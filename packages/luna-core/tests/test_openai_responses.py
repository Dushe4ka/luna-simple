import json

import pytest
from luna_core.errors import ProviderError
from luna_core.messages import Image, Message, Text, Thinking, ToolCall, ToolResult
from luna_core.providers.base import ModelRequest, RetryPolicy, StreamAccumulator
from luna_core.providers.openai_responses import OpenAIResponses
from luna_core.tools.base import ToolSpec
from luna_core.usage import Usage
from sse import Recorder, stream_response

LS = ToolSpec("ls", "List.", {"type": "object", "properties": {"path": {"type": "string"}}})
SIG = json.dumps({"id": "rs_1", "encrypted_content": "ENC"})


def ev(kind, **data):
    return (kind, {"type": kind, **data})


async def run(p, request):
    acc = StreamAccumulator(p.name)
    async for e in p.stream(request):
        acc.feed(e)
    return acc


def req(*messages, **kw):
    return ModelRequest(model="gpt-5", system="sys", messages=list(messages), tools=[LS], **kw)


async def test_reasoning_text_and_function_call():
    rec = Recorder(
        stream_response(
            [
                ev("response.created", response={"id": "resp_1"}),
                ev(
                    "response.output_item.added",
                    output_index=0,
                    item={"type": "reasoning", "id": "rs_1"},
                ),
                ev("response.reasoning_summary_text.delta", output_index=0, delta="plan"),
                ev(
                    "response.output_item.done",
                    output_index=0,
                    item={"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC"},
                ),
                ev(
                    "response.output_item.added",
                    output_index=1,
                    item={"type": "message", "role": "assistant"},
                ),
                ev("response.output_text.delta", output_index=1, delta="Look"),
                ev(
                    "response.output_item.added",
                    output_index=2,
                    item={
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "ls",
                        "arguments": "",
                    },
                ),
                ev("response.function_call_arguments.delta", output_index=2, delta='{"path": "."}'),
                ev(
                    "response.output_item.done",
                    output_index=2,
                    item={
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "ls",
                        "arguments": '{"path": "."}',
                    },
                ),
                ev(
                    "response.completed",
                    response={
                        "status": "completed",
                        "usage": {
                            "input_tokens": 40,
                            "output_tokens": 12,
                            "input_tokens_details": {"cached_tokens": 30},
                            "output_tokens_details": {"reasoning_tokens": 7},
                        },
                    },
                ),
            ]
        )
    )
    acc = await run(
        OpenAIResponses(api_key="k", client=rec.client()), req(Message.user("hi"), reasoning="low")
    )
    assert acc.message().content == [
        Thinking("plan", SIG, "openai_responses"),
        Text("Look"),
        ToolCall("call_9", "ls", {"path": "."}),
    ]
    assert acc.usage == Usage(input=40, output=12, cache_read=30, reasoning=7)
    assert acc.stop_reason == "tool_use"
    body = rec.last_json
    assert rec.requests[0].url.path == "/v1/responses"
    assert body["instructions"] == "sys" and body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}
    assert body["tools"] == [
        {"type": "function", "name": "ls", "description": "List.", "parameters": LS.parameters}
    ]


def test_history_items():
    p = OpenAIResponses(api_key="k")
    items = p.build_payload(
        req(
            Message.user("look", images=[Image("image/png", "AAA")]),
            Message.assistant(
                [
                    Thinking("plan", SIG, "openai_responses"),
                    Text("ok"),
                    ToolCall("call_9", "ls", {"path": "."}),
                ]
            ),
            Message.tool_results([ToolResult("call_9", (Text("a.py"),), name="ls")]),
            Message.assistant([Thinking("x", "S", "anthropic"), Text("done")]),
        )
    )["input"]
    assert items[0] == {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
        ],
    }
    assert items[1] == {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "ENC",
        "summary": [{"type": "summary_text", "text": "plan"}],
    }
    assert items[2] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "ok"}],
    }
    assert items[3] == {
        "type": "function_call",
        "call_id": "call_9",
        "name": "ls",
        "arguments": '{"path": "."}',
    }
    assert items[4] == {"type": "function_call_output", "call_id": "call_9", "output": "a.py"}
    assert items[5] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "done"}],
    }


async def test_incomplete_is_max_tokens_and_failed_raises():
    rec = Recorder(
        stream_response(
            [
                ev("response.output_text.delta", output_index=0, delta="par"),
                ev(
                    "response.incomplete",
                    response={
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                ),
            ]
        ),
        stream_response(
            [ev("response.failed", response={"error": {"message": "server exploded"}})]
        ),
    )
    p = OpenAIResponses(api_key="k", client=rec.client(), retry=RetryPolicy(attempts=1))
    acc = await run(p, req(Message.user("a")))
    assert acc.stop_reason == "max_tokens"
    with pytest.raises(ProviderError, match="server exploded"):
        await run(p, req(Message.user("a")))
