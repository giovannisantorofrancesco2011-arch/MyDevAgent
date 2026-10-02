import threading

import pytest

from mydevagent import plus
from mydevagent.llm import FakeLLM


def test_commands_become_agent_tasks():
    assert "git_diff" in plus.expand("/revisione", "")
    assert "`pytest -x`" in plus.expand("/debug", "pytest -x")
    assert "`app.py`" in plus.expand("/test", "app.py")
    assert plus.expand("/altro", "") is None
    with pytest.raises(ValueError):
        plus.expand("/debug", "")


def test_auto_memory_saves_at_most_two_facts(tmp_path):
    llm = FakeLLM(responses={"unknown": "- i test si lanciano con `pytest -q`\n- usa i tab\n- terzo\nciao"})
    facts = plus.remember(llm, tmp_path, "come lancio i test?", "Con pytest -q")
    assert facts == ["i test si lanciano con `pytest -q`", "usa i tab"]
    assert "- usa i tab" in (tmp_path / "MYDEVAGENT.md").read_text(encoding="utf-8")
    assert plus.remember(FakeLLM(responses={"unknown": "NONE"}), tmp_path, "ciao", "ciao!") == []


def test_background_job_reports_back(monkeypatch, tmp_path):
    class Runner:
        def __init__(self, orch, root, policy, approver):
            assert policy.mode == "auto-edit"
            assert approver(plus.ApprovalRequest("bash", {}, "rm -rf /", dangerous=True))[0] == "no"

        def run(self, task):
            yield f"fatto: {task}"

    monkeypatch.setattr(plus, "AgentRunner", Runner)
    done = threading.Event()
    jobs = plus.Background(None, tmp_path, lambda job: done.set())
    job = jobs.start("aggiungi il README")
    assert done.wait(5)
    assert (job.status, job.answer) == ("finito", "fatto: aggiungi il README")
