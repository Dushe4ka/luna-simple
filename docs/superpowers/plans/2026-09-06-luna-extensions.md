# Luna Extensions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a running Luna agent installable skills, MCP servers, and named subagents (added by the user via CLI or by the agent with approval), activated in-session with `/reload`, plus clear user/agent turn framing in the REPL.

**Architecture:** New leaf modules (`registry`, `mcp`, `skills`, `subagents`, `extension_tools`, `ui/turn`) feed `luna/agent.build_agent`, which now assembles `skills=`, `subagents=`, and MCP + `manage_*` tools. `luna/session` gains `/reload` (rebuild the compiled graph, keep the thread) and turn framing. `luna/cli` gains `mcp` / `skills` / `agents` subcommands.

**Tech Stack:** Python 3.11+, `deepagents~=0.7.13`, `langchain-mcp-adapters>=0.3` (optional extra), `rich`, `pytest`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-09-06-luna-extensions-design.md`

## Global Constraints

- Python 3.11+, PEP 8 / PEP 257, `ruff` clean.
- All `deepagents` / `langgraph` / `langchain_mcp_adapters` imports stay inside
  `luna/agent.py`, `luna/session.py`, `luna/mcp.py`, `luna/subagents.py`,
  `luna/extension_tools.py`.
- Config dirs: user `config_dir()` (from `luna.config`), project `./.luna/`
  (project wins on merge).
- `mcp.json` uses the Claude Desktop `{"mcpServers": {...}}` shape.
- Skills are `<dir>/<name>/SKILL.md` with YAML frontmatter (`name`, `description`).
- MCP tool names are prefixed `mcp__<server>__<tool>`.
- Missing `langchain_mcp_adapters` never crashes Luna — degrade with the hint
  `pip install "luna-simple[mcp]"`.
- Tests never hit the network or spawn real `npx`; skill installs use `file://`
  local repos; MCP is tested at the config/translation layer.
- Commit per task. Trailer: `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.

---

### Task 1: Capability registry (`luna/registry.py`)

**Files:**
- Create: `luna/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `luna.config.config_dir`, `luna.providers.LunaConfigError`.
- Produces:
  - `MCP_REGISTRY: dict[str, dict]`, `SKILL_REGISTRY: dict[str, dict]`
  - `load_user_registry(*, env=None) -> dict` with keys `"mcp"`, `"skills"`
  - `resolve_mcp(name: str, *, env=None) -> dict` — merged spec or `LunaConfigError`
  - `resolve_skill(name: str, *, env=None) -> dict` — `{"repo": str, "path": str | None}` or `LunaConfigError`
  - `known_mcp(*, env=None) -> list[str]`, `known_skills(*, env=None) -> list[str]`

- [ ] **Step 1: failing test** — `tests/test_registry.py`

```python
import pytest
from luna.providers import LunaConfigError
from luna.registry import known_mcp, resolve_mcp, resolve_skill


def test_builtin_mcp_lookup():
    spec = resolve_mcp("filesystem")
    assert spec["command"] == "npx"
    assert "@modelcontextprotocol/server-filesystem" in spec["args"]


def test_builtin_skill_lookup():
    assert resolve_skill("pdf")["repo"] == "anthropics/skills"


def test_unknown_raises_and_lists():
    with pytest.raises(LunaConfigError) as e:
        resolve_mcp("nope")
    assert "filesystem" in str(e.value)


def test_user_registry_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "registry.toml").write_text(
        '[mcp.filesystem]\ncommand = "uvx"\nargs = ["my-fs"]\n'
        '[mcp.custom]\ncommand = "node"\nargs = ["server.js"]\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    assert resolve_mcp("filesystem")["command"] == "uvx"
    assert resolve_mcp("custom")["args"] == ["server.js"]
    assert "custom" in known_mcp()
```

- [ ] **Step 2:** run → FAIL (module missing).

- [ ] **Step 3: implement `luna/registry.py`**

```python
"""Curated registry of well-known MCP servers and skills, plus user overrides."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping

from luna.config import config_dir
from luna.providers import LunaConfigError

MCP_REGISTRY: dict[str, dict] = {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
    },
    "git": {"command": "uvx", "args": ["mcp-server-git"]},
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
    "sequential-thinking": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
    },
    "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
    "time": {"command": "uvx", "args": ["mcp-server-time"]},
    "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
}

SKILL_REGISTRY: dict[str, dict] = {
    "pdf": {"repo": "anthropics/skills", "path": "document-skills/pdf"},
    "docx": {"repo": "anthropics/skills", "path": "document-skills/docx"},
    "xlsx": {"repo": "anthropics/skills", "path": "document-skills/xlsx"},
    "pptx": {"repo": "anthropics/skills", "path": "document-skills/pptx"},
}


def _registry_path(env: Mapping[str, str] | None) -> "object":
    return config_dir(env) / "registry.toml"


def load_user_registry(*, env: Mapping[str, str] | None = None) -> dict:
    path = _registry_path(env)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {"mcp": {}, "skills": {}}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LunaConfigError(f"Cannot read {path}: {exc}") from exc
    return {"mcp": dict(data.get("mcp", {})), "skills": dict(data.get("skills", {}))}


def _merged(kind: str, builtin: dict, env: Mapping[str, str] | None) -> dict:
    merged = dict(builtin)
    merged.update(load_user_registry(env=env)[kind])
    return merged


def known_mcp(*, env: Mapping[str, str] | None = None) -> list[str]:
    return sorted(_merged("mcp", MCP_REGISTRY, env))


def known_skills(*, env: Mapping[str, str] | None = None) -> list[str]:
    return sorted(_merged("skills", SKILL_REGISTRY, env))


def resolve_mcp(name: str, *, env: Mapping[str, str] | None = None) -> dict:
    reg = _merged("mcp", MCP_REGISTRY, env)
    if name not in reg:
        raise LunaConfigError(
            f"Unknown MCP server {name!r}. Known: {', '.join(sorted(reg))}. "
            f"Or pass an explicit command: luna mcp add {name} -- npx -y <pkg>"
        )
    return dict(reg[name])


def resolve_skill(name: str, *, env: Mapping[str, str] | None = None) -> dict:
    reg = _merged("skills", SKILL_REGISTRY, env)
    if name not in reg:
        raise LunaConfigError(
            f"Unknown skill {name!r}. Known: {', '.join(sorted(reg))}. "
            f"Or pass a repo: luna skills add owner/repo[/subdir]"
        )
    return dict(reg[name])
```

- [ ] **Step 4:** run tests → PASS.
- [ ] **Step 5: commit** `feat: capability registry for MCP servers and skills`

---

### Task 2: MCP config + tool loading (`luna/mcp.py`)

**Files:**
- Create: `luna/mcp.py`
- Test: `tests/test_mcp_config.py`

**Interfaces:**
- Consumes: `luna.config.config_dir`, `luna.registry.resolve_mcp`, `luna.providers.LunaConfigError`.
- Produces:
  - `mcp_files(workdir: str, *, env=None) -> list[Path]` (user, then project `./.luna/mcp.json`)
  - `load_mcp_config(workdir=".", *, env=None) -> dict[str, dict]` — merged `mcpServers`, `${ENV}` expanded
  - `to_connections(servers: dict) -> dict[str, dict]` — langchain connection dicts
  - `add_server(name, spec: dict, *, project=False, workdir=".", env=None) -> Path`
  - `remove_server(name, *, project=False, workdir=".", env=None) -> bool`
  - `MCP_AVAILABLE: bool`
  - `load_mcp_tools(connections: dict, *, on_warn=print) -> list`  (empty list + warning on any failure)

- [ ] **Step 1: failing test** — `tests/test_mcp_config.py`

```python
import json

import pytest

from luna.mcp import (
    add_server, load_mcp_config, load_mcp_tools, remove_server, to_connections,
)


def _user_mcp(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return cfg / "mcp.json"


def test_env_substitution_and_merge(tmp_path, monkeypatch):
    f = _user_mcp(tmp_path, monkeypatch)
    monkeypatch.setenv("GH", "tok123")
    f.write_text(json.dumps({"mcpServers": {"gh": {"command": "npx",
                 "args": ["-y", "srv"], "env": {"T": "${GH}"}}}}))
    cfg = load_mcp_config(str(tmp_path))
    assert cfg["gh"]["env"]["T"] == "tok123"


def test_translation_shapes():
    conns = to_connections({
        "a": {"command": "npx", "args": ["x"]},
        "b": {"type": "http", "url": "https://h/mcp"},
        "c": {"type": "sse", "url": "https://h/sse"},
    })
    assert conns["a"]["transport"] == "stdio"
    assert conns["b"]["transport"] == "streamable_http"
    assert conns["c"]["transport"] == "sse"


def test_add_remove_roundtrip(tmp_path, monkeypatch):
    _user_mcp(tmp_path, monkeypatch)
    add_server("fs", {"command": "npx", "args": ["-y", "srv-fs"]}, workdir=str(tmp_path))
    assert "fs" in load_mcp_config(str(tmp_path))
    assert remove_server("fs", workdir=str(tmp_path)) is True
    assert "fs" not in load_mcp_config(str(tmp_path))


def test_project_scope_wins(tmp_path, monkeypatch):
    _user_mcp(tmp_path, monkeypatch)
    add_server("s", {"command": "u", "args": ["a"]}, workdir=str(tmp_path))
    add_server("s", {"command": "p", "args": ["b"]}, project=True, workdir=str(tmp_path))
    assert load_mcp_config(str(tmp_path))["s"]["command"] == "p"


def test_load_tools_without_adapter_returns_empty(monkeypatch):
    import luna.mcp as m
    monkeypatch.setattr(m, "MCP_AVAILABLE", False)
    warned = []
    assert m.load_mcp_tools({"x": {}}, on_warn=warned.append) == []
    assert warned and "luna-simple[mcp]" in warned[0]
```

- [ ] **Step 2:** run → FAIL.

- [ ] **Step 3: implement `luna/mcp.py`**

Key points:
- `try: from langchain_mcp_adapters.client import MultiServerMCPClient; MCP_AVAILABLE = True except ImportError: MCP_AVAILABLE = False`
- `_expand(obj)` recursively replaces `${VAR}` in str values using `os.environ` (or passed `env`); unknown var → left as-is.
- `load_mcp_config`: read user file then project file with `json.load`; each must be `{"mcpServers": {...}}` or `{}`; project entries override.
- `to_connections`: per server — if `"command"` present → `{"transport": "stdio", "command", "args"=spec.get("args", []), "env"=spec.get("env")}` (drop `None`); `type=="http"` → `{"transport": "streamable_http", "url", "headers"?}`; `type=="sse"` → `{"transport": "sse", "url", "headers"?}`; else `LunaConfigError`.
- `add_server`/`remove_server`: load the target file (user or `./.luna/mcp.json`), mutate `mcpServers`, write pretty JSON, `mkdir(parents=True)`.
- `load_mcp_tools(connections, on_warn=print)`:

```python
def load_mcp_tools(connections, *, on_warn=print):
    if not connections:
        return []
    if not MCP_AVAILABLE:
        on_warn('MCP support not installed. Run: pip install "luna-simple[mcp]"')
        return []
    import asyncio
    from langchain_mcp_adapters.client import MultiServerMCPClient
    try:
        client = MultiServerMCPClient(connections, tool_name_prefix=True)
        return asyncio.run(client.get_tools())
    except Exception as exc:  # noqa: BLE001 - one bad server must not break Luna
        on_warn(f"MCP: could not start servers ({exc}). Check 'luna mcp test <name>'.")
        return []
```

- [ ] **Step 4:** run tests → PASS.
- [ ] **Step 5: commit** `feat: MCP config loading, translation, and tool discovery`

---

### Task 3: Skills install / list / remove (`luna/skills.py`)

**Files:**
- Create: `luna/skills.py`
- Test: `tests/test_skills.py`

**Interfaces:**
- Consumes: `luna.config.config_dir`, `luna.registry.resolve_skill`, `luna.providers.LunaConfigError`.
- Produces:
  - `skills_dirs(workdir=".", *, env=None) -> list[Path]` — `[user skills/, project .luna/skills/]`
  - `existing_skill_dirs(workdir=".", *, env=None) -> list[Path]` — only those that exist
  - `install(source: str, *, name: str | None = None, project=False, workdir=".", env=None, run=subprocess.run) -> str`
  - `remove(name: str, *, project=False, workdir=".", env=None) -> bool`
  - `list_skills(workdir=".", *, env=None) -> list[tuple[str, str, str]]` — `(scope, name, description)`

- [ ] **Step 1: failing test** — `tests/test_skills.py`

```python
import subprocess

import pytest

from luna.providers import LunaConfigError
from luna.skills import install, list_skills, remove, skills_dirs


def _fake_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "greet").mkdir(parents=True)
    (repo / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: say hi nicely\n---\n\nSay hi.\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=repo, check=True,
    )
    return repo


def test_install_from_local_repo_subdir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    repo = _fake_repo(tmp_path)
    msg = install(f"{repo}/greet", workdir=str(tmp_path))
    dest = tmp_path / ".config" / "luna" / "skills" / "greet" / "SKILL.md"
    assert dest.is_file()
    assert "/reload" in msg
    assert ("user", "greet", "say hi nicely") in list_skills(str(tmp_path))


def test_install_rejects_skill_without_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    repo = tmp_path / "bad"
    (repo / "x").mkdir(parents=True)
    (repo / "x" / "SKILL.md").write_text("no frontmatter here")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x"], cwd=repo, check=True)
    with pytest.raises(LunaConfigError):
        install(f"{repo}/x", workdir=str(tmp_path))
    assert not (tmp_path / ".config" / "luna" / "skills" / "x").exists()


def test_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    repo = _fake_repo(tmp_path)
    install(f"{repo}/greet", workdir=str(tmp_path))
    assert remove("greet", workdir=str(tmp_path)) is True
    assert remove("greet", workdir=str(tmp_path)) is False
```

- [ ] **Step 2:** run → FAIL.

- [ ] **Step 3: implement `luna/skills.py`**

- `skills_dirs`: `[config_dir(env)/"skills", Path(workdir)/".luna"/"skills"]`.
- `_parse_source(source)`:
  - contains `://` or starts with `/` or `~` → treat as a git URL / local path; if it points at `.../<sub>` where a `SKILL.md` lives directly, `path` = that subdir, `repo` = the part git can clone. Simple rule: if `source` is an existing local dir, `repo = source`, `path = None`; else split on the last segment only when it isn't the repo name — instead: accept `install()` callers passing `"<localpath>/<subdir>"`; detect by walking up to the first dir containing `.git`.
  - `owner/repo` (one slash, no scheme) → `repo = f"https://github.com/{owner}/{repo}.git"`, `path = None`
  - `owner/repo/sub/dir` → `repo = https://github.com/owner/repo.git`, `path = "sub/dir"`
  - otherwise → `resolve_skill(source)` from the registry → `{repo, path}` (repo shorthand expanded)
- `install`:
  1. resolve `repo`, `path`, `name` (default = last path segment or repo name)
  2. `tmp = mkdtemp()`; `run(["git", "clone", "--depth", "1", repo, tmp], check=True, capture_output=True)` — on `FileNotFoundError` → `LunaConfigError("git is required")`; on `CalledProcessError` → `LunaConfigError(stderr)`
  3. `src = Path(tmp) / (path or "")`; require `src / "SKILL.md"` exists
  4. parse frontmatter: first line `---`, collect to next `---`, `yaml`-free minimal parse (`key: value`), require `name` and `description`
  5. `dest = skills_dirs(...)[1 if project else 0] / name`; `shutil.rmtree(dest, ignore_errors=True)`; `shutil.copytree(src, dest)`
  6. cleanup tmp; return `f"installed skill '{name}'. Run /reload to activate."`
- `list_skills`: for each scope dir, for each child with `SKILL.md`, read `description` from frontmatter.
- `remove`: `rmtree` the `<scope>/skills/<name>` dir; return whether it existed.
- Do **not** import `yaml`; write a 10-line `_frontmatter(text) -> dict`.

- [ ] **Step 4:** run tests → PASS (needs `git`; it is available in CI).
- [ ] **Step 5: commit** `feat: install/list/remove Anthropic-style skills`

---

### Task 4: Subagents (`luna/subagents.py`)

**Files:**
- Create: `luna/subagents.py`
- Test: `tests/test_subagents.py`

**Interfaces:**
- Consumes: `deepagents.SubAgent`, `luna.config.config_dir`, `luna.providers.LunaConfigError`.
- Produces:
  - `BUILTIN_SUBAGENTS: list[SubAgent]`
  - `VALID_TOOLS: frozenset[str]` (the built-in fs/planning tool names)
  - `load_subagents(workdir=".", *, env=None) -> list[SubAgent]`
  - `subagent_summaries(...) -> list[tuple[str, str]]` — `(name, description)`

- [ ] **Step 1: failing test** — `tests/test_subagents.py`

```python
import pytest

from luna.providers import LunaConfigError
from luna.subagents import BUILTIN_SUBAGENTS, load_subagents, subagent_summaries


def test_builtins_present():
    names = {s["name"] if isinstance(s, dict) else s.name for s in BUILTIN_SUBAGENTS}
    assert {"researcher", "reviewer"} <= names


def test_user_subagent_parsed(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "subagents.toml").write_text(
        '[subagent.docs]\ndescription = "write docs"\nprompt = "You write docs."\n'
        'tools = ["read_file", "write_file"]\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    names = [n for n, _ in subagent_summaries(str(tmp_path))]
    assert "docs" in names


def test_bad_tool_name_rejected(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "luna"
    cfg.mkdir(parents=True)
    (cfg / "subagents.toml").write_text(
        '[subagent.x]\ndescription = "d"\ntools = ["frobnicate"]\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    with pytest.raises(LunaConfigError):
        load_subagents(str(tmp_path))
```

- [ ] **Step 2:** run → FAIL.

- [ ] **Step 3: implement `luna/subagents.py`**

```python
"""Built-in and user-defined subagents for delegation via the `task` tool."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping

from deepagents import SubAgent

from luna.config import config_dir
from luna.providers import LunaConfigError

VALID_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep",
     "execute", "write_todos"}
)

BUILTIN_SUBAGENTS: list[SubAgent] = [
    SubAgent(
        name="researcher",
        description="Investigate the codebase, docs, or a question and report back. Read-only.",
        system_prompt="You explore and report. You never modify files or run mutating commands.",
        tools=["ls", "read_file", "glob", "grep"],
    ),
    SubAgent(
        name="reviewer",
        description="Review a diff or file for bugs, risks, and simplifications. Read-only.",
        system_prompt="You review code critically and return concrete, prioritized findings.",
        tools=["ls", "read_file", "glob", "grep"],
    ),
]


def _config_files(workdir: str, env: Mapping[str, str] | None):
    from pathlib import Path

    return [config_dir(env) / "subagents.toml", Path(workdir) / ".luna" / "subagents.toml"]


def _load_raw(workdir: str, env: Mapping[str, str] | None) -> dict:
    merged: dict = {}
    for path in _config_files(workdir, env):
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise LunaConfigError(f"Cannot read {path}: {exc}") from exc
        merged.update(data.get("subagent", {}))
    return merged


def load_subagents(workdir: str = ".", *, env: Mapping[str, str] | None = None) -> list[SubAgent]:
    agents = list(BUILTIN_SUBAGENTS)
    for name, cfg in _load_raw(workdir, env).items():
        tools = cfg.get("tools")
        if tools is not None:
            bad = set(tools) - VALID_TOOLS
            if bad:
                raise LunaConfigError(
                    f"subagent {name!r}: unknown tools {sorted(bad)}. "
                    f"Valid: {', '.join(sorted(VALID_TOOLS))}"
                )
        spec: dict = {"name": name, "description": cfg.get("description", name)}
        if cfg.get("prompt"):
            spec["system_prompt"] = cfg["prompt"]
        if tools is not None:
            spec["tools"] = list(tools)
        if cfg.get("model"):
            spec["model"] = cfg["model"]
        agents = [a for a in agents if _name(a) != name] + [SubAgent(**spec)]
    return agents


def _name(a) -> str:
    return a["name"] if isinstance(a, dict) else a.name


def subagent_summaries(workdir: str = ".", *, env: Mapping[str, str] | None = None):
    out = []
    for a in load_subagents(workdir, env=env):
        out.append((_name(a), a["description"] if isinstance(a, dict) else a.description))
    return out
```

(If `SubAgent` is a `TypedDict` rather than a class, `_name`/attr access via the
`isinstance(a, dict)` branch already covers it; adjust the builtins to dict
literals if `SubAgent(**spec)` fails at import — verify in Step 3.)

- [ ] **Step 4:** run tests → PASS.
- [ ] **Step 5: commit** `feat: built-in and user-defined subagents`

---

### Task 5: Agent-facing extension tools (`luna/extension_tools.py`)

**Files:**
- Create: `luna/extension_tools.py`
- Test: `tests/test_extension_tools.py`

**Interfaces:**
- Consumes: `luna.mcp`, `luna.skills`, `luna.registry`.
- Produces:
  - `manage_mcp` and `manage_skills` — `langchain_core.tools` `BaseTool`s (via `@tool`)
  - `EXTENSION_TOOLS: list` = `[manage_mcp, manage_skills]`
  - `EXTENSION_INTERRUPTS: dict` = `{"manage_mcp": True, "manage_skills": True}`

- [ ] **Step 1: failing test** — `tests/test_extension_tools.py`

```python
from luna.extension_tools import EXTENSION_INTERRUPTS, manage_mcp, manage_skills
from luna.mcp import load_mcp_config


def test_manage_mcp_add_from_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    out = manage_mcp.invoke({"action": "add", "name": "filesystem"})
    assert "/reload" in out
    assert "filesystem" in load_mcp_config(str(tmp_path))


def test_manage_mcp_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    assert "filesystem" in manage_mcp.invoke({"action": "list"})


def test_interrupts_registered():
    assert EXTENSION_INTERRUPTS == {"manage_mcp": True, "manage_skills": True}
```

- [ ] **Step 2:** run → FAIL.

- [ ] **Step 3: implement `luna/extension_tools.py`**

```python
"""Tools the Luna agent can call to add MCP servers or skills (approval-gated)."""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import tool

from luna import mcp, skills
from luna.providers import LunaConfigError
from luna.registry import known_mcp, known_skills, resolve_mcp


@tool
def manage_mcp(
    action: Literal["add", "remove", "list"],
    name: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
) -> str:
    """Add, remove, or list MCP servers for Luna.

    add: resolve `name` from the registry, or pass an explicit `command`/`args`.
    Newly added servers activate after the user runs /reload.
    """
    if action == "list":
        cfg = ", ".join(mcp.load_mcp_config()) or "(none configured)"
        return f"configured: {cfg}\nknown: {', '.join(known_mcp())}"
    if not name:
        return "name is required for add/remove"
    if action == "remove":
        return f"removed {name!r}" if mcp.remove_server(name) else f"{name!r} was not configured"
    try:
        spec = {"command": command, "args": args or []} if command else resolve_mcp(name)
    except LunaConfigError as exc:
        return str(exc)
    mcp.add_server(name, spec)
    return f"added MCP server {name!r}. Run /reload to activate."


@tool
def manage_skills(
    action: Literal["add", "remove", "list"],
    name: str | None = None,
    source: str | None = None,
) -> str:
    """Add, remove, or list Luna skills.

    add: `source` is a registry name, `owner/repo`, or `owner/repo/subdir`.
    Newly added skills activate after the user runs /reload.
    """
    if action == "list":
        rows = [f"{s} ({scope})" for scope, s, _ in skills.list_skills()]
        return "installed: " + (", ".join(rows) or "(none)") + \
            f"\nknown: {', '.join(known_skills())}"
    if action == "remove":
        return f"removed {name!r}" if name and skills.remove(name) else f"{name!r} not installed"
    try:
        return skills.install(source or name, name=name if source else None)
    except LunaConfigError as exc:
        return str(exc)


EXTENSION_TOOLS = [manage_mcp, manage_skills]
EXTENSION_INTERRUPTS = {"manage_mcp": True, "manage_skills": True}
```

- [ ] **Step 4:** run tests → PASS.
- [ ] **Step 5: commit** `feat: agent tools to manage MCP servers and skills`

---

### Task 6: Wire everything into `build_agent`

**Files:**
- Modify: `luna/agent.py`
- Modify: `luna/prompts.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- `build_agent(config, *, model=None, checkpointer=None, on_warn=print)` unchanged
  signature except `on_warn`. Still returns a compiled graph.
- Produces: `luna.agent.describe_capabilities(config) -> dict` with keys
  `tools` (int), `mcp` (list[str]), `skills` (list[str]), `subagents` (list[str])
  — used by `/reload` output.

- [ ] **Step 1: extend `tests/test_agent.py`**

```python
def test_build_agent_wires_extensions(tmp_path, fake_model, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    from luna.config import LunaConfig
    from luna.agent import build_agent, describe_capabilities
    cfg = LunaConfig(workdir=str(tmp_path))
    agent = build_agent(cfg, model=fake_model())
    agent.invoke({"messages": [{"role": "user", "content": "hi"}]},
                 config={"configurable": {"thread_id": "x"}})
    caps = describe_capabilities(cfg)
    assert "researcher" in caps["subagents"]
    assert caps["tools"] >= 2  # manage_mcp + manage_skills at least


def test_yolo_still_has_no_interrupts(tmp_path, fake_model, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    from luna.config import LunaConfig
    from luna.agent import build_agent
    build_agent(LunaConfig(workdir=str(tmp_path), yolo=True), model=fake_model())
```

- [ ] **Step 2:** run → FAIL (`describe_capabilities` missing / extensions not wired).

- [ ] **Step 3: implement**

`luna/agent.py`:

```python
from luna import mcp as mcp_mod
from luna import skills as skills_mod
from luna import subagents as subagents_mod
from luna.extension_tools import EXTENSION_INTERRUPTS, EXTENSION_TOOLS


def _extension_bits(config, on_warn):
    skill_dirs = [str(d) for d in skills_mod.existing_skill_dirs(config.workdir)]
    servers = mcp_mod.load_mcp_config(config.workdir)
    mcp_tools = mcp_mod.load_mcp_tools(mcp_mod.to_connections(servers), on_warn=on_warn) if servers else []
    subs = subagents_mod.load_subagents(config.workdir)
    return skill_dirs, list(servers), mcp_tools, subs


def build_agent(config, *, model=None, checkpointer=None, on_warn=print):
    workdir = Path(config.workdir).resolve()
    backend = LocalShellBackend(root_dir=str(workdir), virtual_mode=True, inherit_env=True)
    memory = ["AGENTS.md"] if (workdir / "AGENTS.md").is_file() else None
    skill_dirs, _servers, mcp_tools, subs = _extension_bits(config, on_warn)
    interrupt_on = None if config.yolo else {**INTERRUPT_TOOLS, **EXTENSION_INTERRUPTS}
    return create_deep_agent(
        model=model or build_model(config.provider, config.model, config.model_kwargs),
        system_prompt=LUNA_SYSTEM_PROMPT,
        backend=backend,
        memory=memory,
        tools=[*EXTENSION_TOOLS, *mcp_tools],
        skills=skill_dirs or None,
        subagents=subs or None,
        interrupt_on=interrupt_on,
        checkpointer=checkpointer or InMemorySaver(),
        name="luna",
    )


def describe_capabilities(config) -> dict:
    skill_dirs, servers, mcp_tools, subs = _extension_bits(config, lambda *_: None)
    return {
        "tools": len(EXTENSION_TOOLS) + len(mcp_tools),
        "mcp": servers,
        "skills": [d.split("/")[-2] if d.endswith("/") else "" for d in []] or _skill_names(config),
        "subagents": [s["name"] if isinstance(s, dict) else s.name for s in subs],
    }
```

Add `_skill_names(config)` = `[name for _, name, _ in skills_mod.list_skills(config.workdir)]`.
Adjust `describe_capabilities` to use it directly (drop the messy comprehension).

`luna/prompts.py` — append:

```
You can delegate a large read-only investigation to a subagent with the `task`
tool (e.g. the `researcher` subagent). If the user asks you to add a skill or an
MCP server, use `manage_skills` / `manage_mcp`; tell them to run /reload to
activate what you added.
```

- [ ] **Step 4:** run `uv run pytest -q` → PASS.
- [ ] **Step 5: commit** `feat: assemble skills, MCP tools, and subagents in build_agent`

---

### Task 7: REPL turn framing (`luna/ui/turn.py` + `luna/session.py`)

**Files:**
- Create: `luna/ui/turn.py`
- Modify: `luna/session.py`
- Test: `tests/test_turn_framing.py`

**Interfaces:**
- Produces:
  - `open_turn(console) -> None` — blank line + left rule titled `● luna`
  - `close_turn(console) -> None` — dim rule + blank line
  - `tool_line(console, name: str, summary: str) -> None` — `  ⚙ name · summary`

- [ ] **Step 1: failing test** — `tests/test_turn_framing.py`

```python
import io

from rich.console import Console

from luna.ui.turn import close_turn, open_turn, tool_line


def _c():
    return Console(file=io.StringIO(), force_terminal=True, no_color=True, width=80)


def test_open_and_close_emit_markers():
    c = _c()
    open_turn(c)
    tool_line(c, "read_file", "pyproject.toml")
    close_turn(c)
    out = c.file.getvalue()
    assert "luna" in out
    assert "read_file" in out and "pyproject.toml" in out
    assert "⚙" in out
```

- [ ] **Step 2:** run → FAIL.

- [ ] **Step 3: implement `luna/ui/turn.py`**

```python
"""Visual framing for a REPL turn: a rule opens Luna's reply, a rule closes it."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from luna.ui.theme import PALETTE


def open_turn(console: Console) -> None:
    console.print()
    console.rule(Text("● luna", style=f"bold {PALETTE['peri']}"), align="left",
                 style=PALETTE["blue"])


def tool_line(console: Console, name: str, summary: str) -> None:
    text = Text("  ⚙ ", style=PALETTE["blue"])
    text.append(name, style=f"bold {PALETTE['accent']}")
    if summary:
        text.append(f" · {summary}", style=PALETTE["blue"])
    console.print(text)


def close_turn(console: Console) -> None:
    console.rule(style=PALETTE["blue"])
    console.print()
```

- [ ] **Step 4:** wire into `luna/session.py`:
  - In `_stream_turn`: call `open_turn(console)` before the stream loop, stream
    assistant text as now, replace the raw `_report_tools` dim print with
    `tool_line(...)`, and call `close_turn(console)` at the end (replacing the
    two bare `console.print()` calls).
  - `run_repl`: after reading a non-slash line, do **not** add extra framing for
    the user (their text is already visible after `luna ›`); `_stream_turn`'s
    `open_turn` gives the separation.
  - Keep `render_markdown` unused (already removed).

- [ ] **Step 5:** run `uv run pytest -q` → PASS (existing `test_session.py` still green — `run_once` returns the same text; framing only adds lines).
- [ ] **Step 6: commit** `feat: frame REPL turns so user and Luna are visually distinct`

---

### Task 8: `/reload` in-session rebuild (`luna/session.py`)

**Files:**
- Modify: `luna/session.py`
- Test: `tests/test_session_reload.py`

**Interfaces:**
- `run_repl(agent, *, console, input_fn=input, rebuild=None)` — new optional
  `rebuild: Callable[[], CompiledGraph]`; when present, `/reload` calls it.
- Produces: `SLASH_COMMANDS` gains `"/reload"`; `_reload(state, console) -> None`.

- [ ] **Step 1: failing test** — `tests/test_session_reload.py`

```python
import io

from langchain_core.messages import AIMessage
from rich.console import Console

from luna.session import run_repl


def test_reload_swaps_agent(fake_model):
    built = []

    class FakeAgent:
        def __init__(self, tag):
            self.tag = tag

    def rebuild():
        a = FakeAgent(len(built))
        built.append(a)
        return a

    answers = iter(["/reload", "/exit"])
    out = io.StringIO()
    code = run_repl(
        rebuild()  # initial
        , console=Console(file=out, force_terminal=True, no_color=True),
        input_fn=lambda _: next(answers),
        rebuild=rebuild,
    )
    assert code == 0
    assert len(built) == 2  # initial + one reload
    assert "reload" in out.getvalue().lower()
```

- [ ] **Step 2:** run → FAIL.

- [ ] **Step 3: implement**

- `SLASH_COMMANDS["/reload"] = "rebuild the agent with the current config (skills, MCP, subagents)"`
- In `run_repl`, keep `agent` in a local variable; handle:

```python
if line == "/reload":
    if rebuild is None:
        console.print(f"[{PALETTE['mauve']}]/reload is not available here[/]")
        continue
    agent = rebuild()
    console.print(f"[{PALETTE['blue']}]reloaded — capabilities refreshed[/]")
    continue
```

- `luna/cli.py` `main`: build a `rebuild` closure and pass it:

```python
def _rebuild() -> object:
    return build_agent(config, on_warn=lambda m: console.print(f"[yellow]{m}[/]"))

agent = _rebuild()
...
return run_repl(agent, console=console, rebuild=_rebuild)
```

- Auto-reload: in `_stream_turn`, after the turn, if any tool message came from
  `manage_mcp`/`manage_skills` and its content contains `"Run /reload"`, return a
  sentinel (e.g. `_stream_turn` returns `(text, reload_requested: bool)` — update
  its two call sites) so `run_repl` can call `rebuild()` and print
  `"auto-reloaded"`. `run_once` ignores the flag.

- [ ] **Step 4:** run `uv run pytest -q` → PASS.
- [ ] **Step 5: commit** `feat: /reload rebuilds the agent in-session, keeping the thread`

---

### Task 9: CLI subcommands + packaging

**Files:**
- Modify: `luna/cli.py`, `pyproject.toml`, `README.md`, `CHANGELOG.md`, `AGENTS.md`, `.env.example`
- Test: extend `tests/test_cli.py`

**Interfaces:**
- `_SUBCOMMANDS = {"setup", "config", "mcp", "skills", "agents"}`
- `_run_mcp(argv) -> int`, `_run_skills(argv) -> int`, `_run_agents(argv) -> int`

- [ ] **Step 1: extend `tests/test_cli.py`**

```python
def test_mcp_add_from_registry_and_list(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    assert main(["mcp", "add", "filesystem"]) == 0
    assert main(["mcp", "list"]) == 0
    assert "filesystem" in capsys.readouterr().out


def test_mcp_add_explicit_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    assert main(["mcp", "add", "custom", "--", "node", "srv.js"]) == 0
    from luna.mcp import load_mcp_config
    assert load_mcp_config(".")["custom"]["command"] == "node"


def test_agents_list_shows_builtins(capsys):
    assert main(["agents", "list"]) == 0
    assert "researcher" in capsys.readouterr().out


def test_skills_list_empty_ok(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.chdir(tmp_path)
    assert main(["skills", "list"]) == 0
```

- [ ] **Step 2:** run → FAIL.

- [ ] **Step 3: implement**

`luna/cli.py`:
- `_SUBCOMMANDS` updated; `main` routes `mcp`/`skills`/`agents` like `config`.
- `_run_mcp`:
  - `list` → print configured (`load_mcp_config`) + known (`known_mcp`)
  - `add <name> [-- CMD ARGS...] [--json JSON] [--project]`:
    - if `--`/`argv` remainder → `spec = {"command": rest[0], "args": rest[1:]}`
    - elif `--json` → `spec = json.loads(...)`
    - else `spec = resolve_mcp(name)`
    - `mcp.add_server(name, spec, project=..., workdir=".")` → print path
  - `remove <name> [--project]` → `mcp.remove_server`
  - `test <name>` → `load_mcp_tools(to_connections({name: resolve/config}), on_warn=print)`; print tool names or the warning; return 0/1
  - parse `--` remainder manually (argparse `REMAINDER` on a positional) or split `argv` on `"--"` before `parse_args`.
- `_run_skills`: `list` / `add <src> [--name N] [--project]` / `remove <name> [--project]` → call `luna.skills`.
- `_run_agents`: `list` → `subagent_summaries()` printed as `name — description`.
- On `LunaConfigError` → stderr + return 2 (same pattern as `_run_config`).

`pyproject.toml`:
- `[project.optional-dependencies]` add `mcp = ["langchain-mcp-adapters>=0.3"]`
- add `"langchain-mcp-adapters>=0.3"` to `all`

`README.md` — new "Extending Luna" section: `luna mcp add github`, `luna skills
add pdf`, `luna agents list`, the `mcp.json` shape, `/reload`, and that the agent
can add these itself with your approval.

`CHANGELOG.md` — `[0.1.0]` gains skills / MCP / subagents / `/reload` / turn framing.

`AGENTS.md` — list the new modules.

`.env.example` — note `GITHUB_TOKEN` for the `github` MCP server.

- [ ] **Step 4:** `uv pip install -e ".[dev,all]"`; `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` → all PASS.
- [ ] **Step 5:** manual smoke (documented):

```bash
uv run luna mcp add filesystem
uv run luna mcp list
uv run luna agents list
uv run luna skills list
uv run luna --no-input        # REPL: /tools, /agents, /reload, /exit
```

- [ ] **Step 6: commit** `feat: luna mcp / skills / agents subcommands; mcp extra`

---

## Self-Review

**Spec coverage:** §3 config layout → T2/T3/T4; §4 registry → T1; §5.1 mcp → T2;
§5.2 skills → T3; §5.3 subagents → T4; §5.4 extension_tools → T5; §5.5 agent
wiring → T6; §5.6 /reload + framing → T7 (framing) + T8 (/reload); §6 CLI → T9;
§7 deps → T9; §8 testing → each task's tests. All covered.

**Placeholder scan:** no "TBD"/"handle errors"; all test bodies inline. Task 4
flags a real uncertainty (is `SubAgent` a class or TypedDict) with a concrete
fallback to verify in-step — acceptable, not a placeholder.

**Type consistency:** `build_agent(config, *, model=None, checkpointer=None,
on_warn=print)` consistent T6/T8/T9. `load_mcp_config(workdir=".", *, env=None)`,
`skills.install(source, *, name=None, project=False, workdir=".", env=None)`,
`load_subagents(workdir=".", *, env=None)` consistent across tasks.
`_stream_turn` return type changes in T8 (adds `reload_requested`) — both call
sites (`run_once`, `run_repl`) updated in that task.
