"""Client LLM unico compatibile OpenAI (Ollama, LM Studio, vLLM, llama.cpp, qualsiasi /v1)."""

from __future__ import annotations

import json
import re
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Settings

Message = dict[str, Any]


@dataclass
class Completion:
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: int = 0
    tool_calls: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)  # tool call nativi: {id, name, arguments}


class LLM(Protocol):
    def complete(
        self,
        messages: list[Message],
        *,
        tier: str = "main",
        max_tokens: int = 1024,
        temperature: float = 0.2,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion: ...

    def stream(
        self,
        messages: list[Message],
        *,
        tier: str = "main",
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> Iterator[str]: ...

    def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]],
        executor: Callable[[str, dict[str, Any]], str],
        tier: str = "main",
        max_tokens: int = 1024,
        temperature: float = 0.2,
        max_steps: int = 4,
    ) -> Completion: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def model_name(self, tier: str) -> str: ...


class OpenAICompatLLM:
    """Un client per backend; il modello viene scelto in base al tier del profilo attivo."""

    def __init__(self, settings: Settings, timeout: float = 600.0) -> None:
        from openai import OpenAI  # import lazy: velocizza la CLI

        self._openai_cls = OpenAI
        self.settings = settings
        self.timeout = timeout
        self._clients: dict[str, Any] = {}

    def _client_for(self, tier: str) -> tuple[Any, str]:
        model, backend = self.settings.resolve_model(tier)
        client = self._clients.get(backend.base_url)
        if client is None:
            client = self._openai_cls(
                base_url=backend.base_url, api_key=backend.api_key or "none", timeout=self.timeout, max_retries=2
            )
            self._clients[backend.base_url] = client
        return client, model

    def model_name(self, tier: str) -> str:
        return self.settings.resolve_model(tier)[0]

    def _ollama(self, tier: str) -> tuple[str, str] | None:
        """(modello, host) se il tier gira su Ollama: lì si usa l'API nativa, l'unica che accetta num_ctx."""
        from .health import is_ollama, ollama_host

        model, backend = self.settings.resolve_model(tier)
        return (model, ollama_host(backend.base_url)) if is_ollama(backend.base_url) else None

    def _ollama_body(self, model: str, messages, max_tokens: int, temperature: float, stream: bool) -> dict[str, Any]:
        # senza num_ctx Ollama usa il suo contesto di default (2-4k token) e, se il prompt non ci sta, butta via
        # in silenzio i messaggi più vecchi: la richiesta dell'utente spariva e l'agente rispondeva «nessuna modifica»
        return {"model": model, "messages": [_to_ollama(m) for m in messages], "stream": stream,
                "options": {"num_ctx": self.settings.active_profile.num_ctx, "num_predict": max_tokens,
                            "temperature": temperature}}

    def complete(self, messages, *, tier="main", max_tokens=1024, temperature=0.2, tools=None) -> Completion:
        ollama = self._ollama(tier)
        if ollama:
            return self._ollama_complete(*ollama, messages, max_tokens, temperature, tools)
        client, model = self._client_for(tier)
        start = time.perf_counter()
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens,
                                  "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
        resp = client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        msg = resp.choices[0].message
        calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments or "{}"}
                 for c in (getattr(msg, "tool_calls", None) or [])]
        return Completion(
            text=msg.content or "",
            model=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ms=int((time.perf_counter() - start) * 1000),
            calls=calls,
        )

    def _ollama_complete(self, model, host, messages, max_tokens, temperature, tools) -> Completion:
        import httpx

        start = time.perf_counter()
        body = self._ollama_body(model, messages, max_tokens, temperature, stream=False)
        if tools:
            body["tools"] = tools
        resp = httpx.post(f"{host}/api/chat", json=body, timeout=self.timeout)
        _ollama_check(resp)
        data = resp.json()
        msg = data.get("message") or {}
        calls = []
        for i, call in enumerate(msg.get("tool_calls") or []):
            fn = call.get("function") or {}
            args = fn.get("arguments") or {}
            calls.append({"id": call.get("id") or f"call_{i}", "name": fn.get("name", ""),
                          "arguments": args if isinstance(args, str) else json.dumps(args)})
        return Completion(text=msg.get("content") or "", model=model, prompt_tokens=data.get("prompt_eval_count") or 0,
                          completion_tokens=data.get("eval_count") or 0,
                          ms=int((time.perf_counter() - start) * 1000), calls=calls)

    def stream(self, messages, *, tier="main", max_tokens=1024, temperature=0.2) -> Iterator[str]:
        ollama = self._ollama(tier)
        if ollama:
            import httpx

            model, host = ollama
            body = self._ollama_body(model, messages, max_tokens, temperature, stream=True)
            with httpx.stream("POST", f"{host}/api/chat", json=body, timeout=self.timeout) as resp:
                if resp.status_code >= 400:
                    resp.read()
                _ollama_check(resp)
                for line in resp.iter_lines():
                    if line.strip():
                        text = (json.loads(line).get("message") or {}).get("content")
                        if text:
                            yield text
            return
        client, model = self._client_for(tier)
        stream = client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens, temperature=temperature, stream=True
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # alcuni server (vLLM, llama.cpp) espongono il thinking in reasoning_content: lo ignoriamo
            if delta and delta.content:
                yield delta.content

    def complete_with_tools(
        self, messages, *, tools, executor, tier="main", max_tokens=1024, temperature=0.2, max_steps=4
    ) -> Completion:
        """Loop di function-calling nativo (ReAct) con al massimo `max_steps` round di tool."""
        convo = list(messages)
        total = Completion(text="", model=self.model_name(tier))
        start = time.perf_counter()
        for step in range(max_steps + 1):
            comp = self.complete(convo, tier=tier, max_tokens=max_tokens, temperature=temperature,
                                 tools=tools if step < max_steps else None)
            total.prompt_tokens += comp.prompt_tokens
            total.completion_tokens += comp.completion_tokens
            if not comp.calls:
                total.text = comp.text
                break
            convo.append({"role": "assistant", "content": comp.text, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in comp.calls]})
            for call in comp.calls:
                total.tool_calls += 1
                try:
                    args = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                convo.append({"role": "tool", "tool_call_id": call["id"],
                              "content": executor(call["name"], args)})
        total.ms = int((time.perf_counter() - start) * 1000)
        return total

    def embed(self, texts: list[str]) -> list[list[float]]:
        client, model = self._client_for("embed")
        resp = client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in resp.data]


def _ollama_check(resp) -> None:
    """Gli errori di Ollama con il loro messaggio (es. «model "x" not found»), così explain_error li riconosce."""
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", "")
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"{resp.status_code} {detail}".strip())


def _to_ollama(message: Message) -> Message:
    """Un messaggio in formato OpenAI per /api/chat di Ollama: testo + immagini base64, argomenti come oggetto."""
    if isinstance(message.get("content"), list):
        parts = message["content"]
        text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
        images = [p["image_url"]["url"].split(",", 1)[-1] for p in parts
                  if p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:")]
        message = {**message, "content": text, **({"images": images} if images else {})}
    if not message.get("tool_calls"):
        return message
    calls = []
    for call in message["tool_calls"]:
        fn = call.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        calls.append({"function": {"name": fn.get("name", ""), "arguments": args}})
    return {**message, "tool_calls": calls}


# --------------------------------------------------------------------------- fake
@dataclass
class FakeLLM:
    """LLM deterministico per test e demo offline (`MYDEVAGENT_FAKE_LLM=1`).

    Riconosce il ruolo dell'agente dal system prompt e restituisce output plausibili nel formato atteso.
    """

    responses: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    gate_verdicts: list[str] = field(default_factory=list)

    def model_name(self, tier: str) -> str:
        return f"fake-{tier}"

    def _role(self, messages: list[Message]) -> str:
        system = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")
        match = re.search(r"# Role: ([^\n(]+)", system)
        return match.group(1).strip() if match else "unknown"

    def _answer(self, role: str, messages: list[Message]) -> str:
        for key, value in self.responses.items():
            if key.lower() in role.lower():
                return value
        lowered = role.lower()
        if "architect" in lowered:
            return ("**Goal**: implement the request.\n**Files**: app/main.py — logic\n"
                    "**Assignments**: language: implementation\n**Acceptance criteria**:\n- add(2, 3) == 5")
        if any(k in lowered for k in ("security", "performance", "edge cases", "reviewer")):
            verdict = self.gate_verdicts.pop(0) if self.gate_verdicts else "APPROVE"
            if verdict == "REVISE":
                return "VERDICT: REVISE\n- [BLOCKER] app/main.py: missing input validation → validate types"
            return "VERDICT: APPROVE\n- none"
        if "debugging" in lowered:
            return ("**Test plan**\n- check add\n```python run\nfrom app.main import add\n"
                    "assert add(2, 3) == 5\nprint('SELF-CHECK OK')\n```")
        if "research" in lowered:
            return "**Findings**:\n- fake fact — [1]\n**Sources**: [1] Example — https://example.com"
        if "formatter" in lowered:
            return "**Summary**: done.\n```python file=app/main.py\ndef add(a: int, b: int) -> int:\n    return a + b\n```"
        if "documentation" in lowered:
            return "```markdown file=README.md\n# App\nUsage: `python -m app.main`\n```"
        return "**Approach**\n- simple\n```python file=app/main.py\ndef add(a: int, b: int) -> int:\n    return a + b\n```"

    def complete(self, messages, *, tier="main", max_tokens=1024, temperature=0.2, tools=None) -> Completion:
        role = self._role(messages)
        self.calls.append({"role": role, "tier": tier, "messages": messages})
        text = self._answer(role, messages)
        prompt_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4
        return Completion(text=text, model=self.model_name(tier), prompt_tokens=prompt_tokens,
                          completion_tokens=len(text) // 4, ms=1)

    def stream(self, messages, *, tier="main", max_tokens=1024, temperature=0.2) -> Iterator[str]:
        text = self.complete(messages, tier=tier, max_tokens=max_tokens, temperature=temperature).text
        for i in range(0, len(text), 16):
            yield text[i : i + 16]

    def complete_with_tools(self, messages, *, tools, executor, tier="main", max_tokens=1024,
                            temperature=0.2, max_steps=4) -> Completion:
        return self.complete(messages, tier=tier, max_tokens=max_tokens, temperature=temperature)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * 256
            for token in re.findall(r"\w+", text.lower()):
                vec[zlib.crc32(token.encode()) % 256] += 1.0
            vectors.append(vec)
        return vectors


def build_llm(settings: Settings) -> LLM:
    import os

    if os.environ.get("MYDEVAGENT_FAKE_LLM") == "1":
        return FakeLLM()
    return OpenAICompatLLM(settings)
