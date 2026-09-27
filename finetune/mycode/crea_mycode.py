#!/usr/bin/env python3
"""Crea (o aggiorna) il modello `mycode` in Ollama.

  python finetune/mycode/crea_mycode.py            # dal modello addestrato (il file .gguf)
  python finetune/mycode/crea_mycode.py --subito   # prima dell'addestramento: qwen2.5-coder:14b + manuale

Il file .gguf lo cerca in finetune/mycode/outputs/ e in finetune/mycode/ (lì va messo quello scaricato
da Colab). Il formato della chat e i token di stop li copia dal modello normale qwen2.5-coder:14b, così MyCode
parla con Ollama esattamente come lui. Nel prompt di sistema mette SYSTEM.txt più il manuale MANUALE.md.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "qwen2.5-coder:14b"
# usato solo se il modello normale non c'è: il formato ChatML di Qwen
FALLBACK_TEMPLATE = """{{ if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{ if .Prompt }}<|im_start|>user
{{ .Prompt }}<|im_end|>
{{ end }}<|im_start|>assistant
{{ .Response }}<|im_end|>
"""
FALLBACK_STOPS = ['"<|im_start|>"', '"<|im_end|>"', '"<|endoftext|>"']


def ollama(*args: str) -> str | None:
    try:
        done = subprocess.run(["ollama", *args], capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        sys.exit("Non trovo il comando `ollama`: installa Ollama da https://ollama.com/download e riprova.")
    return done.stdout if done.returncode == 0 else None


def find_gguf(explicit: Path | None) -> Path:
    if explicit:
        return explicit
    found = [*(HERE / "outputs").rglob("*.gguf"), *HERE.glob("*.gguf")]
    if not found:
        sys.exit("Non trovo il file .gguf del modello addestrato.\n"
                 "Addestralo (vedi finetune/mycode/LEGGIMI.md) oppure metti qui il file scaricato da Colab:\n"
                 f"  {HERE}\nPer provare MyCode subito, senza addestramento:  python {Path(__file__).name} --subito")
    preferred = [p for p in found if "q4_k_m" in p.name.lower()] or found
    return max(preferred, key=lambda p: p.stat().st_size)


def system_prompt() -> str:
    parts = [(HERE / "SYSTEM.txt").read_text(encoding="utf-8").strip()]
    manual = HERE / "MANUALE.md"
    if manual.is_file():
        parts.append(manual.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts).replace('"""', "'''")


def modelfile(source: str, *, with_template: bool) -> str:
    lines = [f"FROM {source}"]
    if with_template:
        template = ollama("show", BASE, "--template")
        params = ollama("show", BASE, "--parameters") or ""
        stops = [line.split(None, 1)[1].strip() for line in params.splitlines()
                 if line.strip().startswith("stop") and len(line.split(None, 1)) == 2]
        if not template:
            print(f"(non trovo {BASE} in Ollama: uso il formato ChatML standard)")
        lines.append(f'TEMPLATE """{(template or FALLBACK_TEMPLATE).rstrip()}"""')
        lines += [f"PARAMETER stop {stop}" for stop in (stops or FALLBACK_STOPS)]
    lines += ["PARAMETER num_ctx 16384", "PARAMETER temperature 0.2", "PARAMETER top_p 0.9",
              f'SYSTEM """{system_prompt()}"""']
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subito", action="store_true", help=f"senza addestramento: {BASE} con il manuale di MyCode")
    ap.add_argument("--gguf", type=Path, help="percorso del file .gguf, se è altrove")
    ap.add_argument("--nome", default="mycode", help="nome del modello in Ollama")
    args = ap.parse_args()

    if args.subito:
        folder, text = HERE, modelfile(BASE, with_template=False)   # formato e stop li eredita dal modello base
    else:
        gguf = find_gguf(args.gguf).resolve()
        folder, text = gguf.parent, modelfile(f"./{gguf.name}", with_template=True)
        print(f"Modello addestrato: {gguf}")
    path = folder / "Modelfile"
    path.write_text(text, encoding="utf-8")
    print(f"Creo `{args.nome}` in Ollama…")
    if subprocess.run(["ollama", "create", args.nome, "-f", str(path)], cwd=folder).returncode:
        sys.exit("ollama create non è riuscito: guarda il messaggio qui sopra.")
    print(f"\nFatto! Provalo:  ollama run {args.nome}\n"
          "In MyDevAgent:   mydevagent -p mycode   (oppure MYDEVAGENT_PROFILE=mycode nel file .env)")


if __name__ == "__main__":
    main()
