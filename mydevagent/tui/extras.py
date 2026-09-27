"""Funzioni di supporto della UI: comandi personalizzati, compattazione, notifiche, warmup, modelli."""

from __future__ import annotations

import contextlib
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..plugins import load_plugins
from ..skills import split_frontmatter

COMPACT_PROMPT = (
    "Summarize this conversation between a user and a coding agent so work can continue. Keep: the user's "
    "goals and preferences, decisions taken, files changed and why, commands/tests and their results, open "
    "problems and next steps. Be dense, bullet points, max 300 words. Reply in the conversation's language."
)
INIT_TASK = (
    "Analyze this project with the tools (list_files, read_file on README/config/manifests, grep) and create "
    "the file MYDEVAGENT.md in the project root with these sections, max ~60 lines, only verified facts:\n"
    "# <project name>\n## Overview (2-3 lines)\n## Commands (build, run, test, lint — exact commands, "
    "format `- test: <command>`)\n## Architecture (main folders/modules and their role)\n"
    "## Conventions (style, naming, patterns, libraries to prefer)\n## Notes (pitfalls, things to avoid)\n"
    "If MYDEVAGENT.md already exists, improve it instead of overwriting useful content."
)


# ------------------------------------------------------------ comandi custom
def custom_commands(root: Path) -> dict[str, tuple[str, str]]:
    """Comandi `/nome` da file Markdown, nel formato di Claude Code.

    Da dove, il più specifico vince: `.mydevagent/commands` e `.claude/commands` del progetto,
    `~/.mydevagent/commands`, `~/.claude/commands` e i `commands/` dei plugin. `description:` (nel
    frontmatter o sulla prima riga) è la descrizione del completamento; `$ARGUMENTS`, `$1`, `$2`… vengono
    sostituiti con il testo dopo il comando e `${CLAUDE_PLUGIN_ROOT}` con la cartella del plugin.
    `!`comando`` diventa un'istruzione per l'agente, che lo esegue con i suoi tool e i soliti permessi.
    """
    state = Path(os.environ.get("MYDEVAGENT_STATE_DIR", Path.home() / ".mydevagent"))
    sources = [(d, f" (plugin {p.name})", p.path) for p in load_plugins(root).values() for d in p.dirs("commands")]
    sources += [(d, "", None) for d in (Path.home() / ".claude" / "commands", state / "commands",
                                         root / ".claude" / "commands", root / ".mydevagent" / "commands")]
    out: dict[str, tuple[str, str]] = {}
    for folder, suffix, plugin_root in sources:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.md")):
            meta, text = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            lines = text.strip().splitlines()
            if not meta and lines and lines[0].lower().startswith("description:"):
                meta, text = {"description": lines[0].split(":", 1)[1].strip()}, "\n".join(lines[1:])
            if plugin_root:
                text = text.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
            # ponytail: Claude Code esegue !`comando` prima di inviare; qui lo esegue l'agente, con i permessi
            text = re.sub(r"!`([^`\n]+)`", r"(run `\1` with your tools and use its output)", text)
            out["/" + path.stem.lower()] = ((meta.get("description") or "comando personalizzato") + suffix,
                                            text.strip())
    return out


def expand_command(template: str, arguments: str) -> str:
    words = arguments.split()
    text = re.sub(r"\$(\d)", lambda m: words[int(m[1]) - 1] if 0 < int(m[1]) <= len(words) else "", template)
    if "$ARGUMENTS" in template or text != template:
        return text.replace("$ARGUMENTS", arguments)
    return f"{template}\n\n{arguments}".strip()


# ------------------------------------------------------------- compattazione
def history_chars(history: list[dict[str, Any]]) -> int:
    return sum(len(str(m.get("content", ""))) for m in history)


SUMMARY_MARK = "[Summary of the previous conversation]"
SUMMARY_ACK = "OK, I have the context. Let's continue."
AUTO_COMPACT_AT = 0.85  # oltre questa parte del contesto la conversazione si riassume da sola


def compact_history(llm, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    transcript = "\n\n".join(f"{m['role']}: {m['content']}" for m in history)[-60_000:]
    summary = llm.complete([{"role": "system", "content": COMPACT_PROMPT},
                            {"role": "user", "content": transcript}], tier="fast", max_tokens=600,
                           temperature=0.1).text.strip()
    return [{"role": "user", "content": f"{SUMMARY_MARK}\n{summary}"},
            {"role": "assistant", "content": SUMMARY_ACK}]


def summary_of(history: list[dict[str, Any]]) -> str:
    """Il riassunto fatto da /compact, se la conversazione ne ha uno."""
    for m in history:
        content = str(m.get("content", ""))
        if content.startswith(SUMMARY_MARK):
            return content.removeprefix(SUMMARY_MARK).strip()
    return ""


@dataclass
class ContextUsage:
    """Quanto del contesto del modello occupa una richiesta (stima: ~4 caratteri per token)."""

    window: int
    instructions: int
    summary: int
    messages: int
    count: int  # messaggi della conversazione (senza il riassunto)

    @property
    def used(self) -> int:
        return self.instructions + self.summary + self.messages

    @property
    def free(self) -> int:
        return max(0, self.window - self.used)

    @property
    def percent(self) -> int:
        return min(100, round(100 * self.used / self.window)) if self.window else 0

    def to_dict(self) -> dict[str, int]:
        return {"window": self.window, "instructions": self.instructions, "summary": self.summary,
                "messages": self.messages, "count": self.count, "free": self.free, "percent": self.percent,
                "auto_compact": round(AUTO_COMPACT_AT * 100)}


def context_usage(settings, persona: str, root: Path, history: list[dict[str, Any]]) -> ContextUsage:
    """Le stesse parti che l'agente manda al modello: istruzioni, memoria e mappa del progetto, conversazione."""
    from ..agent.context import project_context
    from ..agent.loop import AGENT_RULES
    from ..agent.protocol import describe_tools
    from ..agent.tools import SPECS
    from ..state import render_history

    def tokens(text: str) -> int:
        return (len(text) + 3) // 4

    summary = [m for m in history if str(m.get("content", "")).startswith(SUMMARY_MARK)
               or m.get("content") == SUMMARY_ACK]
    rest = [m for m in history if m not in summary]
    instructions = "\n\n".join((persona, AGENT_RULES, describe_tools(SPECS), project_context(root)))
    return ContextUsage(window=settings.active_profile.num_ctx, instructions=tokens(instructions),
                        summary=sum(tokens(str(m.get("content", ""))) for m in summary),
                        messages=tokens(render_history(rest, settings.context)), count=len(rest))


def needs_compact(usage: ContextUsage, history: list[dict[str, Any]], max_messages: int) -> bool:
    return len(history) >= max_messages or (len(history) >= 4 and usage.percent >= AUTO_COMPACT_AT * 100)


# ------------------------------------------------------------------ notifiche
def notify(title: str, message: str) -> None:
    """Campanella del terminale + notifica desktop se disponibile (mai bloccante)."""
    sys.stdout.write("\a")
    sys.stdout.flush()
    try:
        system = platform.system()
        if system == "Linux" and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif system == "Darwin":
            clean = message.replace('"', "'")
            script = f'display notification "{clean}" with title "{title}"'
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# --------------------------------------------------------- modelli e warmup
def list_models(settings) -> tuple[set[str] | None, str]:
    model, backend = settings.resolve_model("main")
    try:
        resp = httpx.get(backend.base_url.rstrip("/") + "/models",
                         headers={"Authorization": f"Bearer {backend.api_key}"}, timeout=3)
        resp.raise_for_status()
        return {m["id"] for m in resp.json().get("data", [])}, backend.base_url
    except Exception:
        return None, backend.base_url


def warmup(llm, settings) -> threading.Thread:
    """Carica il modello principale in memoria in background: la prima risposta non paga il caricamento."""

    def run() -> None:
        with contextlib.suppress(Exception):
            llm.complete([{"role": "user", "content": "ok"}], tier="main", max_tokens=1, temperature=0.0)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
