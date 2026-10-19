# luna-core acceptance results

Plan: `docs/superpowers/plans/2026-10-08-luna-core.md`, spec:
`docs/superpowers/specs/2026-10-08-luna-core-design.md`. Run on 2026-10-20 on macOS (Apple
Silicon laptop), Python 3.12, branch `feat/luna-core`.

Legend: **verified by test** (named test), **verified manually** (command and observed
output), **NOT VERIFIED** (reason). Nothing here is marked verified on the basis of a mock
unless it says so.

## 1. Measured targets

| Check | Target | Result |
|---|---|---|
| `luna --version` (median of 5) | < 150 ms | **verified manually**: 21 ms (runs: 23, 21, 21, 21, 21 ms; `.venv/bin/luna --version`, wall clock around `subprocess.run`). Guarded by `tests/test_startup.py::test_version_path_imports_nothing_heavy` |
| TUI first frame | < 0.4 s | **verified manually, with caveats**: 0.30 to 0.39 s (3 runs: 0.387, 0.305, 0.300 s) from interpreter start to the first rendered frame, measured headless with Textual's `run_test` and an in-process server. It includes importing `luna.tui` and the test server helpers, and excludes spawning the real `luna serve` process (`ensure_running`), which was not timed. A real-terminal first frame was not measured |
| Built-in tool schemas | <= 2 000 tokens | **verified**: built-ins only 835 tokens; all 18 tools of a default `build_agent` (built-ins, `task*`, `remember`, `skill`, `save_skill`, `manage_mcp`, `manage_skills`, `web_search`) 1 820 tokens, using the repository's estimator (characters / 4 over description plus schema JSON). Not measured with a provider tokenizer. The four optional LSP tools are not counted. Test: `test_builtin_schemas_fit_the_token_budget` (built-ins only) |
| `uv pip list` has no langchain/langgraph/deepagents | none | **verified manually**: `uv pip list \| grep -i -E "langchain\|langgraph\|deepagents\|litellm\|openai\|anthropic\|google-genai"` printed nothing (69 packages). Tests: `tests/test_no_frameworks.py::test_frameworks_are_not_installed`, `::test_no_framework_imports_in_the_repo`, `::test_importing_the_cli_loads_no_framework` |

## 2. Real-world runs

| Check | Target | Result |
|---|---|---|
| "Find and fix a bug", repo 1 (small Python CLI), DeepSeek | passes | **NOT VERIFIED**: blocked, `deepseek: HTTP 402: Insufficient Balance` (returned by `luna --no-splash -p "say ok" --output-format json`; the CLI reported it as a readable error instead of a traceback) |
| same, Anthropic | passes | **NOT VERIFIED**: blocked, no `ANTHROPIC_API_KEY` in the environment or in `~/.config/luna/credentials.toml` |
| repo 2 (TypeScript web app), DeepSeek / Anthropic | passes | **NOT VERIFIED**: blocked, same reasons (no funded key); no TypeScript repository was prepared |
| repo 3 (this repository, a seeded failing test), DeepSeek / Anthropic | passes | **NOT VERIFIED**: blocked, same reasons |
| Esc cancels a running `execute` | process killed, next turn works | **verified by test, in layers** (no single end-to-end test with a real TUI and a real shell): `tests/test_tui_background.py::test_escape_cancels_a_running_turn` (TUI sends cancel, shows "прервано"; uses a fake client), `packages/luna-core/tests/test_loop.py::test_cancel_kills_running_tool_and_patches_history`, `test_shell_todos.py::test_execute_honours_the_cancel_event`, `test_sandbox.py::test_local_timeout_kills_the_process_group` and `::test_local_cancel_event_stops_the_command` (process group killed), `tests/test_server_background.py::test_background_endpoint_attach_and_cancel`. Next-turn-works: `test_loop.py::test_dangling_calls_from_a_crash_are_patched` |
| TUI closed mid-turn, reopened | finished turn visible, notice shown | **verified by test**: `tests/test_server_turn_flow.py::test_client_disconnect_mid_turn_lets_the_turn_finish`, `tests/test_server_background.py::test_background_session_survives_client_disconnect`, `::test_a_watched_run_logs_no_completion_notice`, `tests/test_tui_chat.py::test_render_history_shows_a_completion_notice_as_a_notice_row`, core `test_background.py::test_background_session_survives_client_disconnect`. Not run with a real terminal |
| Two parallel `general-purpose` subagents editing one file | separate branches, no conflict | **verified by test**: `packages/luna-core/tests/test_subagents.py::test_worktrees_keep_parallel_writers_apart` (real git worktrees), `::test_parallel_children_run_concurrently`, `::test_shared_workspace_serialises_writes_to_one_path`. With a real model: NOT VERIFIED |
| ACP in Zed (or JetBrains) | prompt, tool calls, permission dialog work | **NOT VERIFIED in an editor** (none available). Protocol level verified by test and manually: `test_acp.py::test_acp_stdio_end_to_end` (initialize, session/new, prompt, permission request, tool call updates, `end_turn`) and `python examples/acp_agent.py --self-test` printed `protocol: 1`, `updates: usage_update, tool_call, tool_call_update, agent_message_chunk`, `stop reason: end_turn` |

## 3. Spec criteria (design spec, sections 2 to 4 and Testing)

Suite state behind these rows: 1002 passed, 8 skipped (7 live provider tests without keys, `test_docker_sandbox_real` without the image); the skipped tests are the ones marked NOT VERIFIED for live providers and a real Docker daemon.

| Criterion | Status |
|---|---|
| Own runtime, no deepagents / LangChain / LangGraph, no LiteLLM, no vendor SDKs | **verified by test**: `packages/luna-core/tests/test_package.py::test_core_never_imports_frameworks_or_vendor_sdks`, `tests/test_no_frameworks.py::*`; core dependencies are `httpx`, `httpx-sse`, `pyyaml` (`packages/luna-core/pyproject.toml`) |
| Five provider adapters: parse text, tool calls, parallel calls, reasoning, usage, errors, retries (recorded fixtures) | **verified by test** against recorded fixtures: `test_openai_chat.py`, `test_openai_responses.py`, `test_anthropic.py`, `test_google.py`, `test_gigachat.py`, `test_provider_base.py`, `test_provider_contract.py::test_tool_call_normalises_identically`. **Against live APIs: NOT VERIFIED** (see section 4) |
| Retries on 429/5xx/timeouts, no retry after the first event, auth not retried | **verified by test**: `test_provider_base.py::test_retries_transient_errors_before_any_event`, `::test_no_retry_after_first_event`, `::test_auth_errors_are_not_retried`; error kinds `::test_error_classification` |
| Provider registry: built-ins plus custom OpenAI-compatible, missing key names the variable | **verified by test**: `test_registry.py` (all tests) |
| `@tool` schema from hints and docstring; errors become results | **verified by test**: `test_tools_base.py` |
| Built-in tools with today's names and parameters | **verified by test**: `test_shell_todos.py::test_builtin_tool_names`, `test_file_tools.py` |
| Independent calls run concurrently, writes serialised per path | **verified by test**: `test_loop.py::test_reads_run_in_parallel`, `::test_mutations_on_one_path_are_serialised` |
| Oversized tool results saved to a file with an excerpt | **verified by test**: `test_loop.py::test_big_tool_output_is_saved_to_a_file` |
| Workspace: outside path is an explicit error, never remapped | **verified by test**: `test_workspace.py::test_outside_path_is_an_error_not_remapped`, `::test_dotdot_escape_is_an_error`, `::test_symlink_escape_is_an_error` |
| Local sandbox | **verified by test**: `test_sandbox.py` local tests |
| Docker sandbox (container per session, mount, network, cleanup) | **verified by test with a stubbed docker binary**: `test_sandbox.py::test_docker_sandbox_lifecycle` (asserts the `docker run -d --rm --network none -v ...` and `exec`/`rm -f` command lines). **Against a real daemon: NOT VERIFIED**: Docker is running on this machine but `alpine:3.20` is not present and the pull did not finish within 30 s, so `test_docker_sandbox_real` now skips (it runs when the image is present) |
| Policy: allow/deny/ask rules, plan mode, anchors, approvals approve/edit/reject/always | **verified by test**: `test_policy.py`, `test_loop.py::test_approval_approve_edit_reject_and_always`, `::test_deny_rule_is_an_error_result` |
| Hooks: python and command hooks, block, modify args, timeout never blocks | **verified by test**: `test_hooks.py`, `test_loop.py::test_pre_tool_hook_blocks_and_session_start_adds_context`, `::test_turn_end_hook_can_send_the_agent_back_to_work` |
| System prompt assembly (AGENTS.md, memory, skills index) | **verified by test**: `test_context.py`, `test_skills_memory.py`, `tests/test_agent.py` |
| Compaction at 80%, keeps last 4 user turns, never splits call from result | **verified by test**: `test_compaction.py::test_loop_compacts_automatically_at_the_threshold`, `::test_manual_compact_keeps_last_turns`, `::test_compact_counts_distinct_turns_not_user_role_entries` |
| Dangling tool calls patched after a crash or cancel | **verified by test**: `test_session.py::test_patch_dangling_adds_cancelled_results`, `test_loop.py::test_dangling_calls_from_a_crash_are_patched` |
| Event-log sessions: fork, rewind, export, undo/redo (files and conversation), prune | **verified by test**: `test_session.py`, `test_undo.py` |
| Loop: cancel kills the request and tools and records `Cancelled`; `run_sync`; queue/steer; `/btw` side question | **verified by test**: `test_loop.py` (cancel, `test_run_sync_without_handler_rejects_approvals`, `test_queued_message_arrives_at_the_next_step`), `test_roles.py::test_side_question_is_not_logged` |
| Subagents: delegation, `subagents.toml`, parallel, worktrees, approvals through the parent, budget | **verified by test**: `test_subagents.py` |
| Background tasks (handle, status, cancel, delivery at next step) and background sessions surviving a client disconnect | **verified by test**: `test_background.py` |
| Skills, memory, `save_skill` | **verified by test**: `test_skills_memory.py`, `tests/test_skills.py` |
| MCP: stdio and HTTP via the official client, OAuth | **verified by test for stdio and the OAuth building blocks**: `test_mcp.py` (real stdio echo server, callback listener, token storage). **A real remote HTTP/OAuth MCP server: NOT VERIFIED** |
| Usage, cost from the catalogue, `max_cost` | **verified by test**: `test_usage_catalog.py`; budget stop: `test_subagents.py::test_budget_is_checked_before_spawning`. Catalogue prices against current provider pricing: NOT VERIFIED (section 4) |
| Model roles (fast, plan, vision, summarize) | **verified by test**: `test_roles.py` |
| Client, wire protocol and JSON Schema in lockstep | **verified by test**: `test_client.py`, `test_protocol.py::test_schema_file_is_up_to_date`, `tests/test_server_background.py::test_the_reference_client_speaks_to_the_real_server` |
| ACP over stdio (sessions, chunks, tool calls, permissions, cancel) | **verified by test**: `test_acp.py`; editor integration NOT VERIFIED (section 2) |
| Public API is exactly the documented set, every callable documented | **verified by test**: `test_public_api.py` |
| Examples run offline | **verified by test**: `tests/test_examples.py` (six examples, subprocess, no network) |
| App on the core: REPL, server, TUI consume `agent.run` events; approvals via `session.decide` | **verified by test**: `tests/test_end_to_end.py`, `test_server_turn_flow.py`, `test_server_approvals.py`, `test_tui_chat.py::test_approval_needed_pushes_modal_and_the_same_stream_continues` |
| New commands `/rewind /export /btw /steer /fork /background`, `/compact`, `/undo`, `/redo` | **verified by test**: `tests/test_commands.py`, `tests/test_server_commands.py`, `tests/test_tui_commands.py` |
| TUI: Esc and Ctrl+C cancel, image paste, undo depth, Ctrl+G background | **verified by test** with a fake client: `tests/test_tui_background.py` |
| `luna acp`, `luna sessions rm\|prune`, `luna run --background`, `luna agents` | **verified by test**: `tests/test_cli.py` |
| Image input (attach and paste) reaches the model | **verified by test**: `tests/test_server_turns.py::test_images_in_the_body_reach_the_model`, `tests/test_engine.py`. A real vision model: NOT VERIFIED |
| Old app suite ported as the acceptance gate | **verified**: whole suite, see section 5 |

## 4. Live provider checks (carry-overs from Tasks 3, 10, 12 and 17)

Keys available: only a DeepSeek key in `~/.config/luna/credentials.toml` (values never printed
or copied). No key for Anthropic, OpenAI, Google, GigaChat, Yandex, OpenRouter in the
environment or the credentials file. One tiny prompt was sent to the one configured provider:

| Check | Status |
|---|---|
| DeepSeek tiny prompt (`luna --no-splash -p "say ok" --output-format json`) | **NOT VERIFIED**: `HTTP 402: Insufficient Balance` (payment error, same as on 2026-10-08). The throwaway session it created was deleted with `luna sessions rm` |
| `models.toml` prices against provider pricing | **NOT VERIFIED**: no live access and no web check was done; the plan's values stay (Task 3 ruling) |
| GigaChat: consecutive user messages after compaction | **NOT VERIFIED**: no GigaChat credentials. Anthropic/Google adapters merge consecutive user messages (`test_anthropic.py::test_consecutive_user_messages_are_merged`); OpenAI accepts them; GigaChat behaviour is unknown |
| Gemini thought signatures (including a signature on a `thought: true` part) | **NOT VERIFIED** live: no Google key. Recorded-fixture coverage: `test_google.py::test_thoughts_text_and_function_call_with_signature`, `::test_history_parts_round_trip_signatures`, `::test_text_signature_becomes_a_thinking_block` |
| Provider default model ids (`claude-sonnet-5`, `gpt-6-astra`, `gpt-6.1-sol`, `deepseek-flash`, `gemini-3.1-pro-preview`, `GigaChat-2-Max`, `yandexgpt/latest`, OpenRouter/xAI ids) | **NOT VERIFIED**: no keys to list models; ids are the registry values from Task 12 |
| `pytest -m live` (`test_live.py`) | skipped: no keys (`OPENROUTER_API_KEY not set` and similar) |

To close these, put a funded key for each provider in the environment and run
`uv run pytest -m live`, then the "find and fix a bug" procedure below.

## 5. Whole suite

`uv run pytest -q -o addopts=""` (both suites, live tests included in selection but skipped
without keys): **1002 passed, 8 skipped** (7 live provider tests without keys, `test_docker_sandbox_real` without the image) in 95.8 s. `uv run ruff check .` and
`uv run ruff format --check .` are clean.

## 6. "Find and fix a bug" procedure (for the blocked rows)

Copy the repository to a temporary folder, break one function so one test fails, run
`luna -p "the test suite fails; find and fix the bug" --output-format json`, then run the
tests. Pass means the tests are green and the diff touches only the broken function. Record
tokens and cost per run. If a provider key is unavailable, record "blocked: <reason>" and do
not mark the row passed.
