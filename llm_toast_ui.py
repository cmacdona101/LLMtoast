"""
llm_toast_ui.py
UI layer (tray icon + minimal Tk toast) that uses llm_toast_core for logic.
Run this file to start the app.
"""

import threading, queue, time, ctypes
from ctypes import wintypes

import pystray
from pystray import MenuItem as Item, Menu as TrayMenu
from PIL import Image, ImageDraw
import tkinter as tk

import llm_toast_core as core
import llm_toast_llm as llm

import llm_toast_settings as settings

log = core.log  # shared logger
user32 = core.user32
WM_HOTKEY = core.WM_HOTKEY

APP_NAME = "ClipLLM Tray"
POPUP_WIDTH_PX = 360
POPUP_LIFETIME_MS = 8000

# -------- monitor positioning structs --------
class RECT(ctypes.Structure):
    _fields_ = [("left",   wintypes.LONG),
                ("top",    wintypes.LONG),
                ("right",  wintypes.LONG),
                ("bottom", wintypes.LONG)]

class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize",   wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork",    RECT),
                ("dwFlags",   wintypes.DWORD)]

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

MonitorFromPoint = user32.MonitorFromPoint
MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
MonitorFromPoint.restype  = wintypes.HMONITOR

GetMonitorInfoW = user32.GetMonitorInfoW
GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
GetMonitorInfoW.restype  = wintypes.BOOL

MONITOR_DEFAULTTONEAREST = 2

# --------------------------- UI helpers ---------------------------
def make_tray_icon(size=28):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((1, 3, size-2, size-5), radius=6, fill=(64, 128, 224, 255))
    d.polygon([(size//2 - 3, size-7), (size//2 + 3, size-7), (size//2, size-1)], fill=(64, 128, 224, 255))
    d.text((6, 7), "LL", fill=(255, 255, 255, 255))
    return img

class PopupManager:
    """A tiny, border-light toast that appears near the cursor and clamps to the active monitor."""
    def __init__(self, root):
        self.root = root
        self.popups = []

    def _get_cursor(self):
        # Use Win32 for global cursor (multi-monitor safe)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _place_on_active_monitor(self, w: tk.Toplevel, width: int, height: int):
        x, y = self._get_cursor()

        # Get monitor work area containing the cursor
        hmon = MonitorFromPoint(POINT(x=x, y=y), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not GetMonitorInfoW(hmon, ctypes.byref(mi)):
            # Fallback: don't clamp, just use cursor with small offset
            return x + 12, y + 12

        wx, wy = mi.rcWork.left, mi.rcWork.top
        ww, wh = mi.rcWork.right - mi.rcWork.left, mi.rcWork.bottom - mi.rcWork.top

        # Offset a bit from the cursor, then clamp inside work area
        px = min(max(x + 12, wx), wx + ww - width - 8)
        py = min(max(y + 12, wy), wy + wh - height - 8)
        return px, py

    def show(self, title: str, body: str):
        try:
            # Minimal, border-light toast
            bg = "#e0e0e0"
            fg_title = "#111111"
            fg_body  = "#222222"
            border   = "#d4d4d4"

            w = tk.Toplevel(self.root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            try: w.attributes("-alpha", 0.0)
            except Exception: pass

            # A simple frame with a 1px border
            frame = tk.Frame(w, bg=bg, highlightthickness=1, highlightbackground=border, bd=0, padx=8, pady=8)
            frame.pack(fill="both", expand=True)

            # Title + body (no buttons)
            title_lbl = tk.Label(frame, text=title, bg=bg, fg=fg_title, font=("Segoe UI", 10, "bold"), anchor="w", justify="left")
            title_lbl.pack(fill="x")
            body_lbl  = tk.Label(frame, text=body, bg=bg, fg=fg_body, wraplength=POPUP_WIDTH_PX, justify="left", font=("Segoe UI", 9))
            body_lbl.pack(fill="both", expand=True, pady=(4, 0))

            # Size + placement near cursor on the active monitor
            self.root.update_idletasks()
            w.update_idletasks()
            width  = min(POPUP_WIDTH_PX + 16, w.winfo_reqwidth())
            height = max(w.winfo_reqheight(), 80)

            px, py = self._place_on_active_monitor(w, width, height)
            w.geometry(f"{width}x{height}+{int(px)}+{int(py)}")

            # Track + auto-close with hover pause
            self.popups.append(w)
            def on_destroy(_=None):
                if w in self.popups: self.popups.remove(w)
            w.bind("<Destroy>", on_destroy)

            state = {"inside": False}
            def arm():
                if not state["inside"]:
                    try:
                        w.destroy()
                        log.debug("Popup auto-closed")
                    except Exception:
                        pass

            def _on_enter(_e=None): state["inside"] = True
            def _on_leave(_e=None):
                state["inside"] = False
                w.after(POPUP_LIFETIME_MS, arm)

            w.bind("<Enter>", _on_enter)
            w.bind("<Leave>", _on_leave)
            w.after(POPUP_LIFETIME_MS, arm)

            # Fade-in
            def fade(a=0.0):
                try:
                    a = min(a + 0.16, 1.0)
                    w.attributes("-alpha", a)
                    if a < 1.0:
                        w.after(14, fade, a)
                except Exception:
                    pass
            fade()

        except Exception:
            core.log_exc("PopupManager.show failed")
            
            
class ChatWindow:
    def __init__(self, root, center_cb):
        self.root = root
        self.center_cb = center_cb
        self.win = None
        self.out = None   # transcript (tk.Text)
        self.inp = None   # entry (tk.Entry)
        self.sending = False
        # Session id for GPT-5 Responses API; persists until window is closed
        self.prev_response_id = None

    def is_visible(self):
        return bool(self.win and self.win.winfo_exists() and self.win.state() != "withdrawn")

    def show(self):
        import tkinter as tk
        if self.is_visible():
            # Ensure the window and entry truly gain focus when re-opening
            self.win.deiconify()
            self.win.lift()
            try: self.win.focus_force()
            except Exception: pass
            if self.inp:
                self.inp.focus_set()
                self.inp.icursor("end")
            return
        
        
        w = tk.Toplevel(self.root)
        self.win = w
        w.title("ClipLLM Chat")
        w.resizable(True, False)
        w.attributes("-topmost", True)

        bg = "#efefef"; border = "#cfcfcf"
        frame = tk.Frame(w, bg=bg, padx=10, pady=10, highlightthickness=1, highlightbackground=border, bd=0)
        frame.pack(fill="both", expand=True)

        # Transcript area (Text + vertical Scrollbar) grouped in its own frame
        trans = tk.Frame(frame, bg=bg)
        trans.pack(fill="both", expand=True)
        self.out = tk.Text(trans, height=16, wrap="word", state="disabled")
        self.out.pack(side="left", fill="both", expand=True)
        scroll = tk.Scrollbar(trans, orient="vertical", command=self.out.yview)
        scroll.pack(side="right", fill="y")
        self.out.config(yscrollcommand=scroll.set)
        
        
        
        self.inp = tk.Entry(frame)
        self.inp.pack(fill="x", pady=(8,0))

        self.inp.bind("<Return>", self._on_enter)
        self.inp.bind("<Escape>", lambda e: self.hide())

        # position at center of active monitor
        w.update_idletasks()
        width = max(520, w.winfo_reqwidth())
        base_h = max(220, w.winfo_reqheight())
        height = int(base_h * 1.33)  # increase vertical size by ~33%
        x, y = self.center_cb(width, height)
        w.geometry(f"{width}x{height}+{int(x)}+{int(y)}")

        # Aggressively claim focus on the window and input (handles focus races)
        def _ensure_focus():
            if not self.win or not self.win.winfo_exists():
                return
            try:
                self.win.lift()
                self.win.focus_force()
            except Exception:
                pass
            if self.inp and self.inp.winfo_exists():
                try:
                    self.inp.focus_set()
                    self.inp.icursor("end")
                except Exception:
                    pass
        # Schedule a few times to win against tray/hotkey focus timing
        self.root.after(0, _ensure_focus)
        self.root.after(25, _ensure_focus)
        self.root.after(100, _ensure_focus)
        self.root.after(200, _ensure_focus)
        
        
        
        
        # Clear session only when the window is actually closed via the titlebar
        def _on_close():
            self.prev_response_id = None
            try:
                w.destroy()
            except Exception:
                pass
        w.protocol("WM_DELETE_WINDOW", _on_close)

    def hide(self):
        if self.win and self.win.winfo_exists():
            self.win.withdraw()
        # Do NOT clear prev_response_id here; keep session across hides

    
    
    
    def _append(self, who: str, text: str):
        if not self.out: return
        self.out.config(state="normal")
        self.out.insert("end", f"{who}: {text}\n")
        self.out.see("end")
        self.out.config(state="disabled")

    def _on_enter(self, _evt=None):
        if self.sending or not self.inp: return "break"
        msg = self.inp.get().strip()
        if not msg: return "break"
        self.inp.delete(0, "end")
        self._append("You", msg)
        self.sending = True
        self.inp.config(state="disabled")
        threading.Thread(target=self._send_worker, args=(msg,), daemon=True).start()
        return "break"

    def _send_worker(self, msg: str):
        try:
            reply, rid = llm.chat(msg, prev_response_id=self.prev_response_id)
            if rid:
                self.prev_response_id = rid
        except Exception as e:
            reply = f"Error: {e}"
            
        #print(reply)

        def back():
            if not self.win or not self.win.winfo_exists():
                return
            self._append("Assistant", reply)
            self.inp.config(state="normal")
            self.inp.focus_set()
            self.sending = False
        self.root.after(0, back)
            
          
            

            
            

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
        
        # Chat hotkey (distinct id; choose a combo unlikely to conflict)
        self.chat_hotkey_id = 1002
        self.chat_hotkey_label = "Ctrl+Alt+M"
        self.chat = ChatWindow(self.root, center_cb=self._center_on_active_monitor)

        # Tray
        self.icon = pystray.Icon(
            APP_NAME,
            icon=make_tray_icon(),
            title=APP_NAME,

            menu=TrayMenu(
                Item(lambda i: f"Hotkey: {self.hotkey_label or '…'}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                Item("Open Chat", self._toggle_chat),
                Item("Options...", self._open_options),
                Item("Enable Hotkey", self._toggle_hotkey, checked=lambda i: self.hotkey_enabled),
                Item("Quit", self._quit)
            )


        )

    # Tray actions
    def _toggle_hotkey(self, icon=None, item=None):
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

#    def _test_popup(self, _):
#        log.info("Test Popup clicked")
#        self.popup_mgr.show("ClipLLM", "This is a minimal toast. No buttons, light border, light gray background.")


    def _open_options(self, icon=None, item=None):
        # Single-instance Options window
        if getattr(self, "_options_win", None) and self._options_win.winfo_exists():
            self._options_win.deiconify(); self._options_win.lift(); self._options_win.focus_force(); return
    
        w = tk.Toplevel(self.root)
        self._options_win = w
        w.title("ClipLLM Options")
        w.resizable(False, False)
    
        bg = "#efefef"; border = "#cfcfcf"
        frame = tk.Frame(w, bg=bg, padx=12, pady=12, highlightthickness=1, highlightbackground=border, bd=0)
        frame.pack(fill="both", expand=True)
    
        tk.Label(frame, text="LLM API key", bg=bg).grid(row=0, column=0, sticky="w")
        api_entry = tk.Entry(frame, width=46, show="•")
        api_entry.grid(row=1, column=0, sticky="ew", pady=(4, 8))
    
        # Status + actions
        status = tk.Label(frame, text="", bg=bg, fg="#666666")
        status.grid(row=2, column=0, sticky="w", pady=(0, 8))
    
        btn_row = tk.Frame(frame, bg=bg)
        btn_row.grid(row=3, column=0, sticky="ew")
        btn_row.columnconfigure(0, weight=1)
    
        def refresh_status():
            has = settings.get_api_key() is not None
            status.config(text=("An API key is stored." if has else "No API key stored."))
    
        def on_save():
            val = api_entry.get().strip()
            if not val:
                status.config(text="Enter a key to save.", fg="#B00020"); return
            try:
                settings.set_api_key(val)
                api_entry.delete(0, "end")
                status.config(text="Saved.", fg="#107c10")
            except Exception:
                status.config(text="Save failed. See log.", fg="#B00020")
            finally:
                # Do not log the key
                pass
    
        def on_clear():
            try:
                settings.delete_api_key()
                status.config(text="Cleared.", fg="#666666")
            except Exception:
                status.config(text="Clear failed. See log.", fg="#B00020")
    
        save_btn  = tk.Button(btn_row, text="Save",  width=10, command=on_save)
        clear_btn = tk.Button(btn_row, text="Clear", width=10, command=on_clear)
        close_btn = tk.Button(btn_row, text="Close", width=10, command=w.destroy)
    
        # Layout buttons: Save | Clear          Close
        save_btn.grid(row=0, column=0, sticky="w")
        clear_btn.grid(row=0, column=0, sticky="w", padx=(76, 0))
        close_btn.grid(row=0, column=0, sticky="e")
    
        frame.columnconfigure(0, weight=1)
    
        # Center on active monitor (reuses your helper)
        w.update_idletasks()
        width, height = max(380, w.winfo_reqwidth()), w.winfo_reqheight()
        px, py = self._center_on_active_monitor(width, height)
        w.geometry(f"{width}x{height}+{int(px)}+{int(py)}")
    
        w.protocol("WM_DELETE_WINDOW", w.destroy)
        refresh_status()






    def _center_on_active_monitor(self, width: int, height: int):
        # Center relative to the monitor containing the cursor
        pt = POINT(); user32.GetCursorPos(ctypes.byref(pt))
        hmon = MonitorFromPoint(POINT(x=pt.x, y=pt.y), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
        if GetMonitorInfoW(hmon, ctypes.byref(mi)):
            wx, wy = mi.rcWork.left, mi.rcWork.top
            ww, wh = mi.rcWork.right - mi.rcWork.left, mi.rcWork.bottom - mi.rcWork.top
            return wx + (ww - width)//2, wy + (wh - height)//2
        return pt.x + 20, pt.y + 20

    def _quit(self, icon=None, item=None):
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
            
            
        # Also register the chat hotkey (Ctrl+Alt+M)
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
        VK_M = 0x4D
        if user32.RegisterHotKey(None, self.chat_hotkey_id, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_M):
            log.info("[hotkey] Registered (chat): %s (id=%d)", self.chat_hotkey_label, self.chat_hotkey_id)
        else:
            err = ctypes.get_last_error()
            log.warning("[hotkey] Could not register chat hotkey %s (err=%d)", self.chat_hotkey_label, err)
            ctypes.set_last_error(0)
            
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
                    
                    
                elif msg.message == WM_HOTKEY and msg.wParam == self.chat_hotkey_id and self.hotkey_enabled:
                    log.info("[hotkey] Chat triggered")
                    self.tasks.put(self._toggle_chat)
                    
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            except Exception:
                core.log_exc("Exception in hotkey loop")
                time.sleep(0.25)

    # Selection capture on Tk thread
    def _on_hotkey(self):
        log.debug("_on_hotkey (UI) entered")
        try:
            sel, original = core.attempt_copy_via_wmcopy_and_sendinput(max_wait_ms=500)
            if not sel:
                log.debug("No selection captured; no popup")
                return
            answer = core.ask_llm(sel)
            self.popup_mgr.show("LLM reply", answer)
            if original is not None:
                core.set_clipboard_text(original)
        except Exception:
            core.log_exc("_on_hotkey failed in UI")
            
    def _toggle_chat(self, icon=None, item=None):
        if self.chat.is_visible():
            self.chat.hide()
        else:
            self.chat.show()

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
