import json

import httpx

from mydevagent import llm as llm_mod
from mydevagent.health import explain_error
from mydevagent.llm import OpenAICompatLLM


def fake_ollama(monkeypatch, reply: dict, status: int = 200):
    """POST /api/chat di Ollama: registra le richieste e restituisce `reply`."""
    sent = []

    def post(url, json=None, timeout=None):
        sent.append((url, json))
        return httpx.Response(status, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    return sent


def test_ollama_gets_the_profile_context_size(settings, monkeypatch):
    # senza num_ctx Ollama taglia i prompt lunghi e la richiesta dell'utente sparisce (bug dello Studio)
    sent = fake_ollama(monkeypatch, {"message": {"content": "ok", "tool_calls": [
        {"function": {"name": "write_file", "arguments": {"path": "a.py", "content": "x"}}}]},
        "prompt_eval_count": 9000, "eval_count": 12})
    history = [{"role": "system", "content": "s"}, {"role": "user", "content": "crea a.py"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "b"}'}}]},
               {"role": "tool", "tool_call_id": "c1", "content": "..."}]
    comp = OpenAICompatLLM(settings).complete(history, max_tokens=100, temperature=0.1)
    url, body = sent[0]
    assert url == "http://localhost:11434/api/chat" and body["stream"] is False
    assert body["options"] == {"num_ctx": settings.active_profile.num_ctx, "num_predict": 100, "temperature": 0.1}
    assert body["messages"][2]["tool_calls"] == [{"function": {"name": "read_file", "arguments": {"path": "b"}}}]
    assert comp.text == "ok" and comp.prompt_tokens == 9000 and comp.completion_tokens == 12
    assert json.loads(comp.calls[0]["arguments"]) == {"path": "a.py", "content": "x"}


def test_ollama_images_and_errors(settings, monkeypatch):
    sent = fake_ollama(monkeypatch, {"message": {"content": "un gatto"}})
    content = [{"type": "text", "text": "cosa vedi?"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]
    assert OpenAICompatLLM(settings).complete([{"role": "user", "content": content}], tier="vision").text == "un gatto"
    assert sent[0][1]["messages"][0] == {"role": "user", "content": "cosa vedi?", "images": ["QUJD"]}

    fake_ollama(monkeypatch, {"error": 'model "qwen9:1b" not found, try pulling it first'}, status=404)
    try:
        OpenAICompatLLM(settings).complete([{"role": "user", "content": "ciao"}])
    except RuntimeError as exc:
        title, hint = explain_error(exc, settings)
    assert "qwen9:1b" in title and "/pull qwen9:1b" in hint
    assert llm_mod._to_ollama({"role": "user", "content": "x"}) == {"role": "user", "content": "x"}
