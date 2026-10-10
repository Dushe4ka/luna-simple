from luna_core.messages import Image, Message, Text, Thinking, ToolCall, ToolResult
from luna_core.providers.base import ModelRequest, StreamAccumulator
from luna_core.providers.google import GoogleGenAI, sanitize_schema
from luna_core.tools.base import ToolSpec
from luna_core.usage import Usage
from sse import Recorder, stream_response

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string", "default": ".", "title": "Path"}},
    "required": [],
    "additionalProperties": False,
}
LS = ToolSpec("ls", "List.", SCHEMA)


def chunk(parts=None, finish=None, usage=None):
    body: dict = {"candidates": [{"content": {"role": "model", "parts": parts or []}}]}
    if finish:
        body["candidates"][0]["finishReason"] = finish
    if usage:
        body["usageMetadata"] = usage
    return (None, body)


async def run(p, request):
    acc = StreamAccumulator(p.name)
    async for e in p.stream(request):
        acc.feed(e)
    return acc


def req(*messages, **kw):
    return ModelRequest(
        model="gemini-2.5-pro", system="sys", messages=list(messages), tools=[LS], **kw
    )


def test_sanitize_schema_strips_unsupported_keys_recursively():
    assert sanitize_schema(SCHEMA) == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": [],
    }


async def test_thoughts_text_and_function_call_with_signature():
    rec = Recorder(
        stream_response(
            [
                chunk([{"text": "consider", "thought": True}]),
                chunk([{"text": "Listing"}]),
                chunk(
                    [
                        {
                            "functionCall": {"name": "ls", "args": {"path": "."}},
                            "thoughtSignature": "TS1",
                        }
                    ]
                ),
                chunk(
                    finish="STOP",
                    usage={
                        "promptTokenCount": 20,
                        "candidatesTokenCount": 6,
                        "cachedContentTokenCount": 10,
                        "thoughtsTokenCount": 3,
                    },
                ),
            ]
        )
    )
    acc = await run(
        GoogleGenAI(api_key="k", client=rec.client()), req(Message.user("hi"), reasoning="medium")
    )
    msg = acc.message()
    assert msg.content[0] == Thinking("consider", None, "google")
    assert msg.content[1] == Text("Listing")
    call = msg.tool_calls[0]
    assert (call.name, call.args, call.extra) == ("ls", {"path": "."}, {"thought_signature": "TS1"})
    assert call.id.startswith("call_")
    assert acc.stop_reason == "tool_use"
    assert acc.usage == Usage(input=20, output=9, cache_read=10, reasoning=3)
    r = rec.requests[0]
    assert r.url.path == "/v1beta/models/gemini-2.5-pro:streamGenerateContent"
    assert r.url.params["alt"] == "sse" and r.headers["x-goog-api-key"] == "k"
    body = rec.last_json
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert body["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 8192,
        "includeThoughts": True,
    }
    assert body["tools"][0]["functionDeclarations"][0]["parameters"] == sanitize_schema(SCHEMA)


def test_history_parts_round_trip_signatures():
    p = GoogleGenAI(api_key="k")
    contents = p.build_payload(
        req(
            Message.user("look", images=[Image("image/png", "AAA")]),
            Message.assistant(
                [
                    Thinking("consider", None, "google"),
                    Text("Listing"),
                    Thinking("", "TXT_SIG", "google"),
                    ToolCall("call_1", "ls", {"path": "."}, extra={"thought_signature": "TS1"}),
                ]
            ),
            Message.tool_results([ToolResult("call_1", (Text("a.py"),), name="ls")]),
        )
    )["contents"]
    assert contents[0] == {
        "role": "user",
        "parts": [{"text": "look"}, {"inlineData": {"mimeType": "image/png", "data": "AAA"}}],
    }
    assert contents[1] == {
        "role": "model",
        "parts": [
            {"text": "Listing", "thoughtSignature": "TXT_SIG"},
            {"functionCall": {"name": "ls", "args": {"path": "."}}, "thoughtSignature": "TS1"},
        ],
    }
    assert contents[2] == {
        "role": "user",
        "parts": [{"functionResponse": {"name": "ls", "response": {"result": "a.py"}}}],
    }


async def test_text_signature_becomes_a_thinking_block():
    rec = Recorder(
        stream_response([chunk([{"text": "Done.", "thoughtSignature": "SIG"}], finish="STOP")])
    )
    acc = await run(GoogleGenAI(api_key="k", client=rec.client()), req(Message.user("hi")))
    assert acc.message().content == [Text("Done."), Thinking("", "SIG", "google")]
    assert acc.stop_reason == "end"


async def test_max_tokens_and_safety():
    rec = Recorder(
        stream_response([chunk([{"text": "a"}], finish="MAX_TOKENS")]),
        stream_response([chunk(finish="SAFETY")]),
    )
    p = GoogleGenAI(api_key="k", client=rec.client())
    assert (await run(p, req(Message.user("x")))).stop_reason == "max_tokens"
    assert (await run(p, req(Message.user("x")))).stop_reason == "refusal"
