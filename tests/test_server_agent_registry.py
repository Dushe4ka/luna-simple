"""One agent per session, rooted at that session's project (C3+C4).

* an agent's filesystem root is fixed at build time, so sessions of two
  workdirs must never share one agent — otherwise a server started in
  project A answers project B's requests while reading and writing A's files;
* history must outlive the server process, since the TUI's resume story
  rests on the same ``sessions.db`` the CLI uses.
"""

from pathlib import Path

from luna_core.providers.fake import FakeProvider

from luna.core import store
from tests.server_helpers import client, make_app, turn


def _project(path: Path, name: str) -> Path:
    """A project whose own .luna.toml turns approvals off (also exercises ``cwd=``)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".luna.toml").write_text("[agent]\nyolo = true\n")
    (path / "marker.txt").write_text(name)
    return path


def _write_then_done():
    return [
        FakeProvider.call("write_file", {"file_path": "touched.txt", "content": "written\n"}),
        FakeProvider.text("done"),
    ]


async def test_each_workdir_gets_its_own_agent_rooted_at_that_workdir(tmp_path):
    project_a = _project(tmp_path / "a", "a")
    project_b = _project(tmp_path / "b", "b")
    app = make_app(default=_write_then_done())
    async with client(app) as c:
        await turn(c, "ta", project_a, "write it")
        assert (project_a / "touched.txt").read_text() == "written\n"
        assert not (project_b / "touched.txt").exists()

        await turn(c, "tb", project_b, "write it")
    assert (project_b / "touched.txt").read_text() == "written\n"
    assert (project_a / "marker.txt").read_text() == "a"
    assert (project_b / "marker.txt").read_text() == "b"


async def test_two_workdirs_never_share_a_filesystem_root(tmp_path):
    project_a = _project(tmp_path / "a", "a")
    project_b = _project(tmp_path / "b", "b")
    app = make_app()
    async with client(app):
        one = await app.state.runtimes.get("ra", str(project_a))
        two = await app.state.runtimes.get("rb", str(project_b))
        agent_a, agent_b = (await one.bundle()).agent, (await two.bundle()).agent
        assert (await one.bundle()).agent is agent_a  # built once per session
    assert agent_a is not agent_b
    assert agent_a.workspace.root == project_a.resolve()
    assert agent_b.workspace.root == project_b.resolve()


async def test_history_survives_a_server_restart(tmp_path):
    """A second server over the same sessions.db still shows the turn."""
    project = _project(tmp_path / "proj", "p")
    first = make_app(default=[FakeProvider.text("remembered")])
    async with client(first) as c:
        await turn(c, "persisted", project, "hello there")
    for log in store._LOGS.values():  # the process ends: its database handle closes
        log.close()
    store._LOGS.clear()

    second = make_app(default=[FakeProvider.text("whatever")])
    async with client(second) as c:
        resp = await c.get("/sessions/persisted/messages", params={"workdir": str(project)})
    assert resp.json()["messages"] == [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "remembered"},
    ]


async def test_a_session_id_used_with_another_workdir_is_refused(tmp_path):
    project_a = _project(tmp_path / "a", "a")
    project_b = _project(tmp_path / "b", "b")
    app = make_app()
    async with client(app) as c:
        await turn(c, "t1", project_a, "hi")
        resp, _ = await turn(c, "t1", project_b, "hi")
    assert resp.status_code == 400 and resp.json() == {"error": "workdir_mismatch"}
