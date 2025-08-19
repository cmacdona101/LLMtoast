"""
llm_toast_core.py
Core logic that uses llm_toast_io for keyboard + clipboard.
Keeps the public API the UI depends on: register_first_available, unregister_hotkey,
attempt_copy_via_wmcopy_and_sendinput, ask_llm, set_clipboard_text, get_clipboard_text,
and exposes log, user32, WM_HOTKEY.
"""

import os, sys, time, ctypes, traceback, logging, platform
from ctypes import wintypes

import llm_toast_io as io  # <-- NEW split
import llm_toast_llm as llm

# --------------------------- Logging ---------------------------
def _setup_logger():
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.getcwd()), "ClipLLM")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "clip_llm_tray.log")

    logger = logging.getLogger("clip_llm_tray")
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(threadName)s :: %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        sh = logging.StreamHandler(sys.stdout); sh.setLevel(logging.DEBUG); sh.setFormatter(fmt)
        fh = logging.FileHandler(log_path, encoding="utf-8"); fh.setLevel(logging.DEBUG); fh.setFormatter(fmt)
        logger.addHandler(sh); logger.addHandler(fh)
        logger.debug("==== ClipLLM Core starting ====")
        logger.debug("Python: %s", sys.version.replace("\n", " "))
        logger.debug("Platform: %s", platform.platform())
        logger.debug("Log file: %s", log_path)
    # hand logger to io module so it can log too
    io.set_logger(logger)
    return logger

log = _setup_logger()
def log_exc(msg: str): log.error("%s\n%s", msg, traceback.format_exc())

# --------------------------- Win32 basics ---------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)

# Messages/constants that UI expects from core
WM_HOTKEY = 0x0312  # keep for UI (llm_toast_ui imports this)
WM_COPY   = 0x0301

# Virtual keys (subset, used by core when calling io)
VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN, VK_CONTROL, VK_C = 0x10, 0x12, 0x5B, 0x5C, 0x11, 0x43

# Clipboard sequence for change detection
GetClipboardSequenceNumber = user32.GetClipboardSequenceNumber
GetClipboardSequenceNumber.restype = wintypes.DWORD

# --------------------------- Focus helpers ---------------------------
class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", RECT)]

GetForegroundWindow = user32.GetForegroundWindow
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetGUIThreadInfo = user32.GetGUIThreadInfo; GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
GetClassNameW = user32.GetClassNameW; GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

def _focused_hwnd_and_class():
    hwnd_fg = GetForegroundWindow()
    if not hwnd_fg: return None, None, None
    pid = wintypes.DWORD(0)
    tid = GetWindowThreadProcessId(hwnd_fg, ctypes.byref(pid))
    gti = GUITHREADINFO(); gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return hwnd_fg, None, None
    hwnd_focus = gti.hwndFocus
    buf = ctypes.create_unicode_buffer(256)
    cls = None
    if hwnd_focus:
        GetClassNameW(hwnd_focus, buf, 256)
        cls = buf.value
    return hwnd_fg, hwnd_focus, cls

def focused_info_for_log():
    hwnd_fg, hwnd_focus, cls = _focused_hwnd_and_class()
    log.debug("Focus: hwnd_fg=0x%X hwnd_focus=0x%X class='%s'",
              int(hwnd_fg or 0), int(hwnd_focus or 0), cls)
    return hwnd_fg, hwnd_focus, cls

# --------------------------- Public: Hotkeys (wrappers to io) ---------------------------
# Hotkeys to try (UI text shows whichever succeeds)
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
VK_Z, VK_SPACE, VK_OEM_3 = 0x5A, 0x20, 0xC0

HOTKEY_OPTIONS = [
    ("Ctrl+Shift+Z",       MOD_CONTROL | MOD_SHIFT,               VK_Z),
    ("Ctrl+Alt+Shift+Z",   MOD_CONTROL | MOD_ALT | MOD_SHIFT,     VK_Z),
    ("Ctrl+Alt+Z",         MOD_CONTROL | MOD_ALT,                 VK_Z),
    ("Ctrl+Shift+`",       MOD_CONTROL | MOD_SHIFT,               VK_OEM_3),
    ("Ctrl+Shift+Space",   MOD_CONTROL | MOD_SHIFT,               VK_SPACE),
]

def register_first_available():
    return io.register_first_available(HOTKEY_OPTIONS)

def unregister_hotkey(hotkey_id: int):
    return io.unregister_hotkey(hotkey_id)

# --------------------------- Public: Clipboard (wrappers to io) ---------------------------
def get_clipboard_text():
    return io.get_clipboard_text()

def set_clipboard_text(text: str):
    return io.set_clipboard_text(text)

# --------------------------- LLM stub ---------------------------
def ask_llm(prompt: str) -> str:
    # Delegate to the real LLM client (falls back to helpful message if no key)
    return llm.explain_selection(prompt)

# --------------------------- Selection via clipboard (robust) ---------------------------
def attempt_copy_via_wmcopy_and_sendinput(max_wait_ms=2000):
    """
    Try WM_COPY to the focused control first; if that doesn't change the clipboard,
    send Ctrl+C via SendInput BUT first temporarily release Shift/Alt/Win.
    Returns (selected_text or None, original_clipboard_text).
    """
    focused_info_for_log()
    ks = {
        "SHIFT": io.is_key_down(VK_SHIFT),
        "ALT":   io.is_key_down(VK_MENU),
        "LWIN":  io.is_key_down(VK_LWIN),
        "RWIN":  io.is_key_down(VK_RWIN),
        "CTRL":  io.is_key_down(VK_CONTROL),
    }
    log.debug("Key states before copy: %s", ks)

    seq_before = GetClipboardSequenceNumber()
    original = get_clipboard_text()

    # 1) WM_COPY directly to focused control (if any)
    _, hwnd_focus, _ = _focused_hwnd_and_class()
    changed = False
    if hwnd_focus:
        io.send_wm_copy(hwnd_focus)

        # poll for change quickly (~40% budget)
        deadline = time.time() + (max_wait_ms / 1000.0) * 0.4
        while time.time() < deadline:
            if GetClipboardSequenceNumber() != seq_before:
                changed = True; break
            time.sleep(0.02)

    # 2) If still no change, SendInput Ctrl+C; ensure Shift/Alt/Win are UP temporarily
    if not changed:
        lifted = []
        for vk, name in [(VK_SHIFT, "SHIFT"), (VK_MENU, "ALT"), (VK_LWIN, "LWIN"), (VK_RWIN, "RWIN")]:
            if io.is_key_down(vk):
                log.debug("Temporarily releasing %s", name)
                io.sendinput_key(vk, down=False)
                time.sleep(0.01)
                lifted.append(vk)

        ctrl_was_down = io.is_key_down(VK_CONTROL)
        if not ctrl_was_down:
            io.sendinput_key(VK_CONTROL, down=True); time.sleep(0.01)
        io.sendinput_key(VK_C, down=True); time.sleep(0.005)
        io.sendinput_key(VK_C, down=False); time.sleep(0.005)
        io.sendinput_key(VK_CONTROL, down=False); time.sleep(0.01)
        if ctrl_was_down:
            io.sendinput_key(VK_CONTROL, down=True)

        for vk in lifted:
            io.sendinput_key(vk, down=True); time.sleep(0.005)

        # Poll for change (~60% budget)
        deadline = time.time() + (max_wait_ms / 1000.0) * 0.6
        while time.time() < deadline:
            if GetClipboardSequenceNumber() != seq_before:
                changed = True; break
            time.sleep(0.02)

    if not changed:
        log.info("Clipboard did not change after WM_COPY/SendInput Ctrl+C")
        return None, original

    sel = get_clipboard_text()
    if not sel:
        log.info("Clipboard changed but no text format present")
        return None, original

    log.info("[select] Clipboard path succeeded (len=%d)", len(sel))
    return sel, original
