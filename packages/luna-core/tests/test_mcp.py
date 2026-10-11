import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from luna_core.mcp import (
    FileTokenStorage,
    MCPConfigError,
    MCPManager,
    add_server,
    load_config,
    remove_server,
    transport_of,
)  # noqa: E402
from luna_core.messages import Image  # noqa: E402
from luna_core.tools.base import ToolContext  # noqa: E402

SERVER = str(Path(__file__).with_name("mcp_echo_server.py"))


def test_config_merge_env_and_edit(tmp_path):
    user, proj = tmp_path / "user.json", tmp_path / "proj.json"
    user.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "a": {"command": "x"},
                    "b": {"command": "${TOOL}", "args": ["--k=${KEY}"]},
                }
            }
        )
    )
    proj.write_text(json.dumps({"mcpServers": {"a": {"type": "http", "url": "https://h/mcp"}}}))
    cfg = load_config([user, proj, tmp_path / "missing.json"], environ={"TOOL": "npx", "KEY": "s3"})
    assert cfg["a"]["url"] == "https://h/mcp" and cfg["b"] == {"command": "npx", "args": ["--k=s3"]}
    add_server(proj, "c", {"command": "uvx", "args": ["thing"]})
    assert "c" in json.loads(proj.read_text())["mcpServers"]
    assert remove_server(proj, "c") and not remove_server(proj, "c")
    assert transport_of("a", cfg["a"]) == "http" and transport_of("b", cfg["b"]) == "stdio"
    with pytest.raises(MCPConfigError):
        transport_of("bad", {"type": "http"})


async def test_stdio_server_tools_round_trip():
    manager = MCPManager(
        {"echo": {"command": sys.executable, "args": [SERVER]}}, on_warn=pytest.fail
    )
    tools = {t.name: t for t in await manager.start()}
    try:
        assert {"echo_add", "echo_fail", "echo_pixel"} <= set(tools)
        assert not tools["echo_add"].mutating and tools["echo_fail"].mutating
        assert tools["echo_add"].parameters["required"] == ["a", "b"]
        out = await tools["echo_add"].invoke(ToolContext(), {"a": 2, "b": 3})
        assert out.content[0].text == "5" and not out.is_error
        err = await tools["echo_fail"].invoke(ToolContext(), {"reason": "nope"})
        assert err.is_error and "nope" in err.content[0].text
        img = await tools["echo_pixel"].invoke(ToolContext(), {})
        assert isinstance(img.content[0], Image) and img.content[0].media_type == "image/png"
        assert manager.status() == {"echo": "connected"}
    finally:
        await manager.close()


async def test_broken_server_is_skipped_with_a_warning(tmp_path):
    warnings = []
    manager = MCPManager(
        {"ghost": {"command": str(tmp_path / "does-not-exist")}},
        on_warn=warnings.append,
        connect_timeout=5,
    )
    assert await manager.start() == []
    assert "ghost" in warnings[0] and manager.status()["ghost"].startswith("failed")
    await manager.close()


async def test_stdio_env_does_not_leak_parent_environment(monkeypatch):
    monkeypatch.setenv("LUNA_TEST_SECRET", "s3cr3t")
    manager = MCPManager(
        {"echo": {"command": sys.executable, "args": [SERVER], "env": {"FOO": "bar"}}},
        on_warn=pytest.fail,
    )
    tools = {t.name: t for t in await manager.start()}
    try:
        secret = await tools["echo_env_var"].invoke(ToolContext(), {"name": "LUNA_TEST_SECRET"})
        foo = await tools["echo_env_var"].invoke(ToolContext(), {"name": "FOO"})
        assert secret.content[0].text == ""
        assert foo.content[0].text == "bar"
    finally:
        await manager.close()


async def test_connect_failure_cancels_and_cleans_up_the_server_task():
    manager = MCPManager(
        {"slow": {"command": sys.executable, "args": ["-c", "import time; time.sleep(30)"]}},
        on_warn=lambda _msg: None,
        connect_timeout=1,
    )
    assert await asyncio.wait_for(manager.start(), timeout=10) == []
    assert manager.status()["slow"].startswith("failed")
    assert manager._tasks["slow"].done()
    await asyncio.wait_for(manager.close(), timeout=10)


async def test_oauth_callback_listener_rejects_error_redirect():
    from luna_core.mcp import MCPConfigError, _callback_listener

    redirect_uri, result = await _callback_listener()
    port = int(redirect_uri.rsplit(":", 1)[1].split("/")[0])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /callback?error=access_denied HTTP/1.1\r\n\r\n")
    await writer.drain()
    await reader.read(200)
    writer.close()
    with pytest.raises(MCPConfigError):
        await asyncio.wait_for(result, timeout=5)
    with pytest.raises(OSError):
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1)


async def test_token_storage_round_trip(tmp_path):
    from mcp.shared.auth import OAuthToken

    storage = FileTokenStorage(tmp_path / "tok" / "srv.json")
    assert await storage.get_tokens() is None
    await storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer"))
    again = FileTokenStorage(tmp_path / "tok" / "srv.json")
    assert (await again.get_tokens()).access_token == "a"
    assert stat.S_IMODE((tmp_path / "tok" / "srv.json").stat().st_mode) == 0o600


async def test_token_file_tightens_existing_loose_permissions(tmp_path):
    from mcp.shared.auth import OAuthToken

    path = tmp_path / "srv.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    storage = FileTokenStorage(path)
    await storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_token_storage_never_uses_write_text(tmp_path, monkeypatch):
    from mcp.shared.auth import OAuthToken

    def boom(*_args, **_kwargs):
        raise AssertionError("token files must not go through Path.write_text")

    monkeypatch.setattr(Path, "write_text", boom)
    storage = FileTokenStorage(tmp_path / "srv.json")
    await storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer"))
    assert stat.S_IMODE((tmp_path / "srv.json").stat().st_mode) == 0o600
