from dataclasses import dataclass, field

from luna.commands import REGISTRY, CommandResult, list_commands, run_line
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
