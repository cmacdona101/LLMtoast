# llm_toast_session_log.py
"""
Session logger for ClipLLM.

- Writes ONLY user queries and assistant replies to a single append-only file.
- Adds a hyphen divider at the start of each chat-window session, followed by a
  couple of blank lines for visual separation.
- Prefixes every entry with a local datetime stamp.
- Thread-safe for multi-threaded writes within the same process.

Log file (Windows):
  %LOCALAPPDATA%\ClipLLM\Logs\chat\chat_log.md

On non-Windows systems:
  ~/.local/state/clipllm/logs/chat/chat_log.md  (or similar XDG path)
"""

from __future__ import annotations

import os
import io
import threading
from datetime import datetime

__all__ = ["SessionLogger"]

# -------------------- paths --------------------

def _base_logs_dir() -> str:
    # Prefer LOCALAPPDATA on Windows
    la = os.getenv("LOCALAPPDATA")
    if la:
        return os.path.join(la, "ClipLLM", "Logs", "chat")

    # Cross-platform fallback (XDG-ish)
    xdg = os.getenv("XDG_STATE_HOME")
    if xdg:
        return os.path.join(xdg, "clipllm", "logs", "chat")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "clipllm", "logs", "chat")


# -------------------- logger --------------------

class SessionLogger:
    """
    Minimal, append-only logger for chat sessions.

    Public API you can safely use from the UI:
      - log_user(text: str)         # writes a timestamped "You" entry
      - log_assistant(text: str)    # writes a timestamped "Assistant" entry
      - end_session()               # optional; adds a trailing newline

    Compatibility shim:
      - log_response(text, **kwargs) -> logs as assistant text (ignores kwargs)

    Attributes:
      - path: absolute path to the single log file
    """

    def __init__(self, kind: str = "chat", single_file: bool = True) -> None:
        # Even if single_file is passed False, we still keep single-file behavior per your request.
        # The argument is kept for source compatibility with earlier versions.
        self.kind = kind
        self._dir = _base_logs_dir()
        os.makedirs(self._dir, exist_ok=True)

        self.path = os.path.join(self._dir, "chat_log.md")  # single file
        self._lock = threading.Lock()
        self._open_file_and_write_session_header()

    # -------- public logging methods --------

    def log_user(self, text: str) -> None:
        """Append a timestamped 'You' entry."""
        if text is None:
            return
        self._write_entry("You", text)

    def log_assistant(self, text: str) -> None:
        """Append a timestamped 'Assistant' entry."""
        if text is None:
            return
        self._write_entry("Assistant", text)

    # Compatibility with earlier llm_toast_llm integration:
    # Ignore metadata (usage/finish_reason/etc.) and only store the assistant text.
    def log_response(self, text: str, **_ignored) -> None:
        self.log_assistant(text)

    def log_error(self, *_args, **_kwargs) -> None:
        """No-op: we don't persist errors here (console handles those)."""
        return

    def end_session(self) -> None:
        """Optional nicety—adds a trailing newline so the next session divider stands out."""
        with self._lock:
            self._safe_write("\n")

    # -------- internals --------

    def _open_file_and_write_session_header(self) -> None:
        # Open once per SessionLogger instance, append mode, UTF-8, line-buffered-ish
        self._fh = io.open(self.path, mode="a", encoding="utf-8", buffering=1)
        # Session divider + a few newlines underneath
        with self._lock:
            self._safe_write("\n\n" + "-" * 80 + "\n\n")

    def _write_entry(self, who: str, text: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = self._normalize(text)
        with self._lock:
            self._safe_write(f"[{ts}] {who}:\n{body}\n\n")

    def _normalize(self, s: str) -> str:
        # Normalize newlines; ensure no extra trailing whitespace beyond one newline the writer adds
        s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
        return s.strip("\n")

    def _safe_write(self, s: str) -> None:
        try:
            self._fh.write(s)
            self._fh.flush()
        except Exception:
            # Silently ignore disk errors for UX; console logs still capture stack traces elsewhere.
            pass

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
