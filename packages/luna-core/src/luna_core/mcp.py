"""MCP client: tools from MCP servers as core tools (needs the ``mcp`` extra)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import webbrowser
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from luna_core.errors import LunaCoreError
from luna_core.messages import Image, Text
from luna_core.tools.base import Tool, ToolContext, ToolOutput

INSTALL_HINT = 'MCP support is not installed: pip install "luna-core[mcp]"'
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME = re.compile(r"[^A-Za-z0-9_-]")


class MCPConfigError(LunaCoreError):
    """An MCP server definition is invalid."""


def expand_env(value, environ: Mapping[str, str]):
    """Replace ``${VAR}`` in strings, recursively."""
    if isinstance(value, str):
        return _ENV.sub(lambda m: environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [expand_env(v, environ) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, environ) for k, v in value.items()}
    return value


def _read(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise MCPConfigError(f"cannot read {path}: {exc}") from exc
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}


def load_config(files: list[Path], environ: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Merge ``mcpServers`` from ``files`` (later wins), expanding ``${VAR}``."""
    env = os.environ if environ is None else environ
    merged: dict[str, dict] = {}
    for path in files:
        merged.update(_read(path))
    return {name: expand_env(spec, env) for name, spec in merged.items()}


def add_server(path: Path, name: str, spec: dict) -> Path:
    """Write ``mcpServers.<name>`` into ``path``."""
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    doc.setdefault("mcpServers", {})[name] = spec
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def remove_server(path: Path, name: str) -> bool:
    """Delete ``mcpServers.<name>`` from ``path``."""
    path = Path(path)
    if not path.exists():
        return False
    doc = json.loads(path.read_text(encoding="utf-8"))
    if name not in doc.get("mcpServers", {}):
        return False
    del doc["mcpServers"][name]
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def transport_of(name: str, spec: dict) -> str:
    """``stdio``, ``http`` or ``sse`` for a server spec."""
    kind = spec.get("type") or spec.get("transport")
    if "command" in spec and kind in (None, "stdio"):
        return "stdio"
    if kind in ("http", "streamable_http", "streamable-http") and spec.get("url"):
        return "http"
    if kind == "sse" and spec.get("url"):
        return "sse"
    raise MCPConfigError(
        f"MCP server {name!r}: need a 'command' (stdio) or a 'url' with type 'http' or 'sse'"
    )


class FileTokenStorage:
    """OAuth tokens and client registration for one server, as a private JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data).encode("utf-8")
        # Create (or truncate) the file with 0600 atomically via the ``open(2)`` mode
        # argument, and ``fchmod`` it regardless (the mode argument is ignored by the
        # OS when the file already exists) — tokens must never sit, even briefly, in
        # a file whose permissions are looser than 0600.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, payload)
        finally:
            os.close(fd)

    async def get_tokens(self):
        """Return the stored tokens, or ``None``."""
        from mcp.shared.auth import OAuthToken

        raw = self._load().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        """Store tokens."""
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._save(data)

    async def get_client_info(self):
        """Return the stored dynamic client registration, or ``None``."""
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        """Store the client registration."""
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save(data)


async def _callback_listener() -> tuple[str, asyncio.Future]:
    """Start a one-shot OAuth redirect listener; return (redirect_uri, future of (code, state))."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readline()).decode("latin-1")
        target = request.split(" ")[1] if " " in request else "/"
        query = parse_qs(urlparse(target).query)
        body = (
            "Luna: authorisation finished, you can close this tab."
            if "code" in query
            else "Luna: authorisation failed."
        )
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body.encode())}\r\n\r\n{body}".encode()
        )
        await writer.drain()
        writer.close()
        if result.done():
            return
        if "code" in query:
            result.set_result((query["code"][0], query.get("state", [None])[0]))
        elif "error" in query:
            detail = query.get("error_description", query["error"])[0]
            result.set_exception(MCPConfigError(f"authorisation denied: {detail}"))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    result.add_done_callback(lambda _f: server.close())
    return f"http://127.0.0.1:{port}/callback", result


class MCPManager:
    """Connects to MCP servers and exposes their tools."""

    def __init__(
        self,
        servers: dict[str, dict],
        *,
        on_warn: Callable[[str], None] = print,
        connect_timeout: float = 30.0,
        call_timeout: float = 120.0,
        token_dir: Path | None = None,
        open_browser: Callable[[str], object] | None = None,
    ) -> None:
        self.servers = dict(servers)
        self.on_warn = on_warn
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self.token_dir = (
            Path(token_dir) if token_dir else Path.home() / ".config" / "luna" / "mcp-tokens"
        )
        self.open_browser = open_browser or webbrowser.open
        self.tools: list[Tool] = []
        self._status: dict[str, str] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def status(self) -> dict[str, str]:
        """Return ``connected`` or ``failed: …`` per server."""
        return dict(self._status)

    async def start(self) -> list[Tool]:
        """Connect to every server (concurrently) and collect their tools."""
        try:
            import mcp  # noqa: F401
        except ImportError:
            if self.servers:
                self.on_warn(INSTALL_HINT)
            return []
        results = await asyncio.gather(
            *(self._connect(name, spec) for name, spec in self.servers.items())
        )
        self.tools = [tool for tools in results for tool in tools]
        return self.tools

    async def _connect(self, name: str, spec: dict) -> list[Tool]:
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        # A timed-out ``ready`` may still get an exception set on it later (by
        # ``_serve``'s cleanup, once cancelled) that nothing then awaits; retrieve it
        # quietly so asyncio never logs "exception was never retrieved".
        ready.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        stop = asyncio.Event()
        self._stops[name] = stop
        task = asyncio.create_task(self._serve(name, spec, ready, stop))
        self._tasks[name] = task
        try:
            session, tools = await asyncio.wait_for(ready, self.connect_timeout)
        except Exception as exc:  # noqa: BLE001 - one bad server must not break the agent
            message = str(exc) or type(exc).__name__
            self._status[name] = f"failed: {message}"
            self.on_warn(f"MCP server {name!r} did not start: {message}")
            stop.set()
            # A server stuck mid-connect (hung subprocess, stalled OAuth flow) is
            # still running at this point; cancel it and wait for the transport's
            # context managers to unwind so it never keeps running after "failed".
            if not task.done():
                task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
            return []
        self._status[name] = "connected"
        return [self._tool(name, session, t) for t in tools]

    async def _serve(
        self, name: str, spec: dict, ready: asyncio.Future, stop: asyncio.Event
    ) -> None:
        from mcp import ClientSession, StdioServerParameters

        try:
            async with AsyncExitStack() as stack:
                kind = transport_of(name, spec)
                if kind == "stdio":
                    from mcp.client.stdio import stdio_client

                    # Pass ``spec["env"]`` through unchanged: the SDK itself merges it
                    # with a safe default environment (PATH, HOME, ...). Merging the
                    # *parent* process's full ``os.environ`` here would leak secrets
                    # (API keys, tokens) to any server that declares even one env var.
                    params = StdioServerParameters(
                        command=spec["command"],
                        args=list(spec.get("args", [])),
                        env=spec.get("env"),
                        cwd=spec.get("cwd"),
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                elif kind == "http":
                    from mcp.client.streamable_http import streamablehttp_client

                    auth = await self._oauth(name, spec) if spec.get("oauth") else None
                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(spec["url"], headers=spec.get("headers"), auth=auth)
                    )
                else:
                    from mcp.client.sse import sse_client

                    read, write = await stack.enter_async_context(
                        sse_client(spec["url"], headers=spec.get("headers"))
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                tools, cursor = [], None
                while True:
                    page = await session.list_tools(cursor=cursor)
                    tools.extend(page.tools)
                    cursor = getattr(page, "nextCursor", None)
                    if not cursor:
                        break
                ready.set_result((session, tools))
                await stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported through ``ready``
            if not ready.done():
                ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            if isinstance(exc, asyncio.CancelledError):
                raise

    def _token_file(self, name: str) -> Path:
        """Return server ``name``'s OAuth token file; refuse a name leaving the token dir."""
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise MCPConfigError(f"MCP server name {name!r} cannot name a token file")
        path = self.token_dir / f"{name}.json"
        if path.resolve().parent != self.token_dir.resolve():
            raise MCPConfigError(f"MCP server name {name!r} cannot name a token file")
        return path

    async def _oauth(self, name: str, spec: dict):
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata
        from pydantic import AnyUrl

        token_file = self._token_file(name)
        redirect_uri, code_future = await _callback_listener()

        async def redirect(url: str) -> None:
            self.on_warn(f"MCP server {name!r} needs authorisation; opening {url}")
            self.open_browser(url)

        async def callback() -> tuple[str, str | None]:
            return await code_future

        metadata = OAuthClientMetadata(
            client_name="Luna",
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=spec.get("scope"),
        )
        server_url = (
            spec["url"].rsplit("/mcp", 1)[0] if spec["url"].endswith("/mcp") else spec["url"]
        )
        return OAuthClientProvider(
            server_url=server_url,
            client_metadata=metadata,
            storage=FileTokenStorage(token_file),
            redirect_handler=redirect,
            callback_handler=callback,
        )

    def _tool(self, server: str, session, tool) -> Tool:
        timeout = self.call_timeout

        async def run(ctx: ToolContext, args: dict) -> ToolOutput:
            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool.name, arguments=args), timeout
                )
            except TimeoutError:
                return ToolOutput.error(f"{server}.{tool.name} timed out after {timeout:g}s")
            parts = []
            for item in result.content:
                kind = getattr(item, "type", "")
                if kind == "text":
                    parts.append(Text(item.text))
                elif kind == "image":
                    parts.append(Image(item.mimeType, item.data))
                else:
                    parts.append(
                        Text(json.dumps(item.model_dump(mode="json"), ensure_ascii=False)[:5000])
                    )
            if not parts and getattr(result, "structuredContent", None):
                parts.append(Text(json.dumps(result.structuredContent, ensure_ascii=False)))
            return ToolOutput(
                tuple(parts) or (Text("(no output)"),),
                bool(result.isError),
                f"{server}.{tool.name}",
            )

        annotations = getattr(tool, "annotations", None)
        read_only = bool(annotations and getattr(annotations, "readOnlyHint", False))
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        name = _NAME.sub("_", f"{server}_{tool.name}")[:64]
        return Tool(
            name,
            (tool.description or tool.name)[:1024],
            schema,
            run,
            raw=True,
            mutating=not read_only,
        )

    async def close(self) -> None:
        """Disconnect every server."""
        for stop in self._stops.values():
            stop.set()
        tasks = list(self._tasks.values())
        done, pending = await asyncio.wait(tasks, timeout=5) if tasks else (set(), set())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
