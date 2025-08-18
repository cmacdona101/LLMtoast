"""
llm_toast_io.py
Keyboard (SendInput, key state, hotkey register/unregister) and clipboard helpers.
No UI here. The core module will import this and wire up logging.
"""

import ctypes, time, logging
from ctypes import wintypes

# -------- logger wiring (set by core) --------
_log = logging.getLogger("clip_llm_tray")
def set_logger(logger: logging.Logger):
    global _log
    _log = logger

# -------- Win32 setup --------
user32 = ctypes.WinDLL("user32", use_last_error=True)

# Pointer-sized type (Python 3.13: wintypes.ULONG_PTR may not exist)
try:
    ULONG_PTR = wintypes.ULONG_PTR  # type: ignore[attr-defined]
except AttributeError:
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# Hotkey flags
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

# Message constants
WM_HOTKEY, WM_COPY = 0x0312, 0x0301
SMTO_ABORTIFHUNG = 0x0002

# Virtual keys (subset)
VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN, VK_CONTROL, VK_C = 0x10, 0x12, 0x5B, 0x5C, 0x11, 0x43

# --------------------------- SendInput ---------------------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT),
                ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD),
                ("union", INPUT_UNION)]

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

def sendinput_key(vk: int, down: bool = True):
    """Inject a single keyboard event via SendInput."""
    flags = 0 if down else KEYEVENTF_KEYUP
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = vk
    inp.union.ki.wScan = 0
    inp.union.ki.dwFlags = flags
    inp.union.ki.time = 0
    inp.union.ki.dwExtraInfo = 0
    n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if n != 1:
        _log.debug("SendInput failed for vk=0x%X down=%s err=%d", vk, down, ctypes.get_last_error())

def is_key_down(vk: int) -> bool:
    """Return True if the given virtual-key is currently down."""
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0

# --------------------------- Hotkeys ---------------------------
def register_first_available(hotkey_options):
    """
    Register the first available hotkey from a list of (label, mods, vk).
    Returns (id, label).
    """
    # Ensure this thread has a message queue
    msg = wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

    for i, (label, mods, vk) in enumerate(hotkey_options, start=1):
        _log.debug("Attempting RegisterHotKey %s (id=%d)", label, i)
        if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
            _log.info("[hotkey] Registered: %s (id=%d)", label, i)
            return i, label
        _log.warning("[hotkey] Could not register %s (error %d)", label, ctypes.get_last_error())
        ctypes.set_last_error(0)
    raise SystemExit("No available hotkey from HOTKEY_OPTIONS.")

def unregister_hotkey(hotkey_id: int):
    try:
        user32.UnregisterHotKey(None, hotkey_id)
        _log.info("[hotkey] Unregistered id=%d", hotkey_id)
    except Exception:
        _log.exception("[hotkey] Unregister failed")

# --------------------------- Clipboard helpers ---------------------------
# Use pywin32 for clipboard (import lazily so this module can load without it in stub contexts)
def get_clipboard_text():
    import win32clipboard, win32con
    text = None
    try:
        win32clipboard.OpenClipboard()
        fmt_text = win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT)
        fmt_uni  = win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT)
        _log.debug("Clipboard formats: CF_TEXT=%s CF_UNICODETEXT=%s", fmt_text, fmt_uni)
        if fmt_uni:
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        elif fmt_text:
            raw = win32clipboard.GetClipboardData(win32con.CF_TEXT)
            try: text = raw.decode('mbcs', errors='replace')
            except Exception: text = None
    except Exception:
        _log.exception("get_clipboard_text failed")
    finally:
        try: win32clipboard.CloseClipboard()
        except Exception: pass
    _log.debug("Clipboard text length: %s", (len(text) if text else 0))
    return text

def set_clipboard_text(text: str):
    import win32clipboard, win32con
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        _log.debug("Restored original clipboard length=%d", len(text) if text else 0)
    except Exception:
        _log.exception("set_clipboard_text failed")
    finally:
        try: win32clipboard.CloseClipboard()
        except Exception: pass

# --------------------------- WM_COPY helper ---------------------------
def send_wm_copy(hwnd_focus) -> bool:
    """Send WM_COPY to the given hwnd (returns True if SendMessageTimeoutW didn't error)."""
    SendMessageTimeoutW = user32.SendMessageTimeoutW
    SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ulong)]
    res = ctypes.c_ulong(0)
    try:
        ok = SendMessageTimeoutW(hwnd_focus, WM_COPY, 0, 0, SMTO_ABORTIFHUNG, 300, ctypes.byref(res))
        _log.debug("WM_COPY to hwnd_focus -> ok=%d res=%d", ok, res.value)
        return bool(ok)
    except Exception:
        _log.exception("WM_COPY send failed")
        return False
