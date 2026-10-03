import email
import sys
from email.message import EmailMessage
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mydevagent import license

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bot_pagamenti as bot  # noqa: E402

ME = "gio@gmail.com"
GENUINE = "mx.google.com; dkim=pass header.i=@paypal.it; dmarc=pass (p=REJECT) header.from=paypal.it"


def paypal_mail(body: str, auth: str = GENUINE, subject: str = "Hai ricevuto un pagamento") -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "PayPal <service@paypal.it>", ME, subject
    msg["Authentication-Results"] = auth
    msg.set_content(body)
    return msg.as_bytes()


PAID = paypal_mail("Hai ricevuto 9,99 € EUR da Mario Rossi.\nNota: mario.rossi@example.com\n"
                   "Il pagamento è stato inviato a gio@gmail.com. Domande? service@paypal.it")


class FakeImap:
    def __init__(self, mails):
        self.mails, self.labels = dict(enumerate(mails, 1)), {}

    def select(self, box):
        pass

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in self.mails if u not in self.labels)]
        if command == "FETCH":
            return "OK", [(b"", self.mails[int(args[0])])]
        self.labels[int(args[0])] = args[2].strip("()")
        return "OK", []


class FakeSmtp:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)


def test_parse_reads_amount_buyer_and_name():
    payment = bot.parse(email.message_from_bytes(PAID), ME)
    assert payment == bot.Payment("9.99", "mario.rossi@example.com", "Mario Rossi")


def test_fake_paypal_emails_never_get_codes():
    fake = paypal_mail("Hai ricevuto 79,00 € da Furbo. furbo@example.com", auth="mx.google.com; dmarc=fail")
    assert bot.parse(email.message_from_bytes(fake), ME) == "non risulta inviata da PayPal"
    news = paypal_mail("Scopri le novità di PayPal!", subject="Novità")
    assert bot.parse(email.message_from_bytes(news), ME) == ""


def test_run_sends_codes_and_flags_the_rest(monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    monkeypatch.setattr(license, "PUBLIC_KEY", public)
    odd = paypal_mail("Hai ricevuto 3,00 € da Anna. anna@example.com")
    nobody = paypal_mail("Hai ricevuto 4,99 € da Luca.")
    imap, smtp = FakeImap([PAID, odd, nobody]), FakeSmtp()
    counts = bot.run(imap, smtp, private, ME, bot.PREZZI)
    assert counts == {"inviati": 1, "da controllare": 2, "ignorate": 0}
    assert imap.labels == {1: bot.DONE, 2: bot.CHECK, 3: bot.CHECK}
    sent = smtp.sent[0]
    assert sent["To"] == "mario.rossi@example.com"
    code = next(line for line in sent.get_content().splitlines() if line.startswith("MDA-"))
    assert license.read(code)["p"] == "plus" and license.read(code)["n"] == "Mario Rossi"
    assert all(m["To"] == ME for m in smtp.sent[1:])  # gli avvisi vanno a gio
    assert bot.run(imap, FakeSmtp(), private, ME, bot.PREZZI)["inviati"] == 0  # mai due volte


def test_not_configured_does_nothing(monkeypatch, capsys):
    for var in ("GMAIL_INDIRIZZO", "GMAIL_PASSWORD_APP", "LICENZA_PRIVATA"):
        monkeypatch.delenv(var, raising=False)
    bot.main()
    assert "non ancora configurato" in capsys.readouterr().out
