import pytest
from luna_core.errors import ProviderError
from luna_core.messages import Image, Message, Text, Thinking, ToolCall, ToolResult
from luna_core.providers.anthropic import AnthropicMessages
from luna_core.providers.base import ModelRequest, RetryPolicy, StreamAccumulator
from luna_core.tools.base import ToolSpec
from luna_core.usage import Usage
from sse import Recorder, stream_response

LS = ToolSpec("ls", "List.", {"type": "object", "properties": {"path": {"type": "string"}}})


def ev(name, data):
    return (name, {"type": name, **data})


async def run(p, request):
    acc = StreamAccumulator(p.name)
    async for e in p.stream(request):
        acc.feed(e)
    return acc


def req(*messages, **kw):
    return ModelRequest(
        model="claude-sonnet-4-5", system="sys", messages=list(messages), tools=[LS], **kw
    )


async def test_thinking_text_and_tool_use_with_usage():
    rec = Recorder(
        stream_response(
            [
                ev(
                    "message_start",
                    {
                        "message": {
                            "usage": {
                                "input_tokens": 50,
                                "cache_read_input_tokens": 40,
                                "cache_creation_input_tokens": 5,
                                "output_tokens": 1,
                            }
                        }
                    },
                ),
                ev(
                    "content_block_start",
                    {"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                ),
                ev(
                    "content_block_delta",
                    {"index": 0, "delta": {"type": "thinking_delta", "thinking": "need ls"}},
                ),
                ev(
                    "content_block_delta",
                    {"index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}},
                ),
                ev("content_block_stop", {"index": 0}),
                ev(
                    "content_block_start",
                    {"index": 1, "content_block": {"type": "text", "text": ""}},
                ),
                ev(
                    "content_block_delta",
                    {"index": 1, "delta": {"type": "text_delta", "text": "Listing."}},
                ),
                ev(
                    "content_block_start",
                    {
                        "index": 2,
                        "content_block": {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "ls",
                            "input": {},
                        },
                    },
                ),
                ev(
                    "content_block_delta",
                    {"index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
                ),
                ev(
                    "content_block_delta",
                    {"index": 2, "delta": {"type": "input_json_delta", "partial_json": ' "."}'}},
                ),
                ev("ping", {}),
                ev(
                    "message_delta",
                    {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
                ),
                ev("message_stop", {}),
            ]
        )
    )
    acc = await run(
        AnthropicMessages(api_key="k", client=rec.client()),
        req(Message.user("hi"), reasoning="medium"),
    )
    assert acc.message().content == [
        Thinking("need ls", "SIG", "anthropic"),
        Text("Listing."),
        ToolCall("toolu_1", "ls", {"path": "."}),
    ]
    assert acc.usage == Usage(input=95, output=30, cache_read=40, cache_write=5)
    assert acc.stop_reason == "tool_use"
    r = rec.requests[0]
    assert (
        r.url.path == "/v1/messages"
        and r.headers["x-api-key"] == "k"
        and r.headers["anthropic-version"] == "2023-06-01"
    )
    body = rec.last_json
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert body["max_tokens"] > 8192


def test_payload_cache_breakpoints_and_history():
    p = AnthropicMessages(api_key="k")
    history = [
        Message.user("look", images=[Image("image/png", "AAA")]),
        Message.assistant(
            [Thinking("t", "S", "anthropic"), Text("hm"), ToolCall("toolu_1", "ls", {"path": "."})]
        ),
        Message.tool_results([ToolResult("toolu_1", (Text("a.py"),), name="ls")]),
        Message.assistant([Thinking("foreign", "X", "openai_responses"), Text("ok")]),
        Message.user("next"),
    ]
    body = p.build_payload(req(*history))
    assert body["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][0]["input_schema"] == LS.parameters
    msgs = body["messages"]
    assert msgs[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAA"},
    }
    assert msgs[1]["content"] == [
        {"type": "thinking", "thinking": "t", "signature": "S"},
        {"type": "text", "text": "hm"},
        {"type": "tool_use", "id": "toolu_1", "name": "ls", "input": {"path": "."}},
    ]
    assert msgs[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": [{"type": "text", "text": "a.py"}],
                "is_error": False,
            }
        ],
    }
    assert msgs[3]["content"] == [{"type": "text", "text": "ok"}]  # foreign thinking dropped
    assert msgs[4]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_consecutive_user_messages_are_merged():
    p = AnthropicMessages(api_key="k")
    body = p.build_payload(req(Message.user("a"), Message.user("b")))
    assert len(body["messages"]) == 1
    assert [b["text"] for b in body["messages"][0]["content"]] == ["a", "b"]


def test_no_cache_when_disabled():
    body = AnthropicMessages(api_key="k").build_payload(req(Message.user("a"), cache=False))
    assert "cache_control" not in str(body)


async def test_stream_error_event():
    rec = Recorder(
        stream_response(
            [ev("error", {"error": {"type": "overloaded_error", "message": "Overloaded"}})]
        )
    )
    with pytest.raises(ProviderError) as exc:
        await run(
            AnthropicMessages(api_key="k", client=rec.client(), retry=RetryPolicy(attempts=1)),
            req(Message.user("a")),
        )
    assert exc.value.kind == "server" and "Overloaded" in str(exc.value)
