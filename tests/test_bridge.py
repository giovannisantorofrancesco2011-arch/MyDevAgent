import json
import os
import queue
import subprocess
import sys
import time

import httpx
import pytest

from mydevagent import fim, stats
from mydevagent.bridge import Bridge, inline_code
from mydevagent.llm import FakeLLM
from mydevagent.orchestrator import Orchestrator
from mydevagent.tui import extras
from tests.test_agent import ScriptedLLM, T


class Wire:
    """Lo stdout del ponte: raccoglie le righe JSON e aspetta quelle che servono ai test."""

    def __init__(self) -> None:
        self.lines: queue.Queue = queue.Queue()
        self.seen: list[dict] = []

    def write(self, data: bytes) -> None:
        for line in data.decode("ascii").splitlines():
            self.lines.put(json.loads(line))

    def flush(self) -> None:
        pass

    def wait(self, method: str | None = None, id: int | None = None, timeout: float = 10) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                msg = self.lines.get(timeout=0.05)
            except queue.Empty:
                continue
            self.seen.append(msg)
            if (method and msg.get("method") == method) or (id is not None and msg.get("id") == id):
                return msg
        raise AssertionError(f"nessun messaggio {method or id}: {self.seen[-5:]}")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    return root


def make(settings, project, llm, permission="ask"):
    wire = Wire()
    bridge = Bridge(Orchestrator(settings, llm=llm), project, wire, permission=permission)
    return bridge, wire


def call(bridge, wire, id, method, **params):
    bridge.handle({"id": id, "method": method, "params": params})
    return wire.wait(id=id)


def test_hello_and_settings(settings, project):
    bridge, wire = make(settings, project, FakeLLM())
    hello = call(bridge, wire, 1, "hello")["result"]
    assert hello["protocol"] == 1 and hello["root"] == str(project.resolve())
    assert hello["models"]["main"] == settings.resolve_model("main")[0] and hello["ollama"] is True
    assert hello["permission"] == "ask" and "ultra-deep" in hello["teams"] and hello["history"] == []
    assert hello["agents"]["architect"]
    assert call(bridge, wire, 2, "set", team="fast", permission="auto-edit")["result"]["team"] == "fast"
    assert bridge.policy.mode == "auto-edit"
    assert "sconosciuta" in call(bridge, wire, 3, "set", permission="boh")["error"]["message"]
    assert "metodo sconosciuto" in call(bridge, wire, 4, "../etc")["error"]["message"]


def test_agent_turn_with_approval_diff_and_undo(settings, project):
    llm = ScriptedLLM(steps=[T("edit_file", path="src/calc.py", old_string="a + b", new_string="a + b  # somma"),
                             "Ho aggiunto un commento a src/calc.py"])
    bridge, wire = make(settings, project, llm)
    call(bridge, wire, 1, "hello")
    started = call(bridge, wire, 2, "prompt", text="/fast commenta la somma",
                   context={"file": "src/calc.py", "selection": {"text": "a + b", "start": 2, "end": 2}})
    turn = started["result"]["turn"]
    busy = call(bridge, wire, 3, "prompt", text="altro")
    assert "già lavorando" in busy["error"]["message"]
    ask = wire.wait("approval")["params"]
    assert ask["tool"] == "edit_file" and ask["path"] == "src/calc.py" and ask["turn"] == turn
    assert ask["before"] == "def add(a, b):\n    return a + b\n" and ask["after"].endswith("# somma\n")
    assert "+    return a + b  # somma" in ask["diff"]
    assert call(bridge, wire, 4, "approval_reply", request=ask["request"], answer="yes")["result"]["ok"]
    diff = wire.wait("event")
    while diff["params"]["event"]["type"] != "diff":
        diff = wire.wait("event")
    end = wire.wait("turn_end")["params"]
    assert end["turn"] == turn and not end["cancelled"] and end["error"] is None
    assert end["files"] == ["src/calc.py"] and "commento" in end["answer"]
    assert "# somma" in (project / "src" / "calc.py").read_text()
    task = next(c for c in llm.calls if c["role"] == "agent")["messages"][-1]["content"]
    assert "righe 2-2 selezionate" in task and "a + b" in task  # il contesto dell'editor arriva all'agente
    [record] = stats.load()
    assert record["source"] == "studio" and record["files"] == ["src/calc.py"]
    assert bridge.session.history[0]["content"] == "/fast commenta la somma"
    assert "+    return a + b  # somma" in call(bridge, wire, 5, "diff")["result"]["diff"]
    undone = call(bridge, wire, 6, "undo")["result"]
    assert undone["files"] == ["src/calc.py"] and "# somma" not in (project / "src" / "calc.py").read_text()


def test_rejected_and_cancelled_turns(settings, project):
    llm = ScriptedLLM(steps=[T("write_file", path="src/new.py", content="X = 1\n"), "Ok, non lo creo",
                             T("write_file", path="src/new.py", content="X = 2\n")])
    bridge, wire = make(settings, project, llm)
    call(bridge, wire, 1, "prompt", text="/fast crea src/new.py")
    ask = wire.wait("approval")["params"]
    assert ask["before"] is None and ask["after"] == "X = 1\n"  # file nuovo
    call(bridge, wire, 2, "approval_reply", request=ask["request"], answer="no", feedback="chiamalo nuovo.py")
    wire.wait("turn_end")
    assert not (project / "src" / "new.py").exists()
    second = [c for c in llm.calls if c["role"] == "agent"][1]
    assert any("chiamalo nuovo.py" in str(m.get("content")) for m in second["messages"])
    call(bridge, wire, 3, "prompt", text="/fast riprova")
    wire.wait("approval")
    assert call(bridge, wire, 4, "cancel")["result"]["cancelled"]
    end = wire.wait("turn_end")["params"]
    assert end["cancelled"] and not (project / "src" / "new.py").exists()
    assert bridge.busy is None


def test_commands_are_expanded(settings, project):
    (project / ".mydevagent" / "commands").mkdir(parents=True)
    (project / ".mydevagent" / "commands" / "saluta.md").write_text("description: saluta\nDì ciao a $ARGUMENTS")
    bridge, _ = make(settings, project, FakeLLM())
    assert bridge._expand("/saluta Gio") == "Dì ciao a Gio"
    assert bridge._expand("/init") == extras.INIT_TASK
    assert bridge._expand("ciao") == "ciao"
    with pytest.raises(ValueError):
        bridge._expand("/skill inesistente fai")
    assert {"name": "/saluta", "description": "saluta"} in bridge._commands()


def test_inline_edit_returns_only_code(settings, project):
    llm = FakeLLM(responses={"unknown": "Ecco:\n```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```"})
    bridge, wire = make(settings, project, llm)
    result = call(bridge, wire, 1, "inline_edit", path="src/calc.py", language="python", before="",
                  selection="def add(a, b):\n    return a + b\n", after="", instruction="aggiungi i tipi")
    assert result["result"]["text"] == "def add(a: int, b: int) -> int:\n    return a + b\n"
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "aggiungi i tipi" in prompt and "Selected code" in prompt
    assert inline_code("<think>boh</think>x = 1", "y = 2") == "x = 1"


def test_fim_prompt_clean_and_ollama_request(settings):
    prompt, stop = fim.build_prompt("qwen2.5-coder:1.5b-base", "def add(a, b):\n    return ", "\n")
    assert prompt == "<|fim_prefix|>def add(a, b):\n    return <|fim_suffix|>\n<|fim_middle|>"
    assert "<|endoftext|>" in stop
    assert fim.template("deepseek-coder:1.3b")[0] == "<｜fim▁begin｜>"
    assert fim.clean("a + b<|endoftext|>garbage", "", stop) == "a + b"
    assert fim.clean("print(x)", ")\n", stop) == "print(x"  # la ")" c'era già dopo il cursore
    assert fim.clean("x = 1", "1 + 2", stop) == "x = 1"  # non taglia il codice vero
    assert fim.clean("total = a + b", "a + b", stop) == "total ="  # ha riscritto il resto della riga

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": "a + b\n\n\ndef sub"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    text = fim.complete(settings, "def add(a, b):\n    return ", "\n", client=client, multiline=False)
    assert text == "a + b"
    assert seen["url"] == "http://localhost:11434/api/generate"
    body = seen["body"]
    assert body["raw"] is True and body["model"] == settings.resolve_model("fast")[0] and "\n" in body["options"]["stop"]


def test_stats_method(settings, project):
    bridge, wire = make(settings, project, ScriptedLLM(steps=["Fatto."]))
    call(bridge, wire, 1, "prompt", text="/fast ciao")
    wire.wait("turn_end")
    result = call(bridge, wire, 2, "stats")["result"]
    assert result["session"]["turns"] == 1 and result["history"]["turns"] == 1 and result["history"]["streak"] == 1


def test_context_compact_and_update_methods(settings, project, monkeypatch):
    from mydevagent import update

    bridge, wire = make(settings, project, FakeLLM())
    hello = call(bridge, wire, 1, "hello")["result"]
    assert hello["home"] == str(update.HOME) and "installed" in hello
    assert call(bridge, wire, 2, "compact")["result"]["compacted"] is False  # conversazione troppo corta
    bridge.session.history = [{"role": r, "content": "x" * 800} for r in ["user", "assistant"] * 3]
    usage = call(bridge, wire, 3, "context")["result"]
    assert usage["count"] == 6 and usage["window"] == settings.active_profile.num_ctx and usage["auto_compact"] == 85
    assert usage["free"] == usage["window"] - usage["instructions"] - usage["messages"]
    done = call(bridge, wire, 4, "compact")["result"]
    assert done["compacted"] and done["turns"] == 3 and done["freed"] > 0 and done["summary"]
    assert done["usage"]["count"] == 0 and len(bridge.session.history) == 2
    monkeypatch.setattr(update, "available", lambda: 2)
    monkeypatch.setattr(update, "update", lambda: update.Result(True, "Aggiornato: 2 novità.", ["a", "b"], True))
    assert call(bridge, wire, 5, "updates")["result"]["available"] == 2
    assert call(bridge, wire, 6, "update")["result"] == {"ok": True, "message": "Aggiornato: 2 novità.",
                                                          "changes": ["a", "b"], "restart": True,
                                                          "installed": update.current()}


def test_bridge_process_keeps_stdout_clean(tmp_path):
    env = {**os.environ, "MYDEVAGENT_FAKE_LLM": "1", "MYDEVAGENT_OFFLINE": "1",
           "MYDEVAGENT_STATE_DIR": str(tmp_path / "state")}
    messages = [{"id": 1, "method": "hello", "params": {}}, {"id": 2, "method": "shutdown", "params": {}}]
    proc = subprocess.run([sys.executable, "-m", "mydevagent.cli", "bridge"], cwd=tmp_path, env=env, timeout=60,
                          input="".join(json.dumps(m) + "\n" for m in messages).encode(), capture_output=True)
    lines = [json.loads(line) for line in proc.stdout.decode().splitlines()]  # solo JSON, niente altro
    assert lines[0]["method"] == "ready" and lines[1]["id"] == 1 and lines[2] == {"id": 2, "result": {"ok": True}}
    assert proc.returncode == 0
