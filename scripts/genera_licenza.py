"""Crea i codici di licenza di MyDevAgent. Lo usa solo gio, sul suo computer.

Serve solo il pacchetto `cryptography` (una volta: py -m pip install cryptography).

La prima volta, una volta sola:
    python scripts/genera_licenza.py chiavi
crea la chiave privata in ~/.mydevagent-licenze/privata.pem (NON va mai condivisa né messa su GitHub)
e stampa la chiave pubblica da mettere in PUBLIC_KEY di mydevagent/license.py.

Poi, per ogni cliente che ha pagato:
    python scripts/genera_licenza.py "Mario Rossi" base mese      (abbonamento di un mese)
    python scripts/genera_licenza.py "Mario Rossi" plus anno      (abbonamento Plus di un anno)
    python scripts/genera_licenza.py "Mario Rossi" plus sempre    (acquisto una volta, Plus)
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    sys.exit("Manca il pacchetto cryptography. Installalo una volta con:  py -m pip install cryptography")

PRIVATE = Path.home() / ".mydevagent-licenze" / "privata.pem"
DAYS = {"mese": 31, "anno": 366, "sempre": 0}  # un giorno in più per i pagamenti arrivati in ritardo


def sign(private: Ed25519PrivateKey, name: str, plan: str, expires: str) -> str:
    """Lo stesso formato che legge mydevagent/license.py (lo script non importa MyDevAgent: basta `cryptography`)."""
    payload = json.dumps({"n": name, "p": plan, "s": expires}, separators=(",", ":")).encode()
    return "MDA-" + base64.urlsafe_b64encode(payload + private.sign(payload)).decode().rstrip("=")


def expires_for(durata: str, today: dt.date | None = None) -> str:
    days = DAYS[durata]
    return ((today or dt.date.today()) + dt.timedelta(days=days)).isoformat() if days else ""


def public_hex(private: Ed25519PrivateKey) -> str:
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def main() -> None:
    if sys.argv[1:] == ["chiavi"]:
        if PRIVATE.exists():
            sys.exit(f"La chiave esiste già in {PRIVATE}: non la rifaccio, o i codici venduti smetterebbero "
                     "di funzionare.")
        private = Ed25519PrivateKey.generate()
        PRIVATE.parent.mkdir(parents=True, exist_ok=True)
        PRIVATE.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
        print(f"Chiave privata salvata in {PRIVATE}\nFanne una copia su una chiavetta: se la perdi non puoi "
              f"più creare codici.\n\nChiave pubblica (questa si può condividere):\n{public_hex(private)}")
        return
    parser = argparse.ArgumentParser(description="Crea un codice di licenza")
    parser.add_argument("nome", help="nome del cliente, appare nell'app")
    parser.add_argument("piano", choices=["base", "plus"])
    parser.add_argument("durata", choices=list(DAYS))
    args = parser.parse_args()
    if not PRIVATE.exists():
        sys.exit("Prima crea le chiavi: python scripts/genera_licenza.py chiavi")
    private = serialization.load_pem_private_key(PRIVATE.read_bytes(), password=None)
    print(sign(private, args.nome, args.piano, expires_for(args.durata)))


if __name__ == "__main__":
    main()
