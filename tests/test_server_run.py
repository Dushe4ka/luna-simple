import subprocess
import threading
import time

from luna.server.auth import read_token_file, token_path, write_token_file
from luna.server.run import ensure_running, log_path


def test_ensure_running_reuses_a_live_server_without_spawning(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    write_token_file(port=12345, token="existing", pid=999, fingerprint=1.0)
    spawn_calls = []

    result = ensure_running(
        str(tmp_path),
        on_warn=lambda m: None,
        _is_alive=lambda pid: True,
        _spawn=lambda port, token: spawn_calls.append((port, token)),
        _fingerprint=lambda: 1.0,
    )

    assert result == {"port": 12345, "token": "existing", "pid": 999, "fingerprint": 1.0}
    assert spawn_calls == []


def test_ensure_running_restarts_a_live_server_whose_code_is_stale(tmp_path, monkeypatch):
    """A server can be perfectly alive and still be running outdated code —
    e.g. its process was spawned before the last local edit. It must be
    killed and replaced, not reused, or that edit silently never takes
    effect for as long as the old process keeps running.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    write_token_file(port=12345, token="old", pid=999, fingerprint=1.0)
    spawn_calls = []
    kill_calls = []
    warnings = []

    def fake_spawn(port, token):
        spawn_calls.append((port, token))
        write_token_file(port=port, token=token, pid=4242, fingerprint=2.0)

    result = ensure_running(
        str(tmp_path),
        on_warn=warnings.append,
        _is_alive=lambda pid: True,
        _spawn=fake_spawn,
        _fingerprint=lambda: 2.0,  # code on disk now differs from the recorded 1.0
        _kill=lambda pid: kill_calls.append(pid),
    )

    assert kill_calls == [999]
    assert len(spawn_calls) == 1
    assert result["pid"] == 4242
    assert warnings  # the user is told why a restart just happened


def test_ensure_running_never_treats_a_missing_fingerprint_as_a_match(tmp_path, monkeypatch):
    """A token file written before this feature existed has no
    ``fingerprint`` key at all — that must count as stale too, not as a
    free pass, or every server running when this shipped would be
    reused forever regardless of what changes afterwards.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    write_token_file(port=12345, token="old", pid=999)  # no fingerprint at all
    spawn_calls = []

    def fake_spawn(port, token):
        spawn_calls.append((port, token))
        write_token_file(port=port, token=token, pid=4242, fingerprint=2.0)

    result = ensure_running(
        str(tmp_path),
        on_warn=lambda m: None,
        _is_alive=lambda pid: True,
        _spawn=fake_spawn,
        _fingerprint=lambda: 2.0,
        _kill=lambda pid: None,
    )

    assert len(spawn_calls) == 1
    assert result["pid"] == 4242


def test_ensure_running_spawns_when_token_file_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    write_token_file(port=12345, token="stale", pid=999)
    spawn_calls = []

    def fake_spawn(port, token):
        spawn_calls.append((port, token))
        # Simulate what the real background process does on startup.
        write_token_file(port=port, token=token, pid=4242)

    result = ensure_running(
        str(tmp_path),
        on_warn=lambda m: None,
        _is_alive=lambda pid: False,
        _spawn=fake_spawn,
    )

    assert len(spawn_calls) == 1
    new_port, new_token = spawn_calls[0]
    assert result == {"port": new_port, "token": new_token, "pid": 4242}
    assert result != {"port": 12345, "token": "stale", "pid": 999}
    assert read_token_file() == result


def test_ensure_running_deletes_the_stale_token_file_before_spawning(tmp_path, monkeypatch):
    """The dead server's credentials must be gone by the time we spawn.

    Otherwise a read that happens before the new process has written its own
    file hands the caller a dead port+token — and that port number may by now
    belong to an entirely unrelated process.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    write_token_file(port=12345, token="stale", pid=999)
    seen_at_spawn_time = []

    def fake_spawn(port, token):
        seen_at_spawn_time.append(read_token_file())
        write_token_file(port=port, token=token, pid=4242)

    result = ensure_running(
        str(tmp_path),
        on_warn=lambda m: None,
        _is_alive=lambda pid: False,
        _spawn=fake_spawn,
    )

    assert seen_at_spawn_time == [None]  # stale file already unlinked
    assert result["token"] != "stale"
    assert result["pid"] == 4242


def test_ensure_running_waits_for_the_new_servers_token_file(tmp_path, monkeypatch):
    """A real spawn is asynchronous — the returned dict must still be the new one.

    Regression: ``ensure_running`` used to ``read_token_file()`` exactly once
    right after spawning, so with a real (async) ``subprocess.Popen`` it
    returned whatever was on disk at that instant — the dead server's
    port+token. This fake writes the new file slightly late, the way a real
    background process does.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    write_token_file(port=12345, token="stale", pid=999)
    spawn_calls = []

    def fake_spawn(port, token):
        spawn_calls.append((port, token))

        def later():
            time.sleep(0.15)
            write_token_file(port=port, token=token, pid=4242)

        threading.Thread(target=later, daemon=True).start()

    result = ensure_running(
        str(tmp_path),
        on_warn=lambda m: None,
        _is_alive=lambda pid: False,
        _spawn=fake_spawn,
    )

    new_port, new_token = spawn_calls[0]
    assert result == {"port": new_port, "token": new_token, "pid": 4242}
    assert result["token"] != "stale"
    assert result["port"] != 12345


def test_ensure_running_redirects_the_real_spawns_output_to_a_log_file(tmp_path, monkeypatch):
    """Regression: the background server used to run with stdout AND stderr
    both DEVNULL — a real incident hit this exactly: a provider call failed
    deep inside the server process and there was no way to see why, because
    the detached process's output went nowhere a person could ever read it.
    Exercises the *real* (non-injected) ``subprocess.Popen`` branch, with
    only ``Popen`` itself faked out so no process actually spawns.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    captured = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured["kwargs"] = kwargs
            port = args[args.index("--port") + 1]
            token = args[args.index("--token") + 1]
            write_token_file(port=int(port), token=token, pid=4242)

    monkeypatch.setattr("luna.server.run.subprocess.Popen", _FakePopen)

    result = ensure_running(str(tmp_path), on_warn=lambda m: None, _is_alive=lambda pid: False)

    assert result["pid"] == 4242
    stdout = captured["kwargs"]["stdout"]
    stderr = captured["kwargs"]["stderr"]
    assert stdout is not subprocess.DEVNULL
    assert stderr is not subprocess.DEVNULL
    assert stdout is stderr  # combined stdout+stderr into one log
    assert log_path().exists()


def test_ensure_running_never_returns_a_dead_servers_credentials(tmp_path, monkeypatch):
    """If the spawned server never comes up, report the *new* port/token.

    Never the stale ones: connecting to a dead server's port is at best a
    connection error and at worst a completely unrelated process.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    write_token_file(port=12345, token="stale", pid=999)
    monkeypatch.setattr("luna.server.run.time.sleep", lambda _s: None)
    ticks = iter(range(0, 1000))
    monkeypatch.setattr("luna.server.run.time.monotonic", lambda: next(ticks))
    spawn_calls = []

    result = ensure_running(
        str(tmp_path),
        on_warn=lambda m: None,
        _is_alive=lambda pid: False,
        _spawn=lambda port, token: spawn_calls.append((port, token)),
    )

    new_port, new_token = spawn_calls[0]
    assert result == {"port": new_port, "token": new_token, "pid": 0}
    assert result != {"port": 12345, "token": "stale", "pid": 999}
    assert not token_path().exists()


def test_ensure_running_spawns_the_shared_server_outside_any_project(tmp_path, monkeypatch):
    """Regression: the server inherited the launching project's cwd; deleting
    that folder later made every request's path resolution crash with
    FileNotFoundError (500s for all projects)."""
    from pathlib import Path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    captured = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured["kwargs"] = kwargs
            port = args[args.index("--port") + 1]
            token = args[args.index("--token") + 1]
            write_token_file(port=int(port), token=token, pid=4242)

    monkeypatch.setattr("luna.server.run.subprocess.Popen", _FakePopen)
    ensure_running(str(tmp_path), on_warn=lambda m: None, _is_alive=lambda pid: False)
    assert captured["kwargs"]["cwd"] == str(Path.home())
