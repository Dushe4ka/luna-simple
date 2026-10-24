from dataclasses import dataclass, field

from luna_core.context import PinnedFiles

from luna.commands import REGISTRY, Choice, CommandResult, list_commands, run_line
from luna.config.config import LunaConfig
from luna.turn.engine import Notice


@dataclass
class FakeEnv:
    workdir: str = "."
    user_commands: dict = field(default_factory=dict)
    pinned: PinnedFiles = field(default_factory=PinnedFiles)
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

    async def rebuild(self):
        if self.fail_rebuild:
            raise self.fail_rebuild
        self.rebuilt += 1

    async def switch_model(self, model):
        previous = self.cfg.model
        self.cfg.model = model
        try:
            await self.rebuild()
        except Exception:
            self.cfg.model = previous
            raise

    async def switch_provider(self, provider):
        previous = (self.cfg.provider, self.cfg.model)
        self.cfg.provider, self.cfg.model = provider, None
        try:
            await self.rebuild()
        except Exception:
            self.cfg.provider, self.cfg.model = previous
            raise


def test_all_24_repl_commands_are_registered():
    from luna.repl.commands import HELP

    assert set(HELP) <= set(REGISTRY) | {"/exit"}
    assert {c["kind"] for c in list_commands({})} == {"ui", "read", "mutate", "prompt"}


async def test_context_lists_pinned_files():
    env = FakeEnv()
    assert (await run_line("/context", env)).notices == [Notice("dim", "(no pinned files)")]
    env.pinned.add("a.py")
    assert (await run_line("/context", env)).text == "a.py"


async def test_user_command_expands_to_a_prompt(tmp_path):
    from luna.repl.usercmd import UserCommand

    env = FakeEnv(
        workdir=str(tmp_path),
        user_commands={"greet": UserCommand("greet", "say hi", "hi $ARGUMENTS")},
    )
    assert await run_line("/greet world", env) == CommandResult(prompt="hi world")
    assert {"name": "/greet", "help": "say hi", "kind": "prompt"} in list_commands(
        env.user_commands
    )


async def test_unknown_command_is_an_error_notice():
    assert (await run_line("/nope", FakeEnv())).notices == [
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


async def test_plan_toggles_and_accepts_on_off():
    env = FakeEnv()
    assert (await run_line("/plan", env)).notices == [Notice("info", "plan mode: on")]
    assert env.plan is True
    assert (await run_line("/plan off", env)).effects == {"plan": False}
    assert (await run_line("/plan", FakeEnv(plan=None))).notices == [
        Notice("dim", "plan mode is not available here")
    ]


async def test_add_and_drop_pin_files():
    env = FakeEnv()
    assert (await run_line("/add", env)).notices == [Notice("dim", "usage: /add path ...")]
    assert (await run_line("/add a.py b.py", env)).notices == [Notice("dim", "pinned: a.py, b.py")]
    assert (await run_line("/drop a.py b.py", env)).notices == [Notice("dim", "pinned: (none)")]


async def test_verify_without_command_and_with_failure(monkeypatch):
    from luna.commands import builtin

    assert (await run_line("/verify", FakeEnv())).notices == [
        Notice("dim", "set agent.verify_command in config first")
    ]
    seen = {}
    monkeypatch.setattr(
        builtin, "run_verify", lambda cmd, wd, **kw: seen.update(kw) or (False, "E")
    )
    env = FakeEnv(cfg=LunaConfig(verify_command="make check", execute_pass_env=("GH_TOKEN",)))
    assert (await run_line("/verify", env)).notices == [Notice("warn", "verify failed\nE")]
    assert seen == {"pass_env": ("GH_TOKEN",)}


async def test_reload_rebuilds_and_reports_failure():
    env = FakeEnv()
    assert (await run_line("/reload", env)).effects == {"agent_rebuilt": True} and env.rebuilt == 1
    bad = FakeEnv(fail_rebuild=RuntimeError("bad toml"))
    assert (await run_line("/reload", bad)).notices == [Notice("error", "/reload failed: bad toml")]


async def test_model_with_arg_switches_and_failure_keeps_the_old_one():
    env = FakeEnv()
    result = await run_line("/model gpt-5", env)
    assert result.notices == [Notice("info", "model → gpt-5")] and result.effects == {
        "model": "gpt-5"
    }
    bad = FakeEnv(fail_rebuild=RuntimeError("no such model"))
    assert (await run_line("/model nope", bad)).notices == [
        Notice("error", "could not switch: no such model")
    ]
    assert bad.cfg.model is None


async def test_model_without_arg_offers_a_choice(monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_models_for", lambda cfg: ["a", "b"])
    result = await run_line("/model", FakeEnv())
    title = f"Модель ({LunaConfig().provider})"
    assert result.choice == Choice(title, [("a", "a"), ("b", "b")], "/model {value}")
    assert result.notices == [Notice("dim", "model: (provider default)")]


async def test_model_without_arg_and_no_list_explains_usage(monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_models_for", lambda cfg: [])
    result = await run_line("/model", FakeEnv())
    assert result.choice is None
    assert result.notices == [Notice("dim", "model: (provider default)")]


async def test_provider_unknown_and_missing_key(monkeypatch):
    assert (await run_line("/provider nope", FakeEnv())).notices == [
        Notice("error", "unknown provider 'nope'")
    ]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    notices = (await run_line("/provider openai", FakeEnv())).notices
    assert notices == [Notice("error", "no key for openai; run: luna config set-key openai")]


async def test_init_becomes_a_prompt_and_reloads_after(tmp_path, monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "init_prompt", lambda wd: "WRITE AGENTS.md")
    env = FakeEnv(workdir=str(tmp_path))
    assert (await run_line("/init", env)).prompt == "WRITE AGENTS.md"
    # the flag travels with the message, not set here: a failed send leaves nothing behind
    assert (await run_line("/init", env)).effects == {"reload_after_turn": True}
    assert env.reload_after is False


async def test_model_picker_marks_the_current_model(monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_models_for", lambda cfg: ["a", "b"])
    env = FakeEnv(cfg=LunaConfig(model="b"))
    assert (await run_line("/model", env)).choice.options == [("a", "a"), ("b", "b  ✓ текущая")]


async def test_provider_picker_is_not_shown_empty(monkeypatch):
    from luna.commands import builtin

    monkeypatch.setattr(builtin, "_api_key", lambda provider, spec: None)
    monkeypatch.setattr(
        builtin,
        "merge_providers",
        lambda custom: {"openai": type("S", (), {"env_var": "OPENAI_API_KEY"})()},
    )
    result = await run_line("/provider", FakeEnv())
    assert result.choice is None
    assert (
        Notice("dim", "нет провайдеров с ключом — luna config set-key <провайдер>")
        in result.notices
    )


def test_help_is_built_from_the_registry():
    from luna.repl.commands import HELP

    assert HELP == {c.name: c.help for c in REGISTRY.values()}


def test_exit_is_listed_but_its_quit_alias_is_not():
    """/exit must be findable while typing; /quit is only an alias (noted in /exit's help)."""
    listed = {c["name"]: c for c in list_commands({})}
    assert listed["/exit"]["kind"] == "ui"
    assert "/quit" in listed["/exit"]["help"]
    assert "/quit" not in listed
