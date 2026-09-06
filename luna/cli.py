"""Command-line interface for Luna: one-shot, REPL, and ``setup`` / ``config``."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid

from luna import __version__
from luna.agent import build_agent
from luna.config import config_path, load_config, set_config_values
from luna.credentials import (
    credentials_path,
    get_api_key,
    mask_key,
    set_api_key,
    unset_api_key,
)
from luna.providers import PROVIDERS, LunaConfigError
from luna.session import run_once, run_repl
from luna.setup_wizard import run_setup
from luna.ui.console import get_console
from luna.ui.splash import render_splash

_SUBCOMMANDS = {"setup", "config"}


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
    parser.add_argument("--version", action="version", version=f"luna {__version__}")
    return parser


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


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in _SUBCOMMANDS:
        try:
            if raw[0] == "setup":
                return run_setup(get_console())
            return _run_config(raw[1:])
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

    if config.show_splash and not prompt and console.is_terminal:
        render_splash(console)

    try:
        agent = build_agent(config)
    except LunaConfigError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 2

    try:
        if prompt:
            run_once(agent, prompt, thread_id=uuid.uuid4().hex, console=console)
            return 0
        return run_repl(agent, console=console)
    except KeyboardInterrupt:
        console.print()
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard
        print(f"luna: {exc}", file=sys.stderr)
        return 1
