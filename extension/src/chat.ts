// La chat di Vio: collega il pannello (media/chat.js) al ponte, apre i diff delle modifiche proposte e
// guida il primo avvio (MyDevAgent da installare, Ollama spento, modelli da scaricare).
import { spawn } from "child_process";
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { Bridge, BridgeError, Install, locate } from "./bridge";

type Msg = { type: string; [key: string]: any };
type Action = { id: string; label: string; primary?: boolean };

interface Approval {
  request: string;
  tool: string;
  path?: string;
  before?: string | null;
  after?: string | null;
  diff: string;
}

export const SCHEME = "mydevagent-proposta";
export const TAB_MODEL = "qwen2.5-coder:1.5b-base";
const WIN = process.platform === "win32";
const IGNORE = "**/{node_modules,.git,.venv,venv,__pycache__,dist,build,out,.mydevagent}/**";
const MAX_SELECTION = 50_000;

const config = () => vscode.workspace.getConfiguration("mydevagent");
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function errorInfo(error: unknown): { message: string; hint: string } {
  if (error instanceof BridgeError) return { message: error.message, hint: error.hint };
  return { message: error instanceof Error ? error.message : String(error), hint: "" };
}

export class Chat implements vscode.WebviewViewProvider, vscode.TextDocumentContentProvider {
  private view?: vscode.WebviewView;
  private ready = false;
  private transcript: { role: string; content: string }[] = [];
  private setupCard?: Msg;
  private approvals = new Map<string, Approval>();
  private proposals = new Map<string, string>(); // uri → testo dei documenti prima/dopo
  private files: string[] = [];
  private missing: string[] = [];
  private lastPrompt = "";
  private connecting?: Promise<void>;
  private ollama = false;
  private pullListener?: (p: any) => void;
  install?: Install;
  root?: string;
  readonly state = {
    connected: false, busy: false, permission: "ask", team: "auto", learn: false, model: "",
    commands: [] as { name: string; description: string }[], agents: {} as Record<string, string>,
    ctx: null as number | null, // % del contesto del modello usato (/context)
  };
  private readonly stateEmitter = new vscode.EventEmitter<void>();
  readonly onState = this.stateEmitter.event;

  constructor(private readonly context: vscode.ExtensionContext, readonly bridge: Bridge,
              private readonly log: vscode.OutputChannel) {
    bridge.onNotify(({ method, params }) => this.onNotify(method, params));
    bridge.onExit((tail) => this.onCrash(tail));
  }

  // ------------------------------------------------------------------ pannello
  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    this.ready = false;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    view.webview.onDidReceiveMessage((m: Msg) => this.handle(m));
    view.onDidDispose(() => {
      if (this.view === view) this.view = undefined;
    });
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const uri = (file: string) => webview.asWebviewUri(vscode.Uri.joinPath(media, file));
    const nonce = crypto.randomBytes(16).toString("base64");
    const script = (file: string) => `<script nonce="${nonce}" src="${uri(file)}"></script>`;
    return `<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${webview.cspSource} data:; style-src ${webview.cspSource}; font-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="${uri("chat.css")}" rel="stylesheet">
</head>
<body>
<div id="app">
  <header class="top">
    <div id="vio" class="vio" title="Accarezzami!"></div>
    <div class="who">
      <div class="name">Vio <span id="dot" class="dot off"></span><span id="model" class="model"></span></div>
      <div id="says" class="says"></div>
    </div>
    <div class="actions">
      <button class="icon" data-cmd="stats" title="Statistiche (/stats)"><svg viewBox="0 0 16 16"><path fill="currentColor" d="M2 13h12v1.2H2zM3 8h2.2v4H3zm3.9-4h2.2v8H6.9zm3.9 2H13v6h-2.2z"/></svg></button>
      <button class="icon" data-cmd="undo" title="Annulla le ultime modifiche di Vio (/undo)"><svg viewBox="0 0 16 16"><path fill="none" stroke="currentColor" stroke-width="1.5" d="M4.5 6.5h5.5a3.25 3.25 0 0 1 0 6.5H6"/><path fill="currentColor" d="M1.5 6.5 5.5 3v7z"/></svg></button>
      <button class="icon" data-cmd="clear" title="Nuova chat (/clear)"><svg viewBox="0 0 16 16"><path fill="none" stroke="currentColor" stroke-width="1.5" d="M8 3v10M3 8h10"/></svg></button>
    </div>
  </header>
  <main id="log"></main>
  <footer class="composer">
    <div id="context" class="context"></div>
    <div class="box">
      <div id="menu" class="menu hidden"></div>
      <textarea id="input" rows="1" placeholder="Chiedi a Vio… (/ comandi, @ file)"></textarea>
      <div class="bar">
        <select id="permission" title="Cosa può fare Vio senza chiedere"></select>
        <select id="team" title="Quanti agenti lavorano alla richiesta"></select>
        <span class="spacer"></span>
        <button id="send" class="send" title="Invia"></button>
      </div>
    </div>
    <div id="hint" class="hint"></div>
  </footer>
</div>
${script("vio.js")}
${script("markdown.js")}
${script("chat.js")}
</body>
</html>`;
  }

  private post(message: Msg): void {
    if (this.view && this.ready) void this.view.webview.postMessage(message);
  }

  /** Il pannello si è (ri)caricato: gli rimando quello che deve sapere. */
  private onReady(): void {
    this.ready = true;
    this.post({ type: "state", state: this.state });
    if (this.transcript.length) this.post({ type: "history", messages: this.transcript });
    if (this.setupCard) this.post(this.setupCard);
    this.post({ type: "files", files: this.files });
    this.postContext();
    // ponytail: un turno in corso non viene ridisegnato se il pannello si ricarica (succede solo spostandolo)
  }

  reveal(focusInput = false): void {
    if (this.view) this.view.show(!focusInput);
    else void vscode.commands.executeCommand("mydevagent.chat.focus");
    if (focusInput) setTimeout(() => this.post({ type: "focus" }), 150);
  }

  say(text: string, expression?: string, seconds?: number): void {
    this.post({ type: "say", text, expression, seconds });
  }

  private setup(kind: "info" | "error" | "none", title = "", text = "", actions: Action[] = [], say?: string): void {
    this.setupCard = kind === "none" ? undefined : { type: "setup", kind, title, text, actions, say };
    this.post(this.setupCard || { type: "setup", kind: "none" });
  }

  private setState(change: Record<string, unknown>): void {
    Object.assign(this.state, change);
    void vscode.commands.executeCommand("setContext", "mydevagent.busy", this.state.busy);
    this.post({ type: "state", state: this.state });
    this.stateEmitter.fire();
  }

  // ------------------------------------------------------------ collegamento
  connect(): Promise<void> {
    if (!this.connecting) this.connecting = this.doConnect().finally(() => (this.connecting = undefined));
    return this.connecting;
  }

  private async doConnect(): Promise<void> {
    this.bridge.stop();
    this.setState({ connected: false, busy: false });
    const folder = vscode.workspace.workspaceFolders?.[0]; // ponytail: con più cartelle aperte lavora nella prima
    if (!folder || folder.uri.scheme !== "file") {
      return this.setup("info", "Apri una cartella", "Lavoro dentro la cartella del tuo progetto: aprine una e cominciamo.",
        [{ id: "open-folder", label: "Apri una cartella", primary: true }], "Apri una cartella e cominciamo!");
    }
    if (!vscode.workspace.isTrusted) { // modalità protetta: si ricollega da sola quando ti fidi (extension.ts)
      return this.setup("info", "Questa cartella non è ancora fidata",
        "Studio l'ha aperta in modalità protetta: finché non ti fidi, io non leggo, non modifico e non lancio niente qui dentro.",
        [{ id: "trust-folder", label: "Mi fido di questa cartella", primary: true }], "Posso lavorare qui? Dimmi che ti fidi.");
    }
    this.root = folder.uri.fsPath;
    this.install = locate(config().get("percorso", ""));
    if (!this.install) {
      return this.setup("error", "Non trovo MyDevAgent", WIN
        ? "Posso installarlo io: scarico Python, Ollama, MyDevAgent e i modelli (serve Internet e qualche GB di spazio). Se ce l'hai già, dimmi dov'è."
        : "Installalo seguendo il README di MyDevAgent, oppure dimmi in che cartella si trova.",
      [...(WIN ? [{ id: "install", label: "Installa MyDevAgent", primary: true }] : []),
        { id: "choose-folder", label: "Ce l'ho già: scegli la cartella" }], "Mi manca il mio cervello: MyDevAgent!");
    }
    this.setup("info", "Mi sto svegliando…", "Avvio MyDevAgent e controllo i modelli.", [], "Mi sto svegliando…");
    let hello: any;
    try {
      await this.bridge.start(this.install, this.root, config().get("profilo", ""));
      hello = await this.bridge.request("hello", { resume: true, permission: config().get("permessi", "ask") });
      await this.bridge.request("set", { team: config().get("team", "auto") });
    } catch (error) {
      const { message, hint } = errorInfo(error);
      return this.setup("error", message, hint || "Guarda il registro per i dettagli.",
        [{ id: "retry", label: "Riprova", primary: true }, { id: "log", label: "Mostra il registro" }], "Non riesco a svegliarmi…");
    }
    this.ollama = hello.ollama;
    this.transcript = hello.history;
    this.post({ type: "reset" });
    this.setState({ permission: hello.permission, team: config().get("team", "auto"), learn: hello.learn,
      model: hello.models.main, commands: hello.commands, agents: hello.agents });
    if (this.transcript.length) this.post({ type: "history", messages: this.transcript });
    void this.refreshFiles();
    // quale MyDevAgent sto usando: se ce ne sono più copie sul PC, così si vede subito
    const where = hello.home ? `MyDevAgent in ${hello.home}${hello.installed ? ` · versione ${hello.installed}` : ""}` : "";
    if (where) {
      this.log.appendLine(where);
      this.post({ type: "notice", text: `🟣 ${where}` });
    }
    if (await this.checkHealth()) this.askTrust(hello.untrusted);
    void this.refreshUsage();
    void this.checkUpdates();
  }

  /** Novità di MyDevAgent su GitHub? Le propone con il pulsante «Aggiorna» (in silenzio se offline). */
  private async checkUpdates(): Promise<void> {
    try {
      const { available } = await this.bridge.request("updates");
      if (available > 0) {
        this.post({ type: "update", count: available, actions: [{ id: "update", label: "Aggiorna", primary: true }] });
      }
    } catch {
      // MyDevAgent vecchio (senza il metodo) o niente git: niente avviso
    }
  }

  private async runUpdate(): Promise<void> {
    if (this.state.busy) return this.showError(new BridgeError("Sto lavorando", "Aspetta la fine della richiesta, poi aggiorna."));
    this.setup("info", "Aggiorno MyDevAgent…", "Scarico le novità da GitHub.", [], "Mi aggiorno…");
    let result: any;
    try {
      result = await this.bridge.request("update");
    } catch (error) {
      const { message, hint } = errorInfo(error);
      return this.setup("error", message, hint || "Aggiorna dal terminale con /update.",
        [{ id: "dismiss", label: "Chiudi" }], "Non sono riuscita ad aggiornarmi.");
    }
    if (!result.ok) {
      return this.setup("error", "Aggiornamento non riuscito", result.message, [{ id: "dismiss", label: "Chiudi" }],
        "Non sono riuscita ad aggiornarmi.");
    }
    this.setup("none");
    const news = (result.changes || []).slice(0, 8).map((c: string) => `• ${c}`).join("\n");
    this.post({ type: "notice", text: result.message + (news ? `\n${news}` : "") });
    if (result.restart) {
      await this.connect(); // riparte con il codice nuovo
      this.say("Aggiornata! Adesso ho le ultime novità.", "love", 5);
    }
  }

  /** La percentuale di contesto usato, per la barra in alto. */
  private async refreshUsage(): Promise<void> {
    try {
      const usage = await this.bridge.request("context");
      this.setState({ ctx: usage.percent });
    } catch {
      this.setState({ ctx: null });
    }
  }

  /** Ollama acceso e modelli presenti? Se manca qualcosa lo dice nella chat, con il pulsante per sistemarlo. */
  private async checkHealth(): Promise<boolean> {
    const ok = await this.health();
    this.setState({ connected: ok }); // pronta solo con i modelli: senza, chat, Ctrl+I e Tab aspettano
    return ok;
  }

  private async health(): Promise<boolean> {
    let health: any;
    try {
      health = await this.bridge.request("health", { models: [TAB_MODEL] });
    } catch (error) {
      this.showError(error);
      return false;
    }
    if (health.down.length) {
      this.setup("error", this.ollama ? "Ollama non risponde" : "Il server dei modelli non risponde", this.ollama
        ? "Ollama è il programma che fa girare i modelli sul tuo computer: avvialo e riprovo."
        : `Non riesco a raggiungere ${health.down.join(", ")}: avvia il server e riprova.`,
      [...(this.ollama ? [{ id: "start-ollama", label: "Avvia Ollama", primary: true }] : []),
        { id: "retry", label: "Riprova", primary: !this.ollama }], "I modelli dormono…");
      return false;
    }
    this.missing = [...new Set<string>(health.missing.filter((m: any) => m.tier !== "vision").map((m: any) => m.model))];
    if (this.missing.length) {
      this.setup("info", "Mi mancano dei modelli", `Per lavorare mi servono **${this.missing.join(", ")}**. ` +
        "Li scarico da Ollama una volta sola: possono volerci alcuni minuti.",
      [{ id: "pull", label: "Scarica i modelli", primary: true }], "Mi servono i miei modelli!");
      return false;
    }
    this.setup("none");
    this.say(`Ciao! Sono pronta: lavoro in ${path.basename(this.root || "")}.`, "done", 6);
    void this.suggestTabModel(health.has[TAB_MODEL]);
    return true;
  }

  private askTrust(untrusted: { hooks: string[]; mcp: string[] }): void {
    const items = [...untrusted.hooks.map((h) => `hook ${h}`), ...untrusted.mcp.map((m) => `server MCP ${m}`)];
    if (!items.length) return;
    this.setup("info", "Questo progetto vuole attivare dei comandi",
      `Il progetto ha: ${items.map((i) => `\`${i}\``).join(", ")}. Attivali solo se ti fidi di chi ha scritto il progetto.`,
      [{ id: "trust", label: "Mi fido, attivali" }, { id: "dismiss", label: "No" }]);
  }

  private async suggestTabModel(installed: boolean): Promise<void> {
    if (!this.ollama || config().get("tab.modello", "")) return;
    if (installed) return void config().update("tab.modello", TAB_MODEL, vscode.ConfigurationTarget.Global);
    if (this.context.globalState.get("tabModelAsked")) return;
    await this.context.globalState.update("tabModelAsked", true);
    const choice = await vscode.window.showInformationMessage(
      `Per i suggerimenti con Tab funziona meglio il modello ${TAB_MODEL} (circa 1 GB). Lo scarico?`, "Scarica", "No grazie");
    if (choice !== "Scarica") return;
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: `Scarico ${TAB_MODEL}` },
      async (progress) => {
        let last = 0;
        this.pullListener = (p) => {
          const pct = p.total ? Math.floor((100 * p.completed) / p.total) : 0;
          if (pct > last) progress.report({ increment: pct - last, message: `${pct}%` });
          last = Math.max(last, pct);
        };
        try {
          await this.bridge.request("pull", { models: [TAB_MODEL] });
          await config().update("tab.modello", TAB_MODEL, vscode.ConfigurationTarget.Global);
        } catch (error) {
          void vscode.window.showErrorMessage(`Download non riuscito: ${errorInfo(error).message}`);
        } finally {
          this.pullListener = undefined;
        }
      });
  }

  private onCrash(tail: string): void {
    const busy = this.state.busy;
    this.setState({ connected: false, busy: false });
    if (busy) this.post({ type: "turnEnd", answer: "", cancelled: false, error: { message: "MyDevAgent si è chiuso", hint: tail } });
    this.setup("error", "MyDevAgent si è chiuso", tail || "Guarda il registro per i dettagli.",
      [{ id: "retry", label: "Riavvia", primary: true }, { id: "log", label: "Mostra il registro" }], "Ahi, mi sono addormentata!");
  }

  // ------------------------------------------------------- dal pannello
  /** Un messaggio dal pannello (o da un comando dell'editor): gli errori finiscono nella chat. */
  async handle(m: Msg): Promise<void> {
    try {
      switch (m.type) {
        case "ready": return this.onReady();
        case "send": return await this.send(String(m.text), m.context !== false);
        case "stop": return void (await this.bridge.request("cancel"));
        case "approve": return await this.answer(m.request, m.answer, m.feedback || "");
        case "review": return await this.openProposal(m.request, false);
        case "open": return await this.openFile(m.path);
        case "command": return await this.command(m.name, m.days);
        case "set": return await this.set(m);
        case "action": return await this.action(m.id);
        case "copy": return void (await vscode.env.clipboard.writeText(m.text));
        case "insert": return await this.insert(m.text);
        case "link":
          if (/^https?:\/\//.test(m.href)) await vscode.env.openExternal(vscode.Uri.parse(m.href));
      }
    } catch (error) {
      this.showError(error);
    }
  }

  private showError(error: unknown, actions: Action[] = []): void {
    this.post({ type: "error", ...errorInfo(error), actions });
  }

  async send(text: string, withContext: boolean): Promise<void> {
    if (!this.state.connected) {
      this.showError(new BridgeError("Non sono ancora collegata", "Guarda il messaggio qui sopra per sistemare."),
        [{ id: "retry", label: "Riprova a collegarti" }]);
      return;
    }
    if (config().get("salvaPrimaDiInviare", true)) await vscode.workspace.saveAll(false);
    this.lastPrompt = text;
    this.post({ type: "user", text });
    this.setState({ busy: true });
    try {
      await this.bridge.request("prompt", { text, context: withContext ? this.editorContext(true) : {} });
      this.transcript.push({ role: "user", content: text });
    } catch (error) {
      this.setState({ busy: false });
      this.post({ type: "turnEnd", answer: "", cancelled: false, error: errorInfo(error) });
    }
  }

  private async set(m: Msg): Promise<void> {
    const change: Record<string, unknown> = {};
    for (const key of ["permission", "team", "learn"]) if (key in m) change[key] = m[key];
    this.setState(change);
    if (this.bridge.running) await this.bridge.request("set", change);
    if ("permission" in change) await config().update("permessi", change.permission, vscode.ConfigurationTarget.Global);
    if ("team" in change) await config().update("team", change.team, vscode.ConfigurationTarget.Global);
  }

  /** L'utente ha cambiato le impostazioni a mano. */
  async onConfig(e: vscode.ConfigurationChangeEvent): Promise<void> {
    if (e.affectsConfiguration("mydevagent.percorso") || e.affectsConfiguration("mydevagent.profilo")) return this.connect();
    const permission = config().get("permessi", "ask");
    const team = config().get("team", "auto");
    if (permission !== this.state.permission || team !== this.state.team) await this.set({ type: "set", permission, team });
  }

  private async command(name: string, days?: number): Promise<void> {
    if (name === "undo") {
      const undone = await this.bridge.request("undo");
      this.post({ type: "notice", text: undone ? `↩ Annullate le modifiche di «${undone.label}»: ${undone.files.join(", ")}`
        : "Niente da annullare." });
      if (undone) this.say("Fatto: ho rimesso tutto com'era.", "done", 4);
    } else if (name === "diff") {
      const { diff } = await this.bridge.request("diff");
      if (!diff.trim()) return this.post({ type: "notice", text: "Nessuna modifica in questa sessione." });
      await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({ language: "diff", content: diff }));
    } else if (name === "clear") {
      await this.bridge.request("clear");
      this.transcript = [];
      this.post({ type: "reset" });
      void this.refreshUsage();
      this.say("Chat nuova: dimmi pure!", "done", 4);
    } else if (name === "context") {
      const usage = await this.bridge.request("context");
      this.setState({ ctx: usage.percent });
      this.post({ type: "contextUsage", ...usage });
    } else if (name === "compact") {
      this.setState({ busy: true });
      this.say("Riassumo la conversazione…", "think");
      try {
        const result = await this.bridge.request("compact");
        this.setState({ ctx: result.usage.percent });
        this.post({ type: "compacted", ...result });
        if (result.compacted) this.say("Fatto: ho fatto spazio nella mia memoria.", "done", 4);
      } finally {
        this.setState({ busy: false });
      }
    } else if (name === "update") {
      await this.runUpdate();
    } else if (name === "stats") {
      const result = await this.bridge.request("stats", days ? { days } : {});
      this.post({ type: "stats", ...result, label: days ? `ultimi ${days} giorni` : "da sempre" });
    }
  }

  private async action(id: string): Promise<void> {
    switch (id) {
      case "open-folder": return void (await vscode.commands.executeCommand("workbench.action.files.openFolder"));
      case "install": return this.runInstaller();
      case "choose-folder": {
        const picked = await vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false,
          title: "Dov'è la cartella di MyDevAgent? (quella con .venv)" });
        if (picked) await config().update("percorso", picked[0].fsPath, vscode.ConfigurationTarget.Global);
        return; // onConfig si ricollega
      }
      case "retry": return this.connect();
      case "trust-folder": return void vscode.commands.executeCommand("workbench.trust.manage");
      case "log": return this.log.show();
      case "pull": return this.pullMissing();
      case "start-ollama": return this.startOllama();
      case "trust":
        await this.bridge.request("trust");
        this.setup("none");
        return this.say("Ok, ho attivato gli hook e i server MCP del progetto.", "done", 5);
      case "dismiss": return this.setup("none");
      case "update": return this.runUpdate();
      case "retry-last": if (this.lastPrompt) this.post({ type: "fill", text: this.lastPrompt });
    }
  }

  private async pullMissing(): Promise<void> {
    this.setup("info", "Scarico i modelli…", "Puoi continuare a usare l'editor mentre scarico.", [], "Scarico i miei modelli…");
    try {
      await this.bridge.request("pull", { models: this.missing });
    } catch (error) {
      const { message, hint } = errorInfo(error);
      return this.setup("error", "Download non riuscito", `${message}${hint ? `: ${hint}` : ""}`,
        [{ id: "pull", label: "Riprova", primary: true }]);
    }
    await this.checkHealth();
  }

  private async startOllama(): Promise<void> {
    const app = WIN ? path.join(process.env.LOCALAPPDATA || "", "Programs", "Ollama", "ollama app.exe") : "";
    const [command, args] = app && fs.existsSync(app) ? [app, []] : ["ollama", ["serve"]];
    const proc = spawn(command, args, { detached: true, stdio: "ignore", windowsHide: true });
    proc.on("error", (error) => this.log.appendLine(`Ollama non parte: ${error.message}`));
    proc.unref();
    this.setup("info", "Avvio Ollama…", "Un attimo…", [], "Sveglio Ollama…");
    for (let i = 0; i < 10; i++) {
      await sleep(1500);
      const health = await this.bridge.request("health").catch(() => undefined);
      if (health && !health.down.length) break;
    }
    if (await this.checkHealth()) this.say("Ollama è sveglio: possiamo lavorare!", "done", 5);
  }

  private async runInstaller(): Promise<void> {
    const script = path.join(this.context.extensionPath, "setup", "installa-mydevagent.ps1");
    const task = new vscode.Task({ type: "mydevagent" }, vscode.TaskScope.Global, "Installa MyDevAgent", "MyDevAgent",
      new vscode.ProcessExecution("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]));
    task.presentationOptions = { reveal: vscode.TaskRevealKind.Always, panel: vscode.TaskPanelKind.Dedicated, clear: true };
    const execution = await vscode.tasks.executeTask(task);
    this.setup("info", "Installo MyDevAgent…", "Segui l'avanzamento nel terminale qui sotto: quando finisce mi collego da sola.",
      [], "Installo il mio cervello…");
    const code = await new Promise<number | undefined>((resolve) => {
      const sub = vscode.tasks.onDidEndTaskProcess((e) => {
        if (e.execution === execution) {
          sub.dispose();
          resolve(e.exitCode);
        }
      });
    });
    if (code === 0) return this.connect();
    this.setup("error", "L'installazione non è riuscita", "Nel terminale qui sotto c'è il motivo. Sistemato quello, riprova.",
      [{ id: "install", label: "Riprova", primary: true }, { id: "choose-folder", label: "Scegli la cartella" }]);
  }

  // ------------------------------------------------------------- dal ponte
  private onNotify(method: string, params: any): void {
    if (method === "event") this.post({ type: "event", event: params.event });
    else if (method === "chunk") this.post({ type: "chunk", text: params.text });
    else if (method === "approval") void this.onApproval(params);
    else if (method === "turn_end") void this.onTurnEnd(params);
    else if (method === "pull") {
      const done = params.status === "success";
      this.post({ type: "pull", ...params, done });
      this.pullListener?.(params);
    }
  }

  private async onApproval(approval: Approval): Promise<void> {
    this.approvals.set(approval.request, approval);
    void vscode.commands.executeCommand("setContext", "mydevagent.pendingApproval", true);
    if (!this.view?.visible) this.reveal();
    this.post({ type: "approval", approval });
    if (approval.after !== null && approval.after !== undefined && config().get("diffAutomatico", true)) {
      await this.openProposal(approval.request, true);
    }
  }

  private async onTurnEnd(end: any): Promise<void> {
    this.setState({ busy: false });
    this.post({ type: "turnEnd", answer: end.answer, cancelled: end.cancelled, error: end.error });
    if (end.answer) this.transcript.push({ role: "assistant", content: end.answer });
    for (const request of [...this.approvals.keys()]) await this.closeApproval(request);
    if (end.files?.length) void this.refreshFiles();
    void this.refreshUsage();
  }

  // ------------------------------------------------- modifiche proposte
  private proposalUri(approval: Approval, side: "prima" | "dopo"): vscode.Uri {
    const file = (approval.path || "file").replace(/\\/g, "/");
    return vscode.Uri.from({ scheme: SCHEME, path: "/" + file.replace(/^\/+/, ""), query: `${approval.request}-${side}` });
  }

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.proposals.get(uri.toString()) ?? "";
  }

  /** Apre il confronto prima/dopo: i pulsanti ✓ e ✗ in alto a destra applicano o rifiutano. */
  async openProposal(request: string, preserveFocus: boolean): Promise<void> {
    const approval = this.approvals.get(request);
    if (!approval || approval.after === null || approval.after === undefined) return;
    const before = this.proposalUri(approval, "prima");
    const after = this.proposalUri(approval, "dopo");
    this.proposals.set(before.toString(), approval.before ?? "");
    this.proposals.set(after.toString(), approval.after);
    const name = path.basename(approval.path || "file");
    const title = approval.before === null ? `${name} (file nuovo proposto da Vio)` : `${name} (modifica proposta da Vio)`;
    await vscode.commands.executeCommand("vscode.diff", before, after, title, { preview: true, preserveFocus });
  }

  /** Il ✓/✗ nella barra del diff (o dal riquadro comandi): vale per la proposta aperta o per l'ultima. */
  requestFor(uri?: vscode.Uri): string | undefined {
    if (uri?.scheme === SCHEME) return uri.query.replace(/-(prima|dopo)$/, "");
    return [...this.approvals.keys()].pop();
  }

  async answer(request: string | undefined, answer: "yes" | "always" | "no", feedback: string): Promise<void> {
    if (!request || !this.approvals.has(request)) return;
    this.post({ type: "approvalClosed", request, answer, feedback });
    await this.closeApproval(request);
    await this.bridge.request("approval_reply", { request, answer, feedback });
  }

  private async closeApproval(request: string): Promise<void> {
    this.approvals.delete(request);
    void vscode.commands.executeCommand("setContext", "mydevagent.pendingApproval", this.approvals.size > 0);
    const tabs = vscode.window.tabGroups.all.flatMap((group) => group.tabs).filter((tab) =>
      tab.input instanceof vscode.TabInputTextDiff && tab.input.modified.scheme === SCHEME &&
      tab.input.modified.query.startsWith(`${request}-`));
    if (tabs.length) await vscode.window.tabGroups.close(tabs, true);
    for (const key of [...this.proposals.keys()]) if (key.includes(`${request}-`)) this.proposals.delete(key);
  }

  // ------------------------------------------------------------- editor
  relative(file: string): string {
    const rel = this.root ? path.relative(this.root, file) : file;
    return (rel && !rel.startsWith("..") && !path.isAbsolute(rel) ? rel : file).replace(/\\/g, "/");
  }

  /** Il file aperto e la selezione: Vio li vede senza doverli citare. */
  private editorContext(withText: boolean): Record<string, unknown> {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.uri.scheme !== "file") return {};
    const file = this.relative(editor.document.uri.fsPath);
    const sel = editor.selection;
    if (sel.isEmpty) return { file, line: sel.active.line + 1 };
    const end = sel.end.character === 0 && sel.end.line > sel.start.line ? sel.end.line : sel.end.line + 1;
    const selection: Record<string, unknown> = { start: sel.start.line + 1, end };
    if (withText) selection.text = editor.document.getText(sel).slice(0, MAX_SELECTION);
    return { file, selection };
  }

  postContext(): void {
    const context = this.editorContext(false);
    this.post({ type: "context", context: context.file ? context : null });
  }

  async refreshFiles(): Promise<void> {
    if (!this.root) return;
    const uris = await vscode.workspace.findFiles(new vscode.RelativePattern(this.root, "**/*"), IGNORE, 5000);
    this.files = uris.map((uri) => this.relative(uri.fsPath)).sort();
    this.post({ type: "files", files: this.files });
  }

  private async openFile(file: string): Promise<void> {
    const full = path.isAbsolute(file) || !this.root ? file : path.join(this.root, file);
    if (!fs.existsSync(full)) return this.post({ type: "notice", text: `Non trovo ${file}` });
    await vscode.window.showTextDocument(vscode.Uri.file(full), { preview: true });
  }

  private async insert(text: string): Promise<void> {
    const editor = vscode.window.activeTextEditor ?? vscode.window.visibleTextEditors[0];
    if (!editor) return this.post({ type: "notice", text: "Apri un file per inserire il codice." });
    await editor.edit((edit) => editor.selections.forEach((sel) => edit.replace(sel, text)));
    await vscode.window.showTextDocument(editor.document, editor.viewColumn);
  }
}
