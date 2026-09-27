from mydevagent import license

DAY = license.DAY


def reply(store="42", status="active", expires=None, **extra):
    return {"valid": True, "activated": True, "error": None, "instance": {"id": "inst-1"},
            "license_key": {"status": status, "expires_at": expires}, "meta": {"store_id": store}, **extra}


def shop(monkeypatch, answers):
    """Negozio aperto (STORE_ID 42) e server delle licenze finto che risponde con `answers` in ordine."""
    calls = []

    def post(action, **fields):
        calls.append((action, fields))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(license, "STORE_ID", "42")
    monkeypatch.setattr(license, "_post", post)
    return calls


def test_free_while_the_shop_is_closed(monkeypatch):
    monkeypatch.setattr(license, "STORE_ID", "")
    assert license.status().ok and license.status().kind == "gratis"


def test_trial_then_blocked(monkeypatch):
    shop(monkeypatch, [])
    start = license.status(now=1000.0)
    assert start.ok and start.kind == "prova" and start.days_left == 14
    assert license.status(now=1000.0 + 13 * DAY).message.endswith("ultimo giorno.")
    late = license.status(now=1000.0 + 14 * DAY)
    assert not late.ok and "/licenza" in late.message


def test_lifetime_key_and_offline_grace(monkeypatch):
    calls = shop(monkeypatch, [reply(), OSError("offline"), OSError("offline")])
    state = license.activate(" ABC-123 ")
    assert state.ok and state.kind == "per sempre"
    assert calls[0] == ("activate", {"license_key": "ABC-123", "instance_name": calls[0][1]["instance_name"]})
    later = license._load()["checked"] + 10 * DAY
    assert license.status(now=later).ok  # offline: vale l'ultimo controllo
    assert not license.status(now=later + 30 * DAY).ok  # troppo tempo senza controllare


def test_subscription_expires(monkeypatch):
    shop(monkeypatch, [reply(expires="2026-11-01T00:00:00Z"), reply(valid=False, status="expired")])
    assert license.activate("SUB-1").kind == "abbonamento"
    expired = license.status(now=license._load()["checked"] + 4 * DAY)
    assert not expired.ok and "scaduto" in expired.message


def test_rejects_wrong_store_and_bad_keys(monkeypatch):
    shop(monkeypatch, [reply(store="999"), {"activated": False, "error": "license_key not found"}])
    assert "non è di MyDevAgent" in license.activate("OTHER").message
    assert "not found" in license.activate("NOPE").message
    assert "key" not in license._load()


def test_deactivate_frees_the_key(monkeypatch):
    calls = shop(monkeypatch, [reply(), {"deactivated": True}])
    license.activate("ABC")
    assert "altro" in license.deactivate()
    assert calls[1] == ("deactivate", {"license_key": "ABC", "instance_id": "inst-1"})
    assert license.status(now=license._load()["first_run"]).kind == "prova"
