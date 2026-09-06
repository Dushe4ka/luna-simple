# Luna extensions: skills, MCP, subagents, REPL turn framing — Design

**Date:** 2026-09-06
**Status:** Approved (design), pending implementation plan
**Builds on:** `2026-09-06-luna-cli-agent-design.md`

## 1. Goal

Let a running Luna agent gain capabilities on request:

1. **Skills** — install Anthropic Agent Skills by name / GitHub shorthand.
2. **MCP servers** — add MCP servers by name (curated registry) or explicit
   spec, using the standard `mcpServers` JSON so configs copy-paste from
   Claude Desktop / Claude Code.
3. **Subagents** — built-in and user-defined named subagents the main agent
   delegates to via the existing `task` tool.
4. Activation without restart via `/reload`.
5. Clear visual separation between the user's turn and Luna's turn in the REPL.

Both the agent (with approval) and the user (CLI) can add skills / MCP.

## 2. Verified facts

- `langchain-mcp-adapters` 0.3.x: `MultiServerMCPClient(connections={...})`,
  `await client.get_tools()` (async only). Connection dicts use
  `transport: "stdio"|"sse"|"streamable_http"`, `command`, `args`, `env`,
  `url`, `headers`.
- Claude Desktop / Claude Code `mcp.json` entries use `command`/`args`/`env`
  (no `transport` key) for stdio, and `type: "http"|"sse"` + `url` for remote.
  Luna's loader translates that shape into langchain connection dicts and does
  `${ENV}` substitution.
- `create_deep_agent(skills=[<dir>, ...], subagents=[SubAgent(...)], tools=[...])`.
  `skills` entries are directories that contain `<name>/SKILL.md`.
- deep agents already expose a `task` tool + a general-purpose subagent;
  `subagents=` adds named ones. `SubAgent(name, description, system_prompt=,
  mode="isolated"|"fork", tools=, model=)`.

## 3. Config layout

Everything under the Luna config dir (`config_dir()` from the base design),
plus a project override dir `./.luna/` (project wins).

```
~/.config/luna/
├── config.toml          # existing
├── credentials.toml     # existing
├── mcp.json             # { "mcpServers": { "<name>": { ... } } }
├── registry.toml        # user aliases, merged over the built-in registry
├── subagents.toml       # [subagent.<name>] description / prompt / tools / model
└── skills/
    └── <name>/SKILL.md  # + supporting files
./.luna/                 # same names; project scope, higher precedence
```

### 3.1 `mcp.json` (Claude-compatible)

```json
{
  "mcpServers": {
    "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] },
    "github":     { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": { "GITHUB_TOKEN": "${GITHUB_TOKEN}" } },
    "docs":       { "type": "http", "url": "https://example.com/mcp" }
  }
}
```

`load_mcp_connections()` → merges user + project files, substitutes `${ENV}`,
translates to langchain connection dicts:
- has `command` → `{"transport": "stdio", "command", "args", "env"}`
- `type == "http"` → `{"transport": "streamable_http", "url", "headers"}`
- `type == "sse"` → `{"transport": "sse", "url", "headers"}`

### 3.2 `subagents.toml`

```toml
[subagent.researcher]
description = "Read-only exploration of the codebase and docs."
prompt = "You investigate and report. Never modify files."
tools = ["ls", "read_file", "glob", "grep"]
# model = "anthropic:claude-3-5-haiku-latest"   # optional
```

## 4. Built-in registry (`luna/registry.py`)

```python
MCP_REGISTRY: dict[str, dict] = {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
    "github":     {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
    "git":        {"command": "uvx", "args": ["mcp-server-git"]},
    "fetch":      {"command": "uvx", "args": ["mcp-server-fetch"]},
    "sequential-thinking": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"]},
    "memory":     {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
    "time":       {"command": "uvx", "args": ["mcp-server-time"]},
    "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
}
SKILL_REGISTRY: dict[str, dict] = {
    # name -> {"repo": "owner/repo", "path": "subdir"}  (path optional)
    "pdf": {"repo": "anthropics/skills", "path": "document-skills/pdf"},
    "docx": {"repo": "anthropics/skills", "path": "document-skills/docx"},
    "xlsx": {"repo": "anthropics/skills", "path": "document-skills/xlsx"},
    "pptx": {"repo": "anthropics/skills", "path": "document-skills/pptx"},
}
```

`~/.config/luna/registry.toml` (`[mcp.<name>]`, `[skills.<name>]`) merges on top.

Resolution for `add <name>`:
1. explicit spec on the command line wins (`-- cmd args`, `--json`, `owner/repo[/path]`)
2. else look up `<name>` in the merged registry
3. else error listing available names

## 5. New modules

```
luna/
├── registry.py       # MCP_REGISTRY, SKILL_REGISTRY, load_user_registry(), resolve_*()
├── mcp.py            # load_mcp_config / save entry / remove / list;
│                      #   load_mcp_tools(connections) -> list[BaseTool] (async run at build)
├── skills.py         # skills_dirs(); install(name_or_source); remove(name); list_skills()
├── subagents.py      # load_subagents() -> list[SubAgent]; BUILTIN_SUBAGENTS
├── extension_tools.py # manage_mcp / manage_skills LangChain tools for the agent
└── ui/turn.py        # REPL turn framing helpers (rules, gutters, tool lines)
```

### 5.1 `luna/mcp.py`

- `MCP_FILES = [user mcp.json, project mcp.json]`
- `load_mcp_config() -> dict[str, dict]` — merged `mcpServers`, `${ENV}` expanded.
- `to_connections(cfg) -> dict[str, dict]` — translate to langchain shape.
- `add_server(name, spec, *, project=False)` / `remove_server(name, *, project=False)`
- `load_mcp_tools(connections) -> list[BaseTool]` — builds `MultiServerMCPClient`,
  runs `asyncio.run(client.get_tools())`, returns `[]` (with a printed warning)
  when `langchain_mcp_adapters` is missing or a server fails to start.
- Tool names are prefixed `mcp__<server>__<tool>` (via `tool_name_prefix=True`).

### 5.2 `luna/skills.py`

- `SKILL_DIRS = [user skills/, project skills/]` — only those that exist are
  passed to `create_deep_agent(skills=...)`.
- `install(source, *, name=None, project=False)`:
  - `source` is a registry name, `owner/repo`, or `owner/repo/subdir`
  - `git clone --depth 1` into a temp dir, copy the skill subtree to
    `skills/<name>/`, verify `SKILL.md` has YAML frontmatter with `name` +
    `description`, else abort and clean up.
- `remove(name, *, project=False)`; `list_skills() -> list[(scope, name, description)]`.

### 5.3 `luna/subagents.py`

```python
BUILTIN_SUBAGENTS = [
    SubAgent(name="researcher",
             description="Investigate the codebase, docs, or a question and report back. Read-only.",
             system_prompt="You explore and report. You never modify files or run mutating commands.",
             tools=["ls", "read_file", "glob", "grep"]),
    SubAgent(name="reviewer",
             description="Review a diff or file for bugs, risks, and simplifications. Read-only.",
             system_prompt="You review code critically and return concrete, prioritized findings."),
]
def load_subagents() -> list[SubAgent]:  # builtins + subagents.toml
```

### 5.4 `luna/extension_tools.py`

Two `@tool` functions returning short status strings:

- `manage_mcp(action: Literal["add","remove","list"], name: str | None = None, command: str | None = None, args: list[str] | None = None) -> str`
- `manage_skills(action: Literal["add","remove","list"], name: str | None = None, source: str | None = None) -> str`

`add` writes config / clones, then returns
`"added '<name>'. Run /reload to activate."`. Both tools are added to
`INTERRUPT_TOOLS` so the user approves before a clone / config write / process
spawn.

### 5.5 `luna/agent.py` changes

`build_agent(config, *, model=None, checkpointer=None, extra_tools=None)`:

```python
skills = [d for d in skills.skills_dirs(config.workdir) if d.exists()]
mcp_tools = mcp.load_mcp_tools(mcp.to_connections(mcp.load_mcp_config(config.workdir)))
tools = [manage_mcp, manage_skills, *mcp_tools, *(extra_tools or [])]
return create_deep_agent(
    ...,
    tools=tools,
    skills=[str(d) for d in skills] or None,
    subagents=subagents.load_subagents() or None,
    interrupt_on=None if config.yolo else {**INTERRUPT_TOOLS,
        "manage_mcp": True, "manage_skills": True},
)
```

System prompt gains a short paragraph: delegate big read-only investigations to
the `researcher` subagent via `task`; skills and MCP tools may appear after
`/reload`.

### 5.6 `luna/session.py` — `/reload` + turn framing

- `run_repl` holds a `rebuild: Callable[[], CompiledGraph]` (closure over
  `config` + `console`). `/reload` calls it, swaps the `agent`, keeps
  `thread_id`. Prints what changed (tool count, skills, subagents).
- After a turn whose result mentions a successful `manage_*`, auto-run
  `/reload` and say so.
- **Turn framing** (`luna/ui/turn.py`):
  - `open_turn(console)` → blank line + `console.rule(Text("● luna"), style=peri,
    align="left")`
  - assistant text streamed under it (unchanged)
  - `tool_line(console, name, summary)` → `  ⚙ <name> · <summary>` in dim blue
  - `close_turn(console)` → thin `console.rule(style="dim blue")` + blank line
  - the user's input stays visible after the `luna ›` prompt; the rule +
    spacing give the separation.

## 6. CLI (`luna/cli.py`)

Add subcommands beside `setup` / `config`:

```
luna mcp     list | add <name> [-- CMD ARGS…] [--json JSON] [--project] | remove <name> [--project] | test <name>
luna skills  list | add <name|owner/repo[/path]> [--name NAME] [--project] | remove <name> [--project]
luna agents  list
```

`_SUBCOMMANDS = {"setup", "config", "mcp", "skills", "agents"}`.
`luna mcp test <name>` starts the server, lists its tools, exits.

## 7. Dependencies

`pyproject.toml`:
- new optional group `mcp = ["langchain-mcp-adapters>=0.3"]`
- add `"langchain-mcp-adapters>=0.3"` to `all`
- `luna mcp …` / MCP tool-loading degrade with a clear
  `pip install "luna-simple[mcp]"` hint when the import fails.

`git` is required for `luna skills add` from a repo — checked with a clear error.

## 8. Testing

| Test file | Covers |
| --- | --- |
| `test_registry.py` | builtin lookups; user `registry.toml` merge/override; unknown name error |
| `test_mcp_config.py` | `${ENV}` substitution; Claude→langchain shape translation (stdio/http/sse); add/remove roundtrip in user & project scope; missing-adapter degradation returns `[]` + hint |
| `test_skills.py` | `install()` from a local fake repo (file:// path) writes `skills/<name>/SKILL.md`; frontmatter validation rejects a bad skill; `remove`; scope precedence |
| `test_subagents.py` | builtins present; `subagents.toml` parsed into `SubAgent`; bad tool name rejected |
| `test_extension_tools.py` | `manage_mcp`/`manage_skills` add → config/dir changes + "Run /reload" message; are listed in `INTERRUPT_TOOLS` |
| `test_agent.py` (extend) | `build_agent` wires skills dirs, subagents, `manage_*` tools; still builds with no config |
| `test_session_reload.py` | `/reload` swaps the compiled agent, keeps `thread_id`; reports diff |
| `test_turn_framing.py` | `open_turn`/`close_turn`/`tool_line` render the expected markers |
| `test_cli.py` (extend) | `mcp`/`skills`/`agents` dispatch; `mcp add` from registry & explicit; `--project` writes `./.luna/` |

Network- and process-free: fake skill repos are local dirs; MCP tests use a
tiny stub `mcp` server script only in one opt-in test marked `slow`, otherwise
translation/config logic is tested directly.

## 9. Out of scope (YAGNI)

MCP OAuth flows, filesystem-watcher hot reload, plugin marketplaces, skill
version pinning/upgrade, sandboxing MCP subprocesses, running `npx`/`uvx`
without the approval prompt, async subagents.

## 10. Risks

- `/reload` rebuilding mid-session: mitigated by keeping the checkpointer and
  only swapping the graph; covered by `test_session_reload.py`.
- MCP servers need `npx`/`uvx` on PATH: `luna mcp test` and clear runtime
  warnings; failure to start one server must not break the others or the agent.
- Skill repos can be large: `git clone --depth 1` + copy only the named subtree.
- Registry drift (npm package names change): registry is small, in one file,
  and user-overridable via `registry.toml`.
