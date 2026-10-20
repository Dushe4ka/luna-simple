import httpx
import pytest
from luna_core.errors import ProviderError
from luna_core.events import (
    StopEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
    UsageReport,
)
from luna_core.messages import Message, Text, Thinking, ToolCall
from luna_core.providers.base import (
    ModelRequest,
    RetryPolicy,
    StreamAccumulator,
    error_from_response,
    post_sse,
    with_retries,
)
from luna_core.providers.fake import FakeProvider
from luna_core.usage import Usage
from sse import Recorder, json_response, stream_response


def test_accumulator_keeps_block_order_and_parses_args():
    acc = StreamAccumulator("anthropic")
    for ev in [
        ThinkingDelta("let me "),
        ThinkingDelta("see", signature="sig1"),
        TextDelta("Reading "),
        TextDelta("files."),
        ToolCallStart(0, "c1", "read_file"),
        ToolCallDelta(0, '{"file_'),
        ToolCallDelta(0, 'path": "a.py"}'),
        ToolCallStart(1, "c2", "ls", extra={"k": "v"}),
        ToolCallDelta(1, "{}"),
        UsageReport(Usage(input=10, output=3)),
        StopEvent("tool_use"),
    ]:
        acc.feed(ev)
    assert acc.message() == Message.assistant(
        [
            Thinking("let me see", signature="sig1", provider="anthropic"),
            Text("Reading files."),
            ToolCall("c1", "read_file", {"file_path": "a.py"}),
            ToolCall("c2", "ls", {}, extra={"k": "v"}),
        ]
    )
    assert acc.usage == Usage(input=10, output=3)
    assert acc.stop_reason == "tool_use"


def test_accumulator_signature_without_text_creates_a_thinking_block():
    acc = StreamAccumulator("openai_responses")
    acc.feed(ThinkingDelta("", signature="enc"))
    acc.feed(TextDelta("hi"))
    assert acc.message().content == [
        Thinking("", signature="enc", provider="openai_responses"),
        Text("hi"),
    ]


def test_invalid_tool_json_is_kept_for_the_tool_to_report():
    acc = StreamAccumulator("x")
    acc.feed(ToolCallStart(0, "c1", "grep"))
    acc.feed(ToolCallDelta(0, '{"pattern": '))
    assert acc.message().tool_calls[0].args == {"__invalid_json__": '{"pattern": '}


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (401, '{"error": {"message": "bad key"}}', "auth"),
        (403, "forbidden", "auth"),
        (429, '{"error": {"message": "slow"}}', "rate_limit"),
        (
            400,
            '{"error": {"message": "prompt is too long: 300000 tokens > 200000"}}',
            "context_length",
        ),
        (400, '{"error": {"message": "maximum context length is 128000"}}', "context_length"),
        (400, '{"error": {"message": "bad field"}}', "bad_request"),
        (500, "oops", "server"),
        (529, "overloaded", "server"),
    ],
)
def test_error_classification(status, body, kind):
    err = error_from_response(status, body, {"retry-after": "3"} if status == 429 else {})
    assert err.kind == kind
    assert err.status == status
    if status == 429:
        assert err.retry_after == 3.0
    if body.startswith("{"):
        assert "error" not in str(err)  # the inner message, not the raw JSON


async def test_retries_transient_errors_before_any_event():
    calls = {"n": 0}

    async def opener():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("busy", "server", 503)
        yield TextDelta("ok")

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    events = [
        ev async for ev in with_retries(opener, RetryPolicy(attempts=3, base_delay=1), fake_sleep)
    ]
    assert events == [TextDelta("ok")]
    assert slept == [1, 2]


async def test_no_retry_after_first_event():
    async def opener():
        yield TextDelta("partial")
        raise ProviderError("connection reset", "network")

    got = []
    with pytest.raises(ProviderError, match="connection reset"):
        async for ev in with_retries(opener, RetryPolicy(attempts=5), lambda s: None):
            got.append(ev)
    assert got == [TextDelta("partial")]


async def test_auth_errors_are_not_retried():
    calls = {"n": 0}

    async def opener():
        calls["n"] += 1
        raise ProviderError("bad key", "auth", 401)
        yield  # pragma: no cover

    with pytest.raises(ProviderError):
        async for _ in with_retries(opener, RetryPolicy(attempts=3), lambda s: None):
            pass
    assert calls["n"] == 1


async def test_post_sse_yields_events_and_maps_http_errors():
    rec = Recorder(
        stream_response([("message", {"a": 1}), (None, "[DONE]")]),
        json_response(401, {"error": {"message": "invalid x-api-key"}}),
    )
    client = rec.client()
    got = [pair async for pair in post_sse(client, "https://x/v1", headers={}, json={"q": 1})]
    assert got == [("message", '{"a": 1}'), ("message", "[DONE]")]
    with pytest.raises(ProviderError) as exc:
        [pair async for pair in post_sse(client, "https://x/v1", headers={}, json={})]
    assert exc.value.kind == "auth" and "invalid x-api-key" in str(exc.value)


async def test_post_sse_maps_network_errors():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    with pytest.raises(ProviderError) as exc:
        [pair async for pair in post_sse(client, "https://x", headers={}, json={})]
    assert exc.value.kind == "network"


async def test_fake_provider_replays_messages_and_records_requests():
    fake = FakeProvider(
        [FakeProvider.call("ls", {"path": "."}, call_id="c1"), FakeProvider.text("done")]
    )
    req = ModelRequest(model="m", system="s", messages=[Message.user("hi")])
    first = [ev async for ev in fake.stream(req)]
    assert ToolCallStart(0, "c1", "ls") in first and first[-1] == StopEvent("tool_use")
    second = [ev async for ev in fake.stream(req)]
    assert second[0] == TextDelta("done") and second[-1] == StopEvent("end")
    assert len(fake.requests) == 2
    with pytest.raises(AssertionError, match="script exhausted"):
        [ev async for ev in fake.stream(req)]
