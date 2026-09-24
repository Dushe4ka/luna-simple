from luna.server.auth import read_token_file, write_token_file
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
