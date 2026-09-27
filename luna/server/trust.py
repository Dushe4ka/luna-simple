"""Per-request workdir trust enforcement for the local server.

The server is long-lived and shared across projects, so it re-checks trust
on every request instead of trusting whatever the client claims — a future
web/desktop client must not be able to bypass the CLI's trust prompt.
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse


def trust_error(request: Request, workdir: str | None) -> JSONResponse | None:
    """Return an error response if ``workdir`` may not be used, else ``None``.

    No-op when the app was built without a ``trust_check`` (tests, embedding).
    """
    check = getattr(request.app.state, "trust_check", None)
    if check is None:
        return None
    if not workdir:
        return JSONResponse({"error": "workdir_required"}, status_code=400)
    if not Path(workdir).is_absolute():
        # The server is shared by every project, so "." would resolve against
        # *its* cwd — whichever project started it — and mix projects' data.
        return JSONResponse(
            {"error": "workdir_not_absolute", "workdir": workdir}, status_code=400
        )
    if not check(workdir):
        return JSONResponse(
            {"error": "workdir_not_trusted", "workdir": workdir}, status_code=403
        )
    return None
