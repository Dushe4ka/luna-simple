"""A tiny MCP server for tests (stdio)."""

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

server = FastMCP("echo")


@server.tool(annotations={"readOnlyHint": True})
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


@server.tool()
def fail(reason: str) -> str:
    """Always fails."""
    raise ValueError(reason)


@server.tool()
def pixel() -> Image:
    """Return a 1x1 PNG."""
    import base64

    return Image(
        data=base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        ),
        format="png",
    )


if __name__ == "__main__":
    server.run("stdio")
