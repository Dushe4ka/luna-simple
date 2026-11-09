"""A scripted stand-in for luna_core.client.LunaClient in TUI tests."""

import asyncio


class FakeLunaClient:
    def __init__(self, turns=None, sessions=None, agents=None, attach_events=None):
        self.turns = list(turns or [])  # each: list of event dicts for one send()
        self.sessions = sessions or [
            {
                "id": "s1",
                "title": "first",
                "updated": 0,
                "group": "today",
                "relative_time": "сейчас",
                "running": False,
                "background": False,
            }
        ]
        self.agents_rows = agents or {"runs": [], "tasks": []}
        self.attach_events = list(attach_events or [])  # each: list of events for one attach()
        self.calls = []
        self.decisions = []
        self.cancelled = []
        self.background_calls = []
        self.attached = []
        self.command_results = {}  # line -> result, or a list of results (one per call)
        self.command_lines = []
        self.keys_set = []  # (provider, workdir) — the key value itself is not kept
        self.set_key_error = None

    async def list_sessions(self, workdir):
        return self.sessions

    async def create_session(self, workdir):
        return "s-new"

    async def history(self, session_id, workdir):
        return []

    async def commands(self, workdir):
        return [
            {"name": "/help", "help": "show this help", "kind": "ui"},
            {"name": "/usage", "help": "usage", "kind": "read"},
        ]

    async def state(self, session_id, workdir):
        return {
            "provider": "p",
            "model": "m",
            "plan": False,
            "pinned": [],
            "usage_summary": "",
            "undo_depth": 2,
            "running": False,
        }

    async def send(self, session_id, text, workdir, *, images=(), reload_after=False):
        self.calls.append((session_id, text))
        default = [{"event": "turn_done", "usage": {}, "cost": None, "stop_reason": "end"}]
        for event in self.turns.pop(0) if self.turns else default:
            if event.get("event") == "_pause":
                await asyncio.sleep(event["seconds"])
                continue
            yield event

    async def attach(self, session_id, *, after=-1):
        self.attached.append((session_id, after))
        for event in self.attach_events.pop(0) if self.attach_events else []:
            yield event

    async def decide(self, session_id, call_id, decision):
        self.decisions.append((call_id, decision))

    async def cancel(self, session_id):
        self.cancelled.append(session_id)
        return True

    async def run_command(self, session_id, line, workdir):
        empty = {
            "text": "",
            "notices": [],
            "choice": None,
            "confirm": None,
            "prompt": None,
            "effects": {},
        }
        self.command_lines.append(line)
        result = self.command_results.get(line, empty)
        if isinstance(result, list):
            return result.pop(0) if result else empty
        return result

    async def set_key(self, provider, key, workdir):
        if self.set_key_error is not None:
            raise self.set_key_error
        self.keys_set.append((provider, workdir))
        self.last_key_len = len(key)
        return {"ok": True, "provider": provider}

    async def background(self, session_id, text, workdir):
        self.background_calls.append(text)
        return {"session_id": session_id, "status": "running"}

    async def agents(self):
        return self.agents_rows

    async def aclose(self):
        pass


def make_app(**kwargs):
    """A ``LunaApp`` wired to an in-process server (no network) in a temp workdir."""
    import tempfile

    import httpx

    from luna.tui.app import LunaApp
    from tests.server_helpers import StreamingASGITransport
    from tests.server_helpers import make_app as make_server

    app = LunaApp(
        base_url="http://test",
        token="t",
        workdir=tempfile.mkdtemp(prefix="luna-tui-"),
        thread_id="t1",
        **kwargs,
    )
    app.client._http = httpx.AsyncClient(
        transport=StreamingASGITransport(make_server()), base_url="http://test"
    )
    return app
