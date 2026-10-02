from dataclasses import dataclass, field

from luna.commands import REGISTRY, Choice, CommandResult, Confirm, list_commands, run_line
from luna.config.config import LunaConfig
from luna.config.usage import SessionUsage, TurnUsage
from luna.turn.context import PinnedFiles
from luna.turn.engine import Notice


@dataclass
class FakeEnv:
    workdir: str = "."
    thread_id: str = "t1"
    session_id: str = "t1"
    index: object = None
    user_commands: dict = field(default_factory=dict)
    pinned: PinnedFiles = field(default_factory=PinnedFiles)
    usage: SessionUsage = field(default_factory=SessionUsage)
    cfg: LunaConfig = field(default_factory=LunaConfig)
    plan: bool | None = False
    rebuilt: int = 0
    fail_rebuild: Exception | None = None
    reload_after: bool = False

    @property
    def config(self):
        return self.cfg

    @property
    def agent(self):
        return object()

    @property
    def can_rebuild(self):
        return True

    def get_plan(self):
        return self.plan

    def set_plan(self, on):
        self.plan = on

    def rebuild(self):
        if self.fail_rebuild:
            raise self.fail_rebuild
        self.rebuilt += 1

    def switch_model(self, model):
        previous = self.cfg.model
        self.cfg.model = model
        try:
            self.rebuild()
        except Exception:
            self.cfg.model = previous
            raise

    def switch_provider(self, provider):
        previous = (self.cfg.provider, self.cfg.model)
        self.cfg.provider, self.cfg.model = provider, None
        try:
            self.rebuild()
        except Exception:
            self.cfg.provider, self.cfg.model = previous
            raise

    def after_turn_reload(self):
        self.reload_after = True


def test_all_24_repl_commands_are_registered():
    from luna.repl.commands import HELP

    assert set(HELP) <= set(REGISTRY) | {"/exit"}
    assert {c["kind"] for c in list_commands({})} == {"ui", "read", "mutate", "prompt"}


def test_tools_lists_builtin_tools():
    result = run_line("/tools", FakeEnv())
    assert "read_file" in result.text and "web_search" in result.text
    # in code spans, so Markdown in the TUI doesn't eat <server>/<tool> as HTML tags
    assert "`mcp__<server>__<tool>`" in result.text


def test_usage_without_turns_and_with_turns():
    env = FakeEnv()
    assert run_line("/usage", env).notices == [Notice("info", "no usage recorded yet")]
    env.usage.add_turn(TurnUsage(10, 5, 15))
    assert "turns: 1  in: 10  out: 5  total: 15" in run_line("/usage", env).text


def test_context_lists_pinned_files():
    env = FakeEnv()
    assert run_line("/context", env).notices == [Notice("dim", "(no pinned files)")]
    env.pinned.add("a.py")
    assert run_line("/context", env).text == "a.py"


def test_diff_without_changes(tmp_path):
    assert run_line("/diff", FakeEnv(workdir=str(tmp_path))).notices == [
        Notice("dim", "no changes this session")
    ]


def test_user_command_expands_to_a_prompt(tmp_path):
    from luna.repl.usercmd import UserCommand

    env = FakeEnv(
        workdir=str(tmp_path),
        user_commands={"greet": UserCommand("greet", "say hi", "hi $ARGUMENTS")},
    )
    assert run_line("/greet world", env) == CommandResult(prompt="hi world")
    assert {"name": "/greet", "help": "say hi", "kind": "prompt"} in list_commands(
        env.user_commands
    )


def test_unknown_command_is_an_error_notice():
    assert run_line("/nope", FakeEnv()).notices == [
        Notice("error", "неизвестная команда /nope — /help")
    ]


def test_result_serialises_for_the_api():
    from luna.commands import Choice

    result = CommandResult(
        text="x", notices=[Notice("info", "y")], choice=Choice("T", [("a", "A")], "/model {value}")
    )
    assert result.to_json() == {
        "text": "x",
        "notices": [{"level": "info", "text": "y"}],
        "choice": {"title": "T", "options": [["a", "A"]], "resubmit": "/model {value}"},
        "confirm": None,
        "prompt": None,
        "effects": {},
    }


def test_plan_toggles_and_accepts_on_off():
    env = FakeEnv()
    assert run_line("/plan", env).notices == [Notice("info", "plan mode: on")]
    assert env.plan is True
    assert run_line("/plan off", env).effects == {"plan": False}
    assert run_line("/plan", FakeEnv(plan=None)).notices == [
        Notice("dim", "plan mode is not available here")
    ]


def test_add_and_drop_pin_files():
    env = FakeEnv()
    assert run_line("/add", env).notices == [Notice("dim", "usage: /add path ...")]
    assert run_line("/add a.py b.py", env).notices == [Notice("dim", "pinned: a.py, b.py")]
    assert run_line("/drop a.py b.py", env).notices == [Notice("dim", "pinned: (none)")]


def test_verify_without_command_and_with_failure(monkeypatch):
    from luna.commands import builtin

    assert run_line("/verify", FakeEnv()).notices == [
        Notice("dim", "set agent.verify_command in config first")
    ]
    monkeypatch.setattr(builtin, "run_verify", lambda cmd, wd: (False, "E"))
    env = FakeEnv(cfg=LunaConfig(verify_command="make check"))
    assert run_line("/verify", env).notices == [Notice("warn", "verify failed\nE")]


def test_reload_rebuilds_and_reports_failure():
    env = FakeEnv()
    assert run_line("/reload", env).effects == {"agent_rebuilt": True} and env.rebuilt == 1
    bad = FakeEnv(fail_rebuild=RuntimeError("bad toml"))
    assert run_line("/reload", bad).notices == [Notice("error", "/reload failed: bad toml")]


def test_model_with_arg_switches_and_failure_keeps_the_old_one():
    env = FakeEnv()
    result = run_line("/model gpt-5", env)
    assert result.notices == [Notice("info", "model → gpt-5")] and result.effects == {
        "model": "gpt-5"
    }
    bad = FakeEnv(fail_rebuild=RuntimeError("no such model"))
    assert run_line("/model nope", bad).notices == [
        Notice("error", "could not switch: no such model")
    ]
    assert bad.cfg.model is None


def test_model_without_arg_offers_a_choice(monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_models_for", lambda cfg: ["a", "b"])
    result = run_line("/model", FakeEnv())
    title = f"Модель ({LunaConfig().provider})"
    assert result.choice == Choice(title, [("a", "a"), ("b", "b")], "/model {value}")
    assert result.notices == [Notice("dim", "model: (provider default)")]


def test_model_without_arg_and_no_list_explains_usage(monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_models_for", lambda cfg: [])
    result = run_line("/model", FakeEnv())
    assert result.choice is None
    assert result.notices == [Notice("dim", "model: (provider default)")]


def test_provider_unknown_and_missing_key(monkeypatch):
    assert run_line("/provider nope", FakeEnv()).notices == [
        Notice("error", "unknown provider 'nope'")
    ]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    notices = run_line("/provider openai", FakeEnv()).notices
    assert notices == [Notice("error", "no key for openai; run: luna config set-key openai")]


def test_undo_asks_first_then_undoes(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_is_git", lambda wd: False)
    monkeypatch.setattr(builtin, "peek_last", lambda wd, sid: "undo write_file a.py")
    monkeypatch.setattr(builtin, "undo_last", lambda wd, sid: "reverted a.py")
    env = FakeEnv(workdir=str(tmp_path))
    assert run_line("/undo", env).confirm == Confirm(
        "undo write_file a.py?", "/undo --yes", "undo cancelled"
    )
    assert run_line("/undo --yes", env).notices == [Notice("info", "reverted a.py")]


def test_undo_with_nothing_to_undo(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_is_git", lambda wd: False)
    monkeypatch.setattr(builtin, "peek_last", lambda wd, sid: None)
    assert run_line("/undo", FakeEnv(workdir=str(tmp_path))).notices == [
        Notice("dim", "nothing to undo")
    ]


def test_redo_needs_git(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_is_git", lambda wd: False)
    assert run_line("/redo", FakeEnv(workdir=str(tmp_path))).notices == [
        Notice("dim", "redo needs a git repository")
    ]


def test_compact_reports_and_resyncs_undo(monkeypatch):
    from luna.commands import builtin

    forgot = []
    monkeypatch.setattr(
        builtin,
        "compact_history",
        lambda agent, tid: (True, "compacted — history replaced with a summary"),
    )
    monkeypatch.setattr(builtin, "_message_count", lambda env: 1)
    monkeypatch.setattr(builtin, "forget_messages", lambda wd, sid, n: forgot.append(n))
    assert run_line("/compact", FakeEnv()).notices == [
        Notice("info", "compacted — history replaced with a summary")
    ]
    assert forgot == [1]


def test_init_becomes_a_prompt_and_reloads_after(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "init_prompt", lambda wd: "WRITE AGENTS.md")
    env = FakeEnv(workdir=str(tmp_path))
    assert run_line("/init", env).prompt == "WRITE AGENTS.md"
    assert env.reload_after is True


def test_model_picker_marks_the_current_model(monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_models_for", lambda cfg: ["a", "b"])
    env = FakeEnv(cfg=LunaConfig(model="b"))
    assert run_line("/model", env).choice.options == [("a", "a"), ("b", "b  ✓ текущая")]
