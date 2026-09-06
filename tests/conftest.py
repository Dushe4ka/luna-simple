from collections.abc import Sequence

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeToolCallingModel(BaseChatModel):
    """Deterministic chat model with a scripted queue and a working bind_tools."""

    responses: Sequence[AIMessage] = ()
    idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        i = min(self.idx, len(self.responses) - 1)
        msg = self.responses[i]
        self.idx += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path_factory, monkeypatch):
    """Keep tests away from the real ~/.config/luna."""
    home = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


@pytest.fixture
def fake_model():
    def _make(*messages: AIMessage) -> FakeToolCallingModel:
        return FakeToolCallingModel(responses=list(messages) or [AIMessage(content="ok")])

    return _make
