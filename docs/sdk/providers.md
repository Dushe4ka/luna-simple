# Провайдеры

Провайдер это любой объект с `name` и асинхронным генератором `stream(request)`, который
отдаёт события модели. В ядре пять адаптеров, все на чистом `httpx`:

| API | Адаптер | Для чего |
|---|---|---|
| `anthropic` | `AnthropicMessages` | Claude (точки кеша, подписи рассуждений) |
| `openai_responses` | `OpenAIResponses` | reasoning-модели OpenAI |
| `openai_chat` | `OpenAIChat` | Chat Completions: DeepSeek, Groq, OpenRouter, Ollama, xAI, Mistral, ... |
| `google` | `GoogleGenAI` | нативный Gemini (подписи мыслей) |
| `gigachat` | `GigaChat` | GigaChat (обмен OAuth-токена, необязательный CA-бандл) |

## Выбор пресета

```python
from luna_core import providers

provider, model = providers.resolve("deepseek")                    # DEEPSEEK_API_KEY
provider, model = providers.resolve("openai", model="gpt-6-astra", api_key="...")
print(sorted(providers.PRESETS))
```

`resolve(provider, *, model=None, api_key=None, base_url=None, options=None, custom=None, env=os.environ)`
возвращает `(provider, model_id)`. Если ключа нет, бросается `LunaCoreError` с именем
переменной окружения. Встроенные пресеты всегда главнее записей `custom` с тем же ключом.
Пресеты: `anthropic`, `openai`, `openai_chat`, `deepseek`, `google`, `gigachat`, `yandex`,
`ollama` (ключ не нужен), `mistral`, `xai`, `groq`, `fireworks`, `together`, `openrouter`,
`perplexity`, `cerebras` (см. `providers.PRESETS`). Yandex AI Studio требует
`YANDEX_FOLDER_ID`; GigaChat читает `GIGACHAT_SCOPE` и `GIGACHAT_CA_BUNDLE`.

## Свой OpenAI-совместимый эндпоинт

```python
from luna_core.providers import Preset, resolve

mine = Preset("mylocal", "openai_chat", "local-model", None, "http://localhost:8000/v1")
provider, model = resolve("mylocal", custom={"mylocal": mine})
```

## Свой провайдер

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

`ModelRequest` содержит `model`, `system`, `messages`, `tools` (спецификации), `max_tokens`,
`temperature`, `reasoning` и `cache`. Чтобы вызвать инструменты, отдайте `ToolCallStart(index, id, name)`,
затем `ToolCallDelta(index, args_json)` и завершите `StopEvent("tool_use")`.
См. `examples/custom_provider.py`. Сетевую часть оберните в
`luna_core.providers.base.with_retries` и `post_sse`: повторы с задержкой и разбор ошибок
получаются без дополнительной работы.

## Скриптовый провайдер для тестов

`FakeProvider([...])` проигрывает сценарий; каждый элемент это `Message` (`FakeProvider.text(...)`,
`FakeProvider.call(name, args)`) или список событий модели; запросы он записывает.
Когда сценарий исчерпан, он поднимает assertion.

## Подводные камни

- Повторы происходят только до первого события потока; сбой в середине потока завершает ход
  событием `TurnError`.
- Идентификаторы моделей и цены живут в `providers/__init__.py` и `data/models.toml`, а не
  в логике вашего агента; для неизвестных моделей берутся безопасные значения по умолчанию и
  стоимость не считается.
