"""Command-line interface for Luna: one-shot, REPL, and ``setup`` / ``config``."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import uuid

from luna import __version__
from luna.config.config import config_path, load_config, set_config_values
from luna.config.credentials import (
    credentials_path,
    get_api_key,
    mask_key,
    set_api_key,
    unset_api_key,
)
from luna.config.providers import PROVIDERS, LunaConfigError
from luna.core.agent import build_agent
from luna.core.session import run_once, run_repl
from luna.extensions import mcp, skills
from luna.extensions.initgen import init_prompt
from luna.extensions.registry import known_mcp, known_skills, resolve_mcp
from luna.extensions.subagents import subagent_summaries
from luna.repl.setup_wizard import run_setup
from luna.ui.console import get_console
from luna.ui.splash import render_splash

_SUBCOMMANDS = {"setup", "config", "mcp", "skills", "agents", "init"}


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
    parser.add_argument("--provider", choices=sorted(PROVIDERS), help="model provider")
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
    for n, r in enumerate(rows, 1):
        console.print(f"  [{n}] {r.title}")
    if not interactive:
        print("luna: --resume needs a value in non-interactive mode", file=sys.stderr)
        return None
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


def _has_api_key(provider: str) -> bool:
    spec = PROVIDERS[provider]
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
    p_key.add_argument("provider", choices=sorted(PROVIDERS))
    p_key.add_argument("api_key", nargs="?")
    p_unset = sub.add_parser("unset-key", help="remove a stored API key")
    p_unset.add_argument("provider", choices=sorted(PROVIDERS))
    return parser


def _run_config(argv: list[str]) -> int:
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
        for name in PROVIDERS:
            stored = get_api_key(name)
            if stored:
                console.print(f"key.{name}   {mask_key(stored)}")
        return 0

    if args.cmd == "set":
        path = set_config_values({args.key: args.value})
        console.print(f"set {args.key} = {args.value}  ->  {path}")
        return 0

    if args.cmd == "set-key":
        key = args.api_key or getpass.getpass(f"{args.provider} API key: ")
        path = set_api_key(args.provider, key)
        console.print(f"stored {mask_key(key)} for {args.provider}  ->  {path}")
        return 0

    if args.cmd == "unset-key":
        removed = unset_api_key(args.provider)
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


def _run_mcp(argv: list[str]) -> int:
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
        conns = mcp.to_connections({args.name: spec})
        tools = mcp.load_mcp_tools(conns, on_warn=lambda m: console.print(f"[yellow]{m}[/]"))
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


def _run_agents(argv: list[str]) -> int:
    console = get_console()
    parser = argparse.ArgumentParser(prog="luna agents")
    parser.add_argument("cmd", choices=["list"], nargs="?", default="list")
    parser.parse_args(argv)
    for name, desc in subagent_summaries():
        console.print(f"  {name} — {desc}")
    return 0


# --- init subcommand ------------------------------------------------------


def _run_init(argv: list[str]) -> int:
    """Explore the repo and write/update AGENTS.md in one agent turn."""
    console = get_console()
    config = load_config({})
    try:
        agent = build_agent(config, on_warn=lambda m: console.print(f"[yellow]{m}[/]"))
    except LunaConfigError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 2
    run_once(
        agent,
        init_prompt(config.workdir),
        thread_id=uuid.uuid4().hex,
        console=console,
        workdir=config.workdir,
    )
    return 0


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in _SUBCOMMANDS:
        handlers = {
            "setup": lambda a: run_setup(get_console()),
            "config": _run_config,
            "mcp": _run_mcp,
            "skills": _run_skills,
            "agents": _run_agents,
            "init": _run_init,
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

    if not _has_api_key(config.provider):
        if interactive:
            console.print(f"[yellow]No API key for {config.provider}.[/] Let's set one up.")
            try:
                run_setup(console)
            except (KeyboardInterrupt, EOFError):
                print()
                return 130
            config = load_config(_overrides(args))
        # non-interactive: fall through; build_agent raises the clean error.

    from luna.turn.gitinfo import dirty_paths

    _dirty = dirty_paths(config.workdir) if interactive and not prompt else []
    if _dirty:
        console.print(
            f"[yellow]note:[/] working tree has {len(_dirty)} changed file(s); "
            "Luna edits files in place"
        )

    if config.show_splash and not prompt and console.is_terminal:
        render_splash(console)

    from luna.core.persistence import SessionIndex, checkpointer

    index = SessionIndex()
    cp = checkpointer(on_warn=lambda m: console.print(f"[yellow]{m}[/]"))
    start_thread = uuid.uuid4().hex
    if args.cont or args.resume:
        target = _resolve_resume(args, index, config.workdir, console, interactive)
        if target is None:
            return 2
        start_thread = target
    session_id = start_thread  # the undo journal follows the session across --continue
    plan_state = [False]

    from luna.turn import undo

    undo.gc(config.workdir, keep=session_id)  # after session_id: never gc the journal we resume

    def _rebuild():
        return build_agent(
            config,
            checkpointer=cp,
            on_warn=lambda m: console.print(f"[yellow]{m}[/]"),
            session_id=session_id,
            plan_flag=lambda: plan_state[0],
        )

    try:
        agent = _rebuild()
    except LunaConfigError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 2

    try:
        if prompt:
            run_once(
                agent,
                prompt,
                thread_id=start_thread,
                console=console,
                index=index,
                workdir=config.workdir,
                session_id=session_id,
                cfg=config,
                output_format=args.output_format,
            )
            return 0
        return run_repl(
            agent,
            console=console,
            rebuild=_rebuild,
            index=index,
            thread_id=start_thread,
            workdir=config.workdir,
            config=config,
            session_id=session_id,
            plan_state=plan_state,
        )
    except KeyboardInterrupt:
        console.print()
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard
        print(f"luna: {exc}", file=sys.stderr)
        return 1
