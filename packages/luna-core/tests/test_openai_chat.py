import pytest
from luna_core.errors import ProviderError
from luna_core.messages import Image, Message, Text, Thinking, ToolCall, ToolResult
from luna_core.providers.base import ModelRequest, RetryPolicy, StreamAccumulator
from luna_core.providers.openai_chat import OpenAIChat
from luna_core.tools.base import ToolSpec
from luna_core.usage import Usage
from sse import Recorder, json_response, stream_response

GREP = ToolSpec(
    "grep", "Search.", {"type": "object", "properties": {"pattern": {"type": "string"}}}
)


def chunk(delta=None, finish=None, usage=None):
    body = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        body = {"choices": [], "usage": usage}
    return (None, body)


async def run(provider, request):
    acc = StreamAccumulator(provider.name)
    async for ev in provider.stream(request):
        acc.feed(ev)
    return acc


def req(*messages, tools=(GREP,), **kw):
    return ModelRequest(
        model="gpt-x", system="be brief", messages=list(messages), tools=list(tools), **kw
    )


async def test_text_answer_with_usage():
    rec = Recorder(
        stream_response(
            [
                chunk({"role": "assistant", "content": "Hel"}),
                chunk({"content": "lo"}),
                chunk(finish="stop"),
                chunk(
                    usage={
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "prompt_tokens_details": {"cached_tokens": 8},
                    }
                ),
                (None, "[DONE]"),
            ]
        )
    )
    p = OpenAIChat(api_key="k", client=rec.client())
    acc = await run(p, req(Message.user("hi")))
    assert acc.message().content == [Text("Hello")]
    assert acc.usage == Usage(input=12, output=3, cache_read=8)
    assert acc.stop_reason == "end"
    sent = rec.last_json
    assert rec.requests[0].url.path == "/v1/chat/completions"
    assert rec.requests[0].headers["authorization"] == "Bearer k"
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["messages"][1] == {"role": "user", "content": "hi"}
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {"name": "grep", "description": "Search.", "parameters": GREP.parameters},
        }
    ]


async def test_parallel_tool_calls_stream_in_pieces():
    rec = Recorder(
        stream_response(
            [
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "type": "function",
                                "function": {"name": "grep", "arguments": ""},
                            }
                        ]
                    }
                ),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"pattern"'}}]}),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": ': "x"}'}}]}),
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_b",
                                "type": "function",
                                "function": {"name": "grep", "arguments": '{"pattern": "y"}'},
                            }
                        ]
                    }
                ),
                chunk(finish="tool_calls"),
                (None, "[DONE]"),
            ]
        )
    )
    acc = await run(OpenAIChat(api_key="k", client=rec.client()), req(Message.user("find")))
    assert acc.message().tool_calls == [
        ToolCall("call_a", "grep", {"pattern": "x"}),
        ToolCall("call_b", "grep", {"pattern": "y"}),
    ]
    assert acc.stop_reason == "tool_use"


async def test_deepseek_reasoning_content_and_cache_hits():
    rec = Recorder(
        stream_response(
            [
                chunk({"reasoning_content": "think"}),
                chunk({"content": "ok"}),
                chunk(finish="stop"),
                chunk(
                    usage={
                        "prompt_tokens": 100,
                        "completion_tokens": 9,
                        "prompt_cache_hit_tokens": 64,
                        "completion_tokens_details": {"reasoning_tokens": 4},
                    }
                ),
                (None, "[DONE]"),
            ]
        )
    )
    p = OpenAIChat(
        api_key="k",
        base_url="https://api.deepseek.com",
        name="deepseek",
        client=rec.client(),
        send_reasoning=True,
    )
    acc = await run(p, req(Message.user("q")))
    assert acc.message().content == [Thinking("think", None, "deepseek"), Text("ok")]
    assert acc.usage == Usage(input=100, output=9, cache_read=64, reasoning=4)
    assert rec.requests[0].url.path == "/chat/completions"


def test_history_conversion_tool_round_trip_and_images():
    p = OpenAIChat(api_key="k", name="deepseek", send_reasoning=True)
    history = [
        Message.user("look", images=[Image("image/png", "AAA")]),
        Message.assistant(
            [
                Thinking("plan", None, "deepseek"),
                Text("checking"),
                ToolCall("c1", "grep", {"pattern": "x"}),
            ]
        ),
        Message.tool_results(
            [ToolResult("c1", (Text("3 hits"), Image("image/png", "BBB")), name="grep")]
        ),
        Message.assistant([Thinking("other provider", "sig", "anthropic"), Text("done")]),
    ]
    msgs = p.build_payload(req(*history))["messages"]
    assert msgs[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ],
    }
    assert msgs[2] == {
        "role": "assistant",
        "content": "checking",
        "reasoning_content": "plan",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "grep", "arguments": '{"pattern": "x"}'},
            }
        ],
    }
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": "3 hits"}
    # images from a tool result travel in a follow-up user message (tool messages are text-only)
    assert msgs[4]["role"] == "user" and msgs[4]["content"][-1]["type"] == "image_url"
    assert msgs[5] == {"role": "assistant", "content": "done"}  # foreign thinking is never sent


def test_reasoning_effort_and_max_tokens_field():
    p = OpenAIChat(api_key="k", reasoning_param=True, max_tokens_field="max_completion_tokens")
    body = p.build_payload(
        req(Message.user("hi"), max_tokens=500, reasoning="high", temperature=0.2)
    )
    assert body["max_completion_tokens"] == 500 and "max_tokens" not in body
    assert body["reasoning_effort"] == "high" and body["temperature"] == 0.2


async def test_http_error_and_retry():
    rec = Recorder(
        json_response(503, {"error": {"message": "busy"}}),
        stream_response([chunk({"content": "ok"}), chunk(finish="stop"), (None, "[DONE]")]),
    )
    p = OpenAIChat(api_key="k", client=rec.client(), retry=RetryPolicy(attempts=2, base_delay=0))
    acc = await run(p, req(Message.user("hi")))
    assert acc.message().text == "ok" and len(rec.requests) == 2
    rec = Recorder(json_response(401, {"error": {"message": "Incorrect API key"}}))
    with pytest.raises(ProviderError) as exc:
        await run(OpenAIChat(api_key="bad", client=rec.client()), req(Message.user("hi")))
    assert exc.value.kind == "auth"


async def test_error_object_inside_the_stream():
    rec = Recorder(
        stream_response([(None, {"error": {"message": "model overloaded", "code": 503}})])
    )
    with pytest.raises(ProviderError, match="model overloaded"):
        await run(
            OpenAIChat(api_key="k", client=rec.client(), retry=RetryPolicy(attempts=1)),
            req(Message.user("hi")),
        )
