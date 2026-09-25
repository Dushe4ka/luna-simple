import threading
import time

from luna.server.auth import read_token_file, token_path, write_token_file
from luna.server.run import ensure_running


def test_ensure_running_reuses_a_live_server_without_spawning(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    write_token_file(port=12345, token="existing", pid=999)
    spawn_calls = []

    result = ensure_running(
        str(tmp_path),
        on_warn=lambda m: None,
        _is_alive=lambda pid: True,
        _spawn=lambda port, token: spawn_calls.append((port, token)),
    )

    assert result == {"port": 12345, "token": "existing", "pid": 999}
    assert spawn_calls == []


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
