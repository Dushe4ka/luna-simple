"""Messages between team members: direct only; everybody at once only from the lead."""

from __future__ import annotations

import time
from dataclasses import dataclass

from luna_core.team.board import LEAD, TeamStore


@dataclass(frozen=True)
class TeamMessage:
    """One delivered message."""

    id: int
    run_id: str
    ts: float
    sender: str
    recipient: str
    text: str


class Mailbox:
    """Messages of team runs, stored next to the board."""

    def __init__(self, store: TeamStore) -> None:
        self.store = store

    def _recipients(self, run_id: str) -> set[str]:
        return {m.name for m in self.store.members(run_id, include_retired=False)} | {LEAD}

    def _insert(self, run_id: str, sender: str, recipient: str, text: str) -> TeamMessage:
        ts = time.time()
        with self.store._write() as conn:
            cur = conn.execute(
                "INSERT INTO team_messages(run_id, ts, sender, recipient, text) VALUES (?,?,?,?,?)",
                (run_id, ts, sender, recipient, text),
            )
        return TeamMessage(cur.lastrowid, run_id, ts, sender, recipient, text)

    def send(self, run_id: str, sender: str, recipient: str, text: str) -> TeamMessage:
        """Send ``text`` to one member or the lead."""
        text = text.strip()
        if not text:
            raise ValueError("a message needs text")
        if recipient == sender:
            raise ValueError("you cannot message yourself")
        if recipient not in self._recipients(run_id):
            raise ValueError(
                f"no member {recipient!r}; send to one member or 'lead' "
                "(only the lead may message everybody)"
            )
        message = self._insert(run_id, sender, recipient, text)
        self.store.log_event(run_id, "team_message", sender, to=recipient, text=text[:500])
        return message

    def broadcast(self, run_id: str, sender: str, text: str) -> int:
        """Send ``text`` to every active member; only the lead may. Returns how many got it."""
        if sender != LEAD:
            raise ValueError("only the lead may broadcast")
        text = text.strip()
        if not text:
            raise ValueError("a message needs text")
        names = [
            m.name for m in self.store.members(run_id, include_retired=False) if m.name != LEAD
        ]
        for name in names:
            self._insert(run_id, LEAD, name, text)
        self.store.log_event(run_id, "team_message", LEAD, to="*", text=text[:500])
        return len(names)

    def unread(self, run_id: str, recipient: str, *, mark: bool = True) -> list[TeamMessage]:
        """Unread messages for ``recipient``, oldest first (marked read unless ``mark`` is off)."""
        with self.store._write() as conn:
            rows = conn.execute(
                "SELECT id, run_id, ts, sender, recipient, text FROM team_messages "
                "WHERE run_id = ? AND recipient = ? AND read = 0 ORDER BY id",
                (run_id, recipient),
            ).fetchall()
            if mark and rows:
                marks = ",".join("?" * len(rows))
                conn.execute(
                    f"UPDATE team_messages SET read = 1 WHERE id IN ({marks})", [r[0] for r in rows]
                )
        return [TeamMessage(*r) for r in rows]

    def count_unread(self, run_id: str, recipient: str) -> int:
        """How many unread messages ``recipient`` has."""
        rows = self.store._read(
            "SELECT COUNT(*) FROM team_messages WHERE run_id = ? AND recipient = ? AND read = 0",
            (run_id, recipient),
        )
        return int(rows[0][0])
