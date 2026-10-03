"""Licenza di MyDevAgent: prova gratuita, poi abbonamento o acquisto una volta, normale o Plus.

I codici li crea solo gio con `scripts/genera_licenza.py` e la sua chiave privata, che non sta nel
repository. Ogni codice contiene nome, piano (base o plus) e scadenza, firmati con Ed25519: l'app li
controlla con la chiave pubblica qui sotto, senza internet, e nessuno può inventarne uno valido.
Il Plus sblocca anche le funzioni di Studio Plus (vedi `plus_needed`) e il download di MyCode.

Finché PUBLIC_KEY è vuota (le vendite non sono ancora partite) il controllo è spento e tutto, Plus
compreso, resta gratis.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PUBLIC_KEY = "c9affe19737bcc37f1d46464a9213c3607ffd574f5f873939a33d1e32ca199e4"  # la chiave pubblica di gio
BUY_URL = "https://mydevagent.github.io/prezzi.html"
TRIAL_DAYS = 14
PREFIX = "MDA-"
DAY = 86400


@dataclass
class Status:
    ok: bool  # si può usare MyDevAgent
    kind: str  # "gratis", "prova", "abbonamento", "per sempre" (+ " Plus"), "scaduta"
    message: str
    days_left: int | None = None
    plus: bool = False  # Plus: lo stesso codice vale anche per Studio Plus e MyCode


def _path() -> Path:
    return Path(os.environ.get("MYDEVAGENT_STATE_DIR", Path.home() / ".mydevagent")) / "license.json"


def _load() -> dict[str, Any]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def sign(private: Ed25519PrivateKey, name: str, plan: str, expires: str = "") -> str:
    """Crea un codice. `expires` è una data AAAA-MM-GG (abbonamento) o vuota (per sempre)."""
    payload = json.dumps({"n": name, "p": plan, "s": expires}, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(payload + private.sign(payload)).decode().rstrip("=")


def read(key: str) -> dict[str, str] | None:
    """Il contenuto di un codice, o None se il codice non è stato firmato da gio."""
    try:
        raw = base64.urlsafe_b64decode(key.strip().removeprefix(PREFIX) + "==")
        payload, signature = raw[:-64], raw[-64:]
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY)).verify(signature, payload)
        return json.loads(payload)
    except (ValueError, InvalidSignature):
        return None


def _check(key: str, now: float) -> Status:
    info = read(key)
    if info is None:
        return Status(False, "scaduta", "Questo codice non è valido: controlla di averlo copiato tutto.")
    plus = info.get("p") == "plus"
    name = info.get("n") or "te"
    if not info.get("s"):
        kind = "per sempre" + (" Plus" if plus else "")
        return Status(True, kind, f"Licenza {kind} di {name}. Grazie per il supporto!", plus=plus)
    # ponytail: la data viene dall'orologio del computer, che si può spostare indietro; basta per un deterrente
    left = (dt.date.fromisoformat(info["s"]) - dt.date.fromtimestamp(now)).days + 1
    if left <= 0:
        return Status(False, "scaduta", f"L'abbonamento è scaduto il {info['s']}: rinnovalo su {BUY_URL} "
                                        "e scrivi /licenza <nuovo codice>.", 0)
    kind = "abbonamento" + (" Plus" if plus else "")
    return Status(True, kind, f"{kind.capitalize()} di {name}, valido fino al {info['s']}.", left, plus)


def activate(key: str) -> Status:
    state = _check(key, time.time())
    if state.ok:
        data = _load()
        data["key"] = key.strip()
        _save(data)
    else:
        state.message = f"Codice non attivato. {state.message}"
    return state


def deactivate() -> str:
    data = _load()
    if not data.pop("key", None):
        return "Su questo computer non c'è nessun codice."
    _save(data)
    return "Codice tolto da questo computer."


def status(now: float | None = None) -> Status:
    """Si può usare MyDevAgent?"""
    if not PUBLIC_KEY:
        return Status(True, "gratis", "MyDevAgent è gratis.", plus=True)
    now = time.time() if now is None else now
    data = _load()
    if data.get("key"):
        return _check(data["key"], now)
    if "first_run" not in data:
        data["first_run"] = now
        _save(data)
    left = TRIAL_DAYS - int((now - data["first_run"]) // DAY)
    if left > 0:
        return Status(True, "prova", f"Prova gratuita: {'ultimo giorno' if left == 1 else f'ancora {left} giorni'}.",
                      left, plus=True)  # durante la prova si prova anche il Plus
    return Status(False, "scaduta", f"La prova gratuita è finita. Scegli abbonamento o acquisto una volta su "
                                    f"{BUY_URL}, poi scrivi /licenza <codice>.", 0)


def plus_needed() -> str:
    """"" se le funzioni Plus si possono usare, altrimenti il perché."""
    if status().plus:
        return ""
    return f"Questa è una funzione Plus. Passa al Plus su {BUY_URL} e scrivi /licenza <codice>."
