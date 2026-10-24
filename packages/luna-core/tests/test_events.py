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
from luna_core.usage import Usage


@pytest.mark.parametrize(
    ("event", "wire"),
    [
        (TurnStarted("t1"), {"event": "turn_started", "turn_id": "t1"}),
        (TextDelta("hi"), {"event": "text_delta", "text": "hi"}),
        (ThinkingDelta("hm"), {"event": "thinking_delta", "text": "hm"}),
        (
            ToolStarted("c1", "grep", {"pattern": "x"}, "x"),
            {
                "event": "tool_started",
                "call_id": "c1",
                "name": "grep",
                "args": {"pattern": "x"},
                "args_preview": "x",
            },
        ),
        (
            ToolFinished("c1", "grep", True, "3 matches", 0.25),
            {
                "event": "tool_finished",
                "call_id": "c1",
                "name": "grep",
                "ok": True,
                "detail": "3 matches",
                "duration": 0.25,
            },
        ),
        (
            NeedsApproval(
                ApprovalRequest(
                    "c2", "execute", {"command": "rm -rf build"}, "run a command", "execute(rm *)"
                )
            ),
            {
                "event": "approval_needed",
                "value": {
                    "action_requests": [
                        {
                            "call_id": "c2",
                            "name": "execute",
                            "args": {"command": "rm -rf build"},
                            "description": "run a command",
                            "suggested_rule": "execute(rm *)",
                        }
                    ]
                },
            },
        ),
        (
            TodosUpdated([{"content": "a", "status": "pending"}]),
            {"event": "todos", "todos": [{"content": "a", "status": "pending"}]},
        ),
        (Notice("ok", "✓ verify ok"), {"event": "notice", "level": "ok", "text": "✓ verify ok"}),
        (UsageUpdate(Usage(input=5)), {"event": "usage_delta", "usage": Usage(input=5).to_dict()}),
        (Compacted("summary", 12), {"event": "compacted", "summary": "summary", "removed": 12}),
        (
            BackgroundDone("h1", True, "done"),
            {"event": "background_done", "handle": "h1", "ok": True, "summary": "done"},
        ),
        (ReloadRequested(), {"event": "reload_requested"}),
        (
            TurnDone(Usage(input=1, output=2), 0.5, "end"),
            {
                "event": "turn_done",
                "usage": Usage(input=1, output=2).to_dict(),
                "cost": 0.5,
                "stop_reason": "end",
            },
        ),
        (TurnError("boom", "provider"), {"event": "error", "message": "boom", "kind": "provider"}),
        (Cancelled(), {"event": "cancelled"}),
    ],
)
def test_wire_shapes(event, wire):
    assert to_wire(event) == wire


def test_notice_level_is_validated():
    with pytest.raises(ValueError):
        Notice("loud", "x")


def test_unknown_event_is_rejected():
    with pytest.raises(TypeError):
        to_wire(object())


def test_approval_wire_lists_every_suggested_rule():
    from luna_core.events import ApprovalRequest, NeedsApproval, to_wire

    r = ApprovalRequest(
        "c",
        "execute",
        {"command": "cd x && ls"},
        "",
        "execute:cd *",
        ("execute:cd *", "execute:ls *"),
    )
    item = to_wire(NeedsApproval(r))["value"]["action_requests"][0]
    assert item["suggested_rule"] == "execute:cd *"
    assert item["suggested_rules"] == ["execute:cd *", "execute:ls *"]
