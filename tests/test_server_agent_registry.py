"""The server's per-workdir agent registry (C3+C4).

Two properties the single shared, in-memory-checkpointed agent broke:

* an agent's filesystem root is fixed at build time, so two different
  workdirs must never share one agent — otherwise a server started in
  project A answers project B's requests while still reading and writing
  A's files;
* history must outlive the server process, since the TUI's whole resume
  story rests on the same ``sessions.db`` the CLI uses.
"""

import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from luna.server.app import create_app
from luna.server.run import make_agent_factory


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for block in raw.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def _project(path, name: str):
    """A project directory whose own .luna.toml turns approvals off.

    Reading that file at all also exercises ``load_config(..., cwd=...)``:
    without the ``cwd`` argument the project's own ``.luna.toml`` would be
    ignored and every write below would pause on an approval interrupt.
    """
    path.mkdir(parents=True, exist_ok=True)
    (path / ".luna.toml").write_text("[agent]\nyolo = true\n")
    (path / "marker.txt").write_text(name)
    return path


def _write_then_done(*, times: int = 1):
    """Scripted model turns: a write_file call, then a final answer, ×times."""
    messages = []
    for _ in range(times):
        messages.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "1",
                        "args": {"file_path": "/touched.txt", "content": "written\n"},
                    }
                ],
            )
        )
        messages.append(AIMessage(content="done"))
    return messages


@pytest.fixture
async def registry_client(tmp_path, monkeypatch, fake_model):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    project_a = _project(tmp_path / "a", "a")
    project_b = _project(tmp_path / "b", "b")
    app = create_app(
        agent_factory=make_agent_factory(model=fake_model(*_write_then_done(times=2))),
        token="secret-token",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret-token"},
    ) as c:
        yield c, project_a, project_b


async def _send(c, thread_id: str, content: str, workdir) -> list[dict]:
    async with c.stream(
        "POST",
        f"/sessions/{thread_id}/messages",
        json={"content": content, "workdir": str(workdir)},
    ) as resp:
        assert resp.status_code == 200
        return _parse_sse("".join([chunk async for chunk in resp.aiter_text()]))


async def test_each_workdir_gets_its_own_agent_rooted_at_that_workdir(registry_client):
    """A write for project B must land in B, never in A.

    Before the fix one cached agent — built from ``load_config({})`` against
    the *server process's* cwd — served every request, so whichever project
    happened to start the server owned every file operation forever.
    """
    c, project_a, project_b = registry_client

    await _send(c, "ta", "write it", project_a)
    assert (project_a / "touched.txt").read_text() == "written\n"
    assert not (project_b / "touched.txt").exists()

    await _send(c, "tb", "write it", project_b)
    assert (project_b / "touched.txt").read_text() == "written\n"
    # …and project A's copy was not rewritten by project B's request
    assert (project_a / "marker.txt").read_text() == "a"
    assert (project_b / "marker.txt").read_text() == "b"


async def test_two_workdirs_never_share_a_filesystem_root(tmp_path, monkeypatch, fake_model):
    """The same property, asserted on the agents themselves rather than HTTP."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    project_a = _project(tmp_path / "a", "a")
    project_b = _project(tmp_path / "b", "b")

    factory = make_agent_factory(model=fake_model(*_write_then_done(times=2)))
    agent_a = factory(str(project_a))
    agent_b = factory(str(project_b))

    assert agent_a is not agent_b
    assert factory(str(project_a)) is agent_a  # cached per resolved workdir

    agent_a.invoke(
        {"messages": [{"role": "user", "content": "write"}]},
        {"configurable": {"thread_id": "ra"}},
    )
    assert (project_a / "touched.txt").exists()
    assert not (project_b / "touched.txt").exists()

    agent_b.invoke(
        {"messages": [{"role": "user", "content": "write"}]},
        {"configurable": {"thread_id": "rb"}},
    )
    assert (project_b / "touched.txt").exists()


async def test_history_survives_a_server_restart(tmp_path, monkeypatch, fake_model):
    """A second agent built against the same sessions.db still sees the turn.

    The old factory never passed a checkpointer, so ``build_agent`` fell back
    to ``InMemorySaver`` and every session's history died with the process —
    contradicting the TUI's documented "same --resume/SQLite persistence"
    promise.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    project = _project(tmp_path / "proj", "p")
    config = {"configurable": {"thread_id": "persisted"}}

    first = make_agent_factory(model=fake_model(AIMessage(content="remembered")))(str(project))
    first.invoke({"messages": [{"role": "user", "content": "hello there"}]}, config)

    # A brand-new registry and a brand-new agent = a restarted server.
    second = make_agent_factory(model=fake_model(AIMessage(content="whatever")))(str(project))
    messages = second.get_state(config).values.get("messages", [])

    texts = [getattr(m, "content", "") for m in messages]
    assert any("hello there" in t for t in texts)
    assert any("remembered" in t for t in texts)
