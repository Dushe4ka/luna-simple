# Luna — lightweight CLI coding agent — Design

**Date:** 2026-09-06
**Status:** Approved (design), pending implementation plan
**Distribution name:** `luna-simple` · **CLI:** `luna` (alias `luna-simple`)

## 1. Goal

A minimal, self-contained CLI coding agent built on the `deepagents`
framework (LangChain, Python). Same spirit as
[`pi`](https://github.com/earendil-works/pi) — unified LLM API + agent
loop + coding CLI — but deliberately lightweight: no custom TUI engine,
a single `rich`-based REPL, thin glue over `deepagents`.

Tagline: *Luna — a simple, lightweight CLI coding agent. Observe · Understand · Plan · Act.*

## 2. Constraints & decisions

| Topic | Decision |
| --- | --- |
| Language | Python **3.11+** (deepagents requires 3.11+), PEP 8 / PEP 257 |
| Packaging | `pyproject.toml`, hatchling backend, `uv` for dev env |
| Framework | `deepagents.create_deep_agent` (v0.7.x) |
| Repo layout | `git init` in project root, package `luna/` at root |
| UI | Lightweight REPL on `rich`; ANSI splash reproducing the reference image |
| Filesystem | Real `cwd` via `LocalShellBackend`; human-in-the-loop approval on writes/shell; `--yolo` disables |
| Providers | `anthropic` (default), `deepseek`, `openai`, `google`, `ollama` via `init_chat_model` |
| Default model | `anthropic:claude-sonnet-4-5` (configurable) |

## 3. Verified framework facts (deepagents 0.7.13 / langchain 1.4 / langgraph 1.2)

- `create_deep_agent(model, tools=None, *, system_prompt, middleware, subagents,
  skills, memory, permissions, backend, interrupt_on, checkpointer, store, name, ...)`
- Backends: `from deepagents.backends import LocalShellBackend, FilesystemBackend,
  CompositeBackend, StateBackend`
- `LocalShellBackend(root_dir=None, *, virtual_mode=True, timeout=120,
  max_output_bytes=100_000, env=None, inherit_env=False)` — adds the `execute` shell tool
- Built-in filesystem tool names (valid `interrupt_on` keys):
  `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, `grep`, `execute`
- Planning middleware adds `write_todos`; subagent middleware adds `task`
- `interrupt_on={"write_file": True, ...}` — values `True | False | InterruptOnConfig`
- Resume: `agent.invoke(Command(resume={"decisions": [...]}), config=...)` where each
  decision is `{"type": "approve"|"edit"|"reject"|"respond", ...}`
- HITL requires a `checkpointer` (`langgraph.checkpoint.memory.InMemorySaver`)
- `langchain_anthropic` auto-injects a prompt-caching middleware when the model is Anthropic

## 4. Repository layout

```
Luna_pi/
├── pyproject.toml            # name = "luna-simple"; scripts luna / luna-simple; optional-deps per provider
├── README.md                 # manifesto + slogans (EN/RU) from reference image #2
├── LICENSE                   # MIT
├── CHANGELOG.md              # Keep a Changelog; 0.1.0
├── .gitignore                # Python + .venv + .env
├── .env.example              # all provider API keys, commented
├── AGENTS.md                 # agent memory file (loaded via memory=[...])
├── ruff.toml                 # lint/format config
├── luna/
│   ├── __init__.py           # __version__ = "0.1.0"
│   ├── __main__.py           # `python -m luna` -> cli.main()
│   ├── cli.py                # argparse; one-shot vs REPL; flags
│   ├── config.py             # LunaConfig dataclass + layered resolution
│   ├── providers.py          # PROVIDERS registry -> build_model()
│   ├── prompts.py            # LUNA_SYSTEM_PROMPT (persona)
│   ├── agent.py              # build_agent(config) -> compiled deep agent
│   ├── session.py            # REPL loop, streaming, slash commands, interrupt handling
│   └── ui/
│       ├── __init__.py
│       ├── theme.py          # rich color tokens from the reference palette
│       ├── splash.py         # render_splash(console, steps)
│       ├── console.py        # get_console(), stream renderer, spinner helpers
│       └── approve.py        # approval prompt -> decision dict
├── tests/
│   ├── conftest.py           # FakeToolCallingModel fixture
│   ├── test_providers.py
│   ├── test_config.py
│   ├── test_cli.py
│   ├── test_agent.py
│   ├── test_session_decisions.py
│   └── test_splash.py
├── docs/
│   └── superpowers/specs/2026-09-06-luna-cli-agent-design.md
└── .github/workflows/ci.yml  # ruff + pytest on 3.11/3.12
```

## 5. Components

### 5.1 `luna/config.py`

`LunaConfig` dataclass: `provider`, `model`, `workdir`, `yolo`, `show_splash`,
`max_tokens`, `temperature`, `extra_model_kwargs`.

Resolution order (highest wins):
1. CLI flags (`--provider`, `--model`, `--yolo`, `--no-splash`, `--workdir`)
2. Environment (`LUNA_PROVIDER`, `LUNA_MODEL`, `LUNA_YOLO`)
3. Project file `./.luna.toml`
4. User file `~/.config/luna/config.toml` (XDG-aware)
5. Built-in defaults

`load_config(args) -> LunaConfig`. TOML parsed with stdlib `tomllib`.

### 5.2 `luna/providers.py`

```python
@dataclass(frozen=True)
class ProviderSpec:
    key: str
    init_prefix: str          # value passed to init_chat_model as "<prefix>:<model>"
    default_model: str
    env_var: str | None
    pip_extra: str            # e.g. "anthropic"

PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec("anthropic", "anthropic", "claude-sonnet-4-5", "ANTHROPIC_API_KEY", "anthropic"),
    "deepseek":  ProviderSpec("deepseek",  "deepseek",  "deepseek-chat",     "DEEPSEEK_API_KEY",  "deepseek"),
    "openai":    ProviderSpec("openai",    "openai",    "gpt-4.1",           "OPENAI_API_KEY",    "openai"),
    "google":    ProviderSpec("google",    "google_genai", "gemini-2.5-pro", "GOOGLE_API_KEY",    "google"),
    "ollama":    ProviderSpec("ollama",    "ollama",    "qwen2.5-coder",     None,                "ollama"),
}
```

`build_model(config) -> BaseChatModel`:
- resolve `ProviderSpec`
- if `env_var` set and missing from env -> `LunaConfigError` with a clear hint
- `init_chat_model(f"{prefix}:{model}", **kwargs)`; on `ImportError` -> hint
  `pip install "luna-simple[<extra>]"`

Model IDs are defaults only and live in the registry / config — never hard-coded
in agent logic. Re-check current IDs against provider docs (context7) at build time.

### 5.3 `luna/prompts.py`

`LUNA_SYSTEM_PROMPT` — persona from reference image #2: a calm guide that
observes, understands, plans, then acts; concise; explains what it is about to
do before doing it; respects the approval flow; works in the user's real repo.

### 5.4 `luna/agent.py`

```python
def build_agent(config: LunaConfig, *, checkpointer=None):
    backend = LocalShellBackend(root_dir=config.workdir, inherit_env=True)
    interrupt_on = None if config.yolo else {
        "write_file": True,
        "edit_file": True,
        "delete": True,
        "execute": {"allowed_decisions": ["approve", "edit", "reject"]},
    }
    memory = ["AGENTS.md"] if (Path(config.workdir) / "AGENTS.md").exists() else None
    return create_deep_agent(
        model=build_model(config),
        system_prompt=LUNA_SYSTEM_PROMPT,
        backend=backend,
        memory=memory,
        interrupt_on=interrupt_on,
        checkpointer=checkpointer or InMemorySaver(),
        name="luna",
    )
```

### 5.5 `luna/session.py`

- `run_once(agent, prompt, thread_id)` — stream, render, handle interrupts, return final text.
- `run_repl(agent, console)` — prompt `luna ›`; slash commands
  `/help /model /provider /tools /clear /exit /new`; Ctrl-C cancels current turn,
  Ctrl-D exits.
- Streaming: `agent.stream(payload, config, stream_mode=["messages", "updates"])`.
  Render assistant tokens live; show a spinner labelled with the running tool.
- Interrupt loop: when a chunk carries `__interrupt__`, for each
  `action_request` call `ui.approve.prompt_decision(...)`, collect decisions,
  resume with `Command(resume={"decisions": decisions})`.
- One `thread_id` per REPL session (regenerated by `/new`); conversation memory
  is the checkpointer keyed by that id.

### 5.6 `luna/ui/`

- `theme.py` — palette lifted from the reference: deep navy `#0b1026` bg,
  moon whites `#e8ecff`, periwinkle `#8a9cff`, muted blue `#5566a8`, cloud
  mauve `#b98cc9`. Rich `Theme` with named styles.
- `splash.py` — `render_splash(console, steps: list[str])`:
  block-character moon, large `L U N A`, `YOUR AI AGENT COMPANION`,
  `INITIALIZING ...`, corner slogans (`A BRIGHTER TOMORROW TOGETHER`,
  `SAME MOON BRIGHTER POSSIBILITIES`, `IDEAS INTO REALITY`,
  `HUMAN AND AI FURTHER TOGETHER`), then streamed loading lines
  (`> loading modules ...` etc. — real init steps). Degrades on narrow
  terminals and on no-truecolor; suppressed by `--no-splash` / non-TTY.
- `console.py` — singleton `Console`, markdown renderer for assistant output,
  spinner context manager, tool-call / tool-result formatting.
- `approve.py` — `prompt_decision(action_request) -> dict`: shows tool name +
  args (diff for `write_file`/`edit_file`, command for `execute`); keys:
  Enter/`y` approve, `e` edit (opens `$EDITOR` or inline), `n` reject + reason.

### 5.7 `luna/cli.py`

```
luna [PROMPT]              positional: one-shot prompt (omit for REPL)
  -p, --prompt TEXT        alternative to positional
  --provider {anthropic,deepseek,openai,google,ollama}
  --model TEXT
  --workdir PATH           default: cwd
  --yolo                   disable all approval prompts
  --no-splash
  --version
```

Exit codes: 0 ok, 1 runtime error, 2 bad usage/config, 130 interrupted.

## 6. Testing

`FakeToolCallingModel` (in `conftest.py`) — a `BaseChatModel` subclass with a
scripted queue of `AIMessage`s (with/without `tool_calls`) and a working
`bind_tools`. Enables offline agent-loop tests.

| Test | Asserts |
| --- | --- |
| `test_providers` | registry integrity; missing-key error + hint; unknown provider error |
| `test_config` | layer precedence CLI > env > project toml > user toml > defaults |
| `test_cli` | arg parsing; one-shot vs REPL dispatch; `--version` |
| `test_agent` | `build_agent` returns compiled graph; `interrupt_on` None under `--yolo`; `memory` wired when `AGENTS.md` present |
| `test_session_decisions` | approve/edit/reject mapped to correct `Command(resume=...)` payload; multi-tool ordering |
| `test_splash` | renders without exception at widths 40/80/200; honours `--no-splash` |

CI: `ruff check` + `ruff format --check` + `pytest` on Python 3.11 and 3.12.

## 7. README / git manifesto (from reference image #2)

Slogans to surface in `README.md` and repo description:
- *A brighter tomorrow, together* · *Same moon, brighter possibilities*
- *Ideas into reality* · *Human and AI, further together*
- «Луна не спорит с ночью — она делает её понятной»
- «Ясность вместо шума. Маршрут вместо хаоса»
- «Я свет, который не ослепляет, а помогает видеть путь»
- **LUNA — тихий интеллект для навигации в сложных системах.** Observe · Understand · Plan · Act.

## 8. Out of scope (YAGNI)

Custom diff-render TUI, sandbox/containers, MCP client, session sharing,
multi-screen layouts, providers beyond the five, cost tracking, auto-update.

## 9. Risks

- Provider model IDs drift — mitigated: defaults in registry/config, documented
  `/model` override, re-checked against context7 during implementation.
- deepagents is pre-1.0 and API may shift — mitigated: pin `deepagents~=0.7.13`,
  isolate all framework calls in `agent.py` / `session.py`.
- `virtual_mode` semantics of `LocalShellBackend` — verify whether `False` is
  needed for natural absolute paths during implementation; default to the safer
  value that still lets the agent edit the real repo.
