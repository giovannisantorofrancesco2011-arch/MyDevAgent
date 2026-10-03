"""Bot dei pagamenti: legge le email «Hai ricevuto un pagamento» di PayPal nella casella Gmail di gio, crea il
codice di licenza giusto per l'importo e lo manda per email a chi ha pagato.

Gira da solo su GitHub Actions (.github/workflows/pagamenti.yml) ogni 15 minuti. Non tiene nessun archivio:
le email già gestite le segna in Gmail con l'etichetta «mydevagent-inviato», quelle strane (importo
sconosciuto, email del cliente non trovata) con «mydevagent-controllare», e avvisa gio.

Impostazioni (GitHub > Settings > Secrets and variables > Actions):
    GMAIL_INDIRIZZO     l'indirizzo Gmail che riceve le email di PayPal
    GMAIL_PASSWORD_APP  una «password per le app» di Google (non la password normale)
    LICENZA_PRIVATA     il contenuto del file privata.pem creato da genera_licenza.py
    PREZZI (variabile, facoltativa)  JSON importo -> piano, per esempio {"4.99": "base mese"}

I registri di GitHub di un repository pubblico li vede chiunque: qui si stampano solo numeri, mai nomi o email.
"""

from __future__ import annotations

import email
import email.message
import html
import imaplib
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from genera_licenza import expires_for, public_hex, serialization, sign  # noqa: E402

PREZZI = {"4.99": "base mese", "39.00": "base sempre", "9.99": "plus mese", "79.00": "plus sempre"}
DONE, CHECK = "mydevagent-inviato", "mydevagent-controllare"
SEARCH = f'from:paypal newer_than:14d -label:{DONE} -label:{CHECK}'
RECEIVED = re.compile(r"hai ricevuto|ti ha inviato|you received|sent you", re.IGNORECASE)
AMOUNT = re.compile(r"(?:€|EUR)\s*(\d{1,5}[.,]\d{2})|(\d{1,5}[.,]\d{2})\s*(?:€|EUR)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Gmail scrive se il mittente è davvero PayPal: un'email falsa «da PayPal» non deve regalare codici
GENUINE = re.compile(r"dmarc=pass[^;]*header\.from=(?:[\w-]+\.)*paypal\.[a-z.]+", re.IGNORECASE)


@dataclass
class Payment:
    amount: str  # "4.99"
    buyer: str  # email di chi ha pagato
    name: str


def _text(msg: email.message.Message) -> str:
    """Il testo dell'email (la parte HTML ripulita se non c'è quella di solo testo)."""
    parts = {}
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html") and part.get_content_type() not in parts:
            parts[part.get_content_type()] = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", errors="replace")
    if "text/plain" in parts:
        return parts["text/plain"]
    return html.unescape(re.sub(r"<[^>]+>", " ", parts.get("text/html", "")))


def parse(msg: email.message.Message, me: str) -> Payment | str:
    """Il pagamento descritto dall'email, oppure il motivo per cui va controllata a mano ("" = non è un
    pagamento ricevuto, si ignora)."""
    if not GENUINE.search(" ".join(msg.get_all("Authentication-Results") or [])):
        return "" if "paypal" not in parseaddr(msg.get("From", ""))[1].lower() else "non risulta inviata da PayPal"
    text = re.sub(r"\s+", " ", _text(msg))
    if not RECEIVED.search(f"{msg.get('Subject', '')} {text}"):
        return ""
    found = AMOUNT.search(text)
    if not found:
        return "importo non trovato"
    amount = f"{float((found[1] or found[2]).replace(',', '.')):.2f}"
    buyers = {e.lower().rstrip(".") for e in EMAIL.findall(text)}
    buyers = {e for e in buyers if "paypal" not in e.split("@")[1] and e != me.lower()}
    if len(buyers) != 1:
        return "email del cliente non trovata" if not buyers else "più email nel messaggio, non so quale usare"
    who = re.search(r"(?:da|from)\s+([A-ZÀ-Ý][\w'À-ÿ]+(?:\s+[A-ZÀ-Ý][\w'À-ÿ]+){0,3})", text)
    return Payment(amount, buyers.pop(), who[1] if who else "")


def letter(payment: Payment, plan: str, code: str, sender: str) -> EmailMessage:
    tier, durata = plan.split()
    what = {"mese": "per un mese", "anno": "per un anno", "sempre": "per sempre"}[durata]
    mail = EmailMessage()
    mail["From"], mail["To"] = f"MyDevAgent <{sender}>", payment.buyer
    mail["Subject"] = "Il tuo codice di MyDevAgent"
    mail.set_content(
        f"Ciao{' ' + payment.name if payment.name else ''}!\n\n"
        f"Grazie per aver comprato MyDevAgent{' Plus' if tier == 'plus' else ''} ({what}).\n\n"
        f"Ecco il tuo codice:\n\n{code}\n\n"
        "Per attivarlo apri MyDevAgent (o la chat di MyDevAgent Studio) e scrivi:\n"
        "/licenza seguito dal codice\n\n"
        + ("Quando scade, rinnova il pagamento e ti arriva un codice nuovo.\n\n" if durata != "sempre" else "")
        + "Se qualcosa non funziona, rispondi a questa email.\n\nVio e il team di MyDevAgent\n\n"
        "---\n\nEnglish: thanks for buying MyDevAgent! To activate the code above, open MyDevAgent (or the "
        "MyDevAgent Studio chat) and type /license followed by the code. If something doesn't work, reply to "
        "this email.\n")
    return mail


def alert(sender: str, reason: str, subject: str) -> EmailMessage:
    mail = EmailMessage()
    mail["From"] = mail["To"] = sender
    mail["Subject"] = "MyDevAgent: un pagamento da controllare"
    mail.set_content(f"Il bot non ha mandato il codice per l'email di PayPal «{subject}»: {reason}.\n\n"
                     f"La trovi in Gmail con l'etichetta {CHECK}. Crea il codice a mano con genera_licenza.py, "
                     f"poi togli l'etichetta.\n")
    return mail


def run(imap, smtp, private, me: str, prices: dict[str, str]) -> dict[str, int]:
    counts = {"inviati": 0, "da controllare": 0, "ignorate": 0}
    imap.select("INBOX")
    _, data = imap.uid("SEARCH", "X-GM-RAW", f'"{SEARCH}"')
    for uid in data[0].split():
        _, fetched = imap.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: l'email resta «da leggere» per gio
        msg = email.message_from_bytes(fetched[0][1])
        result = parse(msg, me)
        if result == "":
            counts["ignorate"] += 1
            continue
        if isinstance(result, Payment) and result.amount not in prices:
            result = f"importo {result.amount} € che non corrisponde a nessun piano"
        if isinstance(result, str):
            smtp.send_message(alert(me, result, msg.get("Subject", "")))
            imap.uid("STORE", uid, "+X-GM-LABELS", f"({CHECK})")
            counts["da controllare"] += 1
            continue
        plan = prices[result.amount]
        tier, durata = plan.split()
        code = sign(private, result.name or result.buyer, tier, expires_for(durata))
        smtp.send_message(letter(result, plan, code, me))
        imap.uid("STORE", uid, "+X-GM-LABELS", f"({DONE})")  # solo dopo l'invio: se cade prima, riprova
        counts["inviati"] += 1
    return counts


def load_key(secret: str):
    """La chiave privata dal Secret, anche se incollata male: righe unite, spazi in più, \\n scritti a mano o
    senza le righe BEGIN/END. Basta che ci sia il blocco di lettere del file privata.pem."""
    body = re.sub(r"-----[A-Z ]+-----|\\n|\\r|\s", "", secret)
    if re.fullmatch(r"[0-9a-fA-F]{64}", body):
        sys.exit("LICENZA_PRIVATA contiene la chiave PUBBLICA: serve il contenuto del file privata.pem.")
    pem = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    try:
        return serialization.load_pem_private_key(f"{pem}\n-----END PRIVATE KEY-----\n".encode(), password=None)
    except ValueError:
        sys.exit("LICENZA_PRIVATA non è valida: apri privata.pem con il Blocco note, copia tutto e incollalo di nuovo "
                 f"nel Secret (ora contiene {len(body)} caratteri utili, ne servono 64).")


def main() -> None:
    me, password, pem = (os.environ.get(k, "") for k in ("GMAIL_INDIRIZZO", "GMAIL_PASSWORD_APP", "LICENZA_PRIVATA"))
    if not (me and password and pem):
        print("Bot non ancora configurato (mancano i Secrets): non faccio niente.")
        return
    prices = {f"{float(k):.2f}": v for k, v in json.loads(os.environ.get("PREZZI") or json.dumps(PREZZI)).items()}
    private = load_key(pem)
    app_key = re.search(r'^PUBLIC_KEY = "(\w*)"', (ROOT / "mydevagent" / "license.py").read_text(), re.MULTILINE)[1]
    if public_hex(private) != app_key:
        sys.exit("LICENZA_PRIVATA non è la chiave che usa l'app: i codici non funzionerebbero. Usa il privata.pem "
                 "creato insieme alla chiave pubblica che hai mandato.")
    me, password = me.strip(), re.sub(r"\s", "", password)  # Google mostra la password a gruppi di 4 con spazi
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(me, password)
    except imaplib.IMAP4.error:
        sys.exit("Gmail non accetta indirizzo e password. Controlla che GMAIL_INDIRIZZO sia l'indirizzo completo "
                 "(con @gmail.com) e che GMAIL_PASSWORD_APP sia la «password per le app» di 16 lettere, non la "
                 "password normale di Google.")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(me, password)
        counts = run(imap, smtp, private, me, prices)
    imap.logout()
    print(", ".join(f"{k}: {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
