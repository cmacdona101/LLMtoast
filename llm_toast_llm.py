# llm_toast_llm.py
"""
Thin, resilient LLM client for ClipLLM.

- Reads API key from llm_toast_settings (Credential Manager/DPAPI).
- Optional config in %APPDATA%\ClipLLM\settings.json (api_base, model, timeout_s).
- Public helpers:
    * explain_selection(text) -> str       # single-sentence explain (system prompt)
    * chat(user_text, system_prompt=...)   # one-off chat turn

Auto-adapts across OpenAI-style providers:
  1) POST /chat/completions with max_completion_tokens
  2) ...with max_output_tokens
  3) ...with legacy max_tokens
  4) POST /responses with max_output_tokens (tries 'text' then 'input_text')
"""

from __future__ import annotations

import os
import time
import json
import logging
from typing import Optional, Tuple, Any, Dict

import requests  # pip install requests

import llm_toast_settings as settings
try:
    import llm_toast_session_log as slog
except Exception:
    slog = None

log = logging.getLogger("clip_llm_tray")

# -------------------- configuration --------------------
# DEFAULT_MODEL = "gpt-5-mini-2025-08-07"
DEFAULT_MODEL = "gpt-5-nano-2025-08-07"        # used for selection-explain hotkey
DEFAULT_CHAT_MODEL = "gpt-5-2025-08-07"        # used for chat window
# Back-compat alias for historical typo (if any old code references DEFAULT_CHAT_MODE)
DEFAULT_CHAT_MODE = DEFAULT_CHAT_MODEL

DEFAULT_API_BASE = "https://api.openai.com/v1"  # override via ettings/env if needed
DEFAULT_TIMEOUT_S = 360
DEFAULT_TEMPERATURE = 1  # <-- per request, keep temperature at 1

# Separate token budgets (can be adjusted later or wired to settings if desired)
EXPLAIN_MAX_TOKENS = 4048
CHAT_MAX_TOKENS = 10000

SYSTEM_PROMPT = (
    "You will receive a text selection copied from the user's screen. "
    "Explain what it means in a single clear sentence. "
    "Do not add prefaces or extra sentences."
)

DEFAULT_CHAT_SYSTEM_PROMPT = (
    """You are a concise, helpful assistant. Answer briefly and clearly. Web access for information retrieval is authorized where necessary.
DO NOT PROVIDE ANY URLS OR LINKS IN YOUR RESPONSE."""
)

def _load_config() -> Tuple[str, str, str, int]:
    cfg = settings.load_settings() or {}
    api_base = cfg.get("api_base") or os.getenv("CLIPLLM_API_BASE") or DEFAULT_API_BASE
    # selection model (hotkey explain)
    model = cfg.get("model") or os.getenv("CLIPLLM_MODEL") or DEFAULT_MODEL
    # chat model (chat window)
    chat_model = cfg.get("chat_model") or os.getenv("CLIPLLM_CHAT_MODEL") or DEFAULT_CHAT_MODEL
    timeout = cfg.get("timeout_s") or os.getenv("CLIPLLM_TIMEOUT_S") or DEFAULT_TIMEOUT_S
    try:
        timeout = int(timeout)
    except Exception:
        timeout = DEFAULT_TIMEOUT_S
    log.debug("LLM config loaded: api_base=%s model=%s chat_model=%s timeout=%s",
              api_base, model, chat_model, timeout)
    return api_base, model, chat_model, timeout

# -------------------- public API --------------------
def explain_selection(text: str) -> str:
    """Single-sentence explanation of a selection using a fixed system prompt."""
    key = settings.get_api_key()
    if not key:
        log.info("No API key configured; returning helper message")
        return f"No API key set. Open Options → paste your LLM API key. (Selection length: {len(text)} chars)"

    api_base, model, _chat_model, timeout = _load_config()
    try:
        return _request_with_fallbacks(
            api_base, key, model, SYSTEM_PROMPT, text, timeout,
            token_budget=EXPLAIN_MAX_TOKENS
        )
    except Exception as e:
        log.exception("LLM request failed")
        return f"LLM error: {str(e)}"

def chat(user_text: str,
         system_prompt: str = DEFAULT_CHAT_SYSTEM_PROMPT,
         prev_response_id: Optional[str] = None,
         session: Optional["slog.SessionLogger"] = None) -> tuple[str, Optional[str]]:
    """One-off chat turn: system + user → single assistant reply."""
    key = settings.get_api_key()
    if not key:
        log.info("No API key configured; returning helper message")
        return "No API key set. Open Options and paste your LLM API key.", None
    
    api_base, _model, chat_model, timeout = _load_config()
    
    try:
        # Prefer GPT-5 Responses API with hosted web search (no custom tooling needed)
        if "gpt-5" in (chat_model or ""):
            return _chat_with_gpt5_websearch(
                api_base, key, chat_model, system_prompt, user_text, timeout,
                token_budget=CHAT_MAX_TOKENS,
                previous_response_id=prev_response_id,
                session=session
            )
        # Otherwise, keep legacy tool-less path (no session id available here)
        text = _request_with_fallbacks(
            api_base, key, chat_model, system_prompt, user_text, timeout,
            token_budget=CHAT_MAX_TOKENS,
            session=session
        )
        return text, None
    except Exception as e:
        log.exception("LLM chat request failed")
        if session:
            session.log_error(e, context="chat()")
        return f"LLM error: {str(e)}", None
   
    
def _chat_with_gpt5_websearch(api_base: str, key: str, model: str, system_prompt: str,
                              user_text: str, timeout_s: int, token_budget: int,
                              previous_response_id: Optional[str] = None,
                              session: Optional["slog.SessionLogger"] = None) -> tuple[str, Optional[str]]:
    """
    Use GPT-5 Responses API with the hosted 'web_search' tool.
    No external search code required; OpenAI executes the tool server-side.
    """
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = _join(api_base, "/responses")
    payload = {
        "model": model,
        "temperature": DEFAULT_TEMPERATURE,
        "max_output_tokens": token_budget,
        # Optional GPT-5 controls (uncomment to tune):
        # "reasoning": {"effort": "minimal"},
        # "text": {"verbosity": "low"},
        # Use 'instructions' for system-level guidance and a simple input string
        "instructions": system_prompt,
        "input": user_text,
        # Enable hosted web search; allow the model to call it automatically
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
    }
    
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    
    log.debug("POST %s (gpt5 responses + web_search, budget=%d)", url, token_budget)
    t0 = time.perf_counter()
    if session:
        session.log_request("/responses", {
            "model": model,
            "max_output_tokens": token_budget,
            "has_web_search": True,
        }, tool_choice="auto", prev_id=previous_response_id)
    try:
        data = _post_json(url, headers, payload, timeout_s)
        _log_token_usage(data, context="responses(gpt5+web_search)", token_budget=token_budget)

        # Prefer Responses API extract; fall back to chat-style if provider proxies formats
        text = _extract_text_responses(data) or _extract_text_chat_completions(data)
        rid = data.get("id")
        out = (text if (text and text.strip()) else "(empty response)")
        if session:
            usage = data.get("usage") or {}
            finish = None
            try:
                finish = (data.get("choices") or [{}])[0].get("finish_reason")
            except Exception:
                pass
            session.log_response(
                out, usage=usage, finish_reason=finish,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                response_id=rid
           )
        return out, rid

    except (_RetryableEndpointError, _RetryableParamError):
        # Provider doesn’t support /responses or the param; fall back
        log.debug("Falling back to chat/completions after /responses error")
        if session:
            session.log_error("responses() failed; falling back to chat/completions")
        text = _request_with_fallbacks(
            api_base, key, model, system_prompt, user_text, timeout_s,
            token_budget=token_budget,
            session=session
        )
        return text, previous_response_id
    except Exception as e:
        raise


# -------------------- fallback strategy --------------------
class _RetryableParamError(RuntimeError): ...
class _RetryableEndpointError(RuntimeError): ...

def _request_with_fallbacks(api_base: str, key: str, model: str,
                            system_prompt: str, user_text: str, timeout_s: int,
                            token_budget: int, session: Optional["slog.SessionLogger"] = None) -> str:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    # Prefer modern param first to avoid an initial failing request on many providers:
    # 1) /chat/completions with max_completion_tokens
    try:
        return _chat_completions(api_base, headers, model, system_prompt, user_text,
                                 timeout_s, token_param="max_completion_tokens",
                                 token_budget=token_budget, session=session)
    except _RetryableParamError:
        pass
    except _RetryableEndpointError:
        pass

    # 2) /chat/completions with max_output_tokens
    try:
        return _chat_completions(api_base, headers, model, system_prompt, user_text,
                                 timeout_s, token_param="max_output_tokens",
                                 token_budget=token_budget, session=session)
    except _RetryableParamError:
        pass
    except _RetryableEndpointError:
        pass

    # 3) /chat/completions with legacy max_tokens
    try:
        return _chat_completions(api_base, headers, model, system_prompt, user_text,
                                 timeout_s, token_param="max_tokens",
                                 token_budget=token_budget, session=session)
    except _RetryableParamError:
        pass
    except _RetryableEndpointError:
        pass

    # 4) /responses with max_output_tokens
    return _responses(api_base, headers, model, system_prompt, user_text,
                      timeout_s, token_param="max_output_tokens",
                      token_budget=token_budget, session=session)

# -------------------- HTTP variants --------------------
def _chat_completions(api_base: str, headers: Dict[str, str], model: str,
                      system_prompt: str, user_text: str, timeout_s: int,
                      token_param: str, token_budget: int,
                      session: Optional["slog.SessionLogger"] = None) -> str:
    url = _join(api_base, "/chat/completions")
    payload = {
        "model": model,
        "temperature": DEFAULT_TEMPERATURE,
        token_param: token_budget,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ]
    }
    log.debug("POST %s (model=%s, %s=%d, text_len=%d)", url, model, token_param, token_budget, len(user_text))
    t0 = time.perf_counter()
    if session:
        session.log_request("/chat/completions", {"model": model, token_param: token_budget})
    data = _post_json(url, headers, payload, timeout_s)

    _log_token_usage(data, context=f"chat_completions({token_param})", token_budget=token_budget)

    _raise_if_param_unsupported(data, token_param)
    _raise_if_endpoint_unsupported(data)

    text = _extract_text_chat_completions(data)
    if session:
        usage = data.get("usage") or {}
        finish = None
        try:
            finish = (data.get("choices") or [{}])[0].get("finish_reason")
        except Exception:
            pass
        session.log_response(text, usage=usage, finish_reason=finish, latency_ms=(time.perf_counter() - t0) * 1000.0)
    return text

def _responses(api_base: str, headers: Dict[str, str], model: str,
               system_prompt: str, user_text: str, timeout_s: int,
               token_param: str, token_budget: int,
               session: Optional["slog.SessionLogger"] = None) -> str:
    """
    Try /responses with two content type flavors:
      - 'text' (classic)
      - 'input_text' (typed content providers)
    """
    url = _join(api_base, "/responses")

    def build_payload(content_type: str) -> Dict[str, Any]:
        return {
            "model": model,
            "temperature": DEFAULT_TEMPERATURE,
            token_param: token_budget,  # typically 'max_output_tokens'
            "input": [
                {"role": "system", "content": [{"type": content_type, "text": system_prompt}]},
                {"role": "user",   "content": [{"type": content_type, "text": user_text}]},
            ]
        }

    last_err = None
    for ctype in ("text", "input_text"):
        payload = build_payload(ctype)
        log.debug("POST %s (model=%s, %s=%d, text_len=%d, ctype=%s)",
                  url, model, token_param, token_budget, len(user_text), ctype)
        try:
            t0 = time.perf_counter()
            if session:
                session.log_request("/responses", {"model": model, token_param: token_budget, "ctype": ctype})
            data = _post_json(url, headers, payload, timeout_s)
            _log_token_usage(data, context=f"chat_completions({token_param})", token_budget=token_budget)

        except RuntimeError as e:
            last_err = e
            # If server rejects 'text', auto-retry with 'input_text'
            if ctype == "text" and "Invalid value: 'text'" in str(e):
                log.debug("Retrying /responses with content type 'input_text'")
                continue
            raise

        # Extract either OpenAI-style or typed Responses shapes
        try:
            text = _extract_text_chat_completions(data)
            if session:
                usage = data.get("usage") or {}
                finish = None
                try:
                    finish = (data.get("choices") or [{}])[0].get("finish_reason")
                except Exception:
                    pass
                session.log_response(text, usage=usage, finish_reason=finish, latency_ms=(time.perf_counter() - t0) * 1000.0)
            return text
        except Exception:
            text = _extract_text_responses(data)
            if text is not None and text.strip():
                out = text.strip()
                if session:
                    usage = data.get("usage") or {}
                    session.log_response(out, usage=usage, latency_ms=(time.perf_counter() - t0) * 1000.0)
                return out
            last_err = RuntimeError("Unexpected /responses format")

    # Both variants failed
    if last_err:
        raise last_err
    raise RuntimeError("Unknown /responses error")

# -------------------- HTTP helpers --------------------
def _log_token_usage(data: Dict[str, Any], context: str, token_budget: Optional[int] = None) -> None:
    """Debug-log token usage and finish reason if present."""
    try:
        usage = data.get("usage")
        choice0 = (data.get("choices") or [{}])[0]
        fr = choice0.get("finish_reason")
        if usage or fr:
            budget_str = f", budget={token_budget}" if token_budget is not None else ""
            if usage:
                pt = usage.get("prompt_tokens")
                ct = usage.get("completion_tokens")
                tt = usage.get("total_tokens")
                log.debug("usage[%s]: prompt=%s, completion=%s, total=%s%s", context, pt, ct, tt, budget_str)
            if fr:
                log.debug("finish_reason[%s]=%s", context, fr)
    except Exception:
        # Never fail the request because of logging
        pass

def _post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout_s: int) -> Dict[str, Any]:
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout_s)
    try:
        data = r.json()
    except Exception:
        data = {"_nonjson": (r.text or "")[:300]}
    if r.status_code >= 400:
        msg = _extract_error_message(data) or (r.text or "")[:300]
        if "unsupported" in (msg or "").lower() and "token" in (msg or "").lower():
            raise _RetryableParamError(f"HTTP {r.status_code}: {msg}")
        if r.status_code in (404, 405):
            raise _RetryableEndpointError(f"HTTP {r.status_code}: {msg}")
        raise RuntimeError(f"HTTP {r.status_code}: {msg}")
    return data

def _extract_error_message(data: Dict[str, Any]) -> Optional[str]:
    if isinstance(data, dict) and "error" in data:
        e = data["error"]
        if isinstance(e, dict):
            return e.get("message") or str(e)
        return str(e)
    return None

def _raise_if_param_unsupported(data: Dict[str, Any], token_param: str):
    msg = _extract_error_message(data)
    if not msg:
        return
    if token_param in (msg or "") and "unsupported" in msg.lower():
        raise _RetryableParamError(msg)

def _raise_if_endpoint_unsupported(_data: Dict[str, Any]):
    # Present for symmetry; usually not needed on 200 OK
    pass

def _extract_text_chat_completions(data: Dict[str, Any]) -> str:
    """
    Accept multiple shapes:
    - OpenAI classic: choices[0].message.content -> str
    - Structured: choices[0].message.content -> list[{type,text,...}]
    - Old-style: choices[0].text
    - Some providers: top-level 'output_text'
    """
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    content = msg.get("content")

    def _join_parts(parts):
        texts = []
        for p in parts:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str) and t.strip():
                    texts.append(t.strip())
        return " ".join(texts)

    text = None
    # 1) Plain string content
    if isinstance(content, str) and content.strip():
        text = content.strip()
    # 2) List-of-parts content
    elif isinstance(content, list):
        joined = _join_parts(content)
        if joined:
            text = joined
    # 3) Old completions-style 'text'
    if not text:
        t = choice.get("text")
        if isinstance(t, str) and t.strip():
            text = t.strip()
    # 4) Some providers include 'output_text' as a convenience
    if not text and isinstance(data, dict):
        ot = data.get("output_text")
        if isinstance(ot, str) and ot.strip():
            text = ot.strip()

    if not text:
        try:
            fr = choice.get("finish_reason")
            if fr:
                log.debug("finish_reason=%s", fr)
            if "usage" in data:
                log.debug("usage=%s", data["usage"])
        except Exception:
            pass
        return "(empty response)"
    return text

def _extract_text_responses(data: Dict[str, Any]) -> Optional[str]:
    if isinstance(data, dict):
        if "output_text" in data and isinstance(data["output_text"], str):
            return data["output_text"]
        if "response" in data and isinstance(data["response"], dict):
            if "output_text" in data["response"]:
                return data["response"]["output_text"]
        if "output" in data and isinstance(data["output"], list):
            texts = []
            for item in data["output"]:
                parts = item.get("content") if isinstance(item, dict) else None
                if isinstance(parts, list):
                    for p in parts:
                        if isinstance(p, dict) and "text" in p:
                            texts.append(p["text"])
            if texts:
                return " ".join(t.strip() for t in texts if isinstance(t, str))
    return None

def _join(base: str, path: str) -> str:
    if base.endswith("/"):
        base = base[:-1]
    return base + (path if path.startswith("/") else "/" + path)
