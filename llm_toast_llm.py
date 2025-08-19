"""
llm_toast_llm.py
Thin LLM client used by ClipLLM.

- Reads API key from llm_toast_settings (Credential Manager/DPAPI).
- Reads non-secret config (model, api_base, timeout) from settings.json or env.
- Provides a single helper: explain_selection(text) -> str
"""

from __future__ import annotations

import os
import json
import logging
from typing import Optional

import requests  # pip install requests

import llm_toast_settings as settings

log = logging.getLogger("clip_llm_tray")

# -------------------- configuration --------------------
#DEFAULT_MODEL = "gpt-5-mini-2025-08-07"          
DEFAULT_MODEL = "gpt-5-nano-2025-08-07"           
DEFAULT_API_BASE = "https://api.openai.com/v1"  # change if you use a different provider
DEFAULT_TIMEOUT_S = 12
DEFAULT_TEMPERATURE = 1                     # per your request
SYSTEM_PROMPT = (
    "You will receive a text selection copied from the user's screen. "
    "Explain what it means in a single clear sentence. "
    "Do not add prefaces or extra sentences."
)

def _load_config():
    cfg = settings.load_settings() or {}
    api_base = cfg.get("api_base") or os.getenv("CLIPLLM_API_BASE") or DEFAULT_API_BASE
    model = cfg.get("model") or os.getenv("CLIPLLM_MODEL") or DEFAULT_MODEL
    timeout = cfg.get("timeout_s") or os.getenv("CLIPLLM_TIMEOUT_S") or DEFAULT_TIMEOUT_S
    try:
        timeout = int(timeout)
    except Exception:
        timeout = DEFAULT_TIMEOUT_S
    return api_base, model, timeout

# -------------------- public API --------------------
def explain_selection(text: str) -> str:
    """
    Calls the LLM with a short system prompt and the user's selected text.
    Returns a single-sentence explanation (or a helpful error string).
    """
    key = settings.get_api_key()
    if not key:
        log.info("No API key configured; returning helper message")
        return "No API key set. Open Options → paste your LLM API key. (Selection length: {} chars)".format(len(text))

    api_base, model, timeout = _load_config()
    try:
        return _chat_completions(api_base, key, model, SYSTEM_PROMPT, text, timeout)
    except Exception as e:
        log.exception("LLM request failed")
        return f"LLM error: {str(e)}"

# -------------------- HTTP client --------------------
def _chat_completions(api_base: str, key: str, model: str,
                      system_prompt: str, user_text: str, timeout_s: int) -> str:
    """
    Minimal OpenAI-compatible /chat/completions call.
    Works with many OpenAI-style servers. If yours differs, tweak here.
    """
    url = _join(api_base, "/chat/completions")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": DEFAULT_TEMPERATURE,
        #"max_completion_tokens": 500,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ]
    }
    log.debug("POST %s (model=%s, text_len=%d)", url, model, len(user_text))

    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout_s)
    if r.status_code >= 400:
        # Avoid logging full body; just short snippet
        snippet = (r.text or "")[:300]
        raise RuntimeError(f"HTTP {r.status_code}: {snippet}")

    data = r.json()
    print(data)
    # OpenAI-style shape:
    try:
        out = data["choices"][0]["message"]["content"].strip()
        return out or "(empty response)"
    except Exception:
        # Some providers return a top-level 'content'
        if isinstance(data, dict) and "content" in data:
            c = data.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
        raise RuntimeError("Unexpected response format")

# -------------------- utils --------------------
def _join(base: str, path: str) -> str:
    if base.endswith("/"):
        base = base[:-1]
    return base + (path if path.startswith("/") else "/" + path)
