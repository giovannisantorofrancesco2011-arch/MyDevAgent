"""UI da terminale stile Claude Code per MyDevAgent.

Architettura:
  - thread principale: input (prompt_toolkit), disegno (rich.Live), conferme dei permessi
  - thread worker: AgentRunner.run() (modalità agente) oppure Orchestrator.run() (modalità chat);
    comunica solo tramite una queue.Queue di eventi, chunk e richieste di conferma
  - Esc / Ctrl+C durante il lavoro impostano un threading.Event → il team si ferma
"""

from __future__ import annotations

import contextlib
import getpass
import json
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .. import health, plugins, templates
from .. import hooks as hooks_mod
from .. import license as license_mod
from .. import mcp as mcp_mod
from .. import stats as stats_mod
from .. import update as update_mod
from ..agent import CheckpointStore, PermissionPolicy
from ..agent.context import append_memory, collect_attachments, read_memory
from ..agent.permissions import MODE_LABELS, ApprovalRequest
from ..agent.permissions import MODES as PERMISSION_MODES
from ..agent.runner import LEARN_PROMPT, AgentRunner
from ..config import load_settings
from ..hooks import Hooks
from ..mcp import McpManager
from ..orchestrator import Orchestrator
from ..skills import load_skills
from ..subagents import load_subagents
from ..tools import preview as preview_mod
from ..tools.filesystem import Workspace, WorkspaceError, display_path
from . import extras, mascot, multi, statsview
from .apply import apply_answer
from .completion import DevCompleter
from .keys import EscWatcher
from .render import ACCENT, THEME, TurnRenderer, markdown, render_diff, set_theme
from .session import Session, list_sessions, state_dir

COMMANDS = {
    "/help": "mostra comandi e scorciatoie",
    "/fast": "modalità veloce (1 agente)",
    "/balanced": "team standard: piano, modifiche, test e review",
    "/deep": "team completo: security, performance, edge case",
    "/ultra-deep": "35 agenti: dibattito sul piano, mega review, ricerca web se serve (lento)",
    "/auto": "modalità scelta dal router (default)",
    "/plan": "modalità piano: l'agente legge e propone, non modifica nulla",
    "/permissions": "modalità dei permessi (ask · auto-edit · plan · auto) e regole salvate",
    "/undo": "annulla le modifiche ai file dell'ultimo turno",
    "/rewind": "torna a un punto precedente (annulla più turni)",
    "/diff": "tutte le modifiche fatte in questa sessione",
    "/chat": "modalità chat: risponde senza toccare i file (usa /apply per salvarli)",
    "/agent": "modalità agente: lavora direttamente sui file (default)",
    "/apply": "scrivi su disco i file dell'ultima risposta (modalità chat)",
    "/impara": "modalità impara: Vio spiega cosa fa e ti lascia scrivere un pezzo di codice · /impara off",
    "/add-dir": "lavora anche su un'altra cartella (es. il backend) · /add-dir <cartella> · /add-dir rimuovi <cartella>",
    "/anteprima": "apre nel browser il sito del progetto (su localhost) · /anteprima <file.html | url>",
    "/new": "crea un progetto pronto: sito, gioco, bot-discord, api, python · /new <modello> [nome]",
    "/multi": "multigiocatore: gli amici sulla tua rete seguono la sessione dal browser e scrivono all'agente "
              "· /multi stop",
    "/init": "crea MYDEVAGENT.md con comandi e convenzioni del progetto",
    "/memory": "mostra la memoria del progetto · /memory <testo> aggiunge una nota",
    "/compact": "riassume la conversazione per liberare contesto",
    "/context": "quanto contesto del modello stai usando (istruzioni, riassunto, messaggi, libero)",
    "/model": "cambia modello principale · /model <nome> [--save]",
    "/models": "modelli installati e modelli in uso",
    "/pull": "scarica un modello da Ollama · /pull <nome>",
    "/agents": "elenca gli agenti del team e i sotto-agenti (formato Claude Code)",
    "/files": "file allegati all'ultimo messaggio",
    "/cost": "token e tempo della sessione",
    "/stats": "statistiche: richieste, token, file, giorni di fila e grafico dell'attività · /stats 7 · /stats 30",
    "/think": "mostra/nascondi il ragionamento del modello",
    "/index": "indicizza il progetto per la ricerca semantica",
    "/doctor": "verifica backend, modelli, rete, sandbox",
    "/theme": "tema dark / light",
    "/vio": "saluta Vio, la mascotte (e accarezzala)",
    "/skill": "skill disponibili · /skill <nome> [richiesta] per usarne una",
    "/plugin": "plugin (formato Claude Code) · /plugin install <utente/repo> · update · remove",
    "/hooks": "hook attivi (comandi automatici) · /hooks trust attiva quelli del progetto",
    "/mcp": "server MCP (strumenti esterni) · /mcp reload · /mcp trust",
    "/licenza": "la tua licenza · /licenza <chiave> la attiva · /licenza rimuovi la toglie da questo computer",
    "/update": "aggiorna MyDevAgent all'ultima versione (modelli e impostazioni restano)",
    "/resume": "riprendi una sessione precedente in questa cartella",
    "/export": "salva la conversazione in Markdown",
    "/clear": "nuova conversazione",
    "/exit": "esci",
}
MODES = ("auto", "fast", "balanced", "deep", "ultra-deep")
SHELL_TIMEOUT = 120
MAX_SHELL_OUTPUT = 8000
NOTIFY_AFTER_S = 20
AUTO_COMPACT_MESSAGES = 20
GUEST_TURN = object()  # l'input si chiude da solo: ha scritto un amico collegato con /multi


class TuiApp:
    def __init__(
        self,
        orchestrator: Orchestrator | None = None,
        *,
        profile: str | None = None,
        console: Console | None = None,
        prompt_input=None,
        prompt_output=None,
        ask: Callable[[str], str] | None = None,
        session: Session | None = None,
        root: Path | None = None,
        permission_mode: str = "ask",
        agent_mode: bool = True,
        background: bool = True,
        startup_check: bool | None = None,
        extra_dirs: list[Path] | None = None,
    ) -> None:
        self.orch = orchestrator or Orchestrator(load_settings(overrides={"profile": profile} if profile else None))
        self.console = console or Console()
        self.root = (root or Path.cwd()).resolve()
        self.session = session or Session(cwd=str(self.root))
        self.mode = self.session.mode
        self.agent_mode = agent_mode
        self.policy = PermissionPolicy(mode=permission_mode, root=self.root)
        self.checkpoints = CheckpointStore(self.root)
        existing = self.checkpoints.list()
        self.session_start_cp = (existing[-1].id + 1) if existing else 1
        self.show_thinking = False
        self.last_answer = next((m["content"] for m in reversed(self.session.history)
                                 if m["role"] == "assistant"), "")
        self.last_files: dict[str, str] = {}
        self.pending_context: dict[str, str] = {}  # output di comandi `!` da allegare al prossimo turno
        self.stats = {"tokens": 0, "turns": 0, "seconds": 0.0}
        self.ctx_percent: int | None = None  # per la barra in basso: si aggiorna dopo ogni turno e con /context
        self.turn_log: list[dict[str, Any]] = []  # le richieste di questa sessione, per /stats
        self.names = {a.key: a.name for a in self.orch.registry}
        self.model = self.orch.settings.resolve_model("main")[0]
        self.online: bool | None = None
        self.branch = self._git_branch()
        self._ask = ask
        self._startup_check = background if startup_check is None else startup_check
        self._ctrl_c_at = 0.0
        self.custom = extras.custom_commands(self.root)
        self.hooks = Hooks(self.root)
        self.mcp = McpManager(self.root, configs={})  # si avviano in start_project, dopo il tuo sì
        self._load_prefs()
        self.learn = bool(self._prefs().get("learn"))  # modalità impara
        self.extra_dirs = self._saved_dirs()  # cartelle in più: quelle ricordate per il progetto + --add-dir
        self.extra_dirs += [d for d in (Path(p).expanduser().resolve() for p in extra_dirs or [])
                            if d.is_dir() and d != self.root and d not in self.extra_dirs]
        all_commands = {**COMMANDS, **{k: v[0] for k, v in self.custom.items()}}
        self.completer = DevCompleter(all_commands, dict(self.orch.registry.by_alias), self.root)
        names = {"/skill": lambda: list(load_skills(self.root)), "/new": lambda: list(templates.TEMPLATES)}
        self.completer.arguments = {**names, "/skills": names["/skill"], "/stats": lambda: ["7", "30", "sempre"]}
        self._vio_event: tuple[str | None, str, tuple] | None = None
        self.room: multi.Room | None = None  # multigiocatore (/multi)
        self._at_prompt = False
        self._draft = ""  # quello che stavi scrivendo quando è arrivato il messaggio di un amico
        self.say("Ciao, sono Vio! Scrivi qui sotto cosa vuoi fare.")
        self.prompt = self._build_prompt(prompt_input, prompt_output, animate=background)
        if background:
            threading.Thread(target=self._check_online, daemon=True).start()
            extras.warmup(self.orch.llm, self.orch.settings)
            threading.Thread(target=self._auto_index, daemon=True).start()

    # ------------------------------------------------------------ setup
    def _build_prompt(self, prompt_input, prompt_output, animate: bool = True) -> PromptSession:
        keys = KeyBindings()

        @keys.add("escape", "enter")  # Alt+Enter → nuova riga
        def _newline(event) -> None:
            event.current_buffer.insert_text("\n")

        @keys.add("c-j")  # Ctrl+J → nuova riga (funziona in ogni terminale)
        def _newline2(event) -> None:
            event.current_buffer.insert_text("\n")

        @keys.add("s-tab")  # Shift+Tab → cambia modalità dei permessi, come Claude Code
        def _cycle(event) -> None:
            self.policy.next_mode()
            event.app.invalidate()

        @keys.add("escape", "escape")  # Esc Esc → /rewind
        def _rewind(event) -> None:
            buffer = event.current_buffer
            buffer.text = "/rewind"
            buffer.validate_and_handle()

        try:
            history = FileHistory(str(state_dir() / "history"))
        except OSError:
            history = InMemoryHistory()
        return PromptSession(
            history=history,
            completer=self.completer,
            complete_while_typing=True,
            key_bindings=keys,
            bottom_toolbar=self.toolbar,
            erase_when_done=True,  # Vio resta solo nella zona di input: nella cronologia ristampiamo la domanda
            refresh_interval=0.5 if animate else None,  # tentacoli e palpebre
            reserve_space_for_menu=4,
            color_depth=_color_depth(),
            placeholder=[("class:placeholder", "Chiedi qualcosa… / comandi · @ file · ! shell · # memoria")],
            style=_style(),
            input=prompt_input,
            output=prompt_output,
        )

    def _prefs_path(self) -> Path:
        return state_dir() / "config.json"

    def _prefs(self) -> dict[str, Any]:
        try:
            return json.loads(self._prefs_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _load_prefs(self) -> None:
        set_theme(self._prefs().get("theme", "dark"))

    def _save_pref(self, key: str, value: Any) -> None:
        path = self._prefs_path()
        try:
            prefs = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except ValueError:
            prefs = {}
        prefs[key] = value
        path.write_text(json.dumps(prefs, indent=1), encoding="utf-8")

    def _saved_dirs(self) -> list[Path]:
        saved = self._prefs().get("dirs", {}).get(str(self.root), [])
        return [Path(d) for d in saved if Path(d).is_dir()]

    def _add_dir(self, arg: str) -> None:
        """/add-dir: altre cartelle in cui l'agente legge, cerca e modifica (ricordate per questo progetto)."""
        c = self.console
        action, _, rest = arg.partition(" ")
        remove = action.lower() in ("rimuovi", "remove", "togli")
        target = (rest if remove else arg).strip().strip('"')
        if target:
            folder = (self.root / Path(target).expanduser()).resolve()
            if remove:
                self.extra_dirs = [d for d in self.extra_dirs if d != folder]
            elif not folder.is_dir():
                c.print(f"[red]⎿  non trovo la cartella {escape(str(folder))}[/]", highlight=False)
                return
            elif folder == self.root or folder in self.extra_dirs:
                c.print("[dim]⎿  questa cartella c'è già[/]")
                return
            else:
                self.extra_dirs.append(folder)
            dirs = self._prefs().get("dirs", {})
            dirs[str(self.root)] = [str(d) for d in self.extra_dirs]
            self._save_pref("dirs", dirs)
        if not self.extra_dirs:
            c.print("[dim]⎿  L'agente lavora solo in questa cartella. /add-dir <cartella> ne aggiunge un'altra, "
                    "per esempio /add-dir ../backend[/]", highlight=False)
            return
        if target and not remove:
            c.print(f"[green]⏺[/] Aggiunta {escape(display_path(self.root, folder))}: l'agente può leggere, cercare "
                    "e modificare anche lì (con i soliti permessi)", highlight=False)
            self.say("Ora lavoro su più cartelle insieme!", "love")
        c.print("[dim]⎿  cartelle: questo progetto · "
                + " · ".join(escape(display_path(self.root, d)) for d in self.extra_dirs)
                + " · /add-dir rimuovi <cartella> per toglierne una[/]", highlight=False)

    def _check_online(self) -> None:
        try:
            self.online = self.orch.toolbox.ctx.connectivity.online()
        except Exception:
            self.online = False

    def _auto_index(self) -> None:
        """Crea l'indice RAG in background alla prima apertura di un progetto (se piccolo abbastanza)."""
        rag = self.orch.settings.tools.rag
        index_file = self.root / rag.index_dir / "index.json"
        if not rag.enabled or index_file.exists() or not (self.root / ".git").exists():
            return
        try:
            count = sum(1 for _ in Workspace(self.root).iter_files())
            if count > 2000:
                return
            from ..tools.rag import CodeIndex

            settings = self.orch.settings.model_copy(deep=True)
            settings.tools.filesystem.root = str(self.root)
            extras_private_dir(self.root)
            CodeIndex.for_workspace(settings, self.orch.llm).build()
        except Exception:
            pass

    def _git_branch(self) -> str:
        try:
            out = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--abbrev-ref", "HEAD"],
                                 capture_output=True, text=True, timeout=3)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    # ------------------------------------------------------------- vista
    def expression(self) -> str:
        """Espressione di Vio: la modalità dei permessi, oppure chat."""
        return self.policy.mode if self.agent_mode else "chat"

    def _width(self) -> int:
        try:
            from prompt_toolkit.application import get_app

            return max(20, get_app().output.get_size().columns)
        except Exception:
            return self.console.width

    # ------------------------------------------------------------------ Vio
    def _mode_key(self) -> tuple:
        return (self.policy.mode, self.agent_mode, self.mode)

    def say(self, text: str, expression: str | None = None) -> None:
        """Cosa dice Vio sopra l'input. Vale finché non cambi modalità."""
        self._vio_event = (expression, text, self._mode_key())
        if self.room:
            self.room.publish({"kind": "vio", "text": text})

    def vio_state(self) -> tuple[str, str]:
        """(espressione, frase) di Vio in questo momento."""
        if self._vio_event and self._vio_event[2] == self._mode_key():
            expression, text, _ = self._vio_event
            return expression or self.expression(), text
        return self.expression(), mascot.SAYS[self.expression()]

    def prompt_message(self):
        """Vio con il suo fumetto, una riga, poi l'input: come Claude Code e BluAgent."""
        now = time.monotonic()
        expression, text = self.vio_state()
        if expression in ("ask", "chat", "auto-edit", "done") and int(now * 2) % 12 == 0:
            expression = "blink"
        width = self._width()
        label = (f"modalità {self.policy.mode}" if self.agent_mode else "modalità chat") + (" · impara" if self.learn
                                                                                             else "")
        room = max(10, width - mascot.WIDTH - 4)
        speech = [
            [],
            [("class:vio.name", mascot.NAME), ("class:tb.dim", f" · {label} · team {self.mode}"[:room])],
            [("class:vio.say", text if len(text) <= room else text[: room - 1] + "…")],
            [],
        ]
        out: list[tuple[str, str]] = []
        for line, bubble in zip(mascot.fragments(expression, int(now) % 2), speech, strict=True):
            out += [("", " ")] + line + [("", "  ")] + bubble + [("", "\n")]
        return out + [("class:rule", "─" * width + "\n"), ("class:prompt", "› ")]

    def toolbar(self):
        net = {True: ("class:tb.ok", "✓ online"), False: ("class:tb.warn", "○ offline"),
               None: ("class:tb.dim", "… rete")}[self.online]
        perm = self.policy.mode
        perm_style = {"ask": "class:tb.key", "auto-edit": "class:tb.ok", "plan": "class:tb.plan",
                      "auto": "class:tb.warn"}[perm]
        arrows = {"ask": "⏵", "auto-edit": "⏵⏵", "plan": "⏸", "auto": "⏵⏵⏵"}[perm]
        parts = [("class:rule", "─" * self._width() + "\n"), ("", " ")]
        if self.agent_mode:
            parts += [(perm_style, f"{arrows} modalità {perm}"), ("class:tb.dim", " (shift+tab per cambiare)")]
        else:
            parts += [("class:tb.key", "⏵ modalità chat"), ("class:tb.dim", " (/agent per modificare i file)")]
        parts += [
            ("class:tb.dim", " · "), ("class:tb", self.model),
            ("class:tb.dim", " · team "), ("class:tb.key", self.mode), ("class:tb.dim", " · "), net,
            ("class:tb.dim", f" · ~{self.stats['tokens']:,} tok".replace(",", ".")),
            *([("class:tb.dim", " · ctx "), ("class:tb.warn" if self.ctx_percent >= 70 else "class:tb.dim",
                                                f"{self.ctx_percent}%")] if self.ctx_percent is not None else []),
            ("class:tb.dim", " · / per i comandi"),
        ]
        if self.branch:
            parts.append(("class:tb.dim", f" ·  {self.branch}"))
        return parts

    def banner(self) -> None:
        from .. import __version__

        settings = self.orch.settings
        memory = "MYDEVAGENT.md ✓" if read_memory(self.root) else "nessuna memoria (/init per crearla)"
        skills = load_skills(self.root)
        skill_line = (f"{len(skills)} caricate · /skill per vederle" if skills
                      else "nessuna (.mydevagent/skills/<nome>/SKILL.md)")
        found = plugins.load_plugins(self.root)
        plugin_line = (f"{len(found)} attivi · /plugin per vederli" if found
                       else "nessuno (/plugin install <utente/repo>)")
        tiers = " · ".join(f"{t} {settings.resolve_model(t)[0]}" for t in ("fast", "reasoning"))
        body = (
            f"[bold {ACCENT}]✻[/] [bold]Benvenuto in MyDevAgent[/]  [dim]v{__version__} · "
            f"{len(self.orch.registry)} agenti · local-first[/]\n\n"
            f"[dim]cwd:[/]      {escape(str(self.root))}\n"
            f"[dim]profilo:[/]  {settings.profile} · [dim]modello:[/] {escape(self.model)} [dim]· {escape(tiers)}[/]\n"
            f"[dim]hardware:[/] {self._hardware}\n"
            f"[dim]memoria:[/]  {memory}\n"
            f"[dim]skill:[/]    {skill_line}\n"
            f"[dim]plugin:[/]   {plugin_line}\n"
            + (f"[dim]cartelle:[/] {escape(' · '.join(display_path(self.root, d) for d in self.extra_dirs))} "
               "[dim](/add-dir)[/]\n" if self.extra_dirs else "") + "\n"
            "[dim]Suggerimenti:[/]\n"
            f"  [{ACCENT}]•[/] chiedi di modificare il codice: l'agente legge, modifica, lancia i test e ti mostra i diff\n"
            f"  [{ACCENT}]•[/] [bold]/[/] comandi · [bold]@file[/] allega · [bold]![/]shell · [bold]#[/]nota in memoria\n"
            f"  [{ACCENT}]•[/] [bold]Shift+Tab[/] modalità · [bold]Esc[/] interrompe · [bold]/undo[/] annulla · "
            "[bold]Esc Esc[/] torna indietro · [bold]/vio[/] saluta la mascotte"
        )
        self.console.print(Panel(body, border_style=ACCENT, padding=(1, 2)))
        if self.session.history:
            turns = len(self.session.history) // 2
            self.console.print(f"[dim]⎿  Ripresa sessione «{escape(self.session.title)}» ({turns} turni)[/]")

    @property
    def _hardware(self) -> str:
        if not hasattr(self, "_hw_text"):
            hw = health.detect_hardware()
            self._hw_text = (f"GPU {hw.gpu_gb:.0f} GB" if hw.gpu_gb else
                             f"Apple {hw.apple_gb:.0f} GB" if hw.apple_gb else f"RAM {hw.ram_gb:.0f} GB")
        return self._hw_text

    # --------------------------------------------------------- domande utente
    def _input(self, message: str) -> str:
        answer = self.prompt.prompt(message, bottom_toolbar=None, completer=None, placeholder="").strip()
        self.console.print(f"[dim]{escape(message)}[/]{escape(answer)}")  # l'input viene cancellato: lo ristampo
        return answer

    def ask(self, question: str, allow_always: bool = True) -> str:
        if self._ask:
            return self._ask(question)
        options = "  [dim]1[/] Sì" + ("  [dim]2[/] Sì, e non chiedere più" if allow_always else "") + "  [dim]3[/] No"
        self.console.print(f"[bold]{escape(question)}[/]{options}")
        answer = self._input("  › ")
        valid = ("1", "2", "3") if allow_always else ("1", "3")
        return answer if answer in valid else "3"

    def ask_approval(self, req: ApprovalRequest) -> tuple[str, str]:
        """Conferma di una modifica o di un comando (chiamata dal thread della UI)."""
        c = self.console
        if req.tool in ("edit_file", "write_file"):
            path = req.args.get("path", "")
            c.print(f"\n[{ACCENT}]⏺[/] [bold]{escape(req.summary)}[/]")
            if req.diff:
                c.print(render_diff(req.diff, max_lines=80))
            question = f"Applicare la modifica a {path}?"
        elif req.tool == "web_fetch":
            question = f"Leggere le pagine di {req.args.get('host', 'questo sito')}?"
        elif req.tool == "mcp":
            question = f"Usare lo strumento MCP {req.args.get('server')}.{req.args.get('tool')}?"
        else:  # comando già mostrato dalla riga ⏺ Bash(...)/Test(...) del tool
            if req.dangerous:
                c.print("  [red]⚠ comando potenzialmente distruttivo: controlla bene[/]")
            question = "Eseguire questo comando?"
        choice = self.ask(question, allow_always=not req.dangerous)
        if choice == "1":
            return "yes", ""
        if choice == "2":
            return "always", ""
        feedback = "" if self._ask else self._input("  cosa devo fare invece? (Invio per saltare) › ")
        return "no", feedback

    # ----------------------------------------------------------- comandi
    def handle_command(self, text: str) -> bool:
        """Esegue un comando `/`. Restituisce False per uscire."""
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if ":" in cmd and cmd not in self.custom:  # /plugin:comando, come in Claude Code
            cmd = "/" + cmd.rsplit(":", 1)[1]
        c = self.console
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            table = Table(show_header=False, box=None, padding=(0, 2))
            for name, desc in {**COMMANDS, **{k: v[0] for k, v in self.custom.items()}}.items():
                table.add_row(f"[bold]{name}[/]", f"[dim]{escape(desc)}[/]")
            c.print(table)
            c.print("[dim]Scorciatoie: Tab completa · ↑/↓ cronologia · Alt+Enter/Ctrl+J nuova riga · Shift+Tab "
                    "permessi · Esc o Ctrl+C interrompe · Esc Esc torna indietro · Ctrl+D esce[/]")
        elif cmd[1:] in MODES and not arg:
            self.mode = self.session.mode = cmd[1:]
            key = "auto-team" if self.mode == "auto" else self.mode
            self.say(mascot.SAYS[key], key)
            c.print(f"[dim]⎿  team: {self.mode}[/]")
        elif cmd in ("/fast", "/balanced", "/deep", "/ultra-deep") and arg:
            self.submit(text)  # "/deep crea un'API" → il router gestisce il comando inline
        elif cmd == "/plan":
            self.policy.mode = "ask" if self.policy.mode == "plan" else "plan"
            c.print(f"[dim]⎿  modalità {self.policy.mode}: {mascot.SAYS[self.policy.mode]}[/]")
            if arg:
                self.submit(arg)
        elif cmd == "/permissions":
            if arg in PERMISSION_MODES:
                self.policy.mode = arg
            c.print(f"[dim]⎿  modalità: [bold]{self.policy.mode}[/] ({MODE_LABELS[self.policy.mode]}) · "
                    f"disponibili: {', '.join(PERMISSION_MODES)}[/]")
            rules = self.policy.allow_rules
            c.print("[dim]   regole «consenti sempre»: " + (", ".join(rules) if rules else "nessuna") + "[/]")
        elif cmd == "/undo":
            undone = self.checkpoints.undo()
            if not undone:
                c.print("[dim]⎿  Niente da annullare.[/]")
            else:
                cp, files = undone
                c.print(f"[green]⏺[/] Annullato «{escape(cp.label)}»\n  [dim]⎿  ripristinati: {', '.join(files)}[/]")
                self.completer.refresh()
        elif cmd == "/rewind":
            self._rewind()
        elif cmd == "/diff":
            diff = self.checkpoints.session_diff(self.session_start_cp)
            if diff.strip():
                c.print(Syntax(diff, "diff", theme=THEME["diff"], word_wrap=True, background_color="default"))
            else:
                c.print("[dim]⎿  Nessuna modifica in questa sessione.[/]")
        elif cmd in ("/chat", "/agent"):
            self.agent_mode = cmd == "/agent"
            c.print("[dim]⎿  " + ("modalità agente: lavoro direttamente sui file" if self.agent_mode
                                   else "modalità chat: rispondo senza modificare i file") + "[/]")
        elif cmd == "/apply":
            if not self.last_answer:
                c.print("[dim]⎿  Nessuna risposta da applicare.[/]")
            else:
                written = apply_answer(self.last_answer, self.orch.registry, self.root, c, self.ask)
                if written:
                    self.completer.refresh()
        elif cmd == "/new":
            self._new(arg)
        elif cmd == "/add-dir":
            self._add_dir(arg)
        elif cmd == "/multi":
            self._multi(arg)
        elif cmd == "/anteprima":
            self._preview(arg)
        elif cmd == "/impara":
            self.learn = arg.lower() in ("on", "sì", "si") or (arg.lower() not in ("off", "no") and not self.learn)
            self._save_pref("learn", self.learn)
            if self.learn:
                c.print("[dim]⎿  modalità impara: spiego cosa faccio e ti lascio un pezzo da scrivere (cerca "
                        "TODO(tu)) · /impara off per spegnerla[/]", highlight=False)
                self.say("Impariamo insieme! Qualche pezzo lo scrivi tu.", "love")
            else:
                c.print("[dim]⎿  modalità impara spenta: scrivo io tutto il codice[/]")
                self.say("Ok, torno a scrivere io tutto il codice.", "done")
        elif cmd == "/init":
            was_agent, self.agent_mode = self.agent_mode, True
            self.submit(extras.INIT_TASK, display="/init")
            self.agent_mode = was_agent
        elif cmd == "/memory":
            if arg:
                path = append_memory(self.root, arg)
                c.print(f"[dim]⎿  nota aggiunta a {path.name}[/]")
            else:
                memory = read_memory(self.root)
                c.print(Panel(escape(memory) if memory else "[dim]Nessuna memoria. Usa /init o #nota.[/]",
                              title="memoria del progetto", border_style="grey50", expand=False))
        elif cmd == "/compact":
            self._compact(manual=True)
        elif cmd == "/context":
            self._show_context()
        elif cmd == "/model":
            if not arg:
                c.print(f"[dim]⎿  modello principale: {self.model} · /models per l'elenco[/]")
            else:
                save = "--save" in arg.split()
                name = " ".join(w for w in arg.split() if w != "--save")
                profile = self.orch.settings.active_profile
                profile.main = name
                if isinstance(profile.reasoning, str) and profile.reasoning == self.model:
                    profile.reasoning = name
                self.model = name
                if save:
                    path = health.save_env({"MYDEVAGENT_MODEL_MAIN": name})
                    c.print(f"[dim]⎿  modello principale: {escape(name)} (salvato in {escape(str(path))})[/]")
                else:
                    c.print(f"[dim]⎿  modello principale: {escape(name)} (solo per questa sessione · "
                            "--save per ricordarlo)[/]")
                extras.warmup(self.orch.llm, self.orch.settings)
        elif cmd == "/models":
            self._models()
        elif cmd == "/pull":
            if not arg:
                c.print("[dim]⎿  uso: /pull <nome>, es. /pull qwen2.5-coder:7b[/]")
            else:
                self.pull_models(arg.split())
        elif cmd == "/agents":
            table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
            for col in ("#", "agente", "gruppo", "stage", "alias"):
                table.add_column(col)
            for a in self.orch.registry:
                group = "nucleo" if a.group == "core" else "ultra"
                table.add_row(str(a.id), a.name, group, a.stage, " ".join("@" + x for x in a.aliases))
            c.print(table)
            subs = load_subagents(self.root)
            if subs:
                table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2), expand=True)
                for col in ("sotto-agente", "da"):
                    table.add_column(col, no_wrap=True)
                table.add_column("tool", no_wrap=True, overflow="ellipsis", max_width=28)
                table.add_column("descrizione", no_wrap=True, overflow="ellipsis", ratio=1)
                for sub in subs.values():
                    table.add_row(f"[{ACCENT}]{escape(sub.name)}[/]", escape(sub.source),
                                  escape(", ".join(sub.tools) if sub.tools else "tutti"), escape(sub.description))
                c.print()
                c.print(table)
                c.print("[dim]L'agente li chiama da solo quando servono: lavorano in un contesto separato e "
                        "gli riportano il risultato[/]")
        elif cmd == "/files":
            c.print("[dim]⎿  " + (", ".join(self.last_files) or "nessun file allegato") + "[/]")
        elif cmd == "/stats":
            self._stats(arg)
        elif cmd == "/cost":
            c.print(f"[dim]⎿  {self.stats['turns']} turni · ~{self.stats['tokens']:,} token · "
                    f"{self.stats['seconds']:.0f}s di lavoro[/]".replace(",", "."))
        elif cmd == "/think":
            self.show_thinking = not self.show_thinking
            c.print(f"[dim]⎿  mostra ragionamento: {'sì' if self.show_thinking else 'no'}[/]")
        elif cmd == "/index":
            self._index()
        elif cmd == "/doctor":
            from ..cli import doctor

            doctor(profile=None)
        elif cmd in ("/skill", "/skills"):
            self._skill(arg)
        elif cmd in ("/plugin", "/plugins"):
            self._plugin(arg)
        elif cmd in ("/hooks", "/hook"):
            self._hooks(arg)
        elif cmd == "/mcp":
            self._mcp(arg)
        elif cmd == "/update":
            self._update()
        elif cmd in ("/licenza", "/license"):
            self._license(arg)
        elif cmd == "/vio":
            self._pats = getattr(self, "_pats", 0) + 1
            self.say(mascot.PATS[(self._pats - 1) % len(mascot.PATS)], "love")
        elif cmd == "/theme":
            name = arg or ("light" if THEME["diff"] == "ansi_dark" else "dark")
            set_theme(name)
            self._save_pref("theme", name)
            c.print(f"[dim]⎿  tema: {name}[/]")
        elif cmd == "/resume":
            self._resume()
        elif cmd == "/export":
            path = self.root / f"mydevagent-{self.session.id}.md"
            path.write_text(self.session.to_markdown(), encoding="utf-8")
            c.print(f"[dim]⎿  salvata in {escape(str(path))}[/]")
        elif cmd == "/clear":
            self.session = Session(cwd=str(self.root), mode=self.mode)
            self.last_answer, self.last_files, self.pending_context = "", {}, {}
            self.ctx_percent = None
            c.clear()
            self.banner()
        elif cmd in self.custom:
            self.submit(extras.expand_command(self.custom[cmd][1], arg), display=text)
        else:
            c.print(f"[red]⎿  comando sconosciuto: {escape(cmd)}[/] [dim](/help)[/]")
        return True

    def _license(self, arg: str) -> None:
        if arg.lower() in ("rimuovi", "remove"):
            self.console.print(f"[dim]⎿  {escape(license_mod.deactivate())}[/]")
            return
        state = license_mod.activate(arg) if arg else license_mod.status()
        color = "green" if state.ok else "red"
        self.console.print(f"[{color}]⎿  {escape(state.message)}[/]")
        if not arg and state.kind in ("prova", "scaduta"):
            self.console.print(f"[dim]   Abbonamento o acquisto una volta: {license_mod.BUY_URL}[/]")

    def _stats(self, arg: str) -> None:
        choice = arg.lower() or "sempre"
        if choice not in statsview.RANGES:
            self.console.print("[red]⎿  uso: /stats · /stats 7 (ultimi 7 giorni) · /stats 30[/]")
            return
        days = statsview.RANGES[choice]
        history = stats_mod.summarize(stats_mod.load())
        total = stats_mod.summarize(stats_mod.load(days)) if days else history
        self.console.print(statsview.render(stats_mod.summarize(self.turn_log), total, history,
                                            label=f"ultimi {days} giorni" if days else "da sempre",
                                            width=self.console.width))
        streak, _ = history.streaks()
        self.say(f"{streak} giorni di fila insieme! Continuiamo così." if streak > 1
                 else "Ecco cosa abbiamo fatto insieme!", "love")

    def _skill(self, arg: str) -> None:
        skills = load_skills(self.root)
        name, _, request = arg.partition(" ")
        if not name or name == "list":
            if not skills:
                self.console.print("[dim]⎿  Nessuna skill. Creane una in .mydevagent/skills/<nome>/SKILL.md "
                                   "(guida: docs/TUI.md)[/]")
                return
            table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2), expand=True)
            for col in ("skill", "da"):
                table.add_column(col, no_wrap=True)
            table.add_column("descrizione", no_wrap=True, overflow="ellipsis", ratio=1)
            for skill in skills.values():
                table.add_row(f"[{ACCENT}]{escape(skill.name)}[/]", skill.source, escape(skill.description))
            self.console.print(table)
            self.console.print("[dim]L'agente le usa da solo quando servono · /skill <nome> <richiesta> per "
                               "forzarne una[/]", highlight=False)
            return
        skill = skills.get(name.lower())
        if skill is None:
            self.console.print(f"[red]⎿  skill sconosciuta: {escape(name)}[/] [dim](/skill per l'elenco)[/]")
            return
        task = request.strip() or "Apply this skill to the current project."
        self.say(f"Uso la skill {skill.name}.", "think")
        self.submit(f"Follow the skill `{skill.name}` for this request.\n\n{skill.read()}\n\n# Request\n{task}",
                    display=f"/skill {arg}")

    def _new(self, arg: str) -> None:
        kind, _, name = arg.partition(" ")
        if kind.lower() not in templates.TEMPLATES:
            if kind:
                self.console.print(f"[red]⎿  modello sconosciuto: {escape(kind)}[/]")
            table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
            table.add_column("modello")
            table.add_column("cosa ottieni")
            for key, description in templates.TEMPLATES.items():
                table.add_row(f"[{ACCENT}]{key}[/]", description)
            self.console.print(table)
            self.console.print("[dim]/new <modello> \\[nome] · nasce in una cartella nuova qui (o in questa, se è "
                               "vuota), poi ci lavoro dentro[/]", highlight=False)
            return
        try:
            dest = templates.target_for(self.root, kind.lower(), name.strip())
            created = templates.create(kind.lower(), dest)
        except (ValueError, OSError) as exc:
            self.console.print(f"[red]⎿  {escape(str(exc))}[/]", highlight=False)
            return
        self.console.print(f"[green]⏺[/] Creato il progetto «{kind.lower()}» in {escape(str(dest))}", highlight=False)
        self.console.print(f"  [dim]⎿  {escape(', '.join(created))}[/]", highlight=False)
        if dest != self.root:
            self.switch_root(dest)
        self.say("Progetto pronto! Dimmi come vuoi personalizzarlo.", "love")

    def _preview(self, arg: str) -> None:
        """Il sito del progetto nel browser: un file HTML servito su localhost, oppure l'url di un server acceso."""
        target = arg.strip()
        if "://" in target or re.match(r"(localhost|127\.0\.0\.1)(:\d+)?(/|$)", target):
            url = target if "://" in target else f"http://{target}"
        else:
            page = target or "index.html"
            try:
                path = Workspace(self.root).resolve(page)
            except WorkspaceError:
                path = None
            if path is None or not path.is_file():
                self.console.print(f"[red]⎿  non trovo {escape(page)}[/] [dim]· /anteprima <file.html> oppure "
                                   "/anteprima <url>, es. /anteprima localhost:5173[/]", highlight=False)
                return
            url = f"{preview_mod.serve(self.root)}/{quote(path.relative_to(self.root).as_posix())}"
        opened = webbrowser.open(url)
        self.console.print(f"[green]⏺[/] Anteprima: {escape(url)}", highlight=False)
        self.console.print("  [dim]⎿  " + ("aperta nel browser" if opened else "aprila nel browser")
                           + " · resta accesa finché MyDevAgent è aperto · l'agente la guarda da solo quando "
                             "serve[/]", highlight=False)

    def _multi(self, arg: str) -> None:
        """/multi: gli amici sulla tua rete seguono la sessione dal browser e scrivono all'agente."""
        c = self.console
        if arg.lower() in ("stop", "off", "chiudi"):
            if self.room:
                self.room.system(f"{self.room.host} ha chiuso la sessione")
                self.room.close()
                self.room = None
                self.say("Sessione chiusa: siamo di nuovo solo noi due.", "done")
            c.print("[dim]⎿  multigiocatore spento[/]")
            return
        opened = self.room is None
        if opened:
            try:
                host = getpass.getuser()
            except Exception:
                host = "host"
            try:
                self.room = multi.Room(host)
            except OSError as exc:
                c.print(f"[red]⎿  non riesco ad aprire la sessione: {escape(str(exc))}[/]", highlight=False)
                return
            self.room.on_message = self._wake
            self.room.on_join = lambda name: self.say(f"{name} si è collegato! Ora lavoriamo insieme.", "love")
            for m in self.session.history[-4:]:  # chi entra vede com'eravate rimasti
                if m["role"] == "user":
                    self.room.publish({"kind": "message", "who": host, "text": m["content"]})
                else:
                    self.room.console.print(markdown(m["content"]))
                    self.room.flush()
            self.say("Multigiocatore acceso! Manda il link ai tuoi amici.", "love")
        link = self.room.link()
        c.print("[green]⏺[/] Multigiocatore: chi è sulla tua stessa rete (Wi-Fi) apre questo link", highlight=False)
        c.print(f"  [dim]⎿[/]  [bold {ACCENT}]{link}[/]", highlight=False)
        c.print(f"     [dim]codice {self.room.code} · /multi stop per chiudere[/]", highlight=False)
        c.print("     [dim]vede cosa fa l'agente e gli scrive; modifiche e comandi li confermi sempre tu[/]")
        if self.room.guests:
            c.print(f"     [dim]si sono collegati: {escape(', '.join(self.room.guests))}[/]", highlight=False)
        if "//127." in link:
            c.print("     [yellow]non vedo nessuna rete: collegati al Wi-Fi e rifai /multi[/]")
        elif opened and sys.platform == "win32":
            c.print("     [dim]se Windows chiede il permesso per Python, consentilo sulle reti private[/]")

    def _wake(self) -> None:
        """Un amico ha scritto: se sei fermo all'input lo faccio passare (quello che stavi scrivendo resta lì)."""
        loop = self.prompt.app.loop
        if self._at_prompt and loop:
            with contextlib.suppress(RuntimeError):  # l'input si è appena chiuso
                loop.call_soon_threadsafe(self._leave_prompt)

    def _leave_prompt(self) -> None:
        app = self.prompt.app
        if self._at_prompt and app.future and not app.future.done():
            self._draft = app.current_buffer.text
            app.exit(result=GUEST_TURN)

    def _guests_waiting(self) -> None:
        if self.room and not self.room.inbox.empty():
            self._leave_prompt()

    def _handle_guests(self) -> None:
        """I messaggi degli amici: l'agente li esegue come i tuoi."""
        while self.room and (message := self.room.next_message()):
            name, text = message
            self.console.print(f"[{ACCENT}]›[/] [bold]{escape(name)}:[/] {escape(text)}", highlight=False)
            self.submit(f"{name}: {text}", guest=True)
            self.console.print()

    def switch_root(self, path: Path) -> None:
        """Lavora in un'altra cartella (dopo /new): conversazione, checkpoint, permessi e plugin di lì."""
        self.session.save()
        self.root = Path(path).resolve()
        self.session = Session(cwd=str(self.root), mode=self.mode)
        self.policy = PermissionPolicy(mode=self.policy.mode, root=self.root)
        self.checkpoints = CheckpointStore(self.root)
        existing = self.checkpoints.list()
        self.session_start_cp = (existing[-1].id + 1) if existing else 1
        self.last_answer, self.last_files, self.pending_context = "", {}, {}
        self.extra_dirs = self._saved_dirs()
        self.branch = self._git_branch()
        self.custom = extras.custom_commands(self.root)
        self.completer.commands = {**COMMANDS, **{k: v[0] for k, v in self.custom.items()}}
        self.completer.root = self.root
        self.completer.refresh()
        self.start_project()
        self.console.print(f"  [dim]⎿  ora lavoro in {escape(str(self.root))}[/]", highlight=False)

    def _update(self) -> None:
        with self.console.status(f"[{ACCENT}]Aggiorno MyDevAgent…[/]"):
            result = update_mod.update()
        color = "green" if result.ok else "red"
        self.console.print(f"[{color}]⏺[/] {escape(result.message)}", highlight=False)
        for change in result.changes:
            self.console.print(f"  [dim]• {escape(change)}[/]", highlight=False)
        if result.restart:
            self.console.print(f"[{ACCENT}]⎿  Riavvia MyDevAgent per usare la nuova versione (/exit e poi riaprilo)[/]")
            self.say("Mi sono aggiornata! Riavviami per vedere le novità.", "love")
        elif result.ok:
            self.say("Sono già aggiornata!", "done")
        else:
            self.say("Non sono riuscita ad aggiornarmi: leggi qui sopra.", "error")

    def _check_updates(self) -> None:
        """In background all'avvio: se su GitHub ci sono novità, Vio lo dice."""
        count = update_mod.available()
        if count:
            self.say(f"Ci sono {count} novità di MyDevAgent: scrivi /update per averle.", "love")

    def start_project(self) -> None:
        """Chiede il sì per hook e server MCP del progetto (una volta, o quando cambiano), poi li avvia."""
        hooks_pending, mcp_pending = hooks_mod.untrusted(self.root), mcp_mod.untrusted(self.root)
        if hooks_pending or mcp_pending:
            what = " e ".join(filter(None, [f"{len(hooks_pending)} hook" if hooks_pending else "",
                                            f"{len(mcp_pending)} server MCP" if mcp_pending else ""]))
            self.console.print(f"[bold]Questo progetto ha {what}: programmi che partono da soli.[/]")
            for hook in hooks_pending[:8]:
                self.console.print(f"  [dim]{hook.event}[/] {escape(hook.command[:90])} [dim]({hook.source})[/]",
                                   highlight=False)
            for server in mcp_pending[:8]:
                self.console.print(f"  [dim]MCP {escape(server.name)}[/] {escape(server.describe()[:90])}",
                                   highlight=False)
            answer = self._reply("  Li attivo? Solo se ti fidi di questo progetto [s/N] › ")
            if answer.strip().lower() in ("s", "si", "sì", "y", "yes"):
                hooks_mod.allow(self.root)
                mcp_mod.allow(self.root)
        self.hooks = Hooks(self.root)
        if any(h.event == "SessionStart" and not h.unsupported for h in self.hooks.hooks):
            # in background: alcuni plugin installano pacchetti all'avvio e non devono bloccarti
            threading.Thread(target=self.hooks.start, args=("startup",), daemon=True).start()
        self._start_mcp()

    def _start_mcp(self) -> None:
        self.mcp.close()
        self.mcp = McpManager(self.root)
        if self.mcp:  # si collegano in background: npx può metterci un po' la prima volta
            threading.Thread(target=self.mcp.connect_all, daemon=True).start()

    def _mcp(self, arg: str) -> None:
        c = self.console
        if arg.lower() in ("reload", "trust"):
            if arg.lower() == "trust":
                mcp_mod.allow(self.root)
            self._start_mcp()
            self.mcp.connect_all()
        pending = mcp_mod.untrusted(self.root)
        if not self.mcp and not pending:
            c.print("[dim]⎿  Nessun server MCP. Si configurano come in Claude Code: .mcp.json nel progetto o "
                    "~/.mydevagent/mcp.json (guida: docs/TUI.md)[/]", highlight=False)
            return
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2), expand=True)
        for col in ("server", "da", "stato"):
            table.add_column(col, no_wrap=True)
        table.add_column("comando / url", no_wrap=True, overflow="ellipsis", ratio=1)
        for server in self.mcp.servers.values():
            if server.error:
                state = "[red]errore[/]"
            elif server.transport:
                state = f"[green]✓[/] {len(server.tools)} strumenti"
            else:
                state = "[dim]non avviato[/]"
            table.add_row(f"[{ACCENT}]{escape(server.name)}[/]", escape(server.config.source), state,
                          escape(server.config.describe()))
        c.print(table)
        for server in self.mcp.servers.values():
            if server.error:
                c.print(f"[red]⎿  {escape(server.name)}: {escape(server.error[:200])}[/]", highlight=False)
        if pending:
            c.print(f"[yellow]⎿  {len(pending)} server del progetto sono spenti: /mcp trust per attivarli[/]")
        c.print("[dim]L'agente li usa da solo quando servono · /mcp reload li riavvia[/]", highlight=False)

    def _hooks(self, arg: str) -> None:
        c = self.console
        if arg.lower() == "trust":
            hooks_mod.allow(self.root)
            self.hooks = Hooks(self.root)
            c.print(f"[green]⏺[/] Hook del progetto attivati ({len(self.hooks.hooks)} hook attivi)")
            return
        pending = hooks_mod.untrusted(self.root)
        if not self.hooks.hooks and not pending:
            c.print("[dim]⎿  Nessun hook. Si scrivono come in Claude Code, in .mydevagent/settings.json o "
                    ".claude/settings.json (guida: docs/TUI.md)[/]", highlight=False)
            return
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2), expand=True)
        for col in ("evento", "matcher", "da"):
            table.add_column(col, no_wrap=True)
        table.add_column("comando", no_wrap=True, overflow="ellipsis", ratio=1)
        for hook in self.hooks.hooks:
            event = f"[dim]{hook.event}[/]" if hook.unsupported else f"[{ACCENT}]{hook.event}[/]"
            command = escape(hook.command) + (f" [dim]({hook.unsupported})[/]" if hook.unsupported else "")
            table.add_row(event, escape(hook.matcher or "*"), escape(hook.source), command)
        c.print(table)
        skipped = sum(1 for h in self.hooks.hooks if h.unsupported)
        if skipped:
            c.print(f"[dim]{skipped} hook in grigio non vengono eseguiti: usano funzioni di Claude Code che "
                    "MyDevAgent non ha ancora[/]")
        if pending:
            c.print(f"[yellow]⎿  {len(pending)} hook del progetto sono spenti: /hooks trust per attivarli[/]")

    def _plugin(self, arg: str) -> None:
        c = self.console
        action, _, target = arg.partition(" ")
        action, target = action.lower(), target.strip()
        found = plugins.load_plugins(self.root)
        if action in ("install", "update", "remove") and target:
            if action == "install":
                c.print(f"[dim]⎿  Installo {escape(target)}…[/]")
            try:
                if action == "install":
                    done = [f"Installato [bold]{escape(p.name)}[/] [dim]{self._plugin_summary(p)}[/]"
                            for p in plugins.install(target)]
                elif action == "update":
                    done = [f"Aggiornato [bold]{escape(target)}[/] [dim]{escape(plugins.update(target, self.root))}[/]"]
                else:
                    folder = plugins.remove(target, self.root)
                    done = [f"Rimosso [bold]{escape(target)}[/] [dim](cartella {escape(str(folder))})[/]"]
            except (ValueError, OSError) as exc:
                c.print(f"[red]⎿  {escape(str(exc))}[/]")
                return
            self.custom = extras.custom_commands(self.root)
            self.completer.commands = {**COMMANDS, **{k: v[0] for k, v in self.custom.items()}}
            for line in done:
                c.print(f"[green]⏺[/] {line}", highlight=False)
            if action == "install":
                self.say("Nuovo plugin! Trovi i comandi con /, le skill con /skill.", "love")
            return
        if action not in ("", "list"):
            c.print("[red]⎿  uso: /plugin [list] · install <utente/repo | url git | cartella> · update <nome> · "
                    "remove <nome>[/]", highlight=False)
            return
        if not found:
            c.print("[dim]⎿  Nessun plugin. Installane uno con /plugin install <utente/repo | url git | cartella>, "
                    "anche quelli di Claude Code (guida: docs/TUI.md)[/]", highlight=False)
            return
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2), expand=True)
        for col in ("plugin", "da", "contenuto"):
            table.add_column(col, no_wrap=True)
        table.add_column("descrizione", no_wrap=True, overflow="ellipsis", ratio=1)  # una riga per plugin
        for p in found.values():
            folder = plugins.folder_of(p)
            source = p.source + (f" · {folder.name}" if folder and folder.name != p.name else "")
            table.add_row(f"[{ACCENT}]{escape(p.name)}[/]", escape(source), self._plugin_summary(p),
                          escape(p.description))
        c.print(table)
        c.print("[dim]/plugin install <utente/repo | url | cartella> · /plugin update <nome> · "
                "/plugin remove <nome>[/]", highlight=False)

    @staticmethod
    def _plugin_summary(plugin: plugins.Plugin) -> str:
        names = (("commands", "comando", "comandi"), ("skills", "skill", "skill"), ("agents", "agente", "agenti"))
        parts = [f"{n} {one if n == 1 else many}" for kind, one, many in names if (n := plugin.count(kind))]
        return " · ".join(parts + plugin.features()) or "vuoto"

    def _rewind(self) -> None:
        checkpoints = self.checkpoints.list()
        if not checkpoints:
            self.console.print("[dim]⎿  Nessun punto a cui tornare.[/]")
            return
        recent = checkpoints[-10:]
        for i, cp in enumerate(reversed(recent), start=1):
            when = time.strftime("%H:%M", time.localtime(cp.created))
            self.console.print(f"  [bold]{i}[/] [dim]{when}[/] {escape(cp.label[:60])} [dim]({len(cp.files)} file)[/]")
        choice = self._input("  torna a prima di (numero) › ") if not self._ask else self._ask("rewind")
        if choice.isdigit() and 1 <= int(choice) <= len(recent):
            target = list(reversed(recent))[int(choice) - 1]
            files = self.checkpoints.rewind(target.id)
            self.console.print(f"[green]⏺[/] Tornato a prima di «{escape(target.label[:60])}»\n"
                               f"  [dim]⎿  ripristinati: {', '.join(files)}[/]")
            self.completer.refresh()

    def _models(self) -> None:
        installed, url = extras.list_models(self.orch.settings)
        if installed is None:
            self.console.print(f"[red]⎿  backend non raggiungibile: {url}[/] [dim](avvia Ollama: `ollama serve`)[/]")
            return
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("tier")
        table.add_column("modello")
        table.add_column("")
        for tier in ("main", "fast", "reasoning", "embed", "vision"):
            model, _ = self.orch.settings.resolve_model(tier)
            ok = model in installed or f"{model}:latest" in installed
            table.add_row(tier, model, "[green]✓[/]" if ok else f"[red]✗ ollama pull {model}[/]")
        self.console.print(table)
        self.console.print("[dim]Installati: " + ", ".join(sorted(installed)) + "[/]")

    # ------------------------------------------------------ controllo iniziale
    def startup_check(self) -> health.Health:
        """Backend spento o modelli mancanti → spiega cosa fare e offre di sistemarlo. Silenzioso se è tutto ok."""
        settings = self.orch.settings
        status = health.check_backends(settings)
        while status.down:
            urls = ", ".join(status.down)
            hint = health.start_hint(status.down[0])
            self.console.print(Panel(
                f"[bold]Il server dei modelli non risponde[/] su {escape(urls)}\n\n"
                f"Per avviarlo: {escape(hint)}.\n"
                "Se non l'hai ancora installato: https://ollama.com/download",
                title="MyDevAgent non riesce a partire", border_style="red", expand=False, padding=(0, 2)))
            answer = self._reply("  Invio per riprovare · c per continuare comunque › ")
            if answer.lower().startswith(("c", "q", "3")):
                return status
            status = health.check_backends(settings)
        missing = status.missing()
        if missing:
            self._offer_missing(status, missing)
        self._hardware_hint()
        return status

    def _offer_missing(self, status: health.Health, missing: list[health.TierStatus]) -> None:
        settings = self.orch.settings
        essential = [m for m in missing if m.tier in health.ESSENTIAL_TIERS]
        tiers_by_model: dict[str, list[str]] = {}
        for m in missing:
            tiers_by_model.setdefault(m.model, []).append(m.tier)
        lines = []
        for model, tiers in tiers_by_model.items():
            size = health.model_size(model)
            weight = f" · circa {size * 0.6 + 0.4:.1f} GB" if size else ""
            note = "" if set(tiers) & set(health.ESSENTIAL_TIERS) else " [dim](opzionale)[/]"
            lines.append(f"  [red]✗[/] {escape(model)} [dim]({', '.join(tiers)}{weight})[/]{note}")
        to_pull = list(dict.fromkeys(m.model for m in (essential or missing)))
        subs = health.substitutes(status)
        can_pull = health.is_ollama(missing[0].base_url)
        options = []
        if can_pull:
            options.append(("1", "Scaricali ora" + (" (solo quelli necessari)" if len(to_pull) < len(tiers_by_model)
                                                    else "")))
        if subs:
            used = ", ".join(f"{t}: {escape(m)}" for t, m in subs.items())
            options.append(("2", f"Usa i modelli che ho già ({used})"))
        options.append(("3", "Continua comunque"))
        title = "Mancano dei modelli" if essential else "Mancano dei modelli opzionali"
        body = (f"[bold]Il profilo {settings.profile} usa modelli che non sono installati:[/]\n"
                + "\n".join(lines) + "\n\n" + "\n".join(f"  [bold]{k}[/] {v}" for k, v in options))
        if not essential:
            body += "\n\n[dim]Senza questi MyDevAgent funziona lo stesso (ricerca nel codice senza embedding).[/]"
        self.console.print(Panel(body, title=title, border_style="yellow", expand=False, padding=(0, 2)))
        choice = self._reply("  scelta › ")
        if choice == "1" and can_pull:
            if self.pull_models(to_pull):
                extras.warmup(self.orch.llm, settings)
        elif choice == "2" and subs:
            health.apply_substitutes(settings, subs)
            self.model = settings.resolve_model("main")[0]
            self.console.print(f"[green]⏺[/] Uso i modelli installati · modello principale: {escape(self.model)}")
            save = self._reply("  Ricordare questa scelta per le prossime volte? (s/N) › ")
            if save.lower().startswith(("s", "y", "1")):
                path = health.save_env({f"MYDEVAGENT_MODEL_{t.upper()}": m for t, m in subs.items()})
                self.console.print(f"  [dim]⎿  salvato in {escape(str(path))}[/]")
            extras.warmup(self.orch.llm, settings)
        elif essential:
            self.console.print("[dim]⎿  Ok. Quando vuoi: /pull <nome> scarica un modello, /model <nome> lo cambia.[/]")

    def _hardware_hint(self) -> None:
        """Una volta sola: se il profilo non è adatto all'hardware, lo dice."""
        prefs = self._prefs()
        if prefs.get("hardware_hint"):
            return
        hw = health.detect_hardware()
        best = health.recommend_profile(hw)
        current = self.orch.settings.profile
        if best != current and current in health.PROFILE_ORDER:
            self.console.print(f"[yellow]⏺[/] Su questo PC ({escape(hw.describe())}) ti consiglio il profilo "
                               f"[bold]{best}[/] (ora usi {escape(current)}).\n  [dim]⎿  prova `mydevagent -p {best}` oppure "
                               f"MYDEVAGENT_PROFILE={best} nel file .env[/]")
        self._save_pref("hardware_hint", True)

    def pull_models(self, models: list[str]) -> bool:
        """Scarica i modelli da Ollama con barra di avanzamento. True se tutti scaricati."""
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

        base_url = self.orch.settings.resolve_model("main")[1].base_url
        if not health.is_ollama(base_url):
            self.console.print(f"[red]⎿  /pull funziona solo con Ollama (backend attuale: {escape(base_url)})[/]")
            return False
        all_ok = True
        for model in dict.fromkeys(models):
            columns = (TextColumn("  [bold]{task.description}[/]"), BarColumn(), DownloadColumn(),
                       TransferSpeedColumn(), TimeRemainingColumn())
            try:
                with Progress(*columns, console=self.console, transient=True) as progress:
                    task = progress.add_task(model, total=None)

                    def update(status: str, done: int, total: int, task=task, progress=progress, model=model) -> None:
                        label = model if status.startswith("pulling") else f"{model} · {status}"
                        progress.update(task, description=label, completed=done, total=total or None)

                    health.pull_model(base_url, model, update)
            except KeyboardInterrupt:
                self.console.print(f"[yellow]⎿  download di {escape(model)} interrotto[/]")
                return False
            except Exception as exc:
                _, hint = health.explain_error(exc, self.orch.settings)
                self.console.print(f"[red]⏺ Download di {escape(model)} non riuscito: {escape(str(exc))}[/]\n"
                                   f"  [dim]⎿  controlla il nome su https://ollama.com/library · {escape(hint)}[/]")
                all_ok = False
                continue
            self.console.print(f"[green]⏺[/] Scaricato {escape(model)}")
        return all_ok

    def _reply(self, message: str) -> str:
        return self._ask(message) if self._ask else self._input(message)

    def _index(self) -> None:
        from ..tools.rag import CodeIndex

        settings = self.orch.settings.model_copy(deep=True)
        settings.tools.filesystem.root = str(self.root)
        index = CodeIndex.for_workspace(settings, self.orch.llm)
        extras_private_dir(self.root)
        with self.console.status(f"[{ACCENT}]✻ Indicizzo il progetto…[/]"):
            stats = index.build()
        self.orch.toolbox.ctx.cache.pop("index", None)
        kind = "semantico" if stats["embedded"] else "lessicale"
        self.console.print(f"[green]⏺[/] Indice {kind}: {stats['chunks']} blocchi\n  [dim]⎿  {index.path}[/]")

    def _resume(self) -> None:
        sessions = [s for s in list_sessions(str(self.root)) if s.id != self.session.id]
        if not sessions:
            self.console.print("[dim]⎿  Nessuna sessione precedente in questa cartella.[/]")
            return
        for i, s in enumerate(sessions, start=1):
            when = time.strftime("%d/%m %H:%M", time.localtime(s.updated))
            self.console.print(f"  [bold]{i}[/] [dim]{when}[/] {escape(s.title)} [dim]({len(s.history) // 2} turni)[/]")
        choice = self._input("  numero › ")
        if choice.isdigit() and 1 <= int(choice) <= len(sessions):
            self.session = sessions[int(choice) - 1]
            self.mode = self.session.mode
            self.last_answer = next((m["content"] for m in reversed(self.session.history)
                                     if m["role"] == "assistant"), "")
            self.console.print(f"[dim]⎿  Ripresa «{escape(self.session.title)}»[/]")

    def _usage(self) -> extras.ContextUsage:
        usage = extras.context_usage(self.orch.settings, self.orch.registry.persona, self.root, self.session.history)
        self.ctx_percent = usage.percent
        return usage

    def _show_context(self) -> None:
        u = self._usage()
        width = 40
        parts = [(u.instructions, "#f0a8e0"), (u.summary, "yellow"), (u.messages, "cyan")]
        cells = [max(1, round(width * n / u.window)) if n else 0 for n, _ in parts]
        bar = "".join(f"[{color}]{'█' * c}[/]" for c, (_, color) in zip(cells, parts))
        bar += f"[grey37]{'░' * max(0, width - sum(cells))}[/]"
        n = lambda v: f"{v:,}".replace(",", ".")  # noqa: E731
        rows = [("#f0a8e0", "Istruzioni e memoria", u.instructions), ("yellow", "Riassunto", u.summary),
                ("cyan", f"Messaggi ({u.count})", u.messages), ("grey50", "Libero", u.free)]
        c = self.console
        c.print(f"\n [bold {ACCENT}]Contesto[/]\n  {bar}   [bold]{u.percent}%[/] di {n(u.window)} token\n")
        for color, label, value in rows:
            c.print(f"  [{color}]■[/] {label:<22} {n(value)} token")
        c.print(f"\n  [dim]Stima: circa 4 caratteri per token. Si riassume da sola all'{round(extras.AUTO_COMPACT_AT * 100)}% "
                "(o con /compact).[/]\n")

    def _compact(self, manual: bool = False) -> None:
        if len(self.session.history) < 4:
            if manual:
                self.console.print("[dim]⎿  Conversazione ancora corta, niente da compattare.[/]")
            return
        before = self._usage().used
        with self.console.status(f"[{ACCENT}]✻ Compatto la conversazione…[/]"):
            try:
                compacted = extras.compact_history(self.orch.llm, self.session.history)
            except Exception as exc:
                self.console.print(f"[red]⎿  compattazione fallita: {escape(str(exc))}[/]")
                return
        turns = len(self.session.history) // 2
        self.session.history = compacted
        self.session.save()
        freed = max(0, before - self._usage().used)
        self.console.print(f"  ⎿  [green]✓[/] Conversazione compattata ({turns} turni) · liberati circa "
                           f"{freed:,} token · [dim]/context[/]".replace(",", "."))
        for line in extras.summary_of(compacted).splitlines()[:12]:
            if line.strip():
                self.console.print(f"     [dim]{escape(line.strip().replace('**', ''))}[/]")

    # ------------------------------------------------------------- shell
    def run_shell(self, command: str) -> None:
        """`!comando`: esegue nella cartella del progetto e allega l'output al prossimo messaggio."""
        self.console.print(f"[{ACCENT}]⏺[/] Bash([bold]{escape(command)}[/])")
        try:
            proc = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True,
                                  timeout=SHELL_TIMEOUT)
            output, code = (proc.stdout + proc.stderr).strip(), proc.returncode
        except subprocess.TimeoutExpired:
            output, code = f"timeout dopo {SHELL_TIMEOUT}s", -1
        lines = output.splitlines()
        shown = lines[:15]
        for i, line in enumerate(shown):
            prefix = "  ⎿  " if i == 0 else "     "
            self.console.print(f"[dim]{prefix}{escape(line[:200])}[/]")
        if len(lines) > len(shown):
            self.console.print(f"[dim]     … altre {len(lines) - len(shown)} righe[/]")
        status = "[green]exit 0[/]" if code == 0 else f"[red]exit {code}[/]"
        self.console.print(f"     {status} [dim]· output allegato al prossimo messaggio[/]")
        self.pending_context[f"$ {command}"] = f"exit code {code}\n{output[-MAX_SHELL_OUTPUT:]}"

    # -------------------------------------------------------------- turni
    def collect_attachments(self, text: str) -> dict[str, str]:
        return collect_attachments(self.root, text, self.extra_dirs)

    def submit(self, text: str, display: str | None = None, guest: bool = False) -> str:
        state = license_mod.status()
        if not state.ok:
            self.console.print(f"[red]⎿  {escape(state.message)}[/]")
            self.say("Per continuare mi serve una licenza: /licenza <chiave>", "error")
            return ""
        if extras.needs_compact(self._usage(), self.session.history, AUTO_COMPACT_MESSAGES):
            self._compact()
        files = self.collect_attachments(text)
        for name in files:
            self.console.print(f"  [dim]⎿  allegato {escape(name)}[/]")
        files.update(self.pending_context)
        self.pending_context = {}
        self.last_files = files
        if self.room and not guest:
            self.room.publish({"kind": "message", "who": self.room.host, "text": display or text})
        mode = self.policy.mode
        if guest and mode in ("auto", "auto-edit"):
            self.policy.mode = "ask"  # quello che chiede un amico lo confermi sempre tu
        try:
            answer = self.run_turn(text, files)
        finally:
            self.policy.mode = mode
        self.session.add_turn(display or text, answer)
        with contextlib.suppress(Exception):
            self._usage()  # la percentuale nella barra in basso
        return answer

    def run_turn(self, text: str, files: dict[str, str]) -> str:
        events: queue.Queue = queue.Queue()
        cancel = threading.Event()
        room = self.room
        out = multi.Tee(self.console, room.console) if room else self.console  # con /multi lo vedono anche gli amici
        renderer = TurnRenderer(out, self.names)
        if room:
            room.publish({"kind": "busy", "on": True})
        started = time.monotonic()
        turn = stats_mod.Turn(project=str(self.root), session=self.session.id, model=self.model)
        pending_replies: list[queue.Queue] = []
        mode = None if self.mode == "auto" else self.mode

        def approver(req: ApprovalRequest) -> tuple[str, str]:
            reply: queue.Queue = queue.Queue(maxsize=1)
            pending_replies.append(reply)
            events.put(("approval", (req, reply)))
            return reply.get()

        def worker() -> None:
            try:
                if self.agent_mode:
                    extras_private_dir(self.root)
                    runner = AgentRunner(self.orch, self.root, self.policy, approver=approver,
                                         checkpoints=self.checkpoints, hooks=self.hooks, mcp=self.mcp,
                                         learn=self.learn, extra_dirs=self.extra_dirs)
                    stream = runner.run(text, history=self.session.history, files=files, mode=mode,
                                        on_event=lambda e: events.put(("event", e)), cancel=cancel)
                else:
                    learn = {"Learning mode (follow these instructions)": LEARN_PROMPT} if self.learn else {}
                    stream = self.orch.run(text, history=self.session.history, files={**files, **learn}, mode=mode,
                                           on_event=lambda e: events.put(("event", e)),
                                           show_thinking=self.show_thinking, cancel=cancel)
                for chunk in stream:
                    events.put(("chunk", chunk))
            except Exception as exc:
                events.put(("error", exc))
            finally:
                events.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()
        abandoned = False
        failed = ""
        watcher = EscWatcher()

        def interrupt() -> bool:
            """Primo Esc/Ctrl+C: ferma il team. Secondo: abbandona. Restituisce True per uscire dal ciclo."""
            if cancel.is_set():
                return True
            cancel.set()
            renderer.cancelled = True
            renderer.status = "Interrompo (di nuovo per forzare)"
            for reply in pending_replies:
                if reply.empty():
                    reply.put(("no", "interrupted by the user"))
            return False

        with watcher, Live(renderer.view(), console=self.console, refresh_per_second=12, transient=True) as live:
            while True:
                try:
                    if watcher.pressed() and interrupt():
                        abandoned = True
                        break
                    try:
                        kind, value = events.get(timeout=0.08)
                    except queue.Empty:
                        live.update(renderer.view())
                        continue
                    if kind == "done":
                        break
                    if kind == "approval":
                        req, reply = value
                        if cancel.is_set():
                            reply.put(("no", "interrupted by the user"))
                            continue
                        live.stop()
                        watcher.__exit__(None, None, None)
                        if room:
                            if req.diff:
                                room.console.print(f"[{ACCENT}]⏺[/] [bold]{escape(req.summary)}[/]")
                                room.console.print(render_diff(req.diff, max_lines=80))
                            room.console.print(f"  [dim]⎿  {escape(room.host)} deve confermare…[/]")
                            room.flush()
                        try:
                            answer = self.ask_approval(req)
                        except (KeyboardInterrupt, EOFError):
                            answer = ("no", "")
                            interrupt()
                        if room:
                            room.console.print("  [dim]⎿  confermato[/]" if answer[0] != "no"
                                               else "  [red]⎿  non confermato[/]")
                        if answer[0] != "no" and req.tool in ("edit_file", "write_file"):
                            renderer.shown_diffs.add(req.args.get("path", ""))
                        reply.put(answer)
                        watcher.__enter__()
                        live.start()
                    elif kind == "error":
                        title, hint = health.explain_error(value, self.orch.settings)
                        failed = title
                        out.print(f"[red]⏺ {escape(title)}[/]\n  [dim]⎿  {escape(hint)}[/]")
                    elif kind == "chunk":
                        renderer.on_chunk(value)
                    else:
                        renderer.on_event(value)
                        turn.on_event(value)
                    live.update(renderer.view())
                    if room:
                        room.flush()
                except KeyboardInterrupt:
                    if interrupt():
                        abandoned = True
                        break
            live.update("")
        renderer.finish()
        if abandoned:
            self.console.print("[dim]  ⎿  l'agente in corso terminerà in background[/]")
        if room:
            room.flush()
            room.publish({"kind": "busy", "on": False})
        elapsed = time.monotonic() - started
        self.stats["turns"] += 1
        self.stats["seconds"] += elapsed
        self.stats["tokens"] += renderer.tokens + len(renderer.answer) // 4
        turn.cancelled = turn.cancelled or renderer.cancelled
        self.turn_log.append(turn.finish(renderer.answer, estimate=not self.agent_mode, failed=bool(failed)))
        if elapsed > NOTIFY_AFTER_S and not self._ask:
            extras.notify("MyDevAgent", "Ho finito" if not renderer.cancelled else "Interrotto")
        answer = renderer.answer + ("\n\n[interrotto]" if renderer.cancelled else "")
        self.last_answer = renderer.answer
        self.completer.refresh()
        if failed:
            self.say(f"Ops: {failed}", "error")
        elif renderer.cancelled:
            self.say("Mi sono fermata. Dimmi come proseguire.", "think")
        else:
            self.say(f"Fatto in {elapsed:.0f}s! Cosa facciamo adesso?", "done")
        return answer

    # --------------------------------------------------------------- loop
    def loop(self) -> None:
        self.banner()
        if self._startup_check:
            self.startup_check()
        self.start_project()
        if self._startup_check:
            state = license_mod.status()
            if state.kind in ("prova", "scaduta"):
                self._license("")
            threading.Thread(target=self._check_updates, daemon=True).start()
        while True:
            self.console.print()
            self._handle_guests()
            try:
                self._at_prompt = True
                draft, self._draft = self._draft, ""
                text = self.prompt.prompt(self.prompt_message, default=draft, pre_run=self._guests_waiting)
                if text is GUEST_TURN:
                    continue
                text = text.strip()
                if text:
                    self.console.print(f"[{ACCENT}]›[/] {escape(text)}", highlight=False)
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - self._ctrl_c_at < 1.5:
                    break
                self._ctrl_c_at = now
                self.console.print("[dim]⎿  Ctrl+C di nuovo per uscire[/]")
                continue
            except EOFError:
                break
            finally:
                self._at_prompt = False
            if not text:
                continue
            if text.startswith("!"):
                if text[1:].strip():
                    self.run_shell(text[1:].strip())
                continue
            if text.startswith("#") and not text.startswith("##") and len(text) > 1:
                path = append_memory(self.root, text[1:])
                self.console.print(f"[dim]⎿  nota salvata in {path.name}[/]")
                continue
            if text.startswith("/") and not text.startswith("//"):
                if not self.handle_command(text):
                    break
                continue
            self.submit(text)
        self.mcp.close()
        if self.room:
            self.room.system(f"{self.room.host} ha chiuso MyDevAgent")
            self.room.close()
        self.session.save()
        self.console.print(f"[dim]Sessione salvata: {self.session.id} · riprendi con `mydevagent --continue`[/]")


def extras_private_dir(root: Path) -> None:
    """`.mydevagent/` nel progetto con un .gitignore interno: checkpoint e cache non finiscono in git."""
    folder = root / ".mydevagent"
    try:
        folder.mkdir(exist_ok=True)
        ignore = folder / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
    except OSError:
        pass


def _color_depth():
    """Colori pieni dove il terminale li supporta (Windows Terminal, iTerm, VS Code…): Vio resta viola vero."""
    import os

    from prompt_toolkit.output import ColorDepth

    if os.environ.get("COLORTERM") in ("truecolor", "24bit") or "WT_SESSION" in os.environ:
        return ColorDepth.TRUE_COLOR
    return None


def _style():
    from prompt_toolkit.styles import Style

    return Style.from_dict({
        "prompt": f"bold {ACCENT}",
        "placeholder": "#7a7a7a italic",
        "bottom-toolbar": "noreverse bg:default #9a9a9a",
        "tb": "#c0c0c0",
        "tb.key": f"bold {ACCENT}",
        "tb.dim": "#7a7a7a",
        "tb.ok": "#6fbf73",
        "tb.warn": "#e0b04a",
        "tb.plan": "#67e8f9",
        "vio.name": f"bold {ACCENT}",
        "vio.say": "#e9d5ff",
        "rule": "#7e22ce",
        "completion-menu.completion": "bg:#2b2b2b #d0d0d0",
        "completion-menu.completion.current": f"bg:{ACCENT} #ffffff",
        "completion-menu.meta.completion": "bg:#2b2b2b #8a8a8a",
        "completion-menu.meta.completion.current": f"bg:{ACCENT} #ffffff",
    })


def run_tui(profile: str | None = None, continue_last: bool = False, permission_mode: str = "ask",
            add_dirs: list[Path] | None = None) -> None:
    session = None
    if continue_last:
        previous = list_sessions(str(Path.cwd().resolve()), limit=1)
        session = previous[0] if previous else None
    TuiApp(profile=profile, session=session, permission_mode=permission_mode, extra_dirs=add_dirs).loop()
