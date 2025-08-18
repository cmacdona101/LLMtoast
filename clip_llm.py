# clip_llm_tray.py
# Tray app with global hotkey. Robust "copy selection" with exhaustive logging.
# Strategy:
#   1) Try WM_COPY to the focused control (doesn't depend on key state)
#   2) If no clipboard change, SendInput Ctrl+C BUT first temporarily release Shift/Alt/Win
#   3) Wait/poll up to ~2s for clipboard change before giving up
#
# Deps:
#   pip install pywin32 pystray pillow
#
# Run with python.exe (console) to see logs live. Also logs to:
#   %LOCALAPPDATA%\ClipLLM\clip_llm_tray.log

import os, sys, time, threading, ctypes, queue, traceback, logging, platform
from ctypes import wintypes

# --------------------------- Logging ---------------------------
def _setup_logger():
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.getcwd()), "ClipLLM")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "clip_llm_tray.log")
    logger = logging.getLogger("clip_llm_tray")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(threadName)s :: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout); sh.setLevel(logging.DEBUG); sh.setFormatter(fmt)
    fh = logging.FileHandler(log_path, encoding="utf-8"); fh.setLevel(logging.DEBUG); fh.setFormatter(fmt)
    logger.addHandler(sh); logger.addHandler(fh)
    logger.debug("==== ClipLLM Tray starting ====")
    logger.debug("Python: %s  Tk? will init later", sys.version.replace("\n", " "))
    logger.debug("Platform: %s", platform.platform())
    logger.debug("Log file: %s", log_path)
    return logger

log = _setup_logger()
def log_exc(msg: str): log.error("%s\n%s", msg, traceback.format_exc())

# --------------------------- Imports ---------------------------
try:
    import pystray
    from pystray import MenuItem as Item, Menu as TrayMenu
    log.debug("pystray imported OK")
except Exception: log_exc("Failed to import pystray"); raise

try:
    from PIL import Image, ImageDraw
    log.debug("Pillow imported OK")
except Exception: log_exc("Failed to import Pillow"); raise

try:
    import win32api, win32con, win32clipboard
    log.debug("pywin32 imported OK")
except Exception: log_exc("Failed to import pywin32"); raise

try:
    import tkinter as tk
    from tkinter import ttk
    log.debug("tkinter imported OK")
except Exception: log_exc("Failed to import tkinter"); raise

# --------------------------- Constants & Win32 ---------------------------
APP_NAME = "ClipLLM Tray"
POPUP_MAX_CHARS = 2000
POPUP_WIDTH_PX = 420
POPUP_LIFETIME_MS = 6000

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GetClipboardSequenceNumber = user32.GetClipboardSequenceNumber
GetClipboardSequenceNumber.restype = wintypes.DWORD

# Hotkeys & messages
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_GETTEXT, WM_GETTEXTLENGTH, EM_GETSEL, WM_COPY = 0x0312, 0x000D, 0x000E, 0x00B0, 0x0301
SMTO_ABORTIFHUNG = 0x0002
VK_Z, VK_SPACE, VK_OEM_3 = 0x5A, 0x20, 0xC0
VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN, VK_CONTROL, VK_C = 0x10, 0x12, 0x5B, 0x5C, 0x11, 0x43

# --------------------------- Pointer-sized types (fix for Py 3.13) ---------------------------
# ctypes.wintypes.ULONG_PTR is not present in some Python builds. Define a compatible alias.
try:
    ULONG_PTR = wintypes.ULONG_PTR  # may raise AttributeError
except AttributeError:
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# --------------------------- SendInput (correct structs) ---------------------------
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
KEYEVENTF_SCANCODE = 0x0008  # not used here, but defined for completeness

def _sendinput_key(vk, down=True):
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
        log.debug("SendInput failed for vk=0x%X down=%s err=%d", vk, down, ctypes.get_last_error())

def _is_key_down(vk):
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0

# --------------------------- Focus info helpers ---------------------------
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
SendMessageW = user32.SendMessageW
SendMessageTimeoutW = user32.SendMessageTimeoutW
SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ulong)]

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

# --------------------------- Hotkey setup ---------------------------
HOTKEY_OPTIONS = [
    ("Ctrl+Shift+Z",       MOD_CONTROL | MOD_SHIFT,               VK_Z),
    ("Ctrl+Alt+Shift+Z",   MOD_CONTROL | MOD_ALT | MOD_SHIFT,     VK_Z),
    ("Ctrl+Alt+Z",         MOD_CONTROL | MOD_ALT,                 VK_Z),
    ("Ctrl+Shift+`",       MOD_CONTROL | MOD_SHIFT,               VK_OEM_3),
    ("Ctrl+Shift+Space",   MOD_CONTROL | MOD_SHIFT,               VK_SPACE),
]

def register_first_available():
    msg = wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # ensure message queue
    for i, (label, mods, vk) in enumerate(HOTKEY_OPTIONS, start=1):
        log.debug("Attempting RegisterHotKey %s (id=%d)", label, i)
        if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
            log.info("[hotkey] Registered: %s (id=%d)", label, i)
            return i, label
        log.warning("[hotkey] Could not register %s (error %d)", label, ctypes.get_last_error())
        ctypes.set_last_error(0)
    raise SystemExit("No available hotkey from HOTKEY_OPTIONS.")

def unregister_hotkey(hotkey_id):
    try:
        user32.UnregisterHotKey(None, hotkey_id)
        log.info("[hotkey] Unregistered id=%d", hotkey_id)
    except Exception: log_exc("[hotkey] Unregister failed")

# --------------------------- Clipboard helpers ---------------------------
def get_clipboard_text():
    text = None
    try:
        win32clipboard.OpenClipboard()
        fmt_text = win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT)
        fmt_uni  = win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT)
        log.debug("Clipboard formats: CF_TEXT=%s CF_UNICODETEXT=%s", fmt_text, fmt_uni)
        if fmt_uni:
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        elif fmt_text:
            raw = win32clipboard.GetClipboardData(win32con.CF_TEXT)
            try: text = raw.decode('mbcs', errors='replace')
            except Exception: text = None
    except Exception: log_exc("get_clipboard_text failed")
    finally:
        try: win32clipboard.CloseClipboard()
        except Exception: pass
    log.debug("Clipboard text length: %s", (len(text) if text else 0))
    return text

def set_clipboard_text(text):
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        log.debug("Restored original clipboard length=%d", len(text) if text else 0)
    except Exception: log_exc("set_clipboard_text failed")
    finally:
        try: win32clipboard.CloseClipboard()
        except Exception: pass

# --------------------------- LLM stub ---------------------------
def ask_llm(prompt: str) -> str:
    log.debug("ask_llm(%d chars)", len(prompt))
    p = prompt.strip()
    if len(p) > POPUP_MAX_CHARS: p = p[:POPUP_MAX_CHARS] + "…"
    resp = f"LLM (stub) received {len(prompt)} chars.\n\n{p}"
    log.debug("ask_llm -> %d chars", len(resp))
    return resp

# --------------------------- Popup UI ---------------------------
def make_tray_icon(size=32):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 4, size-3, size-7), radius=7, fill=(0, 122, 204, 255))
    d.polygon([(size//2 - 4, size-8), (size//2 + 4, size-8), (size//2, size-2)], fill=(0, 122, 204, 255))
    d.text((7, 9), "LL", fill=(255, 255, 255, 255))
    return img

class PopupManager:
    def __init__(self, root):
        self.root = root
        self.popups = []
    def show(self, title: str, body: str):
        try:
            log.debug("Popup: '%s' len=%d", title, len(body) if body else 0)
            w = tk.Toplevel(self.root); w.overrideredirect(True); w.attributes("-topmost", True)
            try: w.attributes("-alpha", 0.0)
            except Exception: pass
            frame = ttk.Frame(w, padding=10); frame.pack(fill="both", expand=True)
            ttk.Label(frame, text=title, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
            ttk.Label(frame, text=body, wraplength=POPUP_WIDTH_PX, justify="left").pack(fill="both", expand=True, pady=(6,10))
            btns = ttk.Frame(frame); btns.pack(fill="x")
            ttk.Button(btns, text="Copy", width=10, command=lambda: set_clipboard_text(body)).pack(side="left")
            ttk.Button(btns, text="Close", width=10, command=w.destroy).pack(side="right")
            # styles
            try:
                style = ttk.Style(self.root); style.theme_use("clam")
                w.configure(background="#1e1e1e"); frame.configure(style="Card.TFrame")
                style.configure("Card.TFrame", background="#1e1e1e")
                style.configure("TLabel", background="#1e1e1e", foreground="#ffffff")
                style.configure("TButton")
            except Exception: pass
            # position near cursor
            x, y = win32api.GetCursorPos(); self.root.update_idletasks(); w.update_idletasks()
            width = min(POPUP_WIDTH_PX + 20, w.winfo_reqwidth()); height = max(w.winfo_reqheight(), 120)
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            idx = len(self.popups); yoff = 20 + idx * (height + 10)
            px = min(max(x + 16, 0), sw - width - 8); py = min(max(y + 16 + yoff, 0), sh - height - 8)
            w.geometry(f"{width}x{height}+{px}+{py}"); self.popups.append(w)
            def on_destroy(_=None):
                if w in self.popups: self.popups.remove(w)
            w.bind("<Destroy>", on_destroy)
            # auto-close w/ hover pause
            state = {"inside": False}
            def arm():
                if not state["inside"]:
                    try: w.destroy(); log.debug("Popup auto-closed")
                    except Exception: pass
            def _on_enter(_event=None):
                state["inside"] = True
            def _on_leave(_event=None):
                state["inside"] = False
                w.after(POPUP_LIFETIME_MS, arm)
            w.bind("<Enter>", _on_enter)
            w.bind("<Leave>", _on_leave)
            w.after(POPUP_LIFETIME_MS, arm)
            # fade-in
            def fade(a=0.0):
                try:
                    a=min(a+0.12,1.0); w.attributes("-alpha", a)
                    if a<1.0: w.after(16, fade, a)
                except Exception: pass
            fade()
        except Exception: log_exc("PopupManager.show failed")

# --------------------------- Selection (Clipboard path hardened) ---------------------------
def _focused_hwnd_and_class_for_log():
    hwnd_fg, hwnd_focus, cls = _focused_hwnd_and_class()
    log.debug("Focus: hwnd_fg=0x%X hwnd_focus=0x%X class='%s'",
              int(hwnd_fg or 0), int(hwnd_focus or 0), cls)
    return hwnd_fg, hwnd_focus, cls

def _attempt_copy_via_wmcopy_and_sendinput(max_wait_ms=2000):
    """Try WM_COPY first; if no clipboard change, send Ctrl+C via SendInput with modifiers managed."""
    # Snapshot focus + keys
    _focused_hwnd_and_class_for_log()
    ks = {
        "SHIFT": _is_key_down(VK_SHIFT),
        "ALT":   _is_key_down(VK_MENU),
        "LWIN":  _is_key_down(VK_LWIN),
        "RWIN":  _is_key_down(VK_RWIN),
        "CTRL":  _is_key_down(VK_CONTROL),
    }
    log.debug("Key states before copy: %s", ks)

    seq_before = GetClipboardSequenceNumber()
    original = get_clipboard_text()

    # 1) WM_COPY directly to focused control (if any)
    _, hwnd_focus, _ = _focused_hwnd_and_class()
    changed = False
    if hwnd_focus:
        try:
            res = ctypes.c_ulong(0)
            ok = SendMessageTimeoutW(hwnd_focus, WM_COPY, 0, 0, SMTO_ABORTIFHUNG, 300, ctypes.byref(res))
            log.debug("WM_COPY to hwnd_focus -> ok=%d res=%d", ok, res.value)
        except Exception:
            log_exc("WM_COPY send failed")

        # poll for change quickly (~40% of budget)
        deadline = time.time() + (max_wait_ms / 1000.0) * 0.4
        while time.time() < deadline:
            if GetClipboardSequenceNumber() != seq_before:
                changed = True; break
            time.sleep(0.02)

    # 2) If still no change, use SendInput Ctrl+C, making sure Shift/Alt/Win are UP temporarily
    if not changed:
        lifted = []
        for vk, name in [(VK_SHIFT, "SHIFT"), (VK_MENU, "ALT"), (VK_LWIN, "LWIN"), (VK_RWIN, "RWIN")]:
            if _is_key_down(vk):
                log.debug("Temporarily releasing %s", name)
                _sendinput_key(vk, down=False)
                time.sleep(0.01)
                lifted.append(vk)

        ctrl_was_down = _is_key_down(VK_CONTROL)
        if not ctrl_was_down:
            _sendinput_key(VK_CONTROL, down=True); time.sleep(0.01)
        _sendinput_key(VK_C, down=True); time.sleep(0.005)
        _sendinput_key(VK_C, down=False); time.sleep(0.005)
        _sendinput_key(VK_CONTROL, down=False); time.sleep(0.01)
        if ctrl_was_down:
            _sendinput_key(VK_CONTROL, down=True)

        for vk in lifted:
            _sendinput_key(vk, down=True); time.sleep(0.005)

        # Poll for change (remaining ~60% budget)
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

# --------------------------- Main App ---------------------------
class App:
    def __init__(self):
        log.debug("App.__init__ starting (main thread=%s)", threading.current_thread().name)
        self.root = tk.Tk(); self.root.withdraw(); self.root.attributes("-alpha", 0.0)
        self.popup_mgr = PopupManager(self.root)

        self.tasks = queue.Queue()
        self.root.after(30, self._drain_tasks)

        self.hotkey_enabled = True
        self.hotkey_id = None
        self.hotkey_label = None
        self.hk_thread = threading.Thread(target=self._hotkey_loop, daemon=True, name="HotkeyThread")

        self.icon = pystray.Icon(
            APP_NAME,
            icon=make_tray_icon(),
            title=APP_NAME,
            menu=TrayMenu(
                Item(lambda item: f"Hotkey: {self.hotkey_label or '…'}", None, enabled=False),
                Item("Enable Hotkey", self._toggle_hotkey, checked=lambda item: self.hotkey_enabled),
                Item("Test Popup", self._test_popup),
                Item("Quit", self._quit)
            )
        )
        log.debug("Tray icon created")

    # Tray actions
    def _toggle_hotkey(self, _):
        self.hotkey_enabled = not self.hotkey_enabled
        log.info("Hotkey toggled -> %s", "ENABLED" if self.hotkey_enabled else "DISABLED")
        if self.hotkey_enabled and self.hotkey_id is None:
            self._register_hotkey()
        elif not self.hotkey_enabled and self.hotkey_id is not None:
            unregister_hotkey(self.hotkey_id); self.hotkey_id = None; self.hotkey_label = "(disabled)"
        try: self.icon.update_menu()
        except Exception: log_exc("icon.update_menu failed")

    def _test_popup(self, _):
        log.info("Test Popup clicked")
        self.popup_mgr.show("ClipLLM", "This is a test popup from the tray app.")

    def _quit(self, _):
        log.info("Quit requested")
        try:
            if self.hotkey_id is not None: unregister_hotkey(self.hotkey_id)
        except Exception: pass
        try: self.icon.stop()
        except Exception: pass
        try: self.root.quit()
        except Exception: pass

    # Hotkey thread
    def _register_hotkey(self):
        try:
            self.hotkey_id, self.hotkey_label = register_first_available()
        except SystemExit as e:
            self.hotkey_id, self.hotkey_label = None, "(none)"
            log.error("Hotkey registration failed: %s", e)
        try: self.icon.update_menu()
        except Exception: log_exc("icon.update_menu during registration failed")

    def _hotkey_loop(self):
        log.debug("Hotkey loop starting")
        self._register_hotkey()
        msg = wintypes.MSG()
        while True:
            try:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0: log.debug("GetMessageW -> WM_QUIT"); break
                if ret == -1: log.error("GetMessageW error: %d", ctypes.get_last_error()); time.sleep(0.2); continue
                if msg.message == WM_HOTKEY and self.hotkey_id and msg.wParam == self.hotkey_id and self.hotkey_enabled:
                    log.info("[hotkey] Triggered")
                    self.tasks.put(self._on_hotkey)
                user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
            except Exception: log_exc("Exception in hotkey loop"); time.sleep(0.25)

    # Selection capture (clipboard path, hardened)
    def _on_hotkey(self):
        log.debug("_on_hotkey entered")
        try:
            sel, original = _attempt_copy_via_wmcopy_and_sendinput(max_wait_ms=2000)
            if not sel:
                log.debug("No selection captured; not showing popup")
                return
            answer = ask_llm(sel)
            self.popup_mgr.show("LLM reply", answer)
            if original is not None:
                set_clipboard_text(original)
        except Exception: log_exc("_on_hotkey failed")

    # Tk task pump
    def _drain_tasks(self):
        try:
            while True:
                fn = self.tasks.get_nowait()
                try:
                    log.debug("Running task: %s", getattr(fn, "__name__", str(fn)))
                    fn()
                except Exception: log_exc("Task execution failed")
        except queue.Empty:
            pass
        self.root.after(30, self._drain_tasks)

    # Run
    def run(self):
        try:
            threading.Thread(target=self._run_tray, daemon=True, name="TrayThread").start()
            self.hk_thread.start()
            log.info("Tray + hotkey threads started. App is idle.")
            self.root.mainloop()
            log.info("Tk mainloop exited")
        except Exception: log_exc("App.run failed")

    def _run_tray(self):
        try:
            log.debug("Tray thread entering icon.run()")
            self.icon.run()
            log.debug("icon.run() returned")
        except Exception: log_exc("Tray thread crashed")

# --------------------------- Entrypoint ---------------------------
def _install_thread_excepthook():
    def hook(args):
        try:
            log.error("Unhandled exception in thread %s\n%s",
                      args.thread.name, "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception: pass
    try:
        threading.excepthook = hook
        log.debug("threading.excepthook installed")
    except Exception:
        log.debug("No threading.excepthook; skipping")

if __name__ == "__main__":
    _install_thread_excepthook()
    try:
        App().run()
    except Exception:
        log_exc("Fatal error in main"); raise
