// La chat di Vio dentro l'editor. Riceve dal lato estensione gli stessi eventi del terminale
// (tool, diff, todo, test…) e li disegna; manda indietro messaggi, conferme e comandi.
(function () {
  const vscode = acquireVsCodeApi();
  const { render: markdown, escape } = window.Markdown;
  const $ = (sel) => document.querySelector(sel);
  const log = $("#log");
  const input = $("#input");
  const send = $("#send");
  const menu = $("#menu");

  const TOOL = {
    read_file: "Legge", list_files: "Elenca", grep: "Cerca", bash: "Esegue", run_tests: "Test",
    web_search: "Web", web_fetch: "Pagina", rag_search: "Codice", skill: "Skill", mcp: "MCP", task: "Sotto-agente",
    preview: "Anteprima", git_diff: "Diff",
  };
  const DOING = {
    read_file: (a) => `Leggo ${a.path || "un file"}…`, list_files: () => "Guardo i file del progetto…",
    grep: (a) => `Cerco «${a.pattern || ""}»…`, bash: () => "Eseguo un comando…", run_tests: () => "Lancio i test…",
    web_search: (a) => `Cerco sul web: ${a.query || ""}…`, web_fetch: () => "Leggo una pagina web…",
    preview: () => "Guardo l'anteprima del sito…", task: (a) => `Chiedo aiuto al sotto-agente ${a.agent || ""}…`,
  };
  const PERMISSIONS = { ask: "Chiede conferma", "auto-edit": "Modifica da sola", plan: "Solo piano", auto: "Tutto automatico" };
  const TEAMS = { auto: "Team: auto", fast: "Team: fast", balanced: "Team: balanced", deep: "Team: deep", "ultra-deep": "Team: ultra-deep" };
  const COMMANDS = [
    ["/fast", "team veloce: un solo agente"], ["/balanced", "team standard: piano, modifiche, test e review"],
    ["/deep", "team completo: sicurezza, performance, casi limite"], ["/ultra-deep", "35 agenti, per i lavori importanti (lento)"],
    ["/auto", "sceglie Vio il team giusto"], ["/plan", "modalità piano: legge e propone, non modifica niente"],
    ["/undo", "annulla le modifiche dell'ultima richiesta"], ["/diff", "tutte le modifiche di questa sessione"],
    ["/stats", "statistiche e grafico dell'attività · /stats 7 · /stats 30"], ["/clear", "nuova chat"],
    ["/context", "quanto contesto del modello stai usando"], ["/compact", "riassume la conversazione per fare spazio"],
    ["/update", "aggiorna MyDevAgent all'ultima versione"],
    ["/impara", "modalità impara: ti lascio scrivere un pezzo di codice"],
    ["/init", "crea MYDEVAGENT.md con comandi e convenzioni del progetto"],
  ];
  const MONTHS = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"];
  const LEVELS = ["#3b0764", "#6b21a8", "#9333ea", "#c084fc"];
  const ICONS = {
    send: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M1.7 1.2 15 8 1.7 14.8l1.6-5.8L10 8 3.3 7z"/></svg>',
    stop: '<svg viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/></svg>',
  };

  const state = {
    connected: false, busy: false, permission: "ask", team: "auto", learn: false, model: "", commands: [],
    files: [], context: null, contextOff: false, started: 0, tokens: 0,
  };
  let turn = null; // il turno in corso: elementi e testo della risposta
  let setupEl = null;
  const approvals = new Map();
  let vio = { expression: "ask", until: 0, text: "", frame: 0, blinkAt: Date.now() + 4000 };
  let menuItems = [];
  let menuIndex = 0;

  // ------------------------------------------------------------------ utilità
  const el = (tag, cls, html) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  };
  const num = (n) => Math.round(n || 0).toLocaleString("it-IT");
  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  function scroll(force) {
    if (force || nearBottom()) requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  }
  function append(node, force) {
    const stick = force || nearBottom();
    (turn ? turn.el : log).appendChild(node);
    if (stick) scroll(true);
    return node;
  }
  const post = (message) => vscode.postMessage(message);
  const basename = (p) => String(p || "").replace(/\\/g, "/").split("/").pop();
  function shortArgs(args) {
    const values = Object.values(args || {});
    if (values.length === 1) return String(values[0]).replace(/\s+/g, " ");
    return Object.entries(args || {}).map(([k, v]) => `${k}=${String(v).replace(/\s+/g, " ").slice(0, 40)}`).join(", ");
  }
  function duration(seconds) {
    if (seconds < 60) return `${Math.round(seconds)} s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min`;
    return `${Math.floor(minutes / 60)} h ${minutes % 60 ? `${minutes % 60} min` : ""}`.trim();
  }

  // ------------------------------------------------------------------- Vio
  function say(text, expression, seconds) {
    vio.text = text;
    if (expression) {
      vio.expression = expression;
      vio.until = seconds ? Date.now() + seconds * 1000 : 0;
    }
    drawVio();
  }
  function idleExpression() { return state.permission in window.Vio.SAYS ? state.permission : "ask"; }
  function drawVio() {
    const now = Date.now();
    let expression = vio.expression;
    if (vio.until && now > vio.until) {
      vio.until = 0;
      vio.expression = expression = idleExpression();
      if (!state.busy) vio.text = window.Vio.SAYS[expression];
    }
    if (state.busy && !approvals.size) expression = Math.floor(now / 2000) % 3 ? "think" : "look";
    else if (!vio.until && now > vio.blinkAt) {
      expression = "blink";
      if (now > vio.blinkAt + 180) vio.blinkAt = now + 3500 + Math.random() * 3000;
    }
    const frame = state.busy ? Math.floor(now / 450) % 2 : Math.floor(now / 1400) % 2;
    $("#vio").innerHTML = window.Vio.svg(expression, frame, 3);
    const big = $(".welcome .vio-big");
    if (big) big.innerHTML = window.Vio.svg(expression === "blink" ? "blink" : idleExpression(), frame, 7);
    let text = vio.text || window.Vio.SAYS[idleExpression()];
    if (state.busy && state.started) {
      const seconds = (now - state.started) / 1000;
      $("#hint").textContent = `esc per fermare · ${duration(seconds)} · ${num(state.tokens)} token`;
    }
    $("#says").textContent = text;
  }
  setInterval(drawVio, 150);
  $("#vio").addEventListener("click", () => {
    vio.pats = (vio.pats || 0) + 1;
    say(window.Vio.PATS[(vio.pats - 1) % window.Vio.PATS.length], "love", 3);
  });

  // --------------------------------------------------------------- welcome
  function welcome() {
    if (log.querySelector(".turn")) return;
    log.innerHTML = "";
    const box = el("div", "welcome");
    box.innerHTML = `<div class="vio-big">${window.Vio.svg("ask", 0, 7)}</div>
      <h2>Ciao, sono Vio!</h2>
      <p>Dimmi cosa vuoi fare nel progetto: leggo il codice, modifico i file e lancio i test. Tu confermi ogni modifica.</p>`;
    const chips = el("div", "chips");
    for (const text of ["Spiegami questo progetto", "Trova e correggi i bug", "Aggiungi dei test", "/init"]) {
      const chip = el("button", "chip");
      chip.textContent = text === "/init" ? "Crea MYDEVAGENT.md" : text;
      chip.onclick = () => { input.value = text; autosize(); input.focus(); };
      chips.appendChild(chip);
    }
    box.appendChild(chips);
    log.appendChild(box);
  }

  // ------------------------------------------------------------------ turni
  function startTurn(text) {
    log.querySelector(".welcome")?.remove();
    const node = el("div", "turn");
    log.appendChild(node);
    turn = { el: node, activity: null, steps: null, answerEl: null, answer: "", todo: null, count: 0,
             tools: 0, files: new Set(), diffs: new Map(), renderAt: 0 };
    const bubble = el("div", "user");
    bubble.textContent = text;
    node.appendChild(bubble);
    scroll(true);
  }
  function steps() {
    if (!turn) startTurn("");
    if (!turn.steps) {
      turn.activity = append(el("div", "activity"));
      turn.summary = el("div", "summary hidden");
      turn.summary.onclick = () => {
        turn_toggle(turn.activity);
      };
      turn.steps = el("div", "steps");
      turn.activity.append(turn.summary, turn.steps);
    }
    return turn.steps;
  }
  function turn_toggle(activity) {
    const list = activity.querySelector(".steps");
    const summary = activity.querySelector(".summary");
    list.classList.toggle("collapsed");
    summary.textContent = summary.textContent.replace(/^[▸▾]/, list.classList.contains("collapsed") ? "▸" : "▾");
  }
  function step(what, args, cls) {
    const list = steps();
    const row = el("div", "step" + (cls ? " " + cls : ""));
    const mark = cls && cls.includes("result") ? "⎿" : cls && cls.includes("info") ? "·" : "⏺";
    row.innerHTML = `<span class="mark">${mark}</span>${what ? `<span class="what">${escape(what)}</span>` : ""}` +
      `<span class="args">${escape(args || "")}</span>`;
    list.appendChild(row);
    turn.count++;
    scroll();
    return row;
  }
  function answerEl() {
    if (!turn) startTurn("");
    if (!turn.answerEl) turn.answerEl = append(el("div", "answer"));
    return turn.answerEl;
  }
  function renderAnswer(final) {
    if (!turn) return;
    const now = Date.now();
    if (!final && now - turn.renderAt < 120) {
      clearTimeout(turn.timer);
      turn.timer = setTimeout(() => renderAnswer(true), 130);
      return;
    }
    turn.renderAt = now;
    const stick = nearBottom();
    answerEl().innerHTML = markdown(turn.answer);
    if (stick) scroll(true);
  }

  function miniDiff(diff, max) {
    const lines = String(diff || "").split("\n").filter((l) => !l.startsWith("---") && !l.startsWith("+++") && l !== "");
    const shown = lines.slice(0, max);
    let html = shown.map((line) => {
      const kind = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "";
      return `<span class="${kind}">${escape(line)}</span>`;
    }).join("");
    if (lines.length > shown.length) html += `<span class="more">… altre ${lines.length - shown.length} righe</span>`;
    return `<pre class="diff">${html}</pre>`;
  }
  function diffStats(diff) {
    let added = 0; let removed = 0;
    for (const line of String(diff || "").split("\n")) {
      if (line.startsWith("+") && !line.startsWith("+++")) added++;
      if (line.startsWith("-") && !line.startsWith("---")) removed++;
    }
    return [added, removed];
  }

  function onEvent(e) {
    switch (e.type) {
      case "route": {
        const names = (e.agents || []).filter((a) => a !== "formatter").map((a) => (state.agents || {})[a] || a);
        const line = el("div", "route");
        line.innerHTML = e.mode === "fast" ? `<b>fast</b> · ${escape(names[0] || "")}` :
          `<b>team ${escape(e.mode)}</b> · ${escape(names.join(" → "))}`;
        if (e.suggest_ultra) line.innerHTML += " · per un lavoro così prova <b>/ultra-deep</b>";
        append(line);
        break;
      }
      case "agent_start":
        say(e.agent === "formatter" ? "Scrivo la risposta…" : `${e.name || e.agent} sta lavorando…`);
        break;
      case "agent_end":
        if (!e.quiet && e.agent !== "final") {
          const tokens = (e.prompt_tokens || 0) + (e.completion_tokens || 0);
          if (!e.counted) state.tokens += tokens;
          if (e.error) step(e.name || e.agent, String(e.error), "result bad");
          else step(e.name || e.agent, `${(e.ms / 1000).toFixed(1)} s · ${num(tokens)} token` +
            (e.tool_calls ? ` · ${e.tool_calls} strumenti` : ""), "phase");
        }
        break;
      case "agent_skip":
        step(e.name, e.reason, "info");
        break;
      case "agent_step":
        say(`Sto lavorando · passo ${e.step}…`);
        break;
      case "llm_call":
        state.tokens += (e.prompt_tokens || 0) + (e.completion_tokens || 0);
        break;
      case "agent_text": {
        const row = step("", "", "info");
        row.querySelector(".args").style.whiteSpace = "normal";
        row.querySelector(".args").textContent = e.text;
        break;
      }
      case "tool_call": {
        if (["edit_file", "write_file", "todo_write"].includes(e.tool)) {
          if (e.tool !== "todo_write") say(`Modifico ${(e.args || {}).path || "un file"}…`);
          break;
        }
        turn && turn.tools++;
        const label = TOOL[e.tool] || e.tool;
        const row = step(label, shortArgs(e.args));
        if (e.tool === "read_file" && e.args && e.args.path) {
          row.querySelector(".args").style.cursor = "pointer";
          row.querySelector(".args").onclick = () => post({ type: "open", path: e.args.path });
        }
        say((DOING[e.tool] || (() => `${label}…`))(e.args || {}));
        break;
      }
      case "tool_result":
        if (e.tool === "todo_write" || (["edit_file", "write_file"].includes(e.tool) && e.ok)) break;
        step("", ((e.preview || "").trim().split("\n")[0] || "ok").slice(0, 160), "result" + (e.ok ? "" : " bad"));
        break;
      case "diff": {
        if (turn) turn.files.add(e.path);
        const card = el("div", "card");
        card.innerHTML = `<div class="card-head"><span>${e.action === "Create" ? "🆕" : "✏️"}</span>` +
          `<span class="path" title="${escape(e.path)}">${escape(basename(e.path))}</span><span class="spacer"></span>` +
          `<span class="stat-add">+${e.added}</span><span class="stat-del">−${e.removed}</span>` +
          `<button class="link" data-open>Apri</button></div>` +
          (turn && turn.approved && turn.approved.has(e.path) ? "" : miniDiff(e.diff, 12));
        card.querySelector("[data-open]").onclick = () => post({ type: "open", path: e.path });
        append(card);
        if (turn && turn.approved) turn.approved.delete(e.path);
        break;
      }
      case "todo": {
        if (!turn) break;
        if (!turn.todo) turn.todo = append(el("div", "card todo"));
        const done = e.todos.filter((t) => t.status === "completed").length;
        turn.todo.innerHTML = `<div class="card-head">☑ Cose da fare <span class="spacer"></span>` +
          `<span class="progress-label">${done}/${e.todos.length}</span></div><div class="card-body">` +
          e.todos.map((t) => `<div class="todo-item ${t.status}"><span class="tick">${t.status === "completed" ? "✓" :
            t.status === "in_progress" ? "◼" : "☐"}</span><span>${escape(t.content)}</span></div>`).join("") + "</div>";
        break;
      }
      case "tests":
        step("", e.ok ? "✓ test passati" : "✗ test falliti", "result" + (e.ok ? "" : " bad"));
        break;
      case "info":
        if (/^fase /.test(e.text)) { step("✻", e.text, "phase keep"); say(e.text); } else step("", e.text, "info");
        break;
      case "cancelled":
        break;
      case "done":
        if (turn) turn.summaryText = e.summary;
        break;
    }
  }

  function endTurn(m) {
    state.busy = false;
    const seconds = state.started ? (Date.now() - state.started) / 1000 : 0;
    state.started = 0;
    updateState();
    if (!turn) return;
    if (m.answer && m.answer !== turn.answer) turn.answer = m.answer;
    if (turn.answer) renderAnswer(true);
    if (turn.steps && turn.count > 3) { // a fine turno i passi si chiudono in una riga
      turn.steps.classList.add("collapsed");
      turn.summary.classList.remove("hidden");
      const files = turn.files.size ? ` · ${turn.files.size} ${turn.files.size === 1 ? "file modificato" : "file modificati"}` : "";
      const tools = turn.tools === 1 ? "1 strumento" : `${turn.tools} strumenti`;
      turn.summary.textContent = `▸ ${turn.count} passi${turn.tools ? ` · ${tools}` : ""}${files}`;
    }
    if (m.error) {
      errorCard(m.error.message, m.error.hint, [{ id: "retry-last", label: "Riprova" }]);
      say(`Ops: ${m.error.message}`, "error", 8);
    } else if (m.cancelled) {
      append(el("div", "notice", "■ Interrotto"));
      say("Mi sono fermata. Dimmi come proseguire.", "think", 6);
    } else {
      say(`Fatto in ${duration(seconds)}! Cosa facciamo adesso?`, "done", 6);
    }
    const footer = el("div", "footer-line");
    footer.innerHTML = `✻ ${escape(turn.summaryText || "")}${turn.summaryText ? " · " : ""}${duration(seconds)}`;
    append(footer);
    for (const [request, card] of approvals) closeApproval(request, "no", "", card);
    turn = null;
  }

  // ------------------------------------------------------------- conferme
  function approvalCard(a) {
    const card = el("div", "card approval" + (a.dangerous ? " danger" : ""));
    let head; let body = ""; let yes = "Sì"; let always = "Sempre";
    if (a.tool === "edit_file" || a.tool === "write_file") {
      const created = a.before === null && a.tool === "write_file";
      head = `<span>${created ? "🆕" : "✏️"}</span><span class="path" title="${escape(a.path || "")}">${escape(basename(a.path))}</span>` +
        (created ? `<span class="tag">nuovo</span>` : "");
      const [added, removed] = diffStats(a.diff);
      head += `<span class="spacer"></span><span class="stat-add">+${added}</span><span class="stat-del">−${removed}</span>`;
      body = miniDiff(a.diff, 40);
      yes = "Applica"; always = "Sempre per questo file";
    } else if (a.tool === "bash" || a.tool === "run_tests") {
      head = a.tool === "run_tests" ? "🧪 Vio vuole lanciare i test" : "▶ Vio vuole eseguire un comando";
      body = `<div class="card-body"><div class="command">$ ${escape(a.command || "")}</div>` +
        (a.dangerous ? `<p class="warning">⚠ Comando potenzialmente distruttivo: controlla bene.</p>` : "") + "</div>";
      yes = "Esegui";
      const first = String(a.command || "").trim().split(/\s+/)[0] || "";
      always = a.tool === "run_tests" ? "Sempre i test" : `Sempre «${first}…»`;
    } else if (a.tool === "web_fetch") {
      head = `🌐 Vio vuole leggere le pagine di ${escape(a.host || "questo sito")}`;
      always = "Sempre per questo sito";
    } else if (a.tool === "mcp") {
      head = `🧩 Vio vuole usare lo strumento MCP ${escape(`${a.server}.${a.mcp_tool}`)}`;
    } else {
      head = `Vio chiede il permesso: ${escape(a.summary || a.tool)}`;
    }
    card.innerHTML = `<div class="card-head">${head}</div>${body}<div class="buttons">` +
      `<button class="btn primary" data-a="yes">${yes}</button>` +
      (a.dangerous ? "" : `<button class="btn" data-a="always">${escape(always)}</button>`) +
      `<button class="btn danger" data-a="no">No</button>` +
      (a.after !== null && a.after !== undefined ? `<span class="spacer"></span><button class="link" data-review>Rivedi nell'editor</button>` : "") +
      `</div>`;
    card.querySelectorAll("[data-a]").forEach((button) => {
      button.onclick = () => {
        if (button.dataset.a === "no") return askWhy(a.request, card);
        answer(a.request, button.dataset.a, "");
      };
    });
    const review = card.querySelector("[data-review]");
    if (review) review.onclick = () => post({ type: "review", request: a.request });
    approvals.set(a.request, card);
    append(card, true);
    say("Mi serve il tuo ok qui sotto 👇", "ask");
    card.querySelector(".btn.primary").focus({ preventScroll: true });
  }
  function askWhy(request, card) {
    if (card.querySelector(".feedback")) return;
    const box = el("div", "feedback");
    box.innerHTML = `<input placeholder="Cosa devo fare invece? (facoltativo, Invio per mandare)"><button class="btn">Rifiuta</button>`;
    const field = box.querySelector("input");
    const go = () => answer(request, "no", field.value.trim());
    box.querySelector("button").onclick = go;
    field.onkeydown = (ev) => { if (ev.key === "Enter") go(); if (ev.key === "Escape") go(); };
    card.appendChild(box);
    field.focus();
  }
  function answer(request, choice, feedback) {
    post({ type: "approve", request, answer: choice, feedback });
    closeApproval(request, choice, feedback);
  }
  function closeApproval(request, choice, feedback, cardEl) {
    const card = cardEl || approvals.get(request);
    approvals.delete(request);
    if (!card || card.classList.contains("answered")) return;
    card.classList.add("answered");
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    card.querySelector(".feedback")?.remove();
    const note = { yes: "✓ Confermato", always: "✓ Confermato (non te lo chiedo più)", no: "✗ Rifiutato" }[choice] || "";
    card.appendChild(el("div", "answer-note", escape(note + (feedback ? ` · «${feedback}»` : ""))));
    if (choice !== "no" && turn) {
      const path = card.querySelector(".path");
      if (path) (turn.approved = turn.approved || new Set()).add(path.textContent);
    }
    if (!approvals.size && state.busy) say("Grazie! Continuo…", "think");
  }

  // ------------------------------------------------------------ altre card
  function errorCard(message, hint, actions) {
    const card = el("div", "card error");
    card.innerHTML = `<div class="card-head">⚠ ${escape(message)}</div>` +
      (hint ? `<div class="card-body"><p class="tip">${escape(hint)}</p></div>` : "");
    addActions(card, actions);
    append(card, true);
  }
  function addActions(card, actions) {
    if (!actions || !actions.length) return;
    const row = el("div", "buttons");
    for (const a of actions) {
      const button = el("button", "btn" + (a.primary ? " primary" : ""));
      button.textContent = a.label;
      button.onclick = () => post({ type: "action", id: a.id });
      row.appendChild(button);
    }
    card.appendChild(row);
  }
  function setup(m) {
    if (setupEl) setupEl.remove();
    setupEl = null;
    if (m.kind === "none") return;
    const card = el("div", "card setup" + (m.kind === "error" ? " error" : ""));
    card.innerHTML = `<div class="card-head">${escape(m.title)}</div><div class="card-body">` +
      (m.text || "").split("\n").map((p) => `<p>${Markdown.inline(p)}</p>`).join("") +
      `<div class="pulls"></div></div>`;
    addActions(card, m.actions);
    setupEl = card;
    log.querySelector(".welcome") ? log.insertBefore(card, log.querySelector(".welcome").nextSibling) : log.appendChild(card);
    scroll(true);
  }
  function pullProgress(m) {
    if (!setupEl) return;
    const box = setupEl.querySelector(".pulls");
    let row = box.querySelector(`[data-model="${CSS.escape(m.model)}"]`);
    if (!row) {
      row = el("div", "", `<div class="progress-label"></div><div class="progress"><div></div></div>`);
      row.dataset.model = m.model;
      box.appendChild(row);
    }
    const pct = m.total ? Math.min(100, (100 * m.completed) / m.total) : 0;
    row.querySelector(".progress > div").style.width = `${m.done ? 100 : pct}%`;
    const words = { "pulling manifest": "preparo…", "verifying sha256 digest": "controllo…", "writing manifest": "quasi fatto…" };
    const status = m.done ? "fatto ✓" : m.total ? `${Math.floor(pct)}% · ${(m.completed / 1e9).toFixed(2)} / ${(m.total / 1e9).toFixed(2)} GB`
      : words[m.status] || m.status;
    row.querySelector(".progress-label").textContent = `${m.model} · ${status}`;
  }

  function statsCard(m) {
    const { session: s, total: t, history: h } = m;
    const card = el("div", "card stats");
    const lines = (x) => (x.added || x.removed ? `<span class="stat-add">+${num(x.added)}</span> <span class="stat-del">−${num(x.removed)}</span>` : "—");
    const tests = (x) => (x.tests_ok || x.tests_failed ? `${x.tests_ok ? `<span class="stat-add">${x.tests_ok} ✓</span>` : ""} ${x.tests_failed ? `<span class="stat-del">${x.tests_failed} ✗</span>` : ""}` : "—");
    const rows = [
      ["richieste", num(s.turns), num(t.turns)], ["token", num(s.tokens), num(t.tokens)],
      ["lavoro dell'agente", duration(s.seconds), duration(t.seconds)], ["strumenti usati", num(s.tools), num(t.tools)],
      ["file modificati", num(s.files), num(t.files)], ["righe", lines(s), lines(t)], ["test", tests(s), tests(t)],
    ];
    let html = `<div class="card-head">📊 Statistiche di MyDevAgent</div><div class="card-body">` +
      `<table class="numbers"><tr><th></th><th>questa sessione</th><th>${escape(m.label || "da sempre")}</th></tr>` +
      rows.map(([k, a, b]) => `<tr><td>${k}</td><td>${a}</td><td class="all">${b}</td></tr>`).join("") + "</table>";
    html += `<div class="heat">${heatmap(h.per_day || {}, 20)}</div>`;
    const facts = [];
    if (h.streak) facts.push(`🔥 <b>${h.streak} ${h.streak === 1 ? "giorno" : "giorni di fila"}</b> <span class="dim">${h.longest > h.streak ? `(record ${h.longest})` : "(il tuo record!)"}</span>`);
    const days = Object.keys(h.per_day || {}).length;
    facts.push(`${days} ${days === 1 ? "giorno attivo" : "giorni attivi"}`);
    if (h.sessions) facts.push(`${h.sessions} ${h.sessions === 1 ? "sessione" : "sessioni"}`);
    html += `<div class="facts">${facts.join(" · ")}</div>`;
    const top = (o) => Object.entries(o || {}).sort((a, b) => b[1] - a[1])[0];
    const extra = [];
    if (top(h.models)) extra.push(`modello preferito: ${escape(top(h.models)[0])}`);
    if (top(h.teams)) extra.push(`team preferito: ${escape(top(h.teams)[0])}`);
    if (extra.length) html += `<div class="facts dim">${extra.join(" · ")}</div>`;
    const projects = Object.entries(h.projects || {}).sort((a, b) => b[1] - a[1]).slice(0, 3)
      .map(([p, n]) => `${escape(p.replace(/\\/g, "/").replace(/\/$/, "").split("/").pop())} ${Math.floor((100 * n) / Math.max(1, h.turns))}%`);
    if (projects.length) html += `<div class="facts dim">progetti: ${projects.join(" · ")}</div>`;
    card.innerHTML = html + "</div>";
    append(card, true);
    say(h.streak > 1 ? `${h.streak} giorni di fila insieme! Continuiamo così.` : "Ecco cosa abbiamo fatto insieme!", "love", 5);
  }
  function contextCard(u) {
    const card = el("div", "card usage");
    const parts = [["instr", "Istruzioni e memoria", u.instructions], ["summary", "Riassunto", u.summary],
      ["msgs", `Messaggi (${u.count})`, u.messages]];
    const bar = parts.map(([cls, , n]) => (n ? `<span class="${cls}" style="width:${Math.max(1, (100 * n) / u.window)}%"></span>` : "")).join("");
    const rows = [...parts, ["free", "Libero", u.free]]
      .map(([cls, label, n]) => `<tr><td><span class="sq ${cls}"></span>${escape(label)}</td><td>${num(n)} token</td></tr>`).join("");
    card.innerHTML = `<div class="card-head">🧠 Contesto <span class="spacer"></span><b>${u.percent}%</b>&nbsp;di ${num(u.window)} token</div>` +
      `<div class="card-body"><div class="usage-bar">${bar}</div><table class="numbers">${rows}</table>` +
      `<p class="tip">Stima: circa 4 caratteri per token. Mi riassumo da sola all'${u.auto_compact}%, o quando scrivi /compact.</p></div>`;
    append(card, true);
  }
  function compactedCard(r) {
    if (!r.compacted) return append(el("div", "notice", "Conversazione ancora corta: niente da riassumere."), true);
    const card = el("div", "card usage");
    card.innerHTML = `<div class="card-head">✓ Conversazione compattata <span class="spacer"></span><span class="dim">liberati circa ${num(r.freed)} token</span></div>` +
      `<div class="card-body">${markdown(r.summary || "")}<p class="tip">${r.turns} turni riassunti · /context mostra l'uso</p></div>`;
    append(card, true);
  }
  function updateCard(m) {
    const card = el("div", "card setup");
    card.innerHTML = `<div class="card-head">✨ ${m.count} novità di MyDevAgent</div>` +
      `<div class="card-body"><p>C'è una versione più nuova su GitHub. Aggiorno? I tuoi modelli e le impostazioni restano.</p></div>`;
    addActions(card, m.actions);
    card.querySelector("button")?.addEventListener("click", () => card.remove());
    append(card, true);
    say("Ci sono delle novità per me!", "love", 5);
  }
  function heatmap(perDay, weeks) {
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const monday = new Date(today); monday.setDate(today.getDate() - ((today.getDay() + 6) % 7) - 7 * (weeks - 1));
    const key = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const peak = Math.max(0, ...Object.values(perDay));
    const cell = 11; const left = 24; const top = 14;
    let svg = "";
    let lastLabel = -3;
    for (let w = 0; w < weeks; w++) {
      for (let d = 0; d < 7; d++) {
        const day = new Date(monday); day.setDate(monday.getDate() + 7 * w + d);
        if (day > today) break;
        if (day.getDate() === 1 || (w === 0 && d === 0)) {
          if (w - lastLabel >= 3) {
            svg += `<text x="${left + w * cell}" y="10" font-size="9" fill="currentColor" opacity="0.6">${MONTHS[day.getMonth()]}</text>`;
            lastLabel = w;
          }
        }
        const n = perDay[key(day)] || 0;
        const level = n && peak ? Math.min(4, Math.ceil((4 * n) / peak)) : 0;
        const fill = level ? LEVELS[level - 1] : "rgba(127,127,127,0.18)";
        svg += `<rect x="${left + w * cell}" y="${top + d * cell}" width="9" height="9" rx="2" fill="${fill}">` +
          `<title>${day.getDate()} ${MONTHS[day.getMonth()]}: ${n} ${n === 1 ? "richiesta" : "richieste"}</title></rect>`;
      }
    }
    ["lun", "", "mer", "", "ven", "", ""].forEach((label, d) => {
      if (label) svg += `<text x="0" y="${top + d * cell + 8}" font-size="9" fill="currentColor" opacity="0.6">${label}</text>`;
    });
    const width = left + weeks * cell;
    const legendY = top + 7 * cell + 6;
    svg += `<text x="${left}" y="${legendY + 8}" font-size="9" fill="currentColor" opacity="0.6">meno</text>`;
    LEVELS.forEach((c, i) => { svg += `<rect x="${left + 28 + i * cell}" y="${legendY}" width="9" height="9" rx="2" fill="${c}"/>`; });
    svg += `<text x="${left + 28 + 4 * cell + 2}" y="${legendY + 8}" font-size="9" fill="currentColor" opacity="0.6">più</text>`;
    return `<svg width="${width}" height="${legendY + 12}" viewBox="0 0 ${width} ${legendY + 12}">${svg}</svg>`;
  }

  // ---------------------------------------------------------- stato e barra
  function updateState() {
    const dot = $("#dot");
    dot.className = "dot " + (!state.connected ? "off" : state.busy ? "busy" : "ready");
    $("#model").textContent = state.connected ? state.model + (state.ctx != null ? ` · ctx ${state.ctx}%` : "") : "non collegata";
    $("#model").classList.toggle("full", state.ctx != null && state.ctx >= 70);
    send.innerHTML = state.busy ? ICONS.stop : ICONS.send;
    send.classList.toggle("stop", state.busy);
    send.title = state.busy ? "Ferma (Esc)" : "Invia (Invio)";
    $("#permission").value = state.permission;
    $("#team").value = state.team;
    input.placeholder = state.learn ? "Modalità impara: qualche pezzo di codice lo scrivi tu…" : "Chiedi a Vio… (/ comandi, @ file)";
    if (!state.busy) $("#hint").textContent = "Invio manda · Maiusc+Invio va a capo";
  }
  function renderContext() {
    const box = $("#context");
    box.innerHTML = "";
    const c = state.context;
    if (!c || !c.file) return;
    const chip = el("div", "ctx" + (state.contextOff ? " off" : ""));
    const where = c.selection ? ` · righe ${c.selection.start}–${c.selection.end}` : "";
    chip.innerHTML = `<span title="Vio vede questo file (e la selezione)">📄 ${escape(c.file.split("/").pop())}${where}</span>` +
      `<button title="${state.contextOff ? "Includi" : "Escludi"}">${state.contextOff ? "+" : "×"}</button>`;
    chip.querySelector("button").onclick = () => { state.contextOff = !state.contextOff; renderContext(); };
    box.appendChild(chip);
  }

  // -------------------------------------------------------------- tastiera
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
  }
  function submit() {
    const text = input.value.trim();
    if (!text || state.busy) return;
    const [cmd, ...rest] = text.split(/\s+/);
    const arg = rest.join(" ");
    const local = {
      "/undo": () => post({ type: "command", name: "undo" }),
      "/diff": () => post({ type: "command", name: "diff" }),
      "/clear": () => post({ type: "command", name: "clear" }),
      "/context": () => post({ type: "command", name: "context" }),
      "/compact": () => post({ type: "command", name: "compact" }),
      "/update": () => post({ type: "command", name: "update" }),
      "/stats": () => post({ type: "command", name: "stats", days: /^\d+$/.test(arg) ? Number(arg) : null }),
      "/impara": () => setOption({ learn: arg ? !/^(off|no)$/i.test(arg) : !state.learn }),
      "/plan": () => setOption({ permission: state.permission === "plan" ? "ask" : "plan" }),
    };
    const lower = cmd.toLowerCase();
    if (local[lower] && (lower === "/stats" || lower === "/impara" || !arg)) {
      local[lower]();
    } else if (["/fast", "/balanced", "/deep", "/ultra-deep", "/auto"].includes(lower) && !arg) {
      setOption({ team: lower.slice(1) });
    } else if (lower.startsWith("/") && !COMMANDS.some(([c]) => c === lower) && lower !== "/skill" &&
               !state.commands.some((c) => c.name.split(" ")[0] === lower)) {
      append(el("div", "notice", `Comando sconosciuto: ${escape(cmd)} · scrivi / per vedere quelli che conosco`));
      return;
    } else {
      post({ type: "send", text, context: !state.contextOff });
    }
    input.value = "";
    autosize();
    closeMenu();
  }
  function setOption(change) {
    Object.assign(state, change);
    updateState();
    post({ type: "set", ...change });
    if (change.permission) say(window.Vio.SAYS[change.permission], change.permission);
    if (change.team) say(`Team ${change.team}: ${{ auto: "scelgo io il team giusto per ogni richiesta.", fast: "vado veloce, un solo agente.", balanced: "piano, modifiche, test e review.", deep: "sicurezza, performance e casi limite.", "ultra-deep": "35 agenti, per i lavori importanti." }[change.team]}`, "done", 4);
    if ("learn" in change) say(change.learn ? "Impariamo insieme! Qualche pezzo lo scrivi tu." : "Ok, torno a scrivere io tutto il codice.", change.learn ? "love" : "done", 4);
  }
  send.onclick = () => (state.busy ? post({ type: "stop" }) : submit());
  input.addEventListener("input", () => { autosize(); updateMenu(); });
  input.addEventListener("keydown", (ev) => {
    if (!menu.classList.contains("hidden")) {
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        menuIndex = (menuIndex + (ev.key === "ArrowDown" ? 1 : -1) + menuItems.length) % menuItems.length;
        drawMenu();
        ev.preventDefault();
        return;
      }
      const item = menuItems[menuIndex];
      const w = currentWord();
      if (ev.key === "Enter" && item && w && w.word === item.name.trim()) closeMenu(); // già scritto: si invia
      else if (ev.key === "Enter" || ev.key === "Tab") { pick(item); ev.preventDefault(); return; }
      if (ev.key === "Escape") { closeMenu(); ev.preventDefault(); return; }
    }
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); submit(); }
    if (ev.key === "Escape" && state.busy) post({ type: "stop" });
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && state.busy && document.activeElement !== input) post({ type: "stop" });
  });
  $("#permission").onchange = (ev) => setOption({ permission: ev.target.value });
  $("#team").onchange = (ev) => setOption({ team: ev.target.value });
  document.querySelectorAll("[data-cmd]").forEach((b) => { b.onclick = () => post({ type: "command", name: b.dataset.cmd }); });
  for (const [value, label] of Object.entries(PERMISSIONS)) $("#permission").add(new Option(label, value));
  for (const [value, label] of Object.entries(TEAMS)) $("#team").add(new Option(label, value));

  // autocompletamento: / comandi, @ file
  function currentWord() {
    const upto = input.value.slice(0, input.selectionStart);
    const match = upto.match(/(^|\s)([/@][^\s]*)$/);
    return match ? { word: match[2], start: upto.length - match[2].length } : null;
  }
  function updateMenu() {
    const w = currentWord();
    if (!w) return closeMenu();
    const query = w.word.slice(1).toLowerCase();
    if (w.word[0] === "/" && w.start === 0) {
      const custom = state.commands.map((c) => [c.name, c.description]);
      menuItems = COMMANDS.concat(custom).filter(([name]) => name.slice(1).toLowerCase().startsWith(query))
        .slice(0, 30).map(([name, desc]) => ({ name, desc, insert: name + " " }));
    } else if (w.word[0] === "@") {
      const scored = state.files.filter((f) => f.toLowerCase().includes(query))
        .sort((a, b) => (a.toLowerCase().split("/").pop().startsWith(query) ? -1 : 0) -
                        (b.toLowerCase().split("/").pop().startsWith(query) ? -1 : 0) || a.length - b.length);
      menuItems = scored.slice(0, 12).map((f) => ({ name: "@" + f, desc: "", insert: "@" + f + " " }));
    } else {
      menuItems = [];
    }
    if (!menuItems.length) return closeMenu();
    menuIndex = 0;
    drawMenu();
  }
  function drawMenu() {
    menu.innerHTML = "";
    menuItems.forEach((item, i) => {
      const row = el("div", i === menuIndex ? "on" : "", `<span class="name">${escape(item.name)}</span><span class="desc">${escape(item.desc)}</span>`);
      row.onmousedown = (ev) => { ev.preventDefault(); pick(item); };
      menu.appendChild(row);
    });
    menu.classList.remove("hidden");
    menu.children[menuIndex]?.scrollIntoView({ block: "nearest" });
  }
  function closeMenu() { menu.classList.add("hidden"); menuItems = []; }
  function pick(item) {
    const w = currentWord();
    if (!w || !item) return closeMenu();
    input.value = input.value.slice(0, w.start) + item.insert + input.value.slice(input.selectionStart);
    input.selectionStart = input.selectionEnd = w.start + item.insert.length;
    closeMenu();
    autosize();
    input.focus();
  }

  // click nel log: copia, inserisci, link
  log.addEventListener("click", (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    const code = target.closest(".code");
    if (target.hasAttribute("data-copy") && code) { post({ type: "copy", text: code.querySelector("code").textContent }); target.textContent = "Copiato ✓"; }
    if (target.hasAttribute("data-insert") && code) post({ type: "insert", text: code.querySelector("code").textContent });
    const link = target.closest("a[data-href]");
    if (link) { ev.preventDefault(); post({ type: "link", href: link.dataset.href }); }
  });

  // ------------------------------------------------------ dal lato estensione
  const handlers = {
    reset() { log.innerHTML = ""; turn = null; setupEl = null; approvals.clear(); welcome(); },
    state(m) {
      Object.assign(state, m.state);
      updateState();
      if (!vio.until && !state.busy) { vio.expression = idleExpression(); vio.text = ""; }
    },
    history(m) {
      log.querySelector(".welcome")?.remove();
      for (const msg of m.messages) {
        if (msg.role === "user") startTurn(msg.content);
        else if (turn) { turn.answer = msg.content; renderAnswer(true); turn = null; }
      }
      turn = null;
      if (!m.messages.length) welcome();
      else append(el("div", "notice", "↑ conversazione ripresa"), true);
      scroll(true);
    },
    user(m) {
      startTurn(m.text);
      state.busy = true; state.started = Date.now(); state.tokens = 0;
      updateState();
      say("Ci penso…", "think");
    },
    event(m) { onEvent(m.event); },
    chunk(m) { if (turn) { turn.answer += m.text; renderAnswer(false); } },
    approval(m) { approvalCard(m.approval); },
    approvalClosed(m) { closeApproval(m.request, m.answer, m.feedback || ""); },
    turnEnd(m) { endTurn(m); },
    notice(m) { append(el("div", "notice", escape(m.text)), true); },
    error(m) { errorCard(m.message, m.hint, m.actions); if (m.vio !== false) say(`Ops: ${m.message}`, "error", 8); },
    setup(m) { setup(m); if (m.say) say(m.say, m.kind === "error" ? "error" : "think"); },
    pull(m) { pullProgress(m); },
    stats(m) { statsCard(m); },
    contextUsage(m) { contextCard(m); },
    compacted(m) { compactedCard(m); },
    update(m) { updateCard(m); },
    context(m) { const changed = JSON.stringify(m.context) !== JSON.stringify(state.context); state.context = m.context; if (changed) state.contextOff = false; renderContext(); },
    files(m) { state.files = m.files; },
    fill(m) { input.value = m.text; autosize(); input.focus(); input.selectionStart = input.selectionEnd = input.value.length; },
    focus() { input.focus(); },
    say(m) { say(m.text, m.expression, m.seconds); },
  };
  window.addEventListener("message", (ev) => {
    const handler = handlers[ev.data && ev.data.type];
    if (handler) handler(ev.data);
  });

  welcome();
  updateState();
  drawVio();
  post({ type: "ready" });
})();
