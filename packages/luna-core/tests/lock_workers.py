"""Process entry points for the cross-process lock tests (importable under spawn)."""

from pathlib import Path


def journal_writer(root: str, prefix: str, count: int) -> None:
    """Note ``count`` distinct paths in turn ``t1`` of session ``s``'s journal."""
    from luna_core.undo import UndoJournal

    journal = UndoJournal(root, "s")
    for k in range(count):
        journal.before_write("t1", f"{prefix}{k}.txt")


def worktree_cycler(root: str, name: str, count: int) -> None:
    """Create, change and finish ``count`` subagent worktrees in ``root``."""
    from luna_core.subagents import create_worktree, finish_worktree

    for k in range(count):
        path, branch, base = create_worktree(Path(root), f"{name}{k}")
        (path / f"{name}{k}.txt").write_text("x\n")
        stat, _diff = finish_worktree(Path(root), path, branch, base, "m")
        if f"{name}{k}.txt" not in stat:
            raise AssertionError(f"lost the change of {name}{k}: {stat!r}")


def create_one_worktree(root: str, marker: str) -> None:
    """Create (and finish) one worktree, then write ``marker``."""
    from luna_core.subagents import create_worktree, finish_worktree

    path, branch, base = create_worktree(Path(root), "late")
    finish_worktree(Path(root), path, branch, base, "m")
    Path(marker).write_text("done")
