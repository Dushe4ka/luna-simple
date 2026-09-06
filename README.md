<div align="center">

# 🌙 Luna

**A simple, lightweight CLI coding agent.**

`Observe · Understand · Plan · Act`

*LUNA — тихий интеллект для навигации в сложных системах.*

</div>

---

> «Луна не спорит с ночью — она делает её понятной.»
> «Ясность вместо шума. Маршрут вместо хаоса.»
> «Я свет, который не ослепляет, а помогает видеть путь.»

- *A brighter tomorrow, together*
- *Same moon, brighter possibilities*
- *Ideas into reality*
- *Human and AI, further together*

---

Luna is a **cli coding agent** built on the
[`deepagents`](https://github.com/langchain-ai/deepagents) framework
(LangChain / LangGraph). It is a deliberately small analogue of
[`pi`](https://github.com/earendil-works/pi): a unified LLM API, an agent loop,
and a coding CLI — with a single `rich`-based REPL instead of a bespoke TUI.

The accent is on **`luna-simple`**: few dependencies, one command, easy to read
end to end.

## Install

```bash
# with every provider integration
uv pip install "luna-simple[all]"

# or just the default provider (Anthropic)
uv pip install luna-simple

# from a checkout
uv venv --python 3.12 && uv pip install -e ".[dev,all]"
```

Python **3.11+** is required.

## Quickstart

```bash
# one-shot
luna "read pyproject.toml and tell me the entry points"

# interactive REPL (shows the splash)
luna
```

In the REPL: `/help`, `/tools`, `/model`, `/provider`, `/new`, `/clear`, `/exit`.

## Providers

Luna resolves models through LangChain's `init_chat_model`. Set the matching
API key (see `.env.example`) and select with `--provider` / `--model` or a
config file.

| Provider | `--provider` | Default model | Key | Extra |
| --- | --- | --- | --- | --- |
| Anthropic *(default)* | `anthropic` | `claude-sonnet-4-5` | `ANTHROPIC_API_KEY` | (included) |
| DeepSeek | `deepseek` | `deepseek-chat` | `DEEPSEEK_API_KEY` | `luna-simple[deepseek]` |
| OpenAI | `openai` | `gpt-4.1` | `OPENAI_API_KEY` | `luna-simple[openai]` |
| Google | `google` | `gemini-2.5-pro` | `GOOGLE_API_KEY` | `luna-simple[google]` |
| Ollama | `ollama` | `qwen2.5-coder` | — (local) | `luna-simple[ollama]` |

```bash
luna --provider deepseek --model deepseek-reasoner "refactor utils.py"
luna --provider ollama "explain this stack trace"
```

## Safety

Luna operates on the **real files** in your working directory and can run shell
commands. Before every `write_file`, `edit_file`, `delete`, or `execute` it
stops and asks:

```
[Enter] approve · [e] edit · [n] reject >
```

Pass `--yolo` to disable all approval prompts.

## Configuration

Precedence (highest first): CLI flags → environment → `./.luna.toml` →
`~/.config/luna/config.toml` → defaults.

```toml
# .luna.toml
[model]
provider = "anthropic"
name = "claude-sonnet-4-5"

[agent]
yolo = false
temperature = 0.0

[ui]
splash = true
```

Environment: `LUNA_PROVIDER`, `LUNA_MODEL`, `LUNA_YOLO`, `LUNA_WORKDIR`.

## License

MIT — see [LICENSE](LICENSE).
