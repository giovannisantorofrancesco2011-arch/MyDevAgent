"""Aggiornamento di MyDevAgent: `git pull` nella cartella dove è installato e, solo se sono cambiate, le
dipendenze. Modelli, impostazioni (.env), memoria e sessioni non vengono toccati: stanno altrove o non
sono nel repository."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent  # con l'installazione `pip install -e`, è la cartella del repository
EXTRAS = "[server,search]"  # le stesse di scripts/install.*


@dataclass
class Result:
    ok: bool
    message: str
    changes: list[str] = field(default_factory=list)  # i titoli dei commit nuovi
    restart: bool = False  # serve riavviare per usare la nuova versione


def _git(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(HOME), *args], capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def available() -> int:
    """Quante novità ci sono sul server (0 se offline, senza git o in caso di errore). Fa un `git fetch`."""
    if os.environ.get("MYDEVAGENT_OFFLINE") or not (HOME / ".git").exists():
        return 0
    try:
        if _git("fetch", "--quiet", timeout=20).returncode:
            return 0
        count = _git("rev-list", "--count", "HEAD..@{u}")
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return int(count.stdout.strip()) if count.returncode == 0 and count.stdout.strip().isdigit() else 0


def current() -> str:
    """La versione installata: «0d76099 del 2026-09-27» (vuota se non è una copia git)."""
    if not (HOME / ".git").exists():
        return ""
    try:
        out = _git("log", "-1", "--format=%h del %cs", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def update() -> Result:
    if not (HOME / ".git").exists():
        return Result(False, f"{HOME} non è una copia git (forse è uno zip): segui «Aggiornare» nel README per "
                             "collegarla a GitHub, poi /update funziona")
    try:
        before = _git("rev-parse", "HEAD").stdout.strip()
        deps_before = _digest(HOME / "pyproject.toml")
        pulled = _git("pull", "--ff-only")
    except FileNotFoundError:
        return Result(False, "serve git installato (https://git-scm.com)")
    except subprocess.TimeoutExpired:
        return Result(False, "GitHub non risponde: riprova tra poco")
    if pulled.returncode:
        lines = (pulled.stderr or pulled.stdout).strip().splitlines()
        reason = next((line for line in lines if line.startswith(("fatal:", "error:"))),
                      lines[0] if lines else "errore sconosciuto")
        hint = (" (hai modifiche locali nella cartella di MyDevAgent: `git stash` le mette da parte)"
                if any("local changes" in line or "overwritten" in line for line in lines) else "")
        return Result(False, f"git pull non riuscito: {reason}{hint}")
    after = _git("rev-parse", "HEAD").stdout.strip()
    if after == before:
        return Result(True, "Sei già all'ultima versione.")
    log = _git("log", "--format=%s", f"{before}..{after}").stdout.strip().splitlines()
    message = f"Aggiornato: {len(log)} novità."
    if _digest(HOME / "pyproject.toml") != deps_before:
        pip = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", f".{EXTRAS}"], cwd=HOME,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if pip.returncode:  # su Windows può capitare se il programma è aperto
            message += (" Le dipendenze non si sono aggiornate: chiudi MyDevAgent e lancia "
                        f"`{Path(sys.executable).name} -m pip install -e \".{EXTRAS}\"` nella cartella {HOME}.")
        else:
            message += " Dipendenze aggiornate."
    return Result(True, message, log[:20], restart=True)
