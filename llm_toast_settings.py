"""
llm_toast_settings.py
Persist settings and secrets.

- Secrets (API key): Windows Credential Manager via `keyring`, fallback to DPAPI-encrypted file.
- Non-secrets: %APPDATA%\\ClipLLM\\settings.json
"""

import os, json, logging, ctypes
from ctypes import wintypes

log = logging.getLogger("clip_llm_tray")

# ---------- app dirs ----------
def _config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "ClipLLM")
    os.makedirs(path, exist_ok=True)
    return path

def _settings_path() -> str:
    return os.path.join(_config_dir(), "settings.json")

# ---------- settings (non-secret) ----------
def load_settings() -> dict:
    p = _settings_path()
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        log.exception("Failed to load settings.json")
    return {}

def save_settings(d: dict) -> None:
    p = _settings_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        log.exception("Failed to save settings.json")

# ---------- secrets (API key) ----------
try:
    import keyring  # uses Windows Credential Manager on Windows
except Exception:
    keyring = None  # fallback to DPAPI file

SERVICE = "ClipLLM"
ACCOUNT = "api_key"
_FALLBACK_SECRET_PATH = os.path.join(_config_dir(), "secret.bin")

# DPAPI fallback helpers
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]

_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32

def _bytes_to_blob(b: bytes) -> DATA_BLOB:
    if not b:
        b = b"\x00"
    buf = (ctypes.c_byte * len(b))(*b)
    blob = DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob._buf = buf  # keep alive
    return blob

def _blob_to_bytes(blob: DATA_BLOB) -> bytes:
    if not blob.cbData:
        return b""
    data = ctypes.string_at(blob.pbData, blob.cbData)
    _kernel32.LocalFree(blob.pbData)
    return data

def _dpapi_protect(b: bytes) -> bytes:
    inb = _bytes_to_blob(b)
    outb = DATA_BLOB()
    if not _crypt32.CryptProtectData(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb)):
        raise OSError("CryptProtectData failed")
    return _blob_to_bytes(outb)

def _dpapi_unprotect(b: bytes) -> bytes:
    inb = _bytes_to_blob(b)
    outb = DATA_BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb)):
        raise OSError("CryptUnprotectData failed")
    return _blob_to_bytes(outb)

def set_api_key(key: str) -> None:
    """Store the API key securely."""
    try:
        if keyring:
            keyring.set_password(SERVICE, ACCOUNT, key)
            log.debug("API key saved to Credential Manager")
            return
    except Exception:
        log.exception("keyring.set_password failed; falling back to DPAPI file")

    # Fallback: DPAPI-encrypted file under %APPDATA%\ClipLLM\secret.bin
    try:
        enc = _dpapi_protect(key.encode("utf-8"))
        with open(_FALLBACK_SECRET_PATH, "wb") as f:
            f.write(enc)
        log.debug("API key saved via DPAPI fallback")
    except Exception:
        log.exception("DPAPI save failed")

def get_api_key() -> str | None:
    """Retrieve the API key, or None if not set."""
    try:
        if keyring:
            val = keyring.get_password(SERVICE, ACCOUNT)
            if val:
                return val
    except Exception:
        log.exception("keyring.get_password failed, trying DPAPI fallback")

    try:
        if os.path.exists(_FALLBACK_SECRET_PATH):
            with open(_FALLBACK_SECRET_PATH, "rb") as f:
                enc = f.read()
            raw = _dpapi_unprotect(enc)
            return raw.decode("utf-8", errors="replace")
    except Exception:
        log.exception("DPAPI load failed")
    return None

def delete_api_key() -> None:
    try:
        if keyring:
            try:
                keyring.delete_password(SERVICE, ACCOUNT)
            except keyring.errors.PasswordDeleteError:
                pass
    except Exception:
        log.exception("keyring.delete_password failed")

    try:
        if os.path.exists(_FALLBACK_SECRET_PATH):
            os.remove(_FALLBACK_SECRET_PATH)
    except Exception:
        log.exception("Failed to remove DPAPI fallback file")
