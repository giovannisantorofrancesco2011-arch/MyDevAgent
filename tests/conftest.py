import pytest

from mydevagent.config import load_settings
from mydevagent.llm import FakeLLM
from mydevagent.orchestrator import Orchestrator
from mydevagent.registry import load_registry


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("MYDEVAGENT_OFFLINE", "1")
    monkeypatch.delenv("MYDEVAGENT_API_KEY", raising=False)
    # niente plugin, hook, skill o server MCP veri dell'utente (~/.claude, ~/.mydevagent) nei test
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path / "home"))
    monkeypatch.setenv("MYDEVAGENT_STATE_DIR", str(tmp_path / "state"))
    # licenza spenta nei test (tutto gratis); i test della licenza mettono una chiave di prova
    monkeypatch.setattr("mydevagent.license.PUBLIC_KEY", "")
    for var in ("MYDEVAGENT_SKILLS_DIRS", "MYDEVAGENT_PLUGINS_DIRS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings(tmp_path):
    return load_settings(overrides={
        "tools": {
            "sandbox": {"backend": "local", "allow_unsafe_local": True, "timeout_s": 10},
            "filesystem": {"root": str(tmp_path)},
        }
    })


@pytest.fixture
def registry(settings):
    return load_registry(settings)


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def orchestrator(settings, fake_llm):
    return Orchestrator(settings, llm=fake_llm)
