"""Command-line interface for Luna: one-shot, REPL, and ``setup`` / ``config``."""

from __future__ import annotations

import argparse
import sys
from collections import namedtuple
from pathlib import Path

from luna import __version__

_SUBCOMMANDS = {
    "setup",
    "config",
    "mcp",
    "skills",
    "agents",
    "init",
    "serve",
    "run",
    "sessions",
    "acp",
}


def run_tui(config, *, workdir: str, thread_id: str) -> int:
    """Launch the full-screen TUI, auto-starting the local server first."""
    from luna.server.run import ensure_running
    from luna.tui.app import LunaApp

    info = ensure_running(workdir)
    app = LunaApp(
        base_url=f"http://127.0.0.1:{info['port']}",
        token=info["token"],
        workdir=workdir,
        thread_id=thread_id,
        provider=config.provider,
        model=config.model,
        pricing=config.pricing,
    )
    app.run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the agent-run flow."""
    parser = argparse.ArgumentParser(
        prog="luna",
        description="Luna - a simple, lightweight CLI coding agent. "
        "Also: 'luna setup' and 'luna config'.",
    )
    parser.add_argument(
        "prompt_pos",
        nargs="?",
        metavar="PROMPT",
        help="run this prompt once and exit; omit for an interactive REPL",
    )
    parser.add_argument("-p", "--prompt", help="alternative to the positional PROMPT")
    parser.add_argument("--provider", help="model provider")
    parser.add_argument("--model", help="model id for the chosen provider")
    parser.add_argument("--workdir", default=None, help="working directory (default: cwd)")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None, dest="max_tokens")
    parser.add_argument("--yolo", action="store_true", help="disable all approval prompts")
    parser.add_argument(
        "--no-splash", action="store_true", dest="no_splash", help="skip the splash screen"
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        dest="no_input",
        help="never prompt interactively (fail fast instead)",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="cont",
        action="store_true",
        help="resume the most recent session for this directory",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="__list__",
        default=None,
        help="resume a past session (no value: pick from a list; or a thread id)",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        dest="output_format",
        help="output format for a one-shot prompt",
    )
    parser.add_argument(
        "--json",
        action="store_const",
        const="json",
        dest="output_format",
        help="shorthand for --output-format json",
    )
    parser.add_argument("--version", action="version", version=f"luna {__version__}")
    return parser


def _resolve_resume(args, index, workdir, console, interactive):
    """Return a thread_id to resume, or None on error."""
    if args.cont:
        row = index.latest_for(workdir)
        if row is None:
            print("luna: no previous session for this directory", file=sys.stderr)
            return None
        return row.thread_id
    if args.resume != "__list__":
        rows = index.list(workdir)
        if args.resume.isdigit() and 1 <= int(args.resume) <= len(rows):
            return rows[int(args.resume) - 1].thread_id
        return args.resume  # non-numeric: treat as a raw thread id
    rows = index.list(workdir)
    if not rows:
        print("luna: no sessions recorded for this directory", file=sys.stderr)
        return None
    if not interactive:
        for n, r in enumerate(rows, 1):
            console.print(f"  [{n}] {r.title}")
        print("luna: --resume needs a value in non-interactive mode", file=sys.stderr)
        return None
    from luna.ui.interact import arrow_pick

    picked = arrow_pick(console, input, [(r.thread_id, r.title) for r in rows], default=None)
    if picked is not None:
        return picked
    for n, r in enumerate(rows, 1):
        console.print(f"  [{n}] {r.title}")
    choice = input("resume which? > ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1].thread_id
    return choice or None


def _overrides(args: argparse.Namespace) -> dict:
    over: dict = {
        "provider": args.provider,
        "model": args.model,
        "workdir": args.workdir,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    if args.yolo:
        over["yolo"] = True
    if args.no_splash:
        over["show_splash"] = False
    return {k: v for k, v in over.items() if v is not None}


def _has_api_key(provider: str, registry: dict) -> bool:
    import os

    from luna.config.credentials import get_api_key

    spec = registry[provider]
    if spec.env_var is None:
        return True
    return bool(os.environ.get(spec.env_var) or get_api_key(provider))


# --- config subcommand ------------------------------------------------------


def _config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="luna config")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("path", help="print the config and credentials file paths")
    sub.add_parser("show", help="print the effective configuration")
    p_set = sub.add_parser("set", help="set a config value (e.g. model.provider anthropic)")
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_key = sub.add_parser("set-key", help="store an API key for a provider")
    p_key.add_argument("provider")
    p_key.add_argument("api_key", nargs="?")
    p_unset = sub.add_parser("unset-key", help="remove a stored API key")
    p_unset.add_argument("provider")
    return parser


def _run_config(argv: list[str]) -> int:
    import getpass

    from luna.config.config import config_path, load_config, set_config_values
    from luna.config.credentials import (
        credentials_path,
        get_api_key,
        mask_key,
        set_api_key,
        unset_api_key,
    )
    from luna.config.providers import merge_providers
    from luna.ui.console import get_console

    console = get_console()
    args = _config_parser().parse_args(argv)

    if args.cmd == "path":
        console.print(f"config:      {config_path()}")
        console.print(f"credentials: {credentials_path()}")
        return 0

    if args.cmd == "show":
        cfg = load_config({})
        console.print(f"provider    {cfg.provider}")
        console.print(f"model       {cfg.model or '(provider default)'}")
        console.print(f"workdir     {cfg.workdir}")
        console.print(f"yolo        {cfg.yolo}")
        console.print(f"splash      {cfg.show_splash}")
        for name in merge_providers(cfg.custom_providers):
            stored = get_api_key(name)
            if stored:
                console.print(f"key.{name}   {mask_key(stored)}")
        return 0

    if args.cmd == "set":
        path = set_config_values({args.key: args.value})
        console.print(f"set {args.key} = {args.value}  ->  {path}")
        return 0

    if args.cmd == "set-key":
        registry = merge_providers(load_config({}).custom_providers)
        key = args.api_key or getpass.getpass(f"{args.provider} API key: ")
        path = set_api_key(args.provider, key, registry=registry)
        console.print(f"stored {mask_key(key)} for {args.provider}  ->  {path}")
        return 0

    if args.cmd == "unset-key":
        registry = merge_providers(load_config({}).custom_providers)
        removed = unset_api_key(args.provider, registry=registry)
        console.print(
            f"removed key for {args.provider}" if removed else f"no stored key for {args.provider}"
        )
        return 0

    return 2  # pragma: no cover - argparse guards this


# --- mcp / skills / agents subcommands -------------------------------------


def _split_ddash(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1 :]
    return argv, []


async def _mcp_probe(name: str, spec: dict, console) -> list:
    """Connect to one MCP server, list its tools, disconnect."""
    from luna_core.mcp import MCPManager

    manager = MCPManager({name: spec}, on_warn=lambda m: console.print(f"[yellow]{m}[/]"))
    try:
        return await manager.start()
    finally:
        await manager.close()


def _run_mcp(argv: list[str]) -> int:
    import asyncio
    import json

    from luna.extensions import mcp
    from luna.extensions.registry import known_mcp, resolve_mcp
    from luna.ui.console import get_console

    console = get_console()
    head, rest = _split_ddash(argv)
    parser = argparse.ArgumentParser(prog="luna mcp")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_add = sub.add_parser("add")
    p_add.add_argument("name")
    p_add.add_argument("--json", dest="json_spec")
    p_add.add_argument("--project", action="store_true")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("name")
    p_rm.add_argument("--project", action="store_true")
    p_test = sub.add_parser("test")
    p_test.add_argument("name")
    args = parser.parse_args(head)

    if args.cmd == "list":
        configured = ", ".join(mcp.load_mcp_config()) or "(none)"
        console.print(f"configured: {configured}")
        console.print(f"known:      {', '.join(known_mcp())}")
        return 0
    if args.cmd == "remove":
        ok = mcp.remove_server(args.name, project=args.project)
        console.print(f"removed {args.name!r}" if ok else f"{args.name!r} was not configured")
        return 0
    if args.cmd == "test":
        configured = mcp.load_mcp_config()
        spec = configured.get(args.name) or resolve_mcp(args.name)
        tools = asyncio.run(_mcp_probe(args.name, spec, console))
        if not tools:
            return 1
        console.print(f"{args.name}: {', '.join(t.name for t in tools)}")
        return 0
    # add
    if rest:
        spec: dict = {"command": rest[0], "args": rest[1:]}
    elif args.json_spec:
        spec = json.loads(args.json_spec)
    else:
        spec = resolve_mcp(args.name)
    path = mcp.add_server(args.name, spec, project=args.project)
    console.print(f"added {args.name!r}  ->  {path}")
    return 0


def _run_skills(argv: list[str]) -> int:
    from luna.extensions import skills
    from luna.extensions.registry import known_skills
    from luna.ui.console import get_console

    console = get_console()
    parser = argparse.ArgumentParser(prog="luna skills")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_add = sub.add_parser("add")
    p_add.add_argument("source")
    p_add.add_argument("--name")
    p_add.add_argument("--project", action="store_true")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("name")
    p_rm.add_argument("--project", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        rows = skills.list_skills()
        for scope, name, desc in rows:
            console.print(f"  {name} ({scope}) — {desc}")
        console.print(f"known: {', '.join(known_skills())}")
        return 0
    if args.cmd == "remove":
        ok = skills.remove(args.name, project=args.project)
        console.print(f"removed {args.name!r}" if ok else f"{args.name!r} is not installed")
        return 0
    console.print(skills.install(args.source, name=args.name, project=args.project))
    return 0


def _live_server() -> dict | None:
    """Return the running local server's token-file dict, or ``None`` (none, or a stale file)."""
    from luna.server.auth import read_token_file
    from luna.server.run import _default_is_alive

    info = read_token_file()
    if info is None or not _default_is_alive(info.get("pid", 0)):
        return None
    return info


def _client(info: dict):
    from luna_core.client import LunaClient

    return LunaClient(f"http://127.0.0.1:{info['port']}", info["token"])


def _server_runs() -> list[dict]:
    """Return the live server's runs (empty when there is none or it does not answer)."""
    import asyncio

    from luna_core.client import ServerError

    info = _live_server()
    if info is None:
        return []

    async def fetch() -> list[dict]:
        client = _client(info)
        try:
            return (await client.agents()).get("runs", [])
        finally:
            await client.aclose()

    try:
        return asyncio.run(fetch())
    except ServerError:
        return []


def _run_agents(argv: list[str]) -> int:
    from luna.extensions.subagents import subagent_summaries
    from luna.ui.console import get_console

    console = get_console()
    parser = argparse.ArgumentParser(prog="luna agents")
    parser.add_argument("cmd", choices=["list"], nargs="?", default="list")
    parser.parse_args(argv)
    for name, desc in subagent_summaries():
        console.print(f"  {name} — {desc}")
    runs = _server_runs()
    if runs:
        console.print("background runs:")
    for run in runs:
        cost = run.get("cost")
        price = f" · ${cost:.4f}" if isinstance(cost, int | float) else ""
        label = run.get("label") or run.get("title") or ""
        console.print(f"  {run.get('id')} · {label} · {run.get('status')}{price}", markup=False)
    return 0


# --- sessions / run / acp subcommands -----------------------------------------


def _delete_conversation(log, session_id: str) -> str:
    """Delete a conversation and its subagent/background children; returns the outcome.

    ``"deleted"``, or ``"busy"`` when the live server refuses (a turn or a
    background task is running). With a live server the delete goes through
    it, so it can drop its in-memory runtime; otherwise the log is edited
    directly. Forks are conversations of their own and are kept.
    """
    import asyncio

    from luna_core.client import ServerError

    from luna.core.store import descendants

    children = descendants(log, session_id)
    info = _live_server()
    done = False
    if info is not None:
        if _has_active_work(info, {session_id, *children}):
            return "busy"

        async def remote() -> None:
            client = _client(info)
            try:
                await client.delete_session(session_id)
            finally:
                await client.aclose()

        try:
            asyncio.run(remote())
            done = True
        except ServerError as exc:
            if exc.status == 409:
                return "busy"
            if exc.status is not None:
                raise
    if not done:
        log.delete(session_id)  # cascades to the subagent/background children
    return "deleted"


def _has_active_work(info: dict, ids: set[str]) -> bool:
    """Return whether the server runs a turn or background task of a session in ``ids``."""
    import asyncio

    from luna_core.client import ServerError

    async def fetch() -> dict:
        client = _client(info)
        try:
            return await client.agents()
        finally:
            await client.aclose()

    try:
        agents = asyncio.run(fetch())
    except ServerError:
        return False  # unreachable: the delete below reports it
    active = ("queued", "running", "waiting")
    runs = (r for r in agents.get("runs", []) if r.get("id") in ids)
    tasks = (t for t in agents.get("tasks", []) if t.get("session_id") in ids)
    return any(x.get("status") in active for x in (*runs, *tasks))


def _running_ids(workdir: str) -> set[str]:
    """Ids the live server reports as running in ``workdir`` (empty without a server)."""
    import asyncio

    from luna_core.client import ServerError

    info = _live_server()
    if info is None:
        return set()

    async def fetch() -> list[dict]:
        client = _client(info)
        try:
            return await client.list_sessions(workdir)
        finally:
            await client.aclose()

    try:
        return {r["id"] for r in asyncio.run(fetch()) if r.get("running")}
    except ServerError:
        return set()


def _positive_days(value: str) -> float:
    import math

    try:
        days = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number of days: {value!r}") from None
    if not math.isfinite(days) or days <= 0:
        raise argparse.ArgumentTypeError("--days must be a finite number greater than 0")
    return days


def _run_sessions(argv: list[str]) -> int:
    """``luna sessions [list|rm <id>|prune [--days N]]`` for the current folder."""
    import time

    from luna.core.store import conversations, is_conversation, open_log

    parser = argparse.ArgumentParser(prog="luna sessions")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list this folder's sessions")
    rm = sub.add_parser(
        "rm",
        help="delete a session (any session id, not only this folder's) "
        "and its subagent/background runs",
    )
    rm.add_argument("session_id")
    prune = sub.add_parser("prune", help="delete sessions not touched for N days")
    prune.add_argument("--days", type=_positive_days, default=30)
    args = parser.parse_args(argv)
    log = open_log()
    workdir = str(Path.cwd().resolve())

    if args.cmd in (None, "list"):
        for info in conversations(log, workdir):
            print(f"{info.id}  {info.title or '(untitled)'}")
        return 0

    if args.cmd == "rm":
        info = log.info(args.session_id)
        if info is None:
            print(f"luna: no session {args.session_id}", file=sys.stderr)
            return 2
        if not is_conversation(info):
            print(
                f"luna: {args.session_id} is a subagent/background run of {info.parent_id}; "
                "delete that session instead",
                file=sys.stderr,
            )
            return 2
        if _delete_conversation(log, args.session_id) == "busy":
            print(f"luna: {args.session_id} is running; stop it first", file=sys.stderr)
            return 1
        print(f"deleted {args.session_id}")
        return 0

    cutoff = time.time() - args.days * 86400
    running = _running_ids(workdir)
    removed = 0
    for info in log.list(workdir, limit=1_000_000):
        if not is_conversation(info) or info.updated >= cutoff or info.id in running:
            continue
        if _delete_conversation(log, info.id) == "deleted":
            removed += 1
            print(f"removed {info.id}  {info.title or '(untitled)'}")
    print(f"pruned {removed} session(s)")
    return 0


def _trusted(workdir: str) -> bool:
    from luna.core.projects import ProjectIndex

    return ProjectIndex().is_trusted(workdir)


def _run_run(argv: list[str]) -> int:
    """``luna run --background "prompt"``: start a detached run on the local server."""
    import asyncio

    from luna_core.client import ServerError

    from luna.server.run import ensure_running

    parser = argparse.ArgumentParser(prog="luna run")
    parser.add_argument("--background", action="store_true", required=True)
    parser.add_argument("prompt")
    args = parser.parse_args(argv)
    workdir = str(Path.cwd().resolve())
    if not _trusted(workdir):
        print(
            "luna: this folder is not trusted yet; open it once with `luna` and confirm",
            file=sys.stderr,
        )
        return 1
    from luna.config.config import load_config
    from luna.config.providers import merge_providers

    config = load_config({})
    if not _has_api_key(config.provider, merge_providers(config.custom_providers)):
        print(
            f"luna: No API key for {config.provider}. Run 'luna setup' to configure one, "
            f"export its API key, or pick another provider.",
            file=sys.stderr,
        )
        return 2
    try:
        info = ensure_running(workdir)
    except Exception as exc:  # noqa: BLE001 - a spawn failure must read as an error, not a trace
        print(f"luna: cannot start the local server: {exc}", file=sys.stderr)
        return 1

    async def go() -> str:
        client = _client(info)
        try:
            session_id = await client.create_session(workdir)
            await client.background(session_id, args.prompt, workdir)
            return session_id
        finally:
            await client.aclose()

    try:
        session_id = asyncio.run(go())
    except ServerError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 1
    print(
        f"session {session_id} started in the background; "
        f"follow it with: luna --resume {session_id}"
    )
    return 0


def _run_acp(argv: list[str]) -> int:
    """``luna acp``: ACP over stdio for the current folder (no trust prompt, like ``-p``)."""
    from luna.core.acp_app import run

    return run(Path.cwd())


# --- init subcommand ------------------------------------------------------


def _run_init(argv: list[str]) -> int:
    """Explore the repo and write/update AGENTS.md in one agent turn."""
    import asyncio

    from luna.config.config import load_config
    from luna.ui.console import get_console

    console = get_console()
    config = load_config({})
    return asyncio.run(_init_core(config, console))


async def _init_core(config, console) -> int:
    from luna.config.providers import LunaConfigError
    from luna.core.agent import build_agent
    from luna.core.session import run_once
    from luna.core.store import open_log
    from luna.extensions.initgen import init_prompt

    try:
        bundle = await build_agent(config, on_warn=lambda m: console.print(f"[yellow]{m}[/]"))
    except LunaConfigError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 2
    try:
        await run_once(
            bundle, init_prompt(config.workdir), console=console, log=open_log(), cfg=config
        )
    finally:
        await bundle.aclose()
    return 0


# --- main -------------------------------------------------------------------


def _setup(argv: list[str]) -> int:
    from luna.repl.setup_wizard import run_setup
    from luna.ui.console import get_console

    return run_setup(get_console())


def _serve(argv: list[str]) -> int:
    from luna.server.run import run_serve

    return run_serve(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw in (["--version"], ["-V"]):  # before any heavy import: this path must stay instant
        print(f"luna {__version__}")
        return 0
    return _main(raw)


def _main(raw: list[str]) -> int:
    import asyncio

    from luna.config.config import load_config
    from luna.config.providers import LunaConfigError, merge_providers
    from luna.ui.console import get_console

    if raw and raw[0] in _SUBCOMMANDS:
        handlers = {
            "setup": _setup,
            "config": _run_config,
            "mcp": _run_mcp,
            "skills": _run_skills,
            "agents": _run_agents,
            "init": _run_init,
            "serve": _serve,
            "run": _run_run,
            "sessions": _run_sessions,
            "acp": _run_acp,
        }
        try:
            return handlers[raw[0]](raw[1:])
        except LunaConfigError as exc:
            print(f"luna: {exc}", file=sys.stderr)
            return 2
        except (KeyboardInterrupt, EOFError):
            print()
            return 130

    args = build_parser().parse_args(raw)
    console = get_console()

    try:
        config = load_config(_overrides(args))
    except LunaConfigError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 2

    prompt = args.prompt_pos or args.prompt
    interactive = console.is_terminal and sys.stdin.isatty() and not args.no_input

    # Trust gate: only the TUI path asks. `-p` and a piped-stdin REPL are
    # scripted invocations, where the invocation itself is the consent.
    if interactive and not prompt:
        from luna.ui.trust import ensure_trusted

        if not ensure_trusted(console, config.workdir):
            return 1

    if not _has_api_key(config.provider, merge_providers(config.custom_providers)):
        if interactive:
            from luna.repl.setup_wizard import run_setup

            console.print(f"[yellow]No API key for {config.provider}.[/] Let's set one up.")
            try:
                run_setup(console)
            except (KeyboardInterrupt, EOFError):
                print()
                return 130
            config = load_config(_overrides(args))
        # non-interactive: fall through; build_agent raises the clean error.

    if interactive and not prompt:
        from luna.turn.gitinfo import dirty_paths

        _dirty = dirty_paths(config.workdir)
        if _dirty:
            console.print(
                f"[yellow]note:[/] working tree has {len(_dirty)} changed file(s); "
                "Luna edits files in place"
            )

    if config.show_splash and not prompt and console.is_terminal:
        from luna.ui.splash import render_splash

        render_splash(console)

    if interactive and not prompt:
        return _run_tui_path(args, config, console)
    try:
        return asyncio.run(_run_core(args, config, console, prompt, interactive))
    except KeyboardInterrupt:
        console.print()
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard
        print(f"luna: {exc}", file=sys.stderr)
        return 1


def _run_tui_path(args, config, console) -> int:
    """Launch the full-screen TUI (served by the local server on luna-core)."""
    import uuid

    from luna_core.undo import gc

    from luna.config.providers import LunaConfigError, build_provider, merge_providers
    from luna.core.store import open_log

    try:  # fail fast on a bad provider/model/key instead of inside the server
        build_provider(
            config.provider, config.model, registry=merge_providers(config.custom_providers)
        )
    except LunaConfigError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 2
    log = open_log()
    start_thread = uuid.uuid4().hex
    if args.cont or args.resume:
        try:
            target = _resolve_resume(args, _LogIndex(log), config.workdir, console, True)
        except (KeyboardInterrupt, EOFError):
            print()
            return 130
        if target is None:
            return 2
        start_thread = target
    gc(config.workdir, keep=start_thread)  # never gc the journal we resume
    try:
        # Absolute: the shared server would resolve a relative "." against
        # its own cwd, i.e. whichever project happened to start it.
        workdir = str(Path(config.workdir).resolve())
        return run_tui(config, workdir=workdir, thread_id=start_thread)
    except KeyboardInterrupt:
        console.print()
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard
        print(f"luna: {exc}", file=sys.stderr)
        return 1


_LogRow = namedtuple("_LogRow", "thread_id title")
_ENDS = ("turn_done", "turn_error", "cancelled")


class _LogIndex:
    """The event log in the shape :func:`_resolve_resume` reads (real conversations only)."""

    def __init__(self, log) -> None:
        self._log = log

    def list(self, workdir: str) -> list[_LogRow]:
        from luna.core.store import conversations

        rows = conversations(self._log, str(Path(workdir).resolve()))
        return [_LogRow(i.id, i.title or "(untitled)") for i in rows]

    def latest_for(self, workdir: str) -> _LogRow | None:
        rows = self.list(workdir)
        return rows[0] if rows else None


async def _run_core(args, config, console, prompt: str | None, interactive: bool) -> int:
    """``luna -p`` and the line REPL, on luna-core."""
    import asyncio

    from luna_core.session import Session
    from luna_core.undo import gc

    from luna.config.providers import LunaConfigError
    from luna.core.agent import build_agent
    from luna.core.session import run_once, run_repl
    from luna.core.store import open_log
    from luna.turn import engine

    log = open_log()
    workdir = str(Path(config.workdir).resolve())
    session = None
    if args.cont or args.resume:
        try:
            target = _resolve_resume(args, _LogIndex(log), workdir, console, interactive)
        except (KeyboardInterrupt, EOFError):
            print()
            return 130
        if target is None:
            return 2
        if log.info(target) is None:
            print(f"luna: no session {target!r}", file=sys.stderr)
            return 2
        session = Session.open(log, target)
    await asyncio.to_thread(gc, workdir, keep=session.id if session else None)

    plan_state = [False]
    hooks_state: dict = {}
    current: list = []

    async def rebuild():
        bundle = await build_agent(
            config,
            on_warn=lambda m: console.print(f"[yellow]{m}[/]"),
            plan_flag=lambda: plan_state[0],
            hooks=engine.make_hooks(lambda: hooks_state.get("state"), lambda: config),
        )
        current[:] = [bundle]
        return bundle

    try:
        bundle = await rebuild()
    except LunaConfigError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 2
    try:
        if prompt:
            from luna.core.store import make_title

            session = session or Session.create(log, workdir, title=make_title(prompt))
            await run_once(
                bundle,
                prompt,
                console=console,
                log=log,
                session=session,
                cfg=config,
                output_format=args.output_format,
                hooks_state=hooks_state,
            )
            # a scripted caller must see a failed or cancelled turn in the exit code
            last = next((r.type for r in reversed(session.records()) if r.type in _ENDS), "")
            return 1 if last in ("turn_error", "cancelled") else 0
        return await run_repl(
            bundle,
            console=console,
            rebuild=rebuild,
            log=log,
            session=session or Session.create(log, workdir),
            config=config,
            plan_state=plan_state,
            hooks_state=hooks_state,
        )
    finally:
        for live in current:  # the REPL closes the bundles it replaced; this is the last one
            await live.aclose()
