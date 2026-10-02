import datetime as dt
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mydevagent import license

DAY = license.DAY


@pytest.fixture
def private(monkeypatch):
    """Vendite aperte: una coppia di chiavi di prova al posto di quella di gio."""
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    monkeypatch.setattr(license, "PUBLIC_KEY", public)
    return key


def in_days(n: int) -> str:
    return (dt.date.today() + dt.timedelta(days=n)).isoformat()


def test_free_while_sales_are_closed():
    state = license.status()
    assert state.ok and state.kind == "gratis" and state.plus
    assert license.plus_needed() == ""


def test_trial_then_blocked(private):
    start = license.status(now=1000.0)
    assert start.ok and start.kind == "prova" and start.days_left == 14 and start.plus
    assert license.status(now=1000.0 + 13 * DAY).message.endswith("ultimo giorno.")
    late = license.status(now=1000.0 + 14 * DAY)
    assert not late.ok and "/licenza" in late.message


def test_lifetime_and_plus_keys(private):
    state = license.activate(" " + license.sign(private, "Mario", "base") + " ")
    assert state.ok and state.kind == "per sempre" and not state.plus and "Mario" in state.message
    assert license.status().ok and license.plus_needed()  # base: niente funzioni Plus
    state = license.activate(license.sign(private, "Mario", "plus", in_days(31)))
    assert state.ok and state.kind == "abbonamento Plus" and state.plus and state.days_left == 32
    assert license.plus_needed() == ""


def test_subscription_expires(private):
    license.activate(license.sign(private, "Ada", "base", in_days(0)))
    assert license.status().ok  # vale fino a fine giornata
    assert not license.status(now=license.time.time() + DAY).ok


def test_forged_or_broken_keys_are_rejected(private):
    other = Ed25519PrivateKey.generate()
    assert not license.activate(license.sign(other, "Furbo", "plus")).ok
    good = license.sign(private, "Mario", "plus")
    assert not license.activate(good[:-3]).ok
    assert not license.activate("MDA-ciao").ok
    assert "key" not in license._load()


def test_deactivate(private):
    license.activate(license.sign(private, "Mario", "base"))
    assert "tolto" in license.deactivate()
    assert license.status().kind == "prova"


def test_generator_script(tmp_path):
    script = [sys.executable, "-I", "scripts/genera_licenza.py"]  # -I: senza MyDevAgent sul percorso
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PATH": ""}
    out = subprocess.run(script + ["chiavi"], capture_output=True, text=True, env=env, check=True).stdout
    public = out.strip().splitlines()[-1]
    assert len(public) == 64
    again = subprocess.run(script + ["chiavi"], capture_output=True, text=True, env=env)
    assert again.returncode and "esiste già" in again.stderr  # mai sovrascrivere la chiave
    key = subprocess.run(script + ["Mario Rossi", "plus", "mese"], capture_output=True, text=True, env=env,
                         check=True).stdout.strip()
    license.PUBLIC_KEY, old = public, license.PUBLIC_KEY
    try:
        assert license.read(key) == {"n": "Mario Rossi", "p": "plus", "s": in_days(31)}
    finally:
        license.PUBLIC_KEY = old
