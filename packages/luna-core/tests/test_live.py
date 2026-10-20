import os

import pytest
from luna_core.messages import Message
from luna_core.providers import PRESETS, resolve
from luna_core.providers.base import ModelRequest, StreamAccumulator
from luna_core.tools.base import ToolSpec

pytestmark = pytest.mark.live
ECHO = ToolSpec(
    "echo",
    "Echo the text back.",
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
)
LIVE = ["anthropic", "openai", "deepseek", "google", "gigachat", "yandex", "openrouter"]


@pytest.mark.parametrize("key", LIVE)
async def test_live_tool_call(key):
    preset = PRESETS[key]
    if preset.env_var and not os.environ.get(preset.env_var):
        pytest.skip(f"{preset.env_var} not set")
    provider, model = resolve(key)
    acc = StreamAccumulator(provider.name)
    request = ModelRequest(
        model=model,
        system="Always call the echo tool with the user's text. Do not answer in prose.",
        messages=[Message.user("привет")],
        tools=[ECHO],
        max_tokens=256,
    )
    async for ev in provider.stream(request):
        acc.feed(ev)
    assert acc.message().tool_calls, acc.message()
    assert acc.usage.input > 0
