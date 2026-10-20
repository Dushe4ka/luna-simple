# Tools

A `Tool` is a name, a description, a JSON schema and a function. Build one from a typed
function with `@tool`:

```python
from luna_core import Agent, ToolContext, builtin_tools, tool


@tool(timeout=10)
def word_count(ctx: ToolContext, file_path: str) -> str:
    """Count the words in a text file.

    Args:
        file_path: Path of the file, relative to the project root.
    """
    path = ctx.workspace.resolve(file_path)   # a path outside the project is a tool error
    return f"{file_path}: {len(path.read_text('utf-8').split())} words"


agent = Agent(provider, model, tools=[*builtin_tools(), word_count])
```

- The first parameter named `ctx` receives a `ToolContext` and is hidden from the schema.
- The docstring summary is the description; the `Args:` section documents parameters.
- Supported annotations: `str`, `int`, `float`, `bool`, `dict`, `list[...]`,
  `Literal[...]`, `X | None` / `Optional`. Others are rejected when the tool is defined.
- Return a `str`, a `ToolOutput` (`ToolOutput.text(text, detail)` / `ToolOutput.error(text)`),
  or raise `ToolError`; any exception becomes an error result for the model.
- `@tool(mutating=True)` marks a tool that changes state. It does **not** by itself ask for approval: plan mode
  refuses it, calls with the same `file_path`/`path` are serialised, and undo treats it as a
  command that may have changed any file. To require approval add its name to
  `Policy(approval_tools=...)` or an `ask` rule.
- For dynamic tools (MCP), build `Tool(name, description, parameters, fn, raw=True)` where
  `fn(ctx, args) -> ToolOutput`.

`ToolContext` carries `workspace`, `sandbox`, `session_id`, `call_id`, `emit(event)`
(push a `Notice` or other event into the turn), `state`, `cancel` (an `asyncio.Event`) and
`services` (objects the app injects, for example `"agent"`, `"session"`, `"background"`).

## Built-in tools

`builtin_tools()` returns `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`,
`grep`, `execute` and `write_todos`. `task`, `task_status` and `task_cancel` come with
[subagents](subagents-and-background.md). The schemas of the built-ins fit a budget of
2 000 tokens (checked by `test_builtin_schemas_fit_the_token_budget`).

| Tool | Parameters |
|---|---|
| `ls` | `path` |
| `read_file` | `file_path`, `offset`, `limit` |
| `write_file` | `file_path`, `content` |
| `edit_file` | `file_path`, `old_string`, `new_string`, `replace_all` |
| `delete` | `file_path` |
| `glob` | `pattern`, `path` |
| `grep` | `pattern`, `path`, `glob`, `output_mode`, `max_count` |
| `execute` | `command`, `timeout` |
| `write_todos` | `todos` |

## Workspace and paths

`Workspace(root)` resolves agent paths: relative paths are project-relative, a leading
`/` means the project root, and anything that resolves outside the root (including via
`..` or symlinks) raises `OutsideWorkspace`, a `ToolError`. It is never silently remapped.
`workspace.rel(path)` gives the project-relative POSIX form.

## Sandbox

`execute` runs through `Agent(sandbox=...)`:

```python
from luna_core import DockerSandbox, LocalSandbox

agent = Agent(provider, model, sandbox=DockerSandbox(".", image="python:3.12-slim", network="none"))
```

`LocalSandbox` runs on the host (default). `DockerSandbox` starts one container per session
(`await sandbox.start()`, done lazily by the first command) with the project mounted at
`/workspace`; `network` is `"none"` or `"bridge"`. Call `await agent.sandbox.close()` when
done so the container is removed. A custom sandbox implements
`async run(command, *, cwd, timeout, env, cancel)` and `async close()`.

## Pitfalls

- Large results are written to `.luna/tool-output/<id>.txt` and replaced by an excerpt.
- Independent calls in one assistant message run concurrently; keep tools thread-safe or
  mutate only through paths so per-path locking applies.
- Tool timeouts end the call with an error result; they do not raise.
