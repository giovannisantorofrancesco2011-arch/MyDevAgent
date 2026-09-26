# MyDevAgent

**Assistente di programmazione local-first con 35 agenti specializzati** (15 nel nucleo + 20 per la
modalità `/ultra-deep`). Gira tutto sul tuo PC (GPU 8 GB+ o solo CPU), funziona offline e, quando sei
online, fa ricerche web in tempo reale. Come Claude Code **lavora direttamente sui file del tuo progetto**:
legge il codice, lo modifica mostrandoti i diff, lancia i test, corregge, e puoi annullare tutto con `/undo`.
Lo usi da terminale, come server compatibile OpenAI (VS Code/Continue, Cursor, Aider, Cline…) o come
modello Ollama "single-agent".

> **Cos'è, in concreto.** Non è un LLM addestrato da zero (servirebbero milioni di euro di GPU).
> È un sistema completo costruito sopra i migliori modelli open-weight per il codice (Qwen2.5-Coder,
> Qwen3-Coder, …): una **persona di sistema**, **35 agenti di ruolo orchestrati con LangGraph**, **tool**
> reali (ricerca web, sandbox Docker, filesystem, git, RAG sulla tua codebase, visione), un **router** che
> tiene veloci le richieste semplici, e un **setup QLoRA** per addestrarlo sui tuoi progetti.
> La qualità dipende dal modello che scegli: un 7B locale non eguaglia i modelli cloud di frontiera,
> ma la pipeline (piano → implementazione → test eseguiti davvero → review) ne alza parecchio l'affidabilità.

```
richiesta ─▶ router ─┬─ fast        specialista giusto che legge/modifica/testa i file (ciclo di tool)
                     ├─ balanced    Architect ▶ agente sui file ▶ test ▶ Reviewer sul diff reale ▶ correzioni
                     ├─ deep        + Security, Performance, Edge cases (2 giri di correzione)
                     └─ ultra-deep  35 agenti: ricerca web se serve ▶ requisiti ▶ piano + avvocato del diavolo
                                    ▶ strategia di test ▶ agente ▶ test ▶ 10 gate + 15 lens review in parallelo
                                    ▶ Integratore ▶ correzioni (3 giri) ▶ docs + release ▶ consegna
```

## Gli agenti

**Nucleo (15)** — usati da fast / balanced / deep:

| # | Agente | # | Agente | # | Agente |
|---|---|---|---|---|---|
| 1 | Architetto del Codice | 6 | Backend & API | 11 | Research (web) |
| 2 | Algoritmi & Strutture Dati | 7 | Database & ORM | 12 | Code Reviewer & Refactor |
| 3 | Linguaggi & Framework | 8 | DevOps & CI/CD | 13 | Documentation |
| 4 | Debugging & Testing | 9 | Security | 14 | Edge Case & Robustness |
| 5 | Frontend | 10 | Performance | 15 | Output Formatter |

**Estesi (20)** — si aggiungono in `/ultra-deep` (o se li chiami con `@alias`):

| # | Agente | # | Agente |
|---|---|---|---|
| 16 | Analista dei requisiti | 26 | Osservabilità |
| 17 | API Designer | 27 | Dipendenze & supply chain (web) |
| 18 | Mobile | 28 | Migrazioni & legacy |
| 19 | Cloud Architect | 29 | Test Strategist |
| 20 | Data Engineer | 30 | Threat modeling & privacy |
| 21 | AI/ML Engineer | 31 | Scalabilità & carico |
| 22 | Concorrenza & async | 32 | Release & versioning |
| 23 | Sistemi & low-level | 33 | Fact-checker web |
| 24 | Accessibilità & i18n | 34 | Avvocato del diavolo |
| 25 | UX/UI Designer | 35 | Integratore capo |

Ruoli, prompt, tool, flusso e interazioni: **[docs/AGENTS.md](docs/AGENTS.md)**.

## Cosa sa fare (le idee migliori degli assistenti di coding)
| Funzione | Ispirata a | Dove |
|---|---|---|
| Agente che legge, modifica (edit cerca/sostituisci), esegue comandi e test in un ciclo | Claude Code, Codex CLI | `mydevagent/agent/` |
| Permessi: ask · auto-edit · plan · auto, `Shift+Tab`, regole «consenti sempre» | Claude Code | `/permissions` |
| Diff inline con conferma, rifiuto con feedback, checkpoint, `/undo`, `/rewind` | Claude Code, Cursor | UI |
| Todo list dell'agente, `Esc` per interrompere, notifica a fine lavoro | Claude Code | UI |
| Memoria di progetto `MYDEVAGENT.md` (legge anche `AGENTS.md`/`CLAUDE.md`), `/init`, `#nota` | Claude Code, Codex | `/memory` |
| Repo map con classi e funzioni del progetto | Aider | automatica |
| Ricerca semantica sul codice (RAG), indicizzata in background | Cursor | automatica, `/index` |
| Comandi personalizzati in `.mydevagent/commands/*.md` | Claude Code | `/nome` |
| Skill in cartelle (`SKILL.md` + file di supporto), caricate solo quando servono | Claude Code, BluAgent | `/skill` |
| Plugin nel formato di Claude Code (comandi, skill, agenti), anche quelli già installati in Claude Code | Claude Code | `/plugin` |
| Hook: comandi automatici prima/dopo i tool, all'invio e alla fine (formato Claude Code) | Claude Code | `/hooks` |
| Server MCP (GitHub, database, browser…), anche quelli già configurati in Claude Code | Claude Code | `/mcp` |
| Sotto-agenti con contesto separato (`.claude/agents/*.md`, anche dai plugin) | Claude Code | `/agents` |
| Progetti pronti: sito, gioco Pygame, bot Discord, API FastAPI, programma Python con i test | MyDevAgent | `/new` |
| Modalità impara: spiega cosa fa e ti lascia scrivere un pezzo di codice (`TODO(tu)`) | Claude Code (stile Learning) | `/impara` |
| Anteprima dei siti su localhost: l'agente apre la pagina, la guarda (screenshot + modello vision) e legge gli errori della console | Claude Code + Playwright MCP | `/anteprima`, automatica |
| Più cartelle insieme (es. frontend e backend): l'agente legge, cerca e modifica in tutte | Claude Code | `/add-dir`, `--add-dir` |
| Multigiocatore: gli amici sulla tua rete seguono la sessione dal browser e scrivono all'agente (le modifiche le confermi tu) | MyDevAgent | `/multi` |
| Statistiche: richieste, token, file e righe cambiate, test, grafico dell'attività e giorni di fila | Claude Code | `/stats` |
| Compattazione della conversazione | Claude Code | `/compact`, automatica |
| Team multi-agente con review sul diff reale e dibattito sul piano | MyDevAgent | `/balanced` `/deep` `/ultra-deep` |

---

## Installazione e avvio in 5 minuti

### Automatica
```bash
git clone <questo-repo> mydevagent && cd mydevagent
./scripts/install.sh               # sceglie il profilo in base alla GPU; oppure: ./scripts/install.sh gpu8
./run.sh                           # avvia MyDevAgent, senza attivare l'ambiente virtuale
```
Su Windows: `powershell -ExecutionPolicy Bypass -File scripts\install.ps1`, poi `run.bat`.

Per usarlo su un tuo progetto, lancia `run.sh` (o `run.bat`) dalla cartella del progetto:
`cd ~/code/il-mio-progetto && ~/mydevagent/run.sh`. Se MyDevAgent non è ancora installato, `run.sh` lo installa.

### Manuale
```bash
# 1. Ollama  →  https://ollama.com/download
ollama pull qwen2.5-coder:7b && ollama pull qwen2.5-coder:1.5b && ollama pull nomic-embed-text

# 2. MyDevAgent (Python 3.10+)
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[server,search]"
cp .env.example .env                                      # profilo, chiavi di ricerca (opzionali)

# 3. Verifica e usa
mydevagent doctor
mydevagent
```

### Quale profilo?
| Profilo | Hardware | Modello principale |
|---|---|---|
| `cpu` | solo CPU, 16 GB RAM | qwen2.5-coder:3b |
| `gpu8` | GPU 8 GB (RTX 3060/4060, M1/M2 16 GB) | qwen2.5-coder:7b |
| `gpu16` | GPU 12–16 GB | qwen2.5-coder:14b |
| `gpu24` | GPU 24 GB / Mac 32 GB+ | qwen3-coder:30b (MoE, velocissimo) |

Cambia profilo con `MYDEVAGENT_PROFILE=gpu16` in `.env` o `mydevagent -p gpu16`. `mydevagent doctor` ti dice
quale profilo è adatto al tuo hardware.

### Se non parte
All'apertura MyDevAgent controlla da solo il server dei modelli e i modelli del profilo, e ti propone cosa fare.

| Problema | Soluzione |
|---|---|
| `permission denied` su `run.sh` o `install.sh` | `bash run.sh` (oppure `chmod +x run.sh scripts/install.sh`) |
| `mydevagent: comando non trovato` | usa `./run.sh`, oppure attiva l'ambiente: `source .venv/bin/activate` (Windows: `run.bat`) |
| «Il server dei modelli non risponde» | avvia Ollama: `ollama serve`, o apri l'app Ollama su Windows/macOS |
| «Il modello … non è installato» (errore 404) | all'avvio scegli **Scaricali ora** o **Usa i modelli che ho già**; nella UI: `/pull <nome>`, `/model <nome> --save` |
| «Il modello è troppo lento» / «non entra in memoria» | profilo più piccolo (`mydevagent -p cpu`) o `/fast`; `mydevagent bench` misura la velocità |

`mydevagent doctor` (o `/doctor` nella UI) mostra tutto in una volta: backend, modelli, hardware, rete, sandbox.

### Aggiornare
Nella UI scrivi `/update` (o `mydevagent update` dal terminale): scarica le novità con `git pull`, aggiorna
le dipendenze solo se sono cambiate e ti elenca cosa c'è di nuovo; poi riavvia MyDevAgent. Modelli, `.env`,
memoria e sessioni restano come sono. All'avvio Vio ti avvisa quando su GitHub ci sono novità.

Se avevi scaricato lo zip invece di clonare, collega la cartella a GitHub una volta sola (`.env` e `.venv`
restano):

```
cd <cartella di MyDevAgent>
git init
git remote add origin https://github.com/giovannisantorofrancesco2011-arch/MyDevAgent.git
git fetch origin claude/gracious-mayer-mt8l9b
git checkout -f -B claude/gracious-mayer-mt8l9b origin/claude/gracious-mayer-mt8l9b
```

## Uso

```bash
mydevagent                                        # interfaccia interattiva stile Claude Code (vedi docs/TUI.md)
mydevagent --continue                             # riprende l'ultima sessione di questa cartella
mydevagent --permissions auto-edit                # parte con le modifiche automatiche (comandi con conferma)
mydevagent bench                                  # misura la velocità dei modelli sul tuo PC
mydevagent ask "Scrivi un LRU cache thread-safe in Go con test"
mydevagent ask "Perché crasha?" -f app/main.py -f error.log
mydevagent ask "/deep API FastAPI per upload su S3 con auth JWT, Postgres e Docker"
mydevagent ask "Rifai questa UI in React + Tailwind" -i mockup.png
mydevagent ask "Qual è l'ultima versione di Next.js e cosa cambia? @web"
cat diff.patch | mydevagent ask - -q               # da stdin, solo risposta
mydevagent route "..."                            # mostra modalità e agenti scelti (0 token)
mydevagent agents                                 # tabella dei 35 agenti
mydevagent index                                  # indicizza il progetto corrente per il RAG
mydevagent serve                                  # server OpenAI-compatibile su :8000
ollama run mydevagent                             # modello single-agent (dopo `ollama create`, vedi sotto)
```

Nell'interfaccia chiedi quello che vuoi («aggiungi la paginazione a /users e i test»): l'agente esplora il
progetto, modifica i file mostrandoti i diff (in modalità `ask` chiede conferma), lancia i test e corregge.
`/` comandi · `@file` allega · `!comando` shell · `#nota` memoria · `Shift+Tab` permessi · `Esc` interrompe ·
`/undo` annulla · `/ultra-deep` per i lavori importanti. Guida completa: [docs/TUI.md](docs/TUI.md).

Nel messaggio puoi guidare il team: `/fast`, `/balanced`, `/deep`, `/ultra-deep`, `@security`, `@perf`,
`@web`, `@db`, `@fe`, `@be`, `@devops`, `@review`, `@docs`, `@mobile`, `@cloud`, `@gdpr`…

## Online e offline
- **Offline**: tutto funziona; il Research Agent si disattiva da solo e il team segnala cosa andrebbe
  verificato (versioni, API recenti). Forzalo con `MYDEVAGENT_OFFLINE=1`.
- **Online**: ricerca con catena di fallback **Tavily → Firecrawl → SearXNG → DuckDuckGo**.
  Senza chiavi funziona con DuckDuckGo (`pip install ddgs`, incluso in `[search]`). Per la qualità
  migliore imposta `TAVILY_API_KEY` (piano gratuito) o avvia SearXNG self-hosted
  (`docker compose -f deploy/docker-compose.yml up -d searxng` + `SEARXNG_URL=http://localhost:8080`).
  Le pagine lette passano da un filtro anti-SSRF (niente accesso a localhost/IP privati).

## Tool e sicurezza
| Tool | Note |
|---|---|
| `run_code` | sandbox **Docker**: `--network none`, filesystem read-only, utente nobody, limiti CPU/RAM/PID, timeout. Senza Docker i test vengono saltati (il backend `local` richiede `allow_unsafe_local: true`) |
| `read_file` `list_dir` `grep` | confinati nella workspace, rifiutano `.env`/chiavi, bloccano path traversal e symlink |
| `write_file` `git_commit` | **disattivati** di default (`tools.filesystem.allow_write`, `tools.git.allow_commit`) |
| `git_status` `git_diff` `git_log` | sola lettura |
| `web_search` `web_fetch` | solo online, anti-SSRF |
| `rag_search` | indice locale in `.mydevagent/` (embeddings o fallback lessicale) |
| **agente**: `edit_file` `write_file` `bash` `run_tests` | nella cartella del progetto, con i permessi della modalità scelta; checkpoint prima di ogni modifica; `rm -rf`, `sudo`, `git push --force`, `curl … \| sh` chiedono **sempre** conferma; `.env`/chiavi mai letti né scritti |
| visione | tier `vision` (qwen2.5vl) per screenshot, mockup, errori in immagine |

Il server rispetta `MYDEVAGENT_API_KEY` (Bearer) — obbligatoria se lo esponi fuori da localhost.

## Deploy
| Backend | Guida |
|---|---|
| Ollama (consigliato) | [deploy/ollama.md](deploy/ollama.md) · modelli single-agent: `ollama create mydevagent -f modelfiles/Modelfile.gpu8` |
| LM Studio | [deploy/lmstudio.md](deploy/lmstudio.md) |
| llama.cpp (+ speculative decoding) | [deploy/llamacpp.sh](deploy/llamacpp.sh) |
| vLLM (+ prefix caching) | [deploy/vllm.sh](deploy/vllm.sh) |
| Docker Compose (Ollama + SearXNG + server) | [deploy/docker-compose.yml](deploy/docker-compose.yml) |
| Hugging Face | i modelli si scaricano da HF con llama.cpp (`-hf`) o vLLM; il fine-tuning parte da HF (Unsloth) |

Qualsiasi server compatibile OpenAI funziona: basta `LLM_BASE_URL` e i nomi dei modelli nel profilo.

## Integrazione negli IDE
- **MyDevAgent Studio** (Windows): l'editor basato su VSCodium con Vio già dentro, chat, conferma delle modifiche,
  Ctrl+I e Tab. [Scarica l'installer](https://github.com/giovannisantorofrancesco2011-arch/MyDevAgent/releases/latest/download/MyDevAgent-Studio-Setup.exe),
  sorgenti nel branch [MyDevAgent-Studio](https://github.com/giovannisantorofrancesco2011-arch/MyDevAgent/tree/MyDevAgent-Studio)
  (usa `mydevagent bridge`, vedi `mydevagent/bridge.py`)
- **VS Code + Continue.dev** (chat multi-agente, edit inline, autocomplete, @codebase):
  [integrations/vscode.md](integrations/vscode.md) + [integrations/continue/config.yaml](integrations/continue/config.yaml)
- **GitHub Copilot Chat** con modelli locali (Ollama): [integrations/vscode.md](integrations/vscode.md#2-github-copilot-chat-con-modelli-locali-byok)
- **Cursor**: [integrations/cursor.md](integrations/cursor.md)
- **Aider, Cline, Open WebUI, SDK OpenAI, uso come libreria**: [integrations/aider.md](integrations/aider.md)

## Personalizzazione, velocità, fine-tuning
- [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md) — modelli, agenti, squadre, tool custom, RAG, QLoRA
- [docs/PERFORMANCE.md](docs/PERFORMANCE.md) — come renderlo più veloce e più efficiente con i token
- [finetune/README.md](finetune/README.md) — addestramento sui tuoi repository

## Struttura del progetto
```
config/settings.yaml      profili hardware, modalità, tool, server
config/agents.yaml        i 15 agenti del nucleo (ruolo, tier, budget, sezioni lette, tool, keyword)
config/agents_ultra.yaml  i 20 agenti estesi di /ultra-deep
prompts/                  persona di sistema + 35 prompt di ruolo
mydevagent/               router · grafo LangGraph · orchestratore · client LLM · tool · CLI · server
mydevagent/agent/         modalità agente: ciclo di tool, permessi, checkpoint, memoria, repo map
mydevagent/ultra.py       pipeline /ultra-deep a 35 agenti
mydevagent/tui/           interfaccia da terminale stile Claude Code
modelfiles/               Modelfile Ollama per profilo (persona integrata)
deploy/                   Ollama, LM Studio, llama.cpp, vLLM, Docker Compose, SearXNG
finetune/                 dataset dai tuoi repo, QLoRA con Unsloth, export GGUF → Ollama
integrations/             Continue.dev, VS Code/Copilot, Cursor, Aider/Cline
docs/                     agenti, performance, personalizzazione
tests/                    test con LLM finto (nessun modello richiesto): `pytest`
```

## Sviluppo
```bash
pip install -e ".[dev]"
pytest            # 76 test: registry, router, agente, permessi, checkpoint, ultra-deep, tool, UI, server
ruff check .
MYDEVAGENT_FAKE_LLM=1 mydevagent                 # prova l'interfaccia senza modello
```

Licenza MIT (vedi [LICENSE](LICENSE)). I modelli hanno le loro licenze (Qwen: Apache-2.0 per la maggior parte delle taglie —
verifica sempre la model card).
