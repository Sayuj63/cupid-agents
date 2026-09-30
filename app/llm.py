"""LLM access with a free-provider fallback chain.

Providers are tried in LLM_PROVIDERS order (default gemini,groq,cerebras,openrouter,claude).
A provider is used only if its key is set (claude = local `claude -p` headless CLI, needs no key).
On 429/5xx a provider cools down and the next one is tried.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env() -> None:
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#") and v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

PROVIDERS = {
    "gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai", "key": "GEMINI_API_KEY",
               "model": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"), "parallel": 4},
    "groq": {"base": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY",
             "model": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"), "parallel": 3},
    "cerebras": {"base": "https://api.cerebras.ai/v1", "key": "CEREBRAS_API_KEY",
                 "model": os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b"), "parallel": 3},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY",
                   "model": os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"), "parallel": 2},
    "claude": {"cli": True, "model": os.environ.get("CLAUDE_MODEL", "sonnet"), "parallel": int(os.environ.get("CLAUDE_PARALLEL", "6"))},
}
_cool: dict[str, float] = {}
_sems = {k: threading.Semaphore(v["parallel"]) for k, v in PROVIDERS.items()}
_stats: dict[str, int] = {}


class LLMError(RuntimeError):
    pass


def available() -> list[str]:
    order = [p.strip() for p in os.environ.get("LLM_PROVIDERS", "gemini,groq,cerebras,openrouter,claude").split(",") if p.strip()]
    out = []
    for name in order:
        p = PROVIDERS.get(name)
        if not p:
            continue
        if p.get("cli"):
            if shutil.which("claude"):
                out.append(name)
        elif os.environ.get(p["key"]):
            out.append(name)
    return out


def _call_openai(p: dict, system: str, user: str, temperature: float, max_tokens: int, want_json: bool) -> str:
    body = {"model": p["model"], "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if want_json:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{p['base']}/chat/completions", json=body, timeout=180,
                      headers={"Authorization": f"Bearer {os.environ[p['key']]}"})
    if r.status_code in (429, 500, 502, 503, 504):
        raise _Retryable(f"{r.status_code} {r.text[:200]}")
    if r.status_code >= 400:
        raise LLMError(f"{r.status_code} {r.text[:300]}")
    return r.json()["choices"][0]["message"].get("content") or ""


def _call_claude(p: dict, system: str, user: str, temperature: float, max_tokens: int, want_json: bool) -> str:
    cmd = [shutil.which("claude"), "-p", "--output-format", "json", "--model", p["model"],
           "--system-prompt", system, "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--setting-sources", ""]
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", cwd=cwd, timeout=300)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout)[:300]
        if "limit" in msg.lower() or "overloaded" in msg.lower():
            raise _Retryable(msg)
        raise LLMError(f"claude cli: {msg}")
    out = json.loads(r.stdout)
    if out.get("is_error"):
        raise _Retryable(str(out.get("result"))[:300])
    return out.get("result") or ""


class _Retryable(Exception):
    pass


def complete(system: str, user: str, *, temperature: float = 0.8, max_tokens: int = 1200, want_json: bool = False,
             prefer: str | None = None) -> str:
    names = available()
    if prefer and prefer in names:
        names.remove(prefer)
        names.insert(0, prefer)
    if not names:
        raise LLMError("No LLM configured: set GEMINI_API_KEY (free at aistudio.google.com/apikey) in .env")
    last = None
    for attempt in range(6):
        for name in names:
            if _cool.get(name, 0) > time.time():
                continue
            p = PROVIDERS[name]
            try:
                with _sems[name]:
                    fn = _call_claude if p.get("cli") else _call_openai
                    text = fn(p, system, user, temperature, max_tokens, want_json)
                _stats[name] = _stats.get(name, 0) + 1
                return text
            except _Retryable as e:
                last = f"{name}: {e}"
                _cool[name] = time.time() + 20 * (attempt + 1)
            except (requests.RequestException, subprocess.TimeoutExpired) as e:
                last = f"{name}: {e}"
                _cool[name] = time.time() + 10
            except LLMError as e:
                last = f"{name}: {e}"
                _cool[name] = time.time() + 60
        time.sleep(3 + attempt * 4)
    raise LLMError(f"all providers failed; last error: {last}")


def parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                continue
    raise LLMError(f"model did not return JSON: {text[:200]}")


def complete_json(system: str, user: str, **kw):
    kw.setdefault("temperature", 0.6)
    for attempt in range(2):
        text = complete(system + "\n\nRespond with ONLY valid JSON. No markdown fences.", user, want_json=True, **kw)
        try:
            return parse_json(text)
        except LLMError:
            if attempt:
                raise
    return None


def stats() -> dict:
    return dict(_stats)
