import asyncio
import json
import sys

from luna_core.hooks import CommandHook, HookResult, Hooks, load_command_hooks


async def test_chain_modifies_args_and_first_block_wins():
    hooks = Hooks()
    seen = []

    async def add_flag(payload):
        seen.append(dict(payload["args"]))
        return HookResult(args={**payload["args"], "timeout": 5})

    async def blocker(payload):
        seen.append(dict(payload["args"]))
        return HookResult(block=True, message="no rm")

    async def never(payload):
        raise AssertionError("not reached")

    hooks.add("pre_tool", add_flag, match="execute")
    hooks.add("pre_tool", blocker, match="exec*")
    hooks.add("pre_tool", never)
    res = await hooks.run("pre_tool", {"args": {"command": "rm x"}}, tool="execute")
    assert res.block and res.message == "no rm" and seen[1] == {"command": "rm x", "timeout": 5}


async def test_match_filters_by_tool_and_context_joins():
    hooks = Hooks()
    hooks.add("post_tool", lambda p: _ret(HookResult(context="lint ok")), match="edit_file")
    hooks.add("post_tool", lambda p: _ret(HookResult(context="types ok")), match="*")
    assert (await hooks.run("post_tool", {}, tool="read_file")).context == "types ok"
    assert (await hooks.run("post_tool", {}, tool="edit_file")).context == "lint ok\n\ntypes ok"


async def _ret(value):
    return value


async def test_failures_and_timeouts_become_notices():
    hooks = Hooks()

    async def boom(payload):
        raise RuntimeError("bad")

    async def slow(payload):
        await asyncio.sleep(10)

    hooks.add("turn_end", boom)
    hooks.add("turn_end", slow, timeout=0.1)
    res = await hooks.run("turn_end", {})
    assert (
        not res.block
        and len(res.notices) == 2
        and "bad" in res.notices[0]
        and "timed out" in res.notices[1]
    )


async def test_command_hook_exit_codes(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text(
        "import json, sys\n"
        "p = json.load(sys.stdin)\n"
        "cmd = p['args'].get('command', '')\n"
        "if 'rm' in cmd:\n    print('dangerous', file=sys.stderr); sys.exit(2)\n"
        "if 'warn' in cmd:\n    print('meh', file=sys.stderr); sys.exit(1)\n"
        "print('checked ' + p['tool'])\n"
    )
    hook = CommandHook(f"{sys.executable} {script}", cwd=tmp_path)
    ok = await hook({"tool": "execute", "args": {"command": "ls"}})
    assert ok.context.strip() == "checked execute" and not ok.block
    blocked = await hook({"tool": "execute", "args": {"command": "rm -rf x"}})
    assert blocked.block and blocked.message.strip() == "dangerous"
    warned = await hook({"tool": "execute", "args": {"command": "warn"}})
    assert not warned.block and "meh" in warned.notices[0]


async def test_command_hook_timeout_kills_whole_process_group(tmp_path):
    marker = tmp_path / "marker"
    # The shell backgrounds a grandchild that would touch `marker` after ~2s, then the
    # shell itself sleeps for much longer. A timeout of 0.5s must kill the whole process
    # group (the backgrounded grandchild included), not just the shell, so the marker
    # must never appear.
    hook = CommandHook(f"sh -c '(sleep 2; touch {marker}) & sleep 30'", cwd=tmp_path, timeout=0.5)
    loop = asyncio.get_event_loop()
    start = loop.time()
    result = await hook({"tool": "execute", "args": {}})
    elapsed = loop.time() - start
    assert not result.block
    assert result.notices and "timed out" in result.notices[0]
    assert elapsed < 2.5, f"hook took {elapsed:g}s to return, should return near the 0.5s timeout"
    await asyncio.sleep(2.5)
    assert not marker.exists(), "grandchild survived the timeout and created the marker"


async def test_load_command_hooks_from_config(tmp_path):
    hooks = load_command_hooks(
        [
            {"event": "pre_tool", "match": "execute", "command": "exit 2", "timeout": 5},
            {"event": "nonsense", "command": "true"},
        ],
        tmp_path,
    )
    assert len(hooks) == 1
    assert (await hooks.run("pre_tool", {"args": {}}, tool="execute")).block
    assert not (await hooks.run("pre_tool", {"args": {}}, tool="ls")).block


def test_payload_is_json_serialisable_contract():
    assert json.dumps({"event": "pre_tool", "tool": "x", "args": {}, "session_id": "s"})


async def test_command_hook_env_is_scrubbed_of_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-hook-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-hook-pass")
    hook = CommandHook("env", cwd=tmp_path, env_passthrough=("GITHUB_TOKEN",))
    result = await hook({"event": "pre_tool"})
    assert "ghp-hook-pass" in result.context and "sk-hook-leak" not in result.context


async def test_load_command_hooks_passes_env_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-hook-pass")
    entries = [{"event": "turn_end", "command": f"printenv GITHUB_TOKEN > {tmp_path}/out"}]
    hooks = load_command_hooks(entries, tmp_path, env_passthrough=("GITHUB_TOKEN",))
    await hooks.run("turn_end", {"event": "turn_end"})
    assert (tmp_path / "out").read_text().strip() == "ghp-hook-pass"
