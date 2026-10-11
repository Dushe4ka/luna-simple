import json
import subprocess
import sys
from pathlib import Path

import pytest
from luna_core.events import (
    ApprovalRequest,
    BackgroundDone,
    Cancelled,
    Compacted,
    NeedsApproval,
    Notice,
    ReloadRequested,
    TextDelta,
    ThinkingDelta,
    TodosUpdated,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
    TurnStarted,
    UsageUpdate,
    to_wire,
)
from luna_core.protocol import EVENT_FIELDS, json_schema, validate_event
from luna_core.usage import Usage

SAMPLES = [
    TurnStarted("t1"),
    TextDelta("hi"),
    ThinkingDelta("hm"),
    ToolStarted("c1", "ls", {"path": "."}, "."),
    ToolFinished("c1", "ls", True, "3 entries", 0.1),
    NeedsApproval(ApprovalRequest("c2", "execute", {"command": "ls"}, "ls", "execute:ls *")),
    TodosUpdated([{"content": "x", "status": "pending"}]),
    Notice("info", "note"),
    UsageUpdate(Usage(1, 2)),
    Compacted("s", 3),
    BackgroundDone("h", True, "ok"),
    ReloadRequested(),
    TurnDone(Usage(1, 2), 0.01, "end"),
    TurnError("boom", "server"),
    Cancelled(),
]


def test_every_turn_event_validates():
    names = set()
    for event in SAMPLES:
        wire = to_wire(event)
        validate_event(wire)
        names.add(wire["event"])
    assert names == set(EVENT_FIELDS)


@pytest.mark.parametrize(
    "bad",
    [{"text": "x"}, {"event": "nope"}, {"event": "text_delta"}, {"event": "text_delta", "text": 3}],
)
def test_invalid_events_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_event(bad)


def test_schema_file_is_up_to_date():
    root = Path(__file__).resolve().parents[3]
    on_disk = json.loads((root / "schema" / "events.schema.json").read_text())
    assert on_disk == json_schema()
    printed = subprocess.run(
        [sys.executable, "-m", "luna_core.protocol"], capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(printed) == on_disk
