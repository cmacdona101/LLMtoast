"""
llm_toast_ui.py
UI layer (tray icon + Tk popup) that uses llm_toast_core for the logic.
Run this file to start the app.
"""

import threading, queue, time, ctypes
from ctypes import wintypes

import pystray
from pystray import MenuItem as Item, Menu as TrayMenu
from PIL import Image, ImageDraw
import tkinter as tk
from tkinter import ttk

import llm_toast_core as core

log = core.log  # use the shared logger
user32 = core.user32
WM_HOTKEY = core.WM_HOTKEY

APP_NAME = "ClipLLM Tray"
POPUP_WIDTH_PX = 420
POPUP_LIFETIME_MS = 6000

# --------------------------- UI helpers ---------------------------
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
            ttk.Button(btns, text="Copy", width=10, command=lambda: core.set_clipboard_text(body)).pack(side="left")
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
            import win32api  # safe here; only used to position window
            x, y = win32api.GetCursorPos()
            self.root.update_idletasks(); w.update_idletasks()
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
            def _on_enter(_event=None): state["inside"] = True
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
        except Exception:
            core.log_exc("PopupManager.show failed")

# --------------------------- App (UI) ---------------------------
class App:
    def __init__(self):
        log.debug("UI App.__init__ (main thread)")
        # Tk on main thread
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.attributes("-alpha", 0.0)

        self.popup_mgr = PopupManager(self.root)

        # Task queue for moving work to Tk thread
        self.tasks = queue.Queue()
        self.root.after(30, self._drain_tasks)

        # Hotkey
        self.hotkey_enabled = True
        self.hotkey_id = None
        self.hotkey_label = None
        self.hk_thread = threading.Thread(target=self._hotkey_loop, daemon=True, name="HotkeyThread")

        # Tray
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

    # Tray actions
    def _toggle_hotkey(self, _):
        self.hotkey_enabled = not self.hotkey_enabled
        log.info("Hotkey toggled -> %s", "ENABLED" if self.hotkey_enabled else "DISABLED")
        if self.hotkey_enabled and self.hotkey_id is None:
            self._register_hotkey()
        elif not self.hotkey_enabled and self.hotkey_id is not None:
            core.unregister_hotkey(self.hotkey_id)
            self.hotkey_id = None
            self.hotkey_label = "(disabled)"
        try:
            self.icon.update_menu()
        except Exception:
            core.log_exc("icon.update_menu failed")

    def _test_popup(self, _):
        log.info("Test Popup clicked")
        self.popup_mgr.show("ClipLLM", "This is a test popup from the tray app.")

    def _quit(self, _):
        log.info("Quit requested")
        try:
            if self.hotkey_id is not None:
                core.unregister_hotkey(self.hotkey_id)
        except Exception:
            pass
        try:
            self.icon.stop()
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass

    # Hotkey thread
    def _register_hotkey(self):
        try:
            self.hotkey_id, self.hotkey_label = core.register_first_available()
        except SystemExit as e:
            self.hotkey_id, self.hotkey_label = None, "(none)"
            log.error("Hotkey registration failed: %s", e)
        try:
            self.icon.update_menu()
        except Exception:
            core.log_exc("icon.update_menu during registration failed")

    def _hotkey_loop(self):
        log.debug("Hotkey loop starting (UI)")
        self._register_hotkey()
        msg = wintypes.MSG()
        while True:
            try:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:
                    log.debug("GetMessageW -> WM_QUIT"); break
                if ret == -1:
                    log.error("GetMessageW error: %d", ctypes.get_last_error())
                    time.sleep(0.2)
                    continue
                if msg.message == WM_HOTKEY and self.hotkey_id and msg.wParam == self.hotkey_id and self.hotkey_enabled:
                    log.info("[hotkey] Triggered")
                    self.tasks.put(self._on_hotkey)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            except Exception:
                core.log_exc("Exception in hotkey loop")
                time.sleep(0.25)

    # Selection capture on Tk thread
    def _on_hotkey(self):
        log.debug("_on_hotkey (UI) entered")
        try:
            sel, original = core.attempt_copy_via_wmcopy_and_sendinput(max_wait_ms=2000)
            if not sel:
                log.debug("No selection captured; no popup")
                return
            answer = core.ask_llm(sel)
            self.popup_mgr.show("LLM reply", answer)
            if original is not None:
                core.set_clipboard_text(original)
        except Exception:
            core.log_exc("_on_hotkey failed in UI")

    # Tk task pump
    def _drain_tasks(self):
        try:
            while True:
                fn = self.tasks.get_nowait()
                try:
                    log.debug("Running task: %s", getattr(fn, "__name__", str(fn)))
                    fn()
                except Exception:
                    core.log_exc("Task execution failed")
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
        except Exception:
            core.log_exc("UI App.run failed")

    def _run_tray(self):
        try:
            log.debug("Tray thread entering icon.run()")
            self.icon.run()
            log.debug("icon.run() returned")
        except Exception:
            core.log_exc("Tray thread crashed")

# --------------------------- Entrypoint ---------------------------
def _install_thread_excepthook():
    def hook(args):
        try:
            log.error("Unhandled exception in thread %s\n%s",
                      args.thread.name,
                      "".join(__import__("traceback").format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass
    try:
        import threading
        threading.excepthook = hook
        log.debug("threading.excepthook installed (UI)")
    except Exception:
        log.debug("No threading.excepthook; skipping (UI)")

if __name__ == "__main__":
    _install_thread_excepthook()
    App().run()
