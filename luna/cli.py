"""Command-line interface for Luna: one-shot and interactive REPL."""

from __future__ import annotations

import argparse
import sys
import uuid

from luna import __version__
from luna.agent import build_agent
from luna.config import load_config
from luna.providers import PROVIDERS, LunaConfigError
from luna.session import run_once, run_repl
from luna.ui.console import get_console
from luna.ui.splash import render_splash


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="luna",
        description="Luna - a simple, lightweight CLI coding agent.",
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


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    console = get_console()

    try:
        config = load_config(_overrides(args))
    except LunaConfigError as exc:
        print(f"luna: {exc}", file=sys.stderr)
        return 2

    prompt = args.prompt_pos or args.prompt

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
