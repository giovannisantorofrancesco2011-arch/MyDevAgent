import threading

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from mydevagent.llm import FakeLLM
from mydevagent.orchestrator import Orchestrator
from mydevagent.tui.app import COMMANDS, TuiApp
from mydevagent.tui.apply import apply_answer
from mydevagent.tui.completion import DevCompleter
from mydevagent.tui.render import TurnRenderer, split_complete
from mydevagent.tui.session import Session, list_sessions


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("MYDEVAGENT_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
    return root


def record_console():
    return Console(record=True, width=100, force_terminal=False, color_system=None)


def completions(completer, text):
    return [c.text for c in completer.get_completions(Document(text), None)]


def test_completer(registry, project):
    comp = DevCompleter(COMMANDS, dict(registry.by_alias), project)
    assert "/help" in completions(comp, "/he")
    assert completions(comp, "ciao /he") == []  # i comandi solo a inizio riga
    assert "@security" in completions(comp, "controlla @se")
    assert "@src/calc.py" in completions(comp, "migliora @sr")


def test_split_complete_respects_code_fences():
    done, tail = split_complete("intro\n\n```python\nx = 1\n\ny = 2\n")
    assert done == "intro\n\n" and tail.startswith("```python")
    done, tail = split_complete("a\n\nb\n\nc")
    assert done == "a\n\nb\n\n" and tail == "c"


def test_renderer_prints_agent_blocks():
    console = record_console()
    r = TurnRenderer(console, {"architect": "Architetto", "backend": "Backend"})
    r.on_event({"type": "route", "mode": "balanced", "agents": ["architect", "backend", "formatter"]})
    r.on_event({"type": "agent_end", "agent": "architect", "name": "Architetto", "ms": 1200,
                "prompt_tokens": 500, "completion_tokens": 300})
    r.on_event({"type": "tool_call", "agent": "backend", "tool": "web_search", "args": {"query": "fastapi"}})
    r.on_event({"type": "tool_result", "agent": "backend", "tool": "web_search", "ok": True, "preview": "[1] FastAPI"})
    r.on_chunk("**Summary**: ok.\n\n```python\nprint(1)\n```")
    r.on_event({"type": "done", "summary": "balanced · 2 agenti"})
    r.finish()
    out = console.export_text()
    assert "⏺ Team balanced · 2 agenti" in out and "Architetto → Backend" in out
    assert "⏺ Architetto" in out and "⎿  1.2s · 800 tok" in out
    assert "Web(fastapi)" in out and "print(1)" in out
    assert r.answer.startswith("**Summary**")


def test_apply_writes_only_after_confirmation(registry, project):
    console = record_console()
    answer = ("```python file=src/calc.py\ndef add(a: int, b: int) -> int:\n    return a + b\n```\n"
              "```python file=src/new.py\nX = 1\n```\n"
              "```python file=../escape.py\nboom\n```")
    answers = iter(["3", "1"])
    written = apply_answer(answer, registry, project, console, lambda q: next(answers))
    assert written == ["src/new.py"]
    assert (project / "src" / "calc.py").read_text() == "def add(a, b): return a + b\n"  # rifiutato
    assert not (project.parent / "escape.py").exists()
    out = console.export_text()
    assert "Update(src/calc.py)" in out and "+def add(a: int, b: int) -> int:" in out and "Bloccato ../escape.py" in out


def test_apply_all(registry, project):
    answer = "```python file=a.py\nA = 1\n```\n```python file=b.py\nB = 2\n```"
    asked = []
    written = apply_answer(answer, registry, project, record_console(), lambda q: asked.append(q) or "2")
    assert written == ["a.py", "b.py"] and len(asked) == 1


def test_cancel_stops_team_before_next_agent(settings):
    cancel = threading.Event()
    llm = FakeLLM()
    events = []

    def on_event(e):
        events.append(e)
        if e["type"] == "agent_end" and e["agent"] == "architect":
            cancel.set()

    out = "".join(Orchestrator(settings, llm=llm).run(
        "Crea un endpoint FastAPI con tabella users su Postgres", on_event=on_event, cancel=cancel))
    assert out == ""
    assert [c["role"] for c in llm.calls] == ["Architect"]
    assert events[-1]["type"] == "cancelled"


def test_cancel_stops_streaming(settings):
    cancel = threading.Event()
    chunks = []
    for chunk in Orchestrator(settings, llm=FakeLLM()).run("/fast somma in python", cancel=cancel):
        chunks.append(chunk)
        cancel.set()
    assert len(chunks) == 1


def test_sessions_roundtrip(project):
    s = Session(cwd=str(project))
    s.add_turn("ciao", "risposta")
    loaded = list_sessions(str(project))
    assert loaded[0].id == s.id and loaded[0].title == "ciao"
    assert "## MyDevAgent" in s.to_markdown()


def test_full_session_scripted(settings, project):
    console = record_console()
    orch = Orchestrator(settings, llm=FakeLLM())
    with create_pipe_input() as pipe:
        app = TuiApp(orch, console=console, prompt_input=pipe, prompt_output=DummyOutput(),
                     ask=lambda q: "1", root=project, background=False)
        pipe.send_text("/help\r")
        pipe.send_text("/balanced migliora @src/calc.py\r")
        pipe.send_text("/apply\r")
        pipe.send_text("!echo hello-shell\r")
        pipe.send_text("/cost\r")
        pipe.send_text("/deep\r")
        pipe.send_text("/export\r")
        pipe.send_text("/exit\r")
        app.loop()
    out = console.export_text()
    assert "Benvenuto in MyDevAgent" in out and "/apply" in out
    assert "allegato src/calc.py" in out and "⏺ Team balanced" in out
    assert (project / "app" / "main.py").is_file()  # scritto da /apply
    assert "hello-shell" in out and "$ echo hello-shell" in app.pending_context
    assert app.mode == "deep" and app.stats["turns"] == 1
    assert list(project.glob("mydevagent-*.md"))
    user_msgs = [m["content"] for m in app.session.history if m["role"] == "user"]
    assert user_msgs == ["/balanced migliora @src/calc.py"]


def test_agent_session_approvals_undo_memory_custom(settings, project):
    import json

    from tests.test_agent import ScriptedLLM

    (project / ".mydevagent" / "commands").mkdir(parents=True)
    (project / ".mydevagent" / "commands" / "saluta.md").write_text("description: saluta\nDì ciao a $ARGUMENTS")

    def T(name, **args):
        return f'<tool name="{name}">{json.dumps(args)}</tool>'

    llm = ScriptedLLM(steps=[
        T("write_file", path="src/new.py", content="X = 1\n"), "Creato src/new.py",   # turno 1: approvato
        T("write_file", path="src/other.py", content="Y = 2\n"), "Ok, non lo creo",   # turno 2: rifiutato
        "Ciao Mario!",                                                                # turno 3: comando custom
    ])
    orch = Orchestrator(settings, llm=llm)
    console = record_console()
    answers = iter(["1", "3"])
    with create_pipe_input() as pipe:
        app = TuiApp(orch, console=console, prompt_input=pipe, prompt_output=DummyOutput(),
                     ask=lambda q: next(answers), root=project, background=False)
        pipe.send_text("/fast crea src/new.py\r")
        pipe.send_text("/fast crea src/other.py\r")
        pipe.send_text("#usa sempre type hints\r")
        pipe.send_text("/saluta Mario\r")
        pipe.send_text("/diff\r")
        pipe.send_text("/undo\r")
        pipe.send_text("/plan\r")
        pipe.send_text("/exit\r")
        app.loop()
    out = console.export_text()
    assert "Create(src/new.py)" in out and "📝 File modificati: src/new.py" in out
    assert not (project / "src" / "other.py").exists()
    denied = [m for c in llm.calls for m in c["messages"] if "DENIED by the user" in str(m.get("content"))]
    assert denied
    assert "usa sempre type hints" in (project / "MYDEVAGENT.md").read_text()
    assert "Dì ciao a Mario" in llm.calls[-1]["messages"][-1]["content"]
    assert "+X = 1" in out  # /diff
    assert "Annullato" in out and not (project / "src" / "new.py").exists()  # /undo
    assert app.policy.mode == "plan"
    assert (project / ".mydevagent" / ".gitignore").read_text() == "*\n"


def test_compact_history(settings, project):
    from mydevagent.tui import extras

    history = [{"role": "user", "content": f"domanda {i}"} for i in range(6)]
    compacted = extras.compact_history(FakeLLM(), history)
    assert len(compacted) == 2 and compacted[0]["content"].startswith("[Summary")


def test_context_and_compact_commands(settings, project):
    from mydevagent.tui import extras

    console = record_console()
    with create_pipe_input() as pipe:
        app = TuiApp(Orchestrator(settings, llm=FakeLLM()), console=console, prompt_input=pipe,
                     prompt_output=DummyOutput(), root=project, background=False)
    app.session.history = [{"role": r, "content": f"messaggio {i} " + "x" * 400}
                           for i, r in enumerate(["user", "assistant"] * 3)]
    app.handle_command("/context")
    out = console.export_text()
    assert "Istruzioni e memoria" in out and "Messaggi (6)" in out and "di 16.384 token" in out
    assert "ctx " in "".join(t for _, t in app.toolbar())
    app.handle_command("/compact")
    out = console.export_text()
    assert "Conversazione compattata (3 turni) · liberati circa" in out
    assert extras.summary_of(app.session.history) and app._usage().count == 0 and app._usage().summary > 0
    # oltre l'85% del contesto si riassume da sola, anche con pochi messaggi
    full = extras.ContextUsage(window=1000, instructions=500, summary=0, messages=400, count=4)
    assert extras.needs_compact(full, [{}] * 4, 20) and not extras.needs_compact(full, [{}] * 2, 20)


def test_permission_cycle_and_toolbar(settings, project):
    orch = Orchestrator(settings, llm=FakeLLM())
    with create_pipe_input() as pipe:
        app = TuiApp(orch, console=record_console(), prompt_input=pipe, prompt_output=DummyOutput(),
                     root=project, background=False)
    assert app.policy.next_mode() == "auto-edit" and app.policy.next_mode() == "plan"
    text = "".join(t for _, t in app.toolbar())
    assert "⏸ modalità plan" in text and "shift+tab" in text
    app.agent_mode = False
    assert "modalità chat" in "".join(t for _, t in app.toolbar())


def test_vio_reacts_to_modes(settings, project):
    from mydevagent.tui import mascot

    assert all(len(row) == mascot.WIDTH for e in mascot.EXPRESSIONS for f in (0, 1) for row in mascot.sprite(e, f))
    assert all(e in mascot.SAYS for e in ("ask", "auto-edit", "plan", "auto", "chat", "fast", "ultra-deep"))
    with create_pipe_input() as pipe:
        app = TuiApp(Orchestrator(settings, llm=FakeLLM()), console=record_console(), prompt_input=pipe,
                     prompt_output=DummyOutput(), root=project, background=False)
    assert app.vio_state()[1].startswith("Ciao, sono Vio!")
    app.policy.next_mode()  # Shift+Tab: Vio cambia faccia e frase
    assert app.vio_state() == ("auto-edit", mascot.SAYS["auto-edit"])
    app.handle_command("/plan")
    assert app.vio_state()[0] == "plan"
    app.handle_command("/deep")
    assert app.vio_state() == ("deep", mascot.SAYS["deep"])
    app.handle_command("/vio")
    assert app.vio_state() == ("love", "Grazie! ♥")
    text = "".join(t for _, t in app.prompt_message())
    assert "Vio · modalità plan · team deep" in text and "Grazie! ♥" in text and "▀" in text
    assert text.endswith("› ") and len(text.splitlines()) == 6  # 4 righe di Vio, la riga, l'input


def test_skill_command(settings, project, tmp_path, monkeypatch):
    from tests.test_agent import ScriptedLLM

    folder = project / ".mydevagent" / "skills" / "saluti"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\ndescription: Saluta in dialetto.\n---\nRispondi sempre in napoletano.\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    llm = ScriptedLLM(steps=["Uè!"])
    console = record_console()
    with create_pipe_input() as pipe:
        app = TuiApp(Orchestrator(settings, llm=llm), console=console, prompt_input=pipe,
                     prompt_output=DummyOutput(), root=project, background=False)
    assert list(completions(app.completer, "/skill sa")) == ["saluti"]
    app.handle_command("/skill")
    app.handle_command("/skill saluti ciao a tutti")
    app.handle_command("/skill boh")
    out = console.export_text()
    assert "saluti" in out and "Saluta in dialetto." in out and "skill sconosciuta: boh" in out
    request = llm.calls[-1]["messages"][-1]["content"]
    assert "Rispondi sempre in napoletano." in request and "ciao a tutti" in request
    assert app.session.history[-2]["content"] == "/skill saluti ciao a tutti"


def test_plugin_command(settings, project, tmp_path, monkeypatch):
    from tests.test_agent import ScriptedLLM
    from tests.test_plugins import make_plugin

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MYDEVAGENT_STATE_DIR", str(tmp_path / "state"))
    source = make_plugin(tmp_path / "scaricato")
    llm = ScriptedLLM(steps=["Rivisto."])
    console = record_console()
    with create_pipe_input() as pipe:
        app = TuiApp(Orchestrator(settings, llm=llm), console=console, prompt_input=pipe,
                     prompt_output=DummyOutput(), root=project, background=False)
    app.handle_command("/plugin")
    app.handle_command(f"/plugin install {source}")
    assert "/rivedi" in app.custom and "/rivedi" in app.completer.commands
    app.handle_command("/revisore:rivedi app.py")  # anche con il nome del plugin davanti, come in Claude Code
    assert "Rivedi app.py con" in llm.calls[-1]["messages"][-1]["content"]
    app.handle_command("/plugin")
    app.handle_command("/plugin remove revisore")
    app.handle_command("/plugin boh")
    out = console.export_text()
    assert "Nessun plugin" in out and "Installato revisore" in out and "1 comando · 1 skill · 1 agente" in out
    assert "1 comando · 1 skill · 1 agente · hook" in out and "Rimosso revisore" in out and "uso: /plugin" in out
    assert "/rivedi" not in app.custom


def test_new_project_command(settings, tmp_path):
    work = tmp_path / "lavori"
    (work / "vecchio").mkdir(parents=True)
    console = record_console()
    with create_pipe_input() as pipe:
        app = TuiApp(Orchestrator(settings, llm=FakeLLM()), console=console, prompt_input=pipe,
                     prompt_output=DummyOutput(), root=work, background=False)
    assert completions(app.completer, "/new bo") == ["bot-discord"]
    app.handle_command("/new")
    app.handle_command("/new boh")
    app.handle_command("/new api")
    out = console.export_text()
    assert "bot-discord" in out and "modello sconosciuto: boh" in out and "ora lavoro in" in out
    dest = (work / "api").resolve()
    assert app.root == dest and app.session.cwd == str(dest) and app.policy.root == dest
    assert "python -m pytest -q" in (dest / "MYDEVAGENT.md").read_text()
    assert "main.py" in completions(app.completer, "migliora @mai")[0]


def test_learning_mode_command(settings, project):
    def make_app():
        with create_pipe_input() as pipe:
            return TuiApp(Orchestrator(settings, llm=FakeLLM()), console=record_console(), prompt_input=pipe,
                          prompt_output=DummyOutput(), root=project, background=False)

    app = make_app()
    assert not app.learn
    app.handle_command("/impara")
    assert app.learn and "modalità ask · impara" in "".join(t for _, t in app.prompt_message())
    again = make_app()  # la scelta resta anche al prossimo avvio
    assert again.learn
    again.handle_command("/impara off")
    assert not again.learn and not make_app().learn


def test_preview_command(settings, project, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    (project / "index.html").write_text("<h1>Ciao</h1>")
    console = record_console()
    with create_pipe_input() as pipe:
        app = TuiApp(Orchestrator(settings, llm=FakeLLM()), console=console, prompt_input=pipe,
                     prompt_output=DummyOutput(), root=project, background=False)
    app.handle_command("/anteprima")
    app.handle_command("/anteprima localhost:5173")
    app.handle_command("/anteprima manca.html")
    assert opened[0].startswith("http://127.0.0.1:") and opened[0].endswith("/index.html")
    assert opened[1] == "http://localhost:5173" and len(opened) == 2
    assert "non trovo manca.html" in console.export_text()


def test_add_dir_command(settings, project, tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "app.py").write_text("x = 1\n")
    console = record_console()

    def make_app(**kwargs):
        with create_pipe_input() as pipe:
            return TuiApp(Orchestrator(settings, llm=FakeLLM()), console=console, prompt_input=pipe,
                          prompt_output=DummyOutput(), root=project, background=False, **kwargs)

    app = make_app()
    app.handle_command("/add-dir ../backend")
    app.handle_command("/add-dir ../nessuna")
    assert app.extra_dirs == [backend.resolve()] and "non trovo la cartella" in console.export_text()
    assert app.collect_attachments("guarda @../backend/app.py") == {"../backend/app.py": "x = 1\n"}
    again = make_app()  # ricordata per questo progetto
    assert again.extra_dirs == [backend.resolve()]
    again.handle_command("/add-dir rimuovi ../backend")
    assert again.extra_dirs == [] and make_app().extra_dirs == []
    once = make_app(extra_dirs=[backend])  # mydevagent --add-dir: solo per questa volta
    assert once.extra_dirs == [backend.resolve()] and make_app().extra_dirs == []
