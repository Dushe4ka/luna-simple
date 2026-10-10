import json

import httpx
import pytest
from luna_core.errors import ProviderError
from luna_core.messages import Image, Message, Text, ToolCall, ToolResult
from luna_core.providers.base import ModelRequest, RetryPolicy, StreamAccumulator
from luna_core.providers.gigachat import GigaChat
from luna_core.tools.base import ToolSpec
from luna_core.usage import Usage
from sse import json_response, stream_response

LS = ToolSpec("ls", "List.", {"type": "object", "properties": {"path": {"type": "string"}}})


class Router:
    """Routes OAuth and chat requests to separate response queues."""

    def __init__(self, tokens, chats):
        self.tokens = list(tokens)
        self.chats = list(chats)
        self.oauth = []
        self.chat = []

    def __call__(self, request):
        if request.url.path.endswith("/oauth"):
            self.oauth.append(request)
            return self.tokens.pop(0)
        self.chat.append(request)
        return self.chats.pop(0)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def token(value, expires_ms):
    return json_response(200, {"access_token": value, "expires_at": expires_ms})


def chunk(delta=None, finish=None, usage=None):
    body = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return (None, body)


async def run(p, request):
    acc = StreamAccumulator(p.name)
    async for e in p.stream(request):
        acc.feed(e)
    return acc


def req(*messages):
    return ModelRequest(model="GigaChat-2-Max", system="sys", messages=list(messages), tools=[LS])


async def test_oauth_then_function_call():
    router = Router(
        [token("T1", 1_900_000_000_000)],
        [
            stream_response(
                [
                    chunk({"role": "assistant", "content": ""}),
                    chunk(
                        {
                            "function_call": {"name": "ls", "arguments": {"path": "."}},
                            "functions_state_id": "fs-1",
                        },
                        finish="function_call",
                    ),
                    chunk(
                        usage={
                            "prompt_tokens": 30,
                            "completion_tokens": 8,
                            "precached_prompt_tokens": 12,
                        }
                    ),
                    (None, "[DONE]"),
                ]
            )
        ],
    )
    p = GigaChat(credentials="BASE64KEY", client=router.client(), clock=lambda: 1_700_000_000)
    acc = await run(p, req(Message.user("hi")))
    call = acc.message().tool_calls[0]
    assert (call.name, call.args, call.extra) == (
        "ls",
        {"path": "."},
        {"functions_state_id": "fs-1"},
    )
    assert acc.stop_reason == "tool_use"
    assert acc.usage == Usage(input=30, output=8, cache_read=12)
    oauth = router.oauth[0]
    assert oauth.headers["authorization"] == "Basic BASE64KEY" and len(oauth.headers["rquid"]) == 36
    assert oauth.content == b"scope=GIGACHAT_API_PERS"
    chat = router.chat[0]
    assert chat.headers["authorization"] == "Bearer T1"
    body = json.loads(chat.content)
    assert body["functions"] == [
        {"name": "ls", "description": "List.", "parameters": LS.parameters}
    ]
    assert body["messages"][0] == {"role": "system", "content": "sys"}


async def test_token_is_cached_and_refreshed_near_expiry():
    now = {"t": 1_700_000_000}
    router = Router(
        [token("T1", (1_700_000_000 + 1800) * 1000), token("T2", (1_700_000_000 + 3600) * 1000)],
        [stream_response([chunk({"content": "a"}), (None, "[DONE]")]) for _ in range(3)],
    )
    p = GigaChat(credentials="K", client=router.client(), clock=lambda: now["t"])
    await run(p, req(Message.user("1")))
    await run(p, req(Message.user("2")))
    assert len(router.oauth) == 1
    now["t"] += 1790  # within 60 s of expiry
    await run(p, req(Message.user("3")))
    assert len(router.oauth) == 2
    assert router.chat[-1].headers["authorization"] == "Bearer T2"


async def test_401_refreshes_the_token_once():
    router = Router(
        [token("OLD", 1_900_000_000_000), token("NEW", 1_900_000_000_000)],
        [
            json_response(401, {"message": "Token has expired"}),
            stream_response([chunk({"content": "ok"}), (None, "[DONE]")]),
        ],
    )
    p = GigaChat(credentials="K", client=router.client(), clock=lambda: 1_700_000_000)
    acc = await run(p, req(Message.user("hi")))
    assert acc.message().text == "ok"
    assert [r.headers["authorization"] for r in router.chat] == ["Bearer OLD", "Bearer NEW"]


async def test_bad_credentials_are_an_auth_error():
    router = Router(
        [json_response(401, {"message": "Authorization error: header is incorrect"})], []
    )
    p = GigaChat(credentials="bad", client=router.client(), retry=RetryPolicy(attempts=1))
    with pytest.raises(ProviderError) as exc:
        await run(p, req(Message.user("hi")))
    assert exc.value.kind == "auth" and "header is incorrect" in str(exc.value)


def test_history_function_messages_and_images():
    p = GigaChat(credentials="K")
    msgs = p.build_payload(
        req(
            Message.user("look", images=[Image("image/png", "AAA")]),
            Message.assistant(
                [
                    Text(""),
                    ToolCall("call_1", "ls", {"path": "."}, extra={"functions_state_id": "fs-1"}),
                ]
            ),
            Message.tool_results([ToolResult("call_1", (Text("a.py"),), name="ls")]),
        )
    )["messages"]
    assert msgs[1] == {"role": "user", "content": "look\n[image omitted]"}
    assert msgs[2] == {
        "role": "assistant",
        "content": "",
        "function_call": {"name": "ls", "arguments": {"path": "."}},
        "functions_state_id": "fs-1",
    }
    assert msgs[3] == {
        "role": "function",
        "name": "ls",
        "content": json.dumps({"result": "a.py"}, ensure_ascii=False),
    }


def test_several_calls_in_one_assistant_message_are_split():
    p = GigaChat(credentials="K")
    msgs = p.build_payload(
        req(
            Message.user("x"),
            Message.assistant(
                [ToolCall("c1", "ls", {"path": "a"}), ToolCall("c2", "ls", {"path": "b"})]
            ),
            Message.tool_results(
                [
                    ToolResult("c1", (Text("A"),), name="ls"),
                    ToolResult("c2", (Text("B"),), name="ls"),
                ]
            ),
        )
    )["messages"]
    assert [m["role"] for m in msgs[1:]] == [
        "user",
        "assistant",
        "function",
        "assistant",
        "function",
    ]
