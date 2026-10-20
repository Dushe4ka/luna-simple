import luna_core

EXPECTED = {
    "Agent", "SideAnswer", "PathLocks", "Session", "EventLog", "Message", "Text", "Thinking",
    "Image", "ToolCall", "ToolResult", "image_from_file", "ApprovalRequest", "TurnStarted",
    "TextDelta", "ThinkingDelta", "ToolStarted", "ToolFinished", "NeedsApproval", "TodosUpdated",
    "Notice", "UsageUpdate", "Compacted", "BackgroundDone", "ReloadRequested", "TurnDone",
    "TurnError", "Cancelled", "to_wire", "Decision", "Policy", "Rules", "Hooks", "HookResult",
    "Tool", "ToolContext", "ToolOutput", "tool", "builtin_tools", "Workspace", "LocalSandbox",
    "DockerSandbox", "Compactor", "ModelRoles", "SubagentSpec", "SubagentRunner", "task_tool",
    "BackgroundTasks", "AgentManager", "Usage", "LunaCoreError", "ProviderError", "ToolError",
    "providers", "__version__",
}  # fmt: skip


def test_public_api_is_exactly_the_documented_set():
    assert set(luna_core.__all__) == EXPECTED
    assert len(luna_core.__all__) == len(set(luna_core.__all__))
    for name in EXPECTED:
        assert getattr(luna_core, name) is not None


def test_every_public_callable_has_a_docstring():
    missing = [
        n
        for n in luna_core.__all__
        if callable(getattr(luna_core, n)) and not (getattr(luna_core, n).__doc__ or "").strip()
    ]
    assert missing == []
