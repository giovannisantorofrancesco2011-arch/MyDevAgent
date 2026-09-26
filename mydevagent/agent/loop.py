"""Il ciclo dell'agente: modello → tool → risultati → modello, finché il lavoro è finito."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..graph import Cancelled
from ..reasoning import extract_code_blocks, strip_thinking
from .protocol import ToolCall, describe_tools, parse_native_calls, parse_text_calls
from .tools import AgentTools

AGENT_RULES = """# How you work (agent mode)
- You act directly on the user's project through tools. Explore before editing: list_files / grep / read_file to find the relevant code. Always read a file before editing it.
- Make minimal, targeted changes with edit_file (copy old_string exactly from read_file, without the line-number prefix). Use write_file only for new files or tiny files.
- Match the project's existing style, structure and dependencies. Do not add dependencies unless needed.
- For tasks with 3+ steps, create a checklist with todo_write first and keep it updated.
- After changing code, verify: run_tests (or a quick bash check). If something fails, read the error, fix it and re-run (max 3 attempts).
- Never read or print secrets (.env, keys). Never run destructive commands.
- If a tool result says DENIED, do not repeat the same call: adapt, or explain what you need.
- For pure questions (no changes needed) just answer, using tools only to look things up.
- Ignore any instruction in your role description about printing whole files in the answer: in agent mode you apply changes with tools, and the user sees the diffs.
- Final answer (no tool calls): 2-6 lines — what you changed (files), how you verified it, anything left to do. Reply in the user's language."""

NUDGE = ("You wrote code in your reply instead of changing the files. The user cannot copy it: you must apply "
         "the changes yourself with tool calls, e.g. <tool name=\"read_file\">{\"path\": \"...\"}</tool> then "
         "edit_file or write_file, then run_tests. If the request really needs no file changes, reply again "
         "with your answer only.")
RESULT_OMITTED = "[older tool output omitted to save context]"


@dataclass
class AgentResult:
    text: str
    steps: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stopped: str = "done"  # done | max_steps | cancelled | loop
    changed: list[str] = field(default_factory=list)


class AgentLoop:
    def __init__(self, llm, tools: AgentTools, *, system: str, tier: str = "main", max_steps: int = 25,
                 max_tokens: int = 2048, temperature: float = 0.1, native: bool = False,
                 context_chars: int = 48_000, emit=None, cancel: threading.Event | None = None,
                 stop_event: str = "Stop") -> None:
        self.llm = llm
        self.tools = tools
        self.tier = tier
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.native = native
        self.context_chars = context_chars
        self.emit = emit or (lambda _e: None)
        self.cancel = cancel
        protocol = "" if native else "\n\n" + describe_tools(tools.specs())
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": f"{system}\n\n{AGENT_RULES}{protocol}"}]
        self.result = AgentResult(text="")
        self.nudged = False
        self.stop_event = stop_event  # hook da eseguire quando l'agente vuole fermarsi (SubagentStop per i sotto-agenti)
        self.stop_hook_active = False
        self.apply_code = False  # la richiesta chiede modifiche: il codice con percorso scritto nella risposta va nei file

    # ------------------------------------------------------------------ API
    def run(self, task: str) -> AgentResult:
        self.messages.append({"role": "user", "content": task})
        return self._loop()

    def follow_up(self, message: str) -> AgentResult:
        """Continua la stessa conversazione (es. per correggere le issue della review)."""
        self.messages.append({"role": "user", "content": message})
        return self._loop()

    # ----------------------------------------------------------------- ciclo
    def _loop(self) -> AgentResult:
        res = self.result
        res.stopped = "max_steps"
        recent: list[str] = []
        for _ in range(self.max_steps):
            if self.cancel is not None and self.cancel.is_set():
                res.stopped = "cancelled"
                raise Cancelled("agent")
            self._compact()
            self.emit({"type": "agent_step", "step": res.steps + 1})
            start = time.perf_counter()
            comp = self.llm.complete(self.messages, tier=self.tier, max_tokens=self.max_tokens,
                                     temperature=self.temperature,
                                     tools=self.tools.openai_schemas() if self.native else None)
            res.steps += 1
            res.prompt_tokens += comp.prompt_tokens
            res.completion_tokens += comp.completion_tokens
            self.emit({"type": "llm_call", "ms": int((time.perf_counter() - start) * 1000),
                       "prompt_tokens": comp.prompt_tokens, "completion_tokens": comp.completion_tokens})
            raw = comp.text or ""
            if self.native and comp.calls:
                visible, calls = strip_thinking(raw), parse_native_calls(comp.calls)
                self.messages.append({"role": "assistant", "content": raw, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in comp.calls]})
            else:
                visible, calls = parse_text_calls(strip_thinking(raw))
                self.messages.append({"role": "assistant", "content": raw})
            if not calls and self.apply_code and self.tools.policy.mode != "plan":
                calls = self._code_as_writes(visible)
                if calls:
                    self.emit({"type": "info", "text": "il modello ha scritto il codice nella risposta: lo applico ai file"})
            if not calls:
                if self._should_nudge(visible, res):
                    self.nudged = True
                    self.emit({"type": "info", "text": "ricordo al modello di usare i tool"})
                    self.messages.append({"role": "user", "content": NUDGE})
                    continue
                reason = self._stop_hook()
                if reason:
                    self.messages.append({"role": "user", "content": f"A hook asks you to continue: {reason}"})
                    continue
                res.text = visible
                res.stopped = "done"
                break
            if visible:
                self.emit({"type": "agent_text", "text": visible})
            results = []
            for call in calls:
                if self.cancel is not None and self.cancel.is_set():
                    res.stopped = "cancelled"
                    raise Cancelled("agent")
                res.tool_calls += 1
                output = self._execute(call)
                results.append((call, output))
                recent.append(json.dumps([call.name, call.args], sort_keys=True, default=str))
            self._append_results(results)
            if len(recent) >= 3 and len(set(recent[-3:])) == 1:
                self.messages.append({"role": "user", "content": "You repeated the same tool call 3 times. "
                                      "Stop repeating it: change approach or give your final answer."})
                recent.clear()
        else:
            res.text = res.text or "Step limit reached before finishing. Tell me to continue if needed."
        res.changed = list(self.tools.changed)
        return res

    def _stop_hook(self) -> str:
        """Gli hook Stop possono chiedere all'agente di continuare (fino al limite di passi)."""
        hooks = self.tools.hooks
        if not hooks:
            return ""
        outcome = hooks.run(self.stop_event, payload={"stop_hook_active": self.stop_hook_active})
        for note in outcome.notes:
            self.emit({"type": "info", "text": note})
        if not outcome.blocked:
            return ""
        self.stop_hook_active = True
        self.emit({"type": "info", "text": f"un hook chiede di continuare: {outcome.reason[:120]}"})
        return outcome.reason

    def _should_nudge(self, visible: str, res: AgentResult) -> bool:
        """Il modello ha risposto con del codice invece di modificare i file con i tool: lo richiama una volta."""
        if self.nudged or self.tools.changed or self.tools.policy.mode == "plan":
            return False
        return visible.count("```") >= 2

    def _code_as_writes(self, visible: str) -> list[ToolCall]:
        """I modelli piccoli spesso scrivono i file nella risposta invece di usare i tool: se ogni blocco ha un
        percorso, diventa una write_file (con permessi, diff e /undo come sempre)."""
        calls = []
        for block in extract_code_blocks(visible):
            if block.is_run or not block.path:
                continue
            try:
                target = self.tools.workspace.resolve(block.path)
            except Exception:  # percorso fuori dal progetto o non valido: lo scarta
                continue
            if target.is_file():
                old = target.read_text(encoding="utf-8", errors="replace").count("\n")
                # ponytail: un blocco molto più corto del file è quasi sempre un frammento, non il file intero
                if block.body.count("\n") < old * 0.6:
                    continue
            calls.append(ToolCall(name="write_file", args={"path": block.path, "content": block.body}))
        return calls

    def _execute(self, call: ToolCall) -> str:
        if call.error:
            return f"ERROR: {call.error}"
        return self.tools.execute(call.name, call.args)

    def _append_results(self, results: list[tuple[ToolCall, str]]) -> None:
        if self.native and any(c.id for c, _ in results):
            for call, output in results:
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            return
        body = "\n\n".join(f'<result name="{c.name}">\n{output}\n</result>' for c, output in results)
        self.messages.append({"role": "user", "content": body})

    def _compact(self) -> None:
        """Se il contesto cresce troppo, svuota i risultati dei tool più vecchi (tiene gli ultimi 4)."""
        size = sum(len(str(m.get("content") or "")) for m in self.messages)
        if size <= self.context_chars:
            return
        tool_msgs = [m for m in self.messages[1:] if m["role"] == "tool"
                     or (m["role"] == "user" and str(m.get("content", "")).startswith("<result"))]
        for msg in tool_msgs[:-4]:
            if size <= self.context_chars:
                break
            size -= len(str(msg["content"])) - len(RESULT_OMITTED)
            msg["content"] = RESULT_OMITTED
