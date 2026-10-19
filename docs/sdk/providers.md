# Providers

A provider is any object with a `name` and an async generator `stream(request)` that
yields model events. Five adapters ship with the core, all on plain `httpx`:

| API | Adapter | Used for |
|---|---|---|
| `anthropic` | `AnthropicMessages` | Claude (cache breakpoints, thinking signatures) |
| `openai_responses` | `OpenAIResponses` | OpenAI reasoning models |
| `openai_chat` | `OpenAIChat` | Chat Completions: DeepSeek, Groq, OpenRouter, Ollama, xAI, Mistral, ... |
| `google` | `GoogleGenAI` | native Gemini (thought signatures) |
| `gigachat` | `GigaChat` | GigaChat (OAuth token exchange, optional CA bundle) |

## Resolve a preset

```python
from luna_core import providers

provider, model = providers.resolve("deepseek")                    # DEEPSEEK_API_KEY
provider, model = providers.resolve("openai", model="gpt-6-astra", api_key="...")
print(sorted(providers.PRESETS))
```

`resolve(provider, *, model=None, api_key=None, base_url=None, options=None, custom=None, env=os.environ)`
returns `(provider, model_id)`. A missing key raises `LunaCoreError` naming the environment
variable. Built-in presets always win over `custom` entries with the same key.
Presets: `anthropic`, `openai`, `openai_chat`, `deepseek`, `google`, `gigachat`, `yandex`
and the OpenAI-compatible ones (see `providers.PRESETS`). Yandex AI Studio needs
`YANDEX_FOLDER_ID`; GigaChat reads `GIGACHAT_SCOPE` and `GIGACHAT_CA_BUNDLE`.

## Custom OpenAI-compatible endpoint

```python
from luna_core.providers import Preset, resolve

mine = Preset("mylocal", "openai_chat", "local-model", None, "http://localhost:8000/v1")
provider, model = resolve("mylocal", custom={"mylocal": mine})
```

## Write your own

```python
from luna_core.events import StopEvent, TextDelta, UsageReport
from luna_core import Usage


class EchoProvider:
    name = "echo"

    async def stream(self, request):          # request: ModelRequest
        yield TextDelta("You said: " + request.messages[-1].text)
        yield UsageReport(Usage(input=3, output=4))
        yield StopEvent("end")                # end | tool_use | max_tokens | refusal
```

`ModelRequest` carries `model`, `system`, `messages`, `tools` (specs), `max_tokens`,
`temperature`, `reasoning` and `cache`. To call tools, emit `ToolCallStart(index, id, name)`
followed by `ToolCallDelta(index, args_json)` and stop with `StopEvent("tool_use")`.
See `examples/custom_provider.py`. Wrap the network part with
`luna_core.providers.base.with_retries` and `post_sse` to get backoff and error mapping
for free.

## Scripted provider for tests

`FakeProvider([...])` replays a script; each item is a `Message` (`FakeProvider.text(...)`,
`FakeProvider.call(name, args)`) or a list of model events, and it records the requests.
It raises an assertion when the script is exhausted.

## Pitfalls

- Retries happen only before the first event of a stream; a mid-stream failure ends the turn
  with `TurnError`.
- Model ids and prices live in `providers/__init__.py` and `data/models.toml`, never in
  your agent logic; unknown models get safe defaults and no cost.
