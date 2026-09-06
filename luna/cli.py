"""Command-line interface for Luna (temporary stub, replaced in Task 7)."""

import argparse

from luna import __version__


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Stub implementation."""
    parser = argparse.ArgumentParser(prog="luna")
    parser.add_argument("--version", action="version", version=f"luna {__version__}")
    parser.parse_args(argv)
    return 0
