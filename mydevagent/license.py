"""Licenza di MyDevAgent: prova gratuita, poi abbonamento o acquisto una volta.

Le chiavi le vende e le emette Lemon Squeezy (https://www.lemonsqueezy.com): l'app le attiva e le ricontrolla
con la sua License API pubblica, che non chiede nessuna chiave segreta. Lo stato sta in
`~/.mydevagent/license.json`. Senza internet l'app continua a funzionare per GRACE_DAYS giorni dall'ultimo
controllo riuscito: MyDevAgent lavora in locale e non deve fermarsi per un Wi-Fi che cade.

Finché STORE_ID è vuoto (il negozio non è ancora aperto) il controllo è spento e tutto resta gratis.
"""

from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

STORE_ID = os.environ.get("MYDEVAGENT_STORE_ID", "")  # l'id del negozio Lemon Squeezy, quando esiste
BUY_URL = "https://mydevagent.github.io/prezzi.html"
API = "https://api.lemonsqueezy.com/v1/licenses"
TRIAL_DAYS = 14
RECHECK_DAYS = 3  # ogni quanto ricontrollare la chiave online
GRACE_DAYS = 30  # quanto si può restare offline con una chiave già attivata
DAY = 86400


@dataclass
class Status:
    ok: bool  # si può usare MyDevAgent
    kind: str  # "gratis", "prova", "abbonamento", "per sempre", "scaduta"
    message: str
    days_left: int | None = None
    plus: bool = False  # abbonamento Plus: lo stesso codice vale anche per Studio Plus e MyCode


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


def _post(action: str, **fields: str) -> dict[str, Any]:
    """Chiama la License API. Solleva OSError se non c'è rete o il server non risponde."""
    try:
        response = httpx.post(f"{API}/{action}", data=fields, headers={"Accept": "application/json"}, timeout=15)
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OSError(str(exc)) from exc


def _plus(reply: dict[str, Any]) -> bool:
    """Il prodotto o la variante su Lemon Squeezy si chiama «... Plus»."""
    meta = reply.get("meta") or {}
    return "plus" in f"{meta.get('product_name', '')} {meta.get('variant_name', '')}".lower()


def _kind(reply: dict[str, Any]) -> str:
    return "abbonamento" if (reply.get("license_key") or {}).get("expires_at") else "per sempre"


def _problem(reply: dict[str, Any]) -> str:
    """Perché la chiave non va bene ("" se va bene)."""
    key = reply.get("license_key") or {}
    if key.get("status") == "expired":
        return "l'abbonamento è scaduto: rinnovalo per continuare"
    if key.get("status") == "disabled":
        return "questa chiave è stata disattivata"
    if reply.get("error") or not reply.get("valid", reply.get("activated")):
        return str(reply.get("error") or "chiave non valida")
    if str((reply.get("meta") or {}).get("store_id", "")) != str(STORE_ID):
        return "questa chiave non è di MyDevAgent"
    return ""


def activate(key: str) -> Status:
    """Attiva una chiave su questo computer (usa un'attivazione della chiave)."""
    key = key.strip()
    try:
        reply = _post("activate", license_key=key, instance_name=platform.node() or "computer")
    except OSError:
        return Status(False, "scaduta", "Non riesco a collegarmi al server delle licenze: controlla internet e riprova.")
    problem = _problem(reply)
    if problem:
        return Status(False, "scaduta", f"Chiave non attivata: {problem}.")
    data = _load()
    data.update(key=key, instance=(reply.get("instance") or {}).get("id", ""), kind=_kind(reply), plus=_plus(reply),
                checked=time.time())
    _save(data)
    return status()


def deactivate() -> str:
    """Libera l'attivazione di questo computer, così la chiave si può usare su un altro."""
    data = _load()
    if not data.get("key"):
        return "Su questo computer non c'è nessuna chiave."
    try:
        _post("deactivate", license_key=data["key"], instance_id=data.get("instance", ""))
    except OSError:
        return "Non riesco a collegarmi al server delle licenze: riprova quando sei online."
    for field in ("key", "instance", "kind", "plus", "checked"):
        data.pop(field, None)
    _save(data)
    return "Chiave tolta da questo computer: ora puoi attivarla su un altro."


def status(now: float | None = None) -> Status:
    """Si può usare MyDevAgent? Ricontrolla la chiave online solo ogni RECHECK_DAYS giorni."""
    if not STORE_ID:
        return Status(True, "gratis", "MyDevAgent è gratis.")
    now = time.time() if now is None else now
    data = _load()
    if "first_run" not in data:
        data["first_run"] = now
        _save(data)
    if data.get("key"):
        if now - data.get("checked", 0) >= RECHECK_DAYS * DAY:
            try:
                reply = _post("validate", license_key=data["key"], instance_id=data.get("instance", ""))
            except OSError:
                reply = None  # offline: vale l'ultimo controllo riuscito
            if reply is not None:
                problem = _problem(reply)
                if problem:
                    return Status(False, "scaduta", f"La tua licenza non è più valida: {problem}.")
                data.update(kind=_kind(reply), plus=_plus(reply), checked=now)
                _save(data)
        offline = int((now - data.get("checked", 0)) // DAY)
        if offline > GRACE_DAYS:
            return Status(False, "scaduta", f"Sono {offline} giorni che non riesco a controllare la licenza: "
                                            "collegati a internet una volta e riprova.")
        kind = data.get("kind", "per sempre") + (" Plus" if data.get("plus") else "")
        return Status(True, kind, f"Licenza attiva ({kind}). Grazie per il supporto!", plus=bool(data.get("plus")))
    left = TRIAL_DAYS - int((now - data["first_run"]) // DAY)
    if left > 0:
        return Status(True, "prova", f"Prova gratuita: {'ultimo giorno' if left == 1 else f'ancora {left} giorni'}.",
                      left)
    return Status(False, "scaduta", f"La prova gratuita è finita. Scegli abbonamento o acquisto una volta su "
                                    f"{BUY_URL}, poi scrivi /licenza <chiave>.", 0)
