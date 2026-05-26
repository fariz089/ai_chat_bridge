"""
AI Chat Bridge — GUI untuk capture session ChatGPT & Grok,
lalu chat via browser, dan expose sebagai OpenAI-compatible API.

v1.2 conversation continuity:
  - Browser tetap nyala antar pesan. Pesan kedua dst lanjut di chat yang sama
    → Grok/ChatGPT ingat konteks sebelumnya.
  - 🔄 New Chat — paksa mulai chat baru
  - 🛑 Close Browser — tutup browser (free RAM); auto re-launch saat send

v1.2.1 BridgeWorker fix:
  - Sebelumnya pool dibuat di thread pertama tapi dipakai dari thread send
    berikutnya → greenlet.error: cannot switch to a different thread.
  - Sekarang semua Playwright ops jalan di satu BridgeWorker thread,
    GUI submit lewat queue. Multi-send aman.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

from platforms import PLATFORMS, CaptureResult

try:
    from extension_server import run_server_in_thread as run_extension_server
    EXTENSION_SERVER_IMPORT_ERROR = None
except Exception as _ext_err:
    run_extension_server = None
    EXTENSION_SERVER_IMPORT_ERROR = str(_ext_err)

EXTENSION_SERVER_PORT = 5098

BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
MEDIA_DIR = BASE_DIR / "media"
CONFIG_PATH = BASE_DIR / "ai_chat_bridge_config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".txt", ".md", ".csv", ".json"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}


def _file_icon(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS: return "🖼"
    if ext in VIDEO_EXTS: return "🎬"
    if ext in AUDIO_EXTS: return "🎵"
    if ext in DOC_EXTS:   return "📄"
    return "📎"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except:
            pass
    return {"api_port": 5100, "api_key": "", "last_labels": {}}


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logging.warning(f"Save config failed: {e}")


class AIChatBridgeApp:
    PLATFORM_DISPLAY = {"chatgpt": "ChatGPT", "grok": "Grok"}
    PLATFORM_NOTES = {
        "chatgpt": "Login ke chatgpt.com → session cookie disimpan → bisa chat via API.",
        "grok": "Login ke grok.com (pakai akun X/Twitter) → session cookie disimpan → bisa chat via API.",
    }
    REQUIRED_COOKIES_BY_PLATFORM = {
        "chatgpt": ("__Secure-next-auth.session-token",),
        "grok": ("sso",),
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.current_capture_thread = None
        self.current_finished_event = None
        self.log_queue: queue.Queue = queue.Queue()
        self._ext_server_status = "disabled"
        self._api_process = None

        # Pending attachments staged for the next message
        self.pending_attachments: list[Path] = []
        # Image references (PhotoImage) for inline thumbnails — keep alive
        self._chat_images: list = []

        # Per-platform bridge workers — each platform gets its OWN worker so
        # ChatGPT and Grok browsers can run SIMULTANEOUSLY without needing to
        # close one before opening the other.
        self._bridges: dict[str, object] = {"chatgpt": None, "grok": None}
        self._bridge_headless: dict[str, bool] = {"chatgpt": None, "grok": None}
        self._chat_busy: dict[str, bool] = {"chatgpt": False, "grok": False}
        # Keep legacy alias so _poll_browser_status still works during refactor
        self._bridge = None  # unused — kept to avoid AttributeError on old refs

        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        self._start_extension_server()
        self._build_ui()
        self._poll_log_queue()
        self._poll_session_files()
        self._poll_browser_status()

        # Make sure browser is closed when window is closed
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        for bridge in self._bridges.values():
            try:
                if bridge:
                    bridge.shutdown(wait=True, timeout=5)
            except Exception:
                pass
        try:
            if self._api_process:
                self._api_process.terminate()
        except Exception:
            pass
        self.root.destroy()

    def _poll_session_files(self):
        try:
            for pk, widgets in self.platform_widgets.items():
                self._refresh_session_info(pk, widgets)
        except:
            pass
        finally:
            self.root.after(2000, self._poll_session_files)

    def _poll_browser_status(self):
        """Update browser status indicators for both platforms."""
        try:
            for pk in ("chatgpt", "grok"):
                bridge = self._bridges.get(pk)
                var = getattr(self, f"chat_status_var_{pk}", None)
                lbl = getattr(self, f"chat_status_lbl_{pk}", None)
                if var is None:
                    continue
                if bridge is None:
                    var.set("● not started")
                    if lbl:
                        lbl.configure(foreground="#888")
                else:
                    active = bridge.list_active_sessions()
                    if not active:
                        var.set("● closed (will re-launch on next send)")
                        if lbl:
                            lbl.configure(foreground="#888")
                    else:
                        parts = []
                        for s in active:
                            url = s.get("url") or ""
                            if len(url) > 50:
                                url = url[:47] + "..."
                            parts.append(f"● msgs={s['message_count']}  {url}")
                        var.set("  |  ".join(parts))
                        if lbl:
                            lbl.configure(foreground="#73d13d")
        except Exception:
            pass
        finally:
            self.root.after(1500, self._poll_browser_status)

    def _start_extension_server(self):
        if run_extension_server is None:
            self._ext_server_status = "error"
            self._ext_server_error = f"Flask not installed ({EXTENSION_SERVER_IMPORT_ERROR}). pip install flask"
            return
        try:
            run_extension_server(sessions_dir=SESSIONS_DIR, port=EXTENSION_SERVER_PORT,
                                 host="127.0.0.1",
                                 on_session_saved=self._on_extension_session_saved)
            self._ext_server_status = "running"
            self._ext_server_error = None
        except Exception as e:
            self._ext_server_status = "error"
            self._ext_server_error = str(e)

    def _on_extension_session_saved(self, platform, label, path):
        def _refresh():
            self._enqueue_log(f"[ext] ✓ Captured {platform}/{label} from Chrome extension → {path.name}")
            if (self.current_capture_thread and self.current_capture_thread.is_alive()
                and self.current_finished_event and not self.current_finished_event.is_set()):
                self.current_finished_event.set()
            widgets = self.platform_widgets.get(platform)
            if widgets:
                widgets["label_var"].set(label)
                self._refresh_session_info(platform, widgets)
                try:
                    idx = list(self.PLATFORM_DISPLAY.keys()).index(platform)
                    self.capture_notebook.select(idx)
                except:
                    pass
        try:
            self.root.after(0, _refresh)
        except:
            pass

    def _build_ui(self):
        self.root.title("AI Chat Bridge — ChatGPT & Grok → OpenAI API (v1.2.1)")
        self.root.geometry("1200x720")
        self.root.minsize(1000, 600)

        # ── Modern Dark Theme ──────────────────────────────────────
        BG        = "#1a1b1e"      # main background
        BG2       = "#25262b"      # card / frame background
        BG3       = "#2c2e33"      # input / entry background
        BG_HOVER  = "#373a40"      # hover state
        FG        = "#c1c2c5"      # main text
        FG_DIM    = "#909296"      # muted text
        FG_BRIGHT = "#e9ecef"      # bright text
        ACCENT    = "#339af0"      # blue accent
        GREEN     = "#51cf66"      # success
        RED       = "#ff6b6b"      # error
        YELLOW    = "#fcc419"      # warning
        BORDER    = "#373a40"      # borders
        TAB_SEL   = "#339af0"      # selected tab accent

        self.root.configure(bg=BG)

        style = ttk.Style()
        style.theme_use("clam")

        # Global defaults
        style.configure(".", background=BG, foreground=FG, borderwidth=0,
                        font=("Segoe UI", 10), fieldbackground=BG3,
                        insertcolor=FG_BRIGHT, troughcolor=BG2,
                        selectbackground=ACCENT, selectforeground="#fff")

        # Frame
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=BG2, relief="flat")

        # Label
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Title.TLabel", background=BG, foreground=FG_BRIGHT,
                        font=("Segoe UI Semibold", 11))
        style.configure("Dim.TLabel", background=BG, foreground=FG_DIM,
                        font=("Segoe UI", 9))
        style.configure("Success.TLabel", foreground=GREEN)
        style.configure("Error.TLabel", foreground=RED)

        # Button — rounded modern look
        style.configure("TButton", background=BG3, foreground=FG_BRIGHT,
                        padding=(12, 6), borderwidth=1, relief="flat",
                        font=("Segoe UI", 10))
        style.map("TButton",
                  background=[("active", BG_HOVER), ("pressed", ACCENT)],
                  foreground=[("active", FG_BRIGHT), ("pressed", "#fff")],
                  relief=[("pressed", "flat")])

        style.configure("Accent.TButton", background=ACCENT, foreground="#fff",
                        font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton",
                  background=[("active", "#228be6"), ("pressed", "#1c7ed6")])

        # Entry
        style.configure("TEntry", fieldbackground=BG3, foreground=FG_BRIGHT,
                        insertcolor=FG_BRIGHT, borderwidth=1, relief="flat",
                        padding=(8, 6))
        style.map("TEntry",
                  fieldbackground=[("focus", BG_HOVER)],
                  bordercolor=[("focus", ACCENT)])

        # Combobox
        style.configure("TCombobox", fieldbackground=BG3, foreground=FG_BRIGHT,
                        selectbackground=ACCENT, padding=(8, 4))
        style.map("TCombobox",
                  fieldbackground=[("readonly", BG3)],
                  foreground=[("readonly", FG_BRIGHT)])

        # Checkbutton
        style.configure("TCheckbutton", background=BG, foreground=FG,
                        indicatorcolor=BG3, font=("Segoe UI", 10))
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  indicatorcolor=[("selected", ACCENT)])

        # Radiobutton
        style.configure("TRadiobutton", background=BG, foreground=FG,
                        indicatorcolor=BG3, font=("Segoe UI", 10))
        style.map("TRadiobutton",
                  indicatorcolor=[("selected", ACCENT)])

        # Notebook (tabs)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG2, foreground=FG_DIM,
                        padding=(16, 8), font=("Segoe UI Semibold", 10),
                        borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", BG), ("active", BG_HOVER)],
                  foreground=[("selected", ACCENT), ("active", FG_BRIGHT)],
                  expand=[("selected", (0, 0, 0, 2))])

        # LabelFrame
        style.configure("TLabelframe", background=BG2, foreground=FG_DIM,
                        borderwidth=1, relief="flat",
                        labeloutside=False)
        style.configure("TLabelframe.Label", background=BG2, foreground=FG_DIM,
                        font=("Segoe UI Semibold", 9))

        # Scrollbar
        style.configure("Vertical.TScrollbar", background=BG2,
                        troughcolor=BG, gripcount=0, borderwidth=0,
                        arrowsize=0)
        style.map("Vertical.TScrollbar",
                  background=[("active", BG_HOVER)])

        # Progressbar
        style.configure("Horizontal.TProgressbar", background=ACCENT,
                        troughcolor=BG2, borderwidth=0)

        # Store theme colors for use elsewhere
        self._theme = {
            "bg": BG, "bg2": BG2, "bg3": BG3, "bg_hover": BG_HOVER,
            "fg": FG, "fg_dim": FG_DIM, "fg_bright": FG_BRIGHT,
            "accent": ACCENT, "green": GREEN, "red": RED,
            "yellow": YELLOW, "border": BORDER,
        }

        self.main_notebook = ttk.Notebook(self.root)
        self.main_notebook.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        capture_tab = ttk.Frame(self.main_notebook, padding=12)
        self.main_notebook.add(capture_tab, text="  Sessions  ")

        # Extension server status — only relevant for Grok
        ext_frame = ttk.LabelFrame(capture_tab, text="Chrome Extension (Grok only)", padding=8)
        ext_frame.pack(fill="x", pady=(0, 8))
        if self._ext_server_status == "running":
            st = f"✓ Extension server running — http://127.0.0.1:{EXTENSION_SERVER_PORT}"
            sc = GREEN
        else:
            st = f"⚠ {getattr(self, '_ext_server_error', 'disabled')}"
            sc = YELLOW
        ttk.Label(ext_frame, text=st, foreground=sc, wraplength=780).pack(anchor="w")

        self.capture_notebook = ttk.Notebook(capture_tab)
        self.capture_notebook.pack(fill="both", expand=True, pady=4)
        self.platform_widgets = {}
        for pk, dn in self.PLATFORM_DISPLAY.items():
            tab = ttk.Frame(self.capture_notebook, padding=12)
            self.capture_notebook.add(tab, text=f"  {dn}  ")
            self.platform_widgets[pk] = self._build_platform_tab(tab, pk)

        chat_tab = ttk.Frame(self.main_notebook, padding=12)
        self.main_notebook.add(chat_tab, text="  Chat  ")
        self._build_chat_tab(chat_tab)

        api_tab = ttk.Frame(self.main_notebook, padding=12)
        self.main_notebook.add(api_tab, text="  API Server  ")
        self._build_api_tab(api_tab)

        chrome_tab = ttk.Frame(self.main_notebook, padding=12)
        self.main_notebook.add(chrome_tab, text="  Real Chrome  ")
        self._build_chrome_tab(chrome_tab)

        # Log panel
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=6)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(4, 8))
        log_inner = ttk.Frame(log_frame)
        log_inner.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_inner, height=5, wrap="word",
                                background=BG3, foreground=FG,
                                insertbackground=FG_BRIGHT,
                                font=("Cascadia Code", 9),
                                relief="flat", bd=0,
                                selectbackground=ACCENT,
                                selectforeground="#fff",
                                padx=8, pady=6)
        scrollbar = ttk.Scrollbar(log_inner, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_config("info", foreground=FG)
        self.log_text.tag_config("success", foreground=GREEN)
        self.log_text.tag_config("warn", foreground=YELLOW)
        self.log_text.tag_config("error", foreground=RED)
        self.log_text.tag_config("muted", foreground=FG_DIM)
        self._log("AI Chat Bridge v1.2.1 ready. Browser stays open across messages = same chat continues.", "muted")

    def _build_platform_tab(self, parent, pk):
        ttk.Label(parent, text=self.PLATFORM_NOTES[pk], foreground="#888",
                  wraplength=720).pack(anchor="w", pady=(0, 6))

        badge_frame = ttk.Frame(parent)
        badge_frame.pack(fill="x", pady=(0, 4))
        ttk.Label(badge_frame, text="Status: ", font=("Segoe UI", 10, "bold")).pack(side="left")
        status_badge = tk.Label(badge_frame, text="✗ NO SESSION", foreground="#909296",
                                background="#2c2e33", font=("Segoe UI Semibold", 10), padx=10, pady=3)
        status_badge.pack(side="left")

        label_frame = ttk.Frame(parent)
        label_frame.pack(fill="x", pady=4)
        ttk.Label(label_frame, text="Label akun:").pack(side="left", padx=(0, 8))
        last_label = self.cfg.get("last_labels", {}).get(pk, "")
        label_var = tk.StringVar(value=last_label)
        ttk.Entry(label_frame, textvariable=label_var, width=30).pack(side="left", fill="x", expand=True)

        info_var = tk.StringVar(value="(no session yet)")
        ttk.Label(parent, textvariable=info_var, foreground="#aaa",
                  font=("Segoe UI", 9, "italic"), wraplength=720).pack(anchor="w", pady=(4, 4))

        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill="x", pady=6)
        login_btn = ttk.Button(btn_frame, text="🌐 Open Browser & Login",
                               command=lambda: self._start_capture(pk))
        login_btn.pack(side="left", padx=(0, 6))
        confirm_btn = ttk.Button(btn_frame, text="✓ I'm Logged In",
                                 command=self._confirm_login, state="disabled")
        confirm_btn.pack(side="left", padx=6)
        export_btn = ttk.Button(btn_frame, text="💾 Export",
                                command=lambda: self._export_session(pk), state="disabled")
        export_btn.pack(side="left", padx=6)

        widgets = {"label_var": label_var, "info_var": info_var, "status_badge": status_badge,
                   "login_btn": login_btn, "confirm_btn": confirm_btn, "export_btn": export_btn}
        self._refresh_session_info(pk, widgets)
        label_var.trace_add("write", lambda *_: self._refresh_session_info(pk, widgets))
        return widgets

    def _build_chat_tab(self, parent):
        """Build Chat tab with two sub-tabs: ChatGPT and Grok (run simultaneously)."""
        # Shared options bar (headless, debug, CDP apply to both)
        opts_bar = ttk.Frame(parent)
        opts_bar.pack(fill="x", pady=(0, 6))

        self.chat_headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts_bar, text="Headless", variable=self.chat_headless_var).pack(
            side="left", padx=(0, 6))
        self.chat_debug_phases_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_bar, text="Debug screenshots",
                        variable=self.chat_debug_phases_var).pack(side="left", padx=(0, 8))
        self.chat_cdp_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts_bar, text="CDP", variable=self.chat_cdp_var,
                        command=self._on_cdp_toggle).pack(side="left", padx=(0, 2))
        self.chat_cdp_port_var = tk.StringVar(value="9222")
        ttk.Entry(opts_bar, textvariable=self.chat_cdp_port_var, width=5).pack(
            side="left", padx=(0, 8))
        ttk.Label(opts_bar, text="(Both browsers run simultaneously)",
                  foreground="#888", font=("Segoe UI", 9, "italic")).pack(side="left")

        # Imagine options (Grok-only, shown when enabled)
        self.imagine_enabled_var = tk.BooleanVar(value=False)
        self.imagine_frame = ttk.Frame(parent)
        self.imagine_opts_frame = ttk.Frame(self.imagine_frame)
        self.imagine_opts_frame.pack(fill="x")
        ttk.Label(self.imagine_opts_frame, text="Mode:").pack(side="left", padx=(0, 4))
        self.imagine_mode_var = tk.StringVar(value="image")
        mode_combo = ttk.Combobox(self.imagine_opts_frame, textvariable=self.imagine_mode_var,
                                   values=["image", "video"], width=7, state="readonly")
        mode_combo.pack(side="left", padx=(0, 10))
        mode_combo.bind("<<ComboboxSelected>>", lambda e: self._on_imagine_mode_changed())
        ttk.Label(self.imagine_opts_frame, text="Aspect:").pack(side="left", padx=(0, 4))
        self.imagine_aspect_var = tk.StringVar(value="9:16")
        aspect_combo = ttk.Combobox(self.imagine_opts_frame, textvariable=self.imagine_aspect_var,
                                     values=["2:3 (Tall)", "3:2 (Wide)", "1:1 (Square)",
                                             "9:16 (Vertical)", "16:9 (Widescreen)"],
                                     width=14, state="readonly")
        aspect_combo.pack(side="left", padx=(0, 10))
        aspect_combo.set("9:16 (Vertical)")
        ttk.Label(self.imagine_opts_frame, text="Quality:").pack(side="left", padx=(0, 4))
        self.imagine_res_var = tk.StringVar(value="720p")
        ttk.Radiobutton(self.imagine_opts_frame, text="Speed",
                        variable=self.imagine_res_var, value="480p").pack(side="left", padx=(0, 2))
        ttk.Radiobutton(self.imagine_opts_frame, text="Quality",
                        variable=self.imagine_res_var, value="720p").pack(side="left", padx=(0, 10))
        self.imagine_dur_var = tk.StringVar(value="6s")
        self.imagine_quality_row = ttk.Frame(self.imagine_opts_frame)
        self.imagine_video_row = ttk.Frame(self.imagine_opts_frame)
        self._on_imagine_mode_changed()

        # Sub-notebook: one tab per platform
        self.chat_platform_notebook = ttk.Notebook(parent)
        self.chat_platform_notebook.pack(fill="both", expand=True)

        self._platform_panels = {}
        platform_configs = [
            ("chatgpt", "🟢 ChatGPT", "#10a37f"),
            ("grok",    "⚡ Grok",    "#1d9bf0"),
        ]
        for pk, tab_label, accent in platform_configs:
            pf = ttk.Frame(self.chat_platform_notebook, padding=6)
            self.chat_platform_notebook.add(pf, text=f"  {tab_label}  ")
            self._build_platform_chat_panel(pf, pk, accent)

        # Keep compat aliases pointing to Grok (legacy batch_zip etc.)
        self.chat_platform_var = tk.StringVar(value="grok")
        self.chat_label_var = self._platform_panels["grok"]["label_var"]
        self.chat_input = self._platform_panels["grok"]["input"]
        self.send_btn = self._platform_panels["grok"]["send_btn"]
        self.batch_btn = self._platform_panels["grok"]["batch_btn"]
        self.attach_list_frame = self._platform_panels["grok"]["attach_list_frame"]
        self.chat_display = self._platform_panels["grok"]["display"]
        self.chat_status_var = self._platform_panels["grok"]["status_var"]
        self.chat_status_lbl = self._platform_panels["grok"]["status_lbl"]

        # Pending attachments are shared (user chooses target platform via active tab)
        self.pending_attachments: list[Path] = []
        self._refresh_attachment_bar()

    def _build_platform_chat_panel(self, parent, pk: str, accent: str):
        """Build one platform's chat panel (Recent chats sidebar + chat area)."""
        # Main horizontal split: sidebar (recent chats) + chat area
        main_split = ttk.Frame(parent)
        main_split.pack(fill="both", expand=True)

        # ── LEFT: Recent Chats sidebar ──────────────────────────────────
        sidebar = ttk.LabelFrame(main_split, text="Recent Chats", padding=4)
        sidebar.pack(side="left", fill="y", padx=(0, 6), pady=0)
        sidebar.configure(width=200)
        sidebar.pack_propagate(False)

        recent_listbox = tk.Listbox(sidebar, width=24, font=("Segoe UI", 9),
                                     background="#16213e", foreground="#c1c2c5",
                                     selectbackground=accent, selectforeground="#fff",
                                     relief="flat", bd=0, activestyle="none",
                                     highlightthickness=0)
        recent_scroll = ttk.Scrollbar(sidebar, command=recent_listbox.yview)
        recent_listbox.configure(yscrollcommand=recent_scroll.set)
        recent_scroll.pack(side="right", fill="y")
        recent_listbox.pack(side="left", fill="both", expand=True)

        btn_row = ttk.Frame(sidebar)
        btn_row.pack(fill="x", pady=(4, 0))
        refresh_btn = ttk.Button(btn_row, text="🔄 Refresh",
                                  command=lambda: self._fetch_recent_chats(pk, recent_listbox))
        refresh_btn.pack(side="left", fill="x", expand=True, padx=(0, 2))

        recent_listbox.bind("<<ListboxSelect>>",
                            lambda e, lb=recent_listbox, p=pk: self._on_recent_chat_select(e, lb, p))

        # ── RIGHT: Chat area ────────────────────────────────────────────
        right = ttk.Frame(main_split)
        right.pack(side="left", fill="both", expand=True)

        # Controls row
        ctrl = ttk.Frame(right)
        ctrl.pack(fill="x", pady=(0, 4))

        ttk.Label(ctrl, text="Label:").pack(side="left", padx=(0, 4))
        label_var = tk.StringVar(value="default")
        ttk.Entry(ctrl, textvariable=label_var, width=12).pack(side="left", padx=(0, 8))

        if pk == "grok":
            ttk.Checkbutton(ctrl, text="Grok Imagine",
                            variable=self.imagine_enabled_var,
                            command=self._on_imagine_toggle).pack(side="left", padx=(0, 8))

        ttk.Button(ctrl, text="New Chat",
                   command=lambda p=pk: self._new_chat_for(p)).pack(side="right", padx=(4, 0))
        ttk.Button(ctrl, text="Close Browser",
                   command=lambda p=pk: self._close_browser_for(p)).pack(side="right", padx=(4, 0))
        ttk.Button(ctrl, text="Clear",
                   command=lambda p=pk: self._clear_chat_display_for(p)).pack(side="right", padx=(4, 0))

        # Status
        status_var = tk.StringVar(value="● not started")
        status_lbl = ttk.Label(right, textvariable=status_var,
                                foreground="#909296", font=("Segoe UI", 9, "italic"))
        status_lbl.pack(anchor="w", pady=(0, 4))

        # Store per-platform status refs for _poll_browser_status
        setattr(self, f"chat_status_var_{pk}", status_var)
        setattr(self, f"chat_status_lbl_{pk}", status_lbl)

        # Chat display
        chat_frame = ttk.Frame(right)
        chat_frame.pack(fill="both", expand=True, pady=(0, 4))
        display = tk.Text(chat_frame, wrap="word",
                          background="#1a1b1e", foreground="#c1c2c5",
                          font=("Cascadia Code", 10),
                          state="disabled", cursor="arrow",
                          relief="flat", bd=0,
                          selectbackground="#339af0", selectforeground="#fff",
                          padx=10, pady=8)
        scroll = ttk.Scrollbar(chat_frame, command=display.yview)
        display.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        display.pack(side="left", fill="both", expand=True)
        for tag, fg in [("user", "#58a6ff"), ("ai", "#7ee787"), ("system", "#8b949e"),
                        ("attachment", "#d2a8ff"), ("media", "#ffa657"),
                        ("link", "#58a6ff"), ("divider", "#444")]:
            display.tag_config(tag, foreground=fg)
        if tag == "link":
            display.tag_config("link", underline=True)

        # Bottom bar
        bottom = ttk.Frame(right)
        bottom.pack(fill="x")

        att_row = ttk.Frame(bottom)
        att_row.pack(fill="x", pady=(0, 2))
        attach_list_frame = ttk.Frame(att_row)
        attach_list_frame.pack(side="left", fill="x", expand=True)
        ttk.Button(att_row, text="Media ↗",
                   command=self._open_media_folder).pack(side="right", padx=(2, 0))
        ttk.Button(att_row, text="Clear att",
                   command=self._clear_attachments).pack(side="right", padx=(2, 0))
        ttk.Button(att_row, text="+ Image",
                   command=lambda: self._add_attachment("image")).pack(side="right", padx=(2, 0))
        ttk.Button(att_row, text="+ File",
                   command=lambda: self._add_attachment("any")).pack(side="right", padx=(2, 0))

        input_frame = ttk.Frame(bottom)
        input_frame.pack(fill="x", pady=(2, 0))
        chat_input = ttk.Entry(input_frame)
        chat_input.pack(side="left", fill="x", expand=True, padx=(0, 4))
        send_btn = ttk.Button(input_frame, text="Send", style="Accent.TButton",
                               command=lambda p=pk: self._send_chat_for(p))
        send_btn.pack(side="left", padx=(0, 4))
        batch_btn = ttk.Button(input_frame, text="Batch ZIP",
                                command=self._batch_zip)
        batch_btn.pack(side="left")

        chat_input.bind("<Return>", lambda e, p=pk: self._send_chat_for(p))

        # Store panel refs
        self._platform_panels[pk] = {
            "display": display,
            "input": chat_input,
            "send_btn": send_btn,
            "batch_btn": batch_btn,
            "label_var": label_var,
            "status_var": status_var,
            "status_lbl": status_lbl,
            "attach_list_frame": attach_list_frame,
            "recent_listbox": recent_listbox,
            "_recent_data": [],      # list of {"id":..., "title":...}
        }

        # Kick off initial recent-chats fetch (non-blocking)
        self.root.after(2000, lambda: self._fetch_recent_chats(pk, recent_listbox))

    # ---- Recent chats ------------------------------------------------
    def _fetch_recent_chats(self, pk: str, listbox: tk.Listbox):
        """Fetch recent conversations from the platform's web API using saved session cookies."""
        listbox.delete(0, "end")
        listbox.insert("end", "⏳ Loading...")
        panel = self._platform_panels.get(pk, {})

        def worker():
            try:
                import json as _json
                import http.cookiejar
                import urllib.request

                session_file = SESSIONS_DIR / f"{pk}_default.json"
                if not session_file.exists():
                    label = panel.get("label_var", tk.StringVar()).get().strip() or "default"
                    session_file = SESSIONS_DIR / f"{pk}_{label}.json"
                if not session_file.exists():
                    self.root.after(0, self._set_recent_error, pk, listbox, "No session file found")
                    return

                raw = _json.loads(session_file.read_text("utf-8"))
                cookies_list = raw.get("cookies", [])

                jar = http.cookiejar.CookieJar()

                class _FakeCookie:
                    def __init__(self, c):
                        self.name = c["name"]
                        self.value = c["value"]
                        self.domain = c.get("domain", "")
                        self.path = c.get("path", "/")
                        self.secure = c.get("secure", False)
                        self.expires = int(c.get("expires", -1)) if c.get("expires", -1) != -1 else None
                        self.has_nonstandard_attr = lambda *a: False
                        self.is_expired = lambda: False
                        self.discard = False
                        self.comment = None
                        self.comment_url = None
                        self.rfc2109 = False
                        self.port = None
                        self.port_specified = False
                        self.domain_specified = True
                        self.domain_initial_dot = self.domain.startswith(".")
                        self.path_specified = True
                        self._rest = {}

                for c in cookies_list:
                    jar._cookies.setdefault(c.get("domain", ""), {}).setdefault(
                        c.get("path", "/"), {})[c["name"]] = _FakeCookie(c)

                # Build cookie header manually
                cookie_header = "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies_list
                    if c.get("value")
                )

                if pk == "chatgpt":
                    url = "https://chatgpt.com/backend-api/conversations?offset=0&limit=28&order=updated"
                    req = urllib.request.Request(url, headers={
                        "Cookie": cookie_header,
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://chatgpt.com/",
                    })
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = _json.loads(resp.read())
                    items = data.get("items", [])
                    chats = [{"id": it["id"], "title": it.get("title", "Untitled")} for it in items]

                elif pk == "grok":
                    url = "https://grok.com/rest/app-chat/conversations?pageSize=28"
                    req = urllib.request.Request(url, headers={
                        "Cookie": cookie_header,
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://grok.com/",
                        "Content-Type": "application/json",
                    })
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = _json.loads(resp.read())
                    convs = data.get("conversations", data.get("items", []))
                    chats = [{"id": c.get("conversationId", c.get("id", "")),
                              "title": c.get("title", "Untitled")} for c in convs]
                else:
                    chats = []

                self.root.after(0, self._set_recent_chats, pk, listbox, chats)
            except Exception as e:
                self.root.after(0, self._set_recent_error, pk, listbox, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _set_recent_chats(self, pk: str, listbox: tk.Listbox, chats: list):
        listbox.delete(0, "end")
        panel = self._platform_panels.get(pk, {})
        panel["_recent_data"] = chats
        if not chats:
            listbox.insert("end", "(no conversations found)")
            return
        for c in chats:
            title = c["title"]
            if len(title) > 26:
                title = title[:24] + "…"
            listbox.insert("end", title)

    def _set_recent_error(self, pk: str, listbox: tk.Listbox, err: str):
        listbox.delete(0, "end")
        listbox.insert("end", f"⚠ {err[:28]}")
        self._enqueue_log(f"[{pk}] Recent chats error: {err}")

    def _on_recent_chat_select(self, event, listbox: tk.Listbox, pk: str):
        sel = listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        panel = self._platform_panels.get(pk, {})
        chats = panel.get("_recent_data", [])
        if idx >= len(chats):
            return
        chat = chats[idx]
        title = chat.get("title", "")
        chat_id = chat.get("id", "")
        self._enqueue_log(f"[{pk}] Selected recent chat: {title} (id={chat_id})")
        # Show the URL/ID in the display
        display = panel.get("display")
        if display:
            display.configure(state="normal")
            if pk == "chatgpt":
                url = f"https://chatgpt.com/c/{chat_id}"
            else:
                url = f"https://grok.com/chat/{chat_id}"
            display.insert("end", f"\n[Recent chat loaded] {title}\n{url}\n", "system")
            display.see("end")
            display.configure(state="disabled")

    # ---- Per-platform bridge controls ---------------------------------
    def _ensure_bridge_for(self, pk: str):
        """Get or create the BridgeWorker for a specific platform."""
        desired_headless = bool(self.chat_headless_var.get())
        desired_cdp = self._get_cdp_url()
        bridge = self._bridges.get(pk)
        if bridge is None:
            from chat_engine import BridgeWorker
            bridge = BridgeWorker(SESSIONS_DIR, media_dir=MEDIA_DIR)
            if desired_cdp:
                bridge._cdp_url = desired_cdp
            bridge.start(headless=desired_headless)
            self._bridges[pk] = bridge
            self._bridge_headless[pk] = desired_headless
        elif self._bridge_headless.get(pk) != desired_headless:
            try:
                bridge.set_headless(desired_headless)
            except Exception as e:
                self._enqueue_log(f"⚠ set_headless failed: {e}")
            self._bridge_headless[pk] = desired_headless
        return bridge

    def _new_chat_for(self, pk: str):
        panel = self._platform_panels.get(pk, {})
        label = panel.get("label_var", tk.StringVar()).get().strip() or "default"
        bridge = self._bridges.get(pk)
        if bridge is None:
            self._enqueue_log(f"ℹ No {pk} browser yet — next message starts fresh.")
            return

        def worker():
            try:
                ok = bridge.start_new_chat(pk, label)
                msg = f"✓ New {pk} chat started" if ok else f"ℹ No active {pk} browser."
                self.root.after(0, self._enqueue_log, msg)
                self.root.after(0, self._append_chat_divider_for, pk, f"— NEW {pk} chat —")
            except Exception as e:
                self.root.after(0, self._enqueue_log, f"✗ {pk} new chat failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _close_browser_for(self, pk: str):
        bridge = self._bridges.get(pk)
        if bridge is None:
            self._enqueue_log(f"ℹ {pk} browser not running.")
            return

        def worker():
            try:
                bridge.close_all_sessions()
                self.root.after(0, self._enqueue_log, f"✓ {pk} browser closed.")
                self.root.after(0, self._append_chat_divider_for, pk, f"— {pk} browser closed —")
            except Exception as e:
                self.root.after(0, self._enqueue_log, f"✗ {pk} close failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _clear_chat_display_for(self, pk: str):
        display = self._platform_panels.get(pk, {}).get("display")
        if display:
            display.configure(state="normal")
            display.delete("1.0", "end")
            display.configure(state="disabled")

    def _append_chat_divider_for(self, pk: str, text: str):
        display = self._platform_panels.get(pk, {}).get("display")
        if display:
            try:
                display.configure(state="normal")
                display.insert("end", f"\n{text}\n", "divider")
                display.see("end")
                display.configure(state="disabled")
            except Exception:
                pass

    def _send_chat_for(self, pk: str):
        """Send a message for a specific platform independently."""
        if self._chat_busy.get(pk):
            self._enqueue_log(f"⚠ [{pk}] Tunggu pesan sebelumnya selesai.")
            return

        panel = self._platform_panels.get(pk, {})
        chat_input = panel.get("input")
        send_btn = panel.get("send_btn")
        batch_btn = panel.get("batch_btn")
        display = panel.get("display")
        label_var = panel.get("label_var", tk.StringVar())

        msg = chat_input.get().strip()
        attachments = list(self.pending_attachments)
        if not msg and not attachments:
            return
        chat_input.delete(0, "end")
        label = label_var.get().strip() or "default"
        imagine_opts = self._get_imagine_opts() if pk == "grok" else None

        # Show in display
        display.configure(state="normal")
        prefix = f"Imagine/{imagine_opts['mode']}" if imagine_opts else "You"
        display.insert("end", f"\n[{prefix}] {msg or '(no text)'}\n", "user")
        for p in attachments:
            display.insert("end", f"  {_file_icon(p)} {p.name}\n", "attachment")
        status_text = "generating..." if imagine_opts else "thinking..."
        display.insert("end", f"[{pk}] {status_text}\n", "system")
        display.see("end")
        display.configure(state="disabled")

        self.pending_attachments = []
        self._refresh_attachment_bar()

        self._chat_busy[pk] = True
        if send_btn:
            send_btn.configure(state="disabled", text="...")

        if self.chat_debug_phases_var.get():
            os.environ["DEBUG_PHASES"] = "1"
        else:
            os.environ.pop("DEBUG_PHASES", None)

        gui = self

        def worker():
            try:
                bridge = gui._ensure_bridge_for(pk)
                result = bridge.chat(pk, msg, label=label, timeout=240,
                                     attachments=attachments,
                                     imagine_opts=imagine_opts)
                gui.root.after(0, gui._on_chat_result_for, pk, result)
            except Exception as e:
                gui.root.after(0, gui._on_chat_result_for, pk,
                               {"ok": False, "error": str(e), "platform": pk})
            finally:
                gui.root.after(0, gui._unlock_send_btn_for, pk)

        threading.Thread(target=worker, daemon=True).start()

    def _unlock_send_btn_for(self, pk: str):
        self._chat_busy[pk] = False
        panel = self._platform_panels.get(pk, {})
        btn = panel.get("send_btn")
        batch = panel.get("batch_btn")
        if btn:
            btn.configure(state="normal", text="Send")
        if batch:
            batch.configure(state="normal", text="Batch ZIP")

    def _on_chat_result_for(self, pk: str, result: dict):
        panel = self._platform_panels.get(pk, {})
        display = panel.get("display")
        if display is None:
            return
        display.configure(state="normal")
        # Remove "thinking..." placeholder
        content = display.get("1.0", "end")
        cleaned = [l for l in content.split("\n") if "thinking..." not in l and "generating..." not in l]
        display.delete("1.0", "end")
        display.insert("1.0", "\n".join(cleaned))

        if result.get("ok"):
            response = result.get("response", "")
            media = result.get("media", []) or []
            elapsed = result.get("elapsed_ms", 0)
            is_new = result.get("is_new_chat", False)
            url = result.get("chat_url", "")
            meta = f"  ({'NEW' if is_new else 'continuation'}, {elapsed/1000:.1f}s"
            if url:
                meta += f", {url[:55]}{'...' if len(url) > 55 else ''}"
            meta += ")"
            display.insert("end", f"[{pk}]{meta}\n", "system")
            display.insert("end", f"{response}\n", "ai")
            for m in media:
                local = m.get("local_path", "")
                mtype = m.get("type", "file")
                if mtype == "image" and local and Path(local).exists():
                    display.insert("end", f"  🖼 {Path(local).name}\n", "media")
                    self._insert_image_thumbnail_to(display, local)
                elif mtype == "video" and local:
                    display.insert("end", f"  🎬 video saved → {local}\n", "media")
                else:
                    display.insert("end", f"  📎 {mtype}: {local}\n", "media")
        else:
            display.insert("end", f"[ERROR] {result.get('error', 'Unknown')}\n", "system")
        display.see("end")
        display.configure(state="disabled")

    def _insert_image_thumbnail_to(self, display: tk.Text, path: str):
        try:
            ext = Path(path).suffix.lower()
            img = None
            if ext in (".png", ".gif"):
                img = tk.PhotoImage(file=path)
            elif ext in (".jpg", ".jpeg", ".webp", ".bmp"):
                from PIL import Image, ImageTk
                pil = Image.open(path)
                pil.thumbnail((320, 240))
                img = ImageTk.PhotoImage(pil)
            if img:
                self._chat_images.append(img)
                display.configure(state="normal")
                display.image_create("end", image=img)
                display.insert("end", "\n")
                display.configure(state="disabled")
        except Exception as e:
            self._enqueue_log(f"⚠ thumbnail: {e}")

    # ---- Chat session controls (legacy/compat shims) ------------------
    def _ensure_bridge(self):
        """Legacy shim — returns Grok bridge (used by batch_zip)."""
        return self._ensure_bridge_for("grok")

    def _get_cdp_url(self):
        if self.chat_cdp_var.get():
            port = self.chat_cdp_port_var.get().strip() or "9222"
            return f"http://127.0.0.1:{port}"
        return None

    def _on_cdp_toggle(self):
        cdp_url = self._get_cdp_url()
        for pk, bridge in self._bridges.items():
            if bridge:
                try:
                    bridge.set_cdp_url(cdp_url)
                except Exception as e:
                    self._enqueue_log(f"⚠ {pk} CDP toggle failed: {e}")

    def _on_headless_toggle(self):
        self._enqueue_log("ℹ Headless toggle applies on next send for each platform.")

    def _on_chat_target_changed(self):
        pass

    def _on_imagine_toggle(self):
        """Show/hide Imagine options panel (Grok tab only)."""
        grok_panel = self._platform_panels.get("grok", {})
        display = grok_panel.get("display")
        if self.imagine_enabled_var.get():
            # Show imagine frame before chat display inside grok panel
            if display:
                try:
                    self.imagine_frame.pack(fill="x", pady=(0, 4),
                                            before=display.master)
                except Exception:
                    self.imagine_frame.pack(fill="x", pady=(0, 4))
        else:
            self.imagine_frame.pack_forget()

    def _on_imagine_mode_changed(self):
        pass  # all options are inline now

    def _get_imagine_opts(self) -> dict | None:
        if not self.imagine_enabled_var.get():
            return None
        aspect_raw = self.imagine_aspect_var.get()
        aspect = aspect_raw.split(" ")[0] if " " in aspect_raw else aspect_raw
        return {
            "mode": self.imagine_mode_var.get(),
            "resolution": self.imagine_res_var.get(),
            "duration": self.imagine_dur_var.get(),
            "aspect": aspect,
        }

    def _new_chat(self):
        """Legacy shim — new chat on Grok."""
        self._new_chat_for("grok")

    def _close_browser(self):
        """Legacy shim — close Grok browser."""
        self._close_browser_for("grok")

    def _clear_chat_display(self):
        self._clear_chat_display_for("grok")
        self._chat_images.clear()

    def _append_chat_divider(self, text: str):
        self._append_chat_divider_for("grok", text)

    # ---- Attachment management ----
    def _add_attachment(self, kind: str):
        filetypes_map = {
            "image": [("Images", "*.png *.jpg *.jpeg *.gif *.webp *.bmp"),
                      ("All files", "*.*")],
            "doc": [("Documents", "*.pdf *.doc *.docx *.xls *.xlsx *.ppt *.pptx *.txt *.md *.csv *.json"),
                    ("All files", "*.*")],
            "video": [("Videos", "*.mp4 *.webm *.mov *.avi *.mkv"),
                      ("All files", "*.*")],
            "any": [("All files", "*.*")],
        }
        paths = filedialog.askopenfilenames(
            title="Select file(s) to attach",
            filetypes=filetypes_map.get(kind, [("All files", "*.*")])
        )
        if not paths:
            return
        for p in paths:
            pp = Path(p)
            if pp.exists() and pp not in self.pending_attachments:
                self.pending_attachments.append(pp)
        self._refresh_attachment_bar()

    def _clear_attachments(self):
        self.pending_attachments = []
        self._refresh_attachment_bar()

    def _remove_attachment(self, path: Path):
        try:
            self.pending_attachments.remove(path)
        except ValueError:
            pass
        self._refresh_attachment_bar()

    def _refresh_attachment_bar(self):
        for w in self.attach_list_frame.winfo_children():
            w.destroy()
        if not self.pending_attachments:
            ttk.Label(self.attach_list_frame,
                      text="(belum ada — klik tombol di bawah untuk attach file)",
                      foreground="#888").pack(anchor="w")
            return
        for p in self.pending_attachments:
            row = ttk.Frame(self.attach_list_frame)
            row.pack(fill="x", pady=1)
            size_kb = max(1, p.stat().st_size // 1024) if p.exists() else 0
            label = f"  {_file_icon(p)}  {p.name}  ({size_kb} KB)"
            ttk.Label(row, text=label, foreground="#d2a8ff").pack(side="left")
            ttk.Button(row, text="✗", width=3,
                       command=lambda pp=p: self._remove_attachment(pp)).pack(side="right")

    def _open_media_folder(self):
        try:
            if sys.platform == "win32":
                os.startfile(MEDIA_DIR)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(MEDIA_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(MEDIA_DIR)])
        except Exception as e:
            messagebox.showinfo("Media folder", f"Open this folder manually:\n{MEDIA_DIR}\n\n({e})")

    def _build_chrome_tab(self, parent):
        """Settings panel for launching real Chrome with a specific profile."""
        rc = self.cfg.get("real_chrome", {})

        # --- Enable toggle ---
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 8))
        self._rc_enabled_var = tk.BooleanVar(value=bool(rc.get("enabled", False)))
        ttk.Checkbutton(
            top, text="✅ Gunakan Chrome Asli (bukan Chromium Playwright)",
            variable=self._rc_enabled_var,
            command=self._on_rc_toggle
        ).pack(anchor="w")

        # --- Profile number ---
        frm = ttk.LabelFrame(parent, text="Profile Chrome", padding=8)
        frm.pack(fill="x", pady=(0, 6))
        ttk.Label(frm, text="Nomor Profile (contoh: 25  →  folder 'Profile 25'):").pack(anchor="w")
        self._rc_profile_var = tk.StringVar(value=str(rc.get("profile", "25")))
        ttk.Entry(frm, textvariable=self._rc_profile_var, width=12).pack(anchor="w", pady=2)

        # --- Chrome exe ---
        exe_frm = ttk.LabelFrame(parent, text="Path Chrome.exe (kosongkan = auto-detect)", padding=8)
        exe_frm.pack(fill="x", pady=(0, 6))
        exe_row = ttk.Frame(exe_frm)
        exe_row.pack(fill="x")
        self._rc_exe_var = tk.StringVar(value=str(rc.get("exe", "")))
        ttk.Entry(exe_row, textvariable=self._rc_exe_var, width=55).pack(side="left", fill="x", expand=True)
        ttk.Button(exe_row, text="Browse...", command=self._rc_browse_exe).pack(side="left", padx=(4,0))

        # --- User Data ---
        ud_frm = ttk.LabelFrame(parent, text="Chrome User Data Dir (kosongkan = auto-detect)", padding=8)
        ud_frm.pack(fill="x", pady=(0, 6))
        ud_row = ttk.Frame(ud_frm)
        ud_row.pack(fill="x")
        self._rc_user_data_var = tk.StringVar(value=str(rc.get("user_data", "")))
        ttk.Entry(ud_row, textvariable=self._rc_user_data_var, width=55).pack(side="left", fill="x", expand=True)
        ttk.Button(ud_row, text="Browse...", command=self._rc_browse_user_data).pack(side="left", padx=(4,0))

        # --- Info ---
        info = ttk.LabelFrame(parent, text="ℹ Info", padding=6)
        info.pack(fill="x", pady=(0, 8))
        ttk.Label(info, text=(
            "Mode ini membuka Chrome ASLI milik Anda (bukan Chromium bawaan Playwright).\n"
            "Semua cookie, login, dan ekstensi Chrome yang sudah ada akan aktif.\n"
            "⚠ Chrome dengan profile yang sama TIDAK boleh sedang terbuka sebelum klik Login!"
        ), wraplength=480, justify="left", foreground="#cccccc").pack(anchor="w")

        # --- Save button ---
        ttk.Button(parent, text="💾 Simpan Pengaturan", command=self._rc_save).pack(pady=8)

    def _on_rc_toggle(self):
        self._rc_save()

    def _rc_browse_exe(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Pilih chrome.exe",
            filetypes=[("Chrome", "chrome.exe google-chrome google-chrome-stable *"), ("All", "*.*")]
        )
        if path:
            self._rc_exe_var.set(path)

    def _rc_browse_user_data(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Pilih Chrome User Data Directory")
        if path:
            self._rc_user_data_var.set(path)

    def _rc_save(self):
        self.cfg["real_chrome"] = {
            "enabled": self._rc_enabled_var.get(),
            "profile": self._rc_profile_var.get().strip() or "25",
            "exe": self._rc_exe_var.get().strip(),
            "user_data": self._rc_user_data_var.get().strip(),
        }
        save_config(self.cfg)
        self._log("✅ Real Chrome settings disimpan.", "info")

    def _build_api_tab(self, parent):
        info = ttk.LabelFrame(parent, text="OpenAI-Compatible API Server (multimodal + continuity)", padding=8)
        info.pack(fill="x", pady=(0, 8))
        ttk.Label(info, text=(
            "Start the API server to access ChatGPT & Grok via OpenAI-compatible endpoints.\n"
            "Supports text + images (vision format) + PDF + video + any file upload.\n"
            "Conversation continuity: subsequent requests reuse the same Grok/ChatGPT chat."
        ), wraplength=820, foreground="#aaa").pack(anchor="w")

        cfg_frame = ttk.Frame(parent)
        cfg_frame.pack(fill="x", pady=4)
        ttk.Label(cfg_frame, text="Port:").pack(side="left", padx=(0, 6))
        self.api_port_var = tk.StringVar(value=str(self.cfg.get("api_port", 5100)))
        ttk.Entry(cfg_frame, textvariable=self.api_port_var, width=8).pack(side="left", padx=(0, 12))
        ttk.Label(cfg_frame, text="API Key (optional):").pack(side="left", padx=(0, 6))
        self.api_key_var = tk.StringVar(value=self.cfg.get("api_key", ""))
        ttk.Entry(cfg_frame, textvariable=self.api_key_var, width=25, show="*").pack(side="left")

        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill="x", pady=8)
        self.api_start_btn = ttk.Button(btn_frame, text="▶ Start API Server",
                                         command=self._start_api_server)
        self.api_start_btn.pack(side="left", padx=(0, 6))
        self.api_stop_btn = ttk.Button(btn_frame, text="■ Stop", command=self._stop_api_server,
                                        state="disabled")
        self.api_stop_btn.pack(side="left")

        self.api_status_var = tk.StringVar(value="● Stopped")
        ttk.Label(parent, textvariable=self.api_status_var, foreground="#888",
                  font=("Segoe UI", 10)).pack(anchor="w", pady=4)

        usage_frame = ttk.LabelFrame(parent, text="Usage / Configuration", padding=8)
        usage_frame.pack(fill="both", expand=True, pady=8)
        usage_text = (
            "Base URL:  http://localhost:{port}/v1\n"
            "Models:    chatgpt, grok, chatgpt:<label>, grok:<label>\n\n"
            "OpenClaw / Custom Provider config:\n"
            '{{\n'
            '  "name": "AI Chat Bridge",\n'
            '  "baseUrl": "http://localhost:{port}/v1",\n'
            '  "apiKey": "bridge",\n'
            '  "models": ["chatgpt", "grok"]\n'
            '}}\n\n'
            "Conversation continuity (automatic):\n"
            "  - First request (no assistant msgs) → NEW chat\n"
            "  - Subsequent requests → CONTINUATION (same chat in Grok/ChatGPT)\n\n"
            "Force a new chat:\n"
            '  POST /v1/new_chat   body: {{"model": "grok"}}\n'
            "  OR  add to chat completion body:\n"
            '       "bridge_options": {{"new_chat": true}}\n\n'
            "Multimodal example:\n"
            'curl http://localhost:{port}/v1/chat/completions \\\n'
            '  -H "Content-Type: application/json" \\\n'
            '  -d \'{{"model":"grok","messages":[{{"role":"user","content":[\n'
            '    {{"type":"text","text":"describe this"}},\n'
            '    {{"type":"image_url","image_url":{{"url":"data:image/png;base64,..."}}}}\n'
            '  ]}}]}}\'\n\n'
            "Close browser (free RAM):\n"
            '  POST /v1/close_session   body: {{"model": "grok"}}\n'
        )
        port = self.api_port_var.get()
        usage_text_widget = tk.Text(usage_frame, height=22, wrap="word",
                                     background="#1e1e1e", foreground="#8b949e",
                                     font=("Consolas", 9))
        usage_text_widget.insert("1.0", usage_text.format(port=port))
        usage_text_widget.configure(state="disabled")
        usage_text_widget.pack(fill="both", expand=True)

    # ---- Session info ----
    def _inspect_session_file(self, pk, path):
        result = {"exists": path.exists(), "valid": False, "n_cookies": 0,
                  "cookie_names": set(), "missing_required": [], "found_required": [], "error": None}
        if not result["exists"]:
            return result
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            result["error"] = str(e)
            return result
        cookies = state.get("cookies") or []
        names = set()
        for c in cookies:
            if isinstance(c, dict) and c.get("name") and c.get("value"):
                names.add(c["name"])
        result["n_cookies"] = len(cookies)
        result["cookie_names"] = names
        required = self.REQUIRED_COOKIES_BY_PLATFORM.get(pk, ())
        for r in required:
            if r in names:
                result["found_required"].append(r)
            else:
                result["missing_required"].append(r)
        result["valid"] = len(result["missing_required"]) == 0
        return result

    def _refresh_session_info(self, pk, widgets):
        label = widgets["label_var"].get().strip() or "default"
        cap_cls = PLATFORMS[pk]
        cap = cap_cls(label, SESSIONS_DIR)
        path = cap.session_path()
        info = self._inspect_session_file(pk, path)
        badge = widgets["status_badge"]
        if not info["exists"]:
            badge.configure(text="✗ NO SESSION", foreground="#888", background="#2a2a2a")
            widgets["info_var"].set("No session — use Chrome extension or click 'Open Browser & Login'.")
            widgets["export_btn"].configure(state="disabled")
        elif info["error"]:
            badge.configure(text="✗ ERROR", foreground="#ff7875", background="#3a1f1f")
            widgets["info_var"].set(f"⚠ {info['error']}")
            widgets["export_btn"].configure(state="normal")
        elif info["valid"]:
            badge.configure(text="✓ VALID — LOGGED IN", foreground="#73d13d", background="#1f3a1f")
            widgets["info_var"].set(f"Session OK — {info['n_cookies']} cookies. ({path.name})")
            widgets["export_btn"].configure(state="normal")
        else:
            badge.configure(text="⚠ INCOMPLETE", foreground="#fadb14", background="#3a3a1f")
            widgets["info_var"].set(f"Missing: {', '.join(info['missing_required'])}")
            widgets["export_btn"].configure(state="normal")

    # ---- Capture flow ----
    def _start_capture(self, pk):
        if self.current_capture_thread and self.current_capture_thread.is_alive():
            messagebox.showwarning("Busy", "Another capture is running.")
            return
        widgets = self.platform_widgets[pk]
        label = widgets["label_var"].get().strip() or "default"
        self.cfg.setdefault("last_labels", {})[pk] = label
        save_config(self.cfg)
        widgets["login_btn"].configure(state="disabled")
        widgets["confirm_btn"].configure(state="normal")
        cap_cls = PLATFORMS[pk]
        # --- Real Chrome config injection ---
        rc = self.cfg.get("real_chrome", {})
        rc_enabled = rc.get("enabled", False)
        rc_profile = str(rc.get("profile", "")).strip() if rc_enabled else None
        rc_exe = str(rc.get("exe", "")).strip() or None if rc_enabled else None
        rc_user_data = str(rc.get("user_data", "")).strip() or None if rc_enabled else None
        capture = cap_cls(
            label=label,
            sessions_dir=SESSIONS_DIR,
            status_callback=self._enqueue_log,
            real_chrome_exe=rc_exe,
            real_chrome_user_data=rc_user_data,
            real_chrome_profile=rc_profile if rc_profile else None,
        )
        self.current_finished_event = threading.Event()
        self.current_capture_thread = threading.Thread(
            target=self._run_capture, args=(capture, pk, widgets), daemon=True)
        self.current_capture_thread.start()
        self._log(f"Starting capture for {pk} (label: '{label}')", "info")

    def _run_capture(self, capture, pk, widgets):
        try:
            result = capture.run(finished_event=self.current_finished_event)
            self.root.after(0, self._on_capture_done, result, pk, widgets, capture)
        except Exception as e:
            self.root.after(0, self._on_capture_error, str(e), widgets)

    def _on_capture_done(self, result, pk, widgets, capture=None):
        widgets["login_btn"].configure(state="normal")
        widgets["confirm_btn"].configure(state="disabled")
        if result.success:
            tag = "success" if result.has_required_cookies else "warn"
            self._log(f"✓ {result.platform} saved ({result.cookie_count} cookies)", tag)

            # If Direct CDP mode, auto-enable CDP for chat
            cdp_url = getattr(capture, "_cdp_url_result", None) if capture else None
            if cdp_url:
                self._log(f"🔗 Direct CDP aktif: {cdp_url}", "success")
                # Enable CDP checkbox and set port
                self.chat_cdp_var.set(1)
                port = cdp_url.rsplit(":", 1)[-1]
                self.chat_cdp_port_var.set(port)
                self._on_cdp_toggle()
        else:
            self._log(f"✗ Capture failed: {result.error}", "error")
        self._refresh_session_info(pk, widgets)

    def _on_capture_error(self, err, widgets):
        widgets["login_btn"].configure(state="normal")
        widgets["confirm_btn"].configure(state="disabled")
        self._log(f"✗ Error: {err}", "error")

    def _confirm_login(self):
        if self.current_finished_event:
            self.current_finished_event.set()
            self._log("User confirmed login — saving...", "info")

    def _export_session(self, pk):
        widgets = self.platform_widgets[pk]
        label = widgets["label_var"].get().strip() or "default"
        cap = PLATFORMS[pk](label, SESSIONS_DIR)
        path = cap.session_path()
        if not path.exists():
            messagebox.showerror("Error", "No session file.")
            return
        dst = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON", "*.json")],
                                            initialfile=path.name)
        if dst:
            Path(dst).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            self._log(f"✓ Exported → {dst}", "success")

    # ---- Chat ----
    def _send_chat(self):
        """Legacy shim — sends on Grok (used by batch_zip hotkey, etc.)."""
        self._send_chat_for("grok")

    def _unlock_send_button(self):
        """Legacy shim — unlock Grok send button."""
        self._unlock_send_btn_for("grok")

    # ---- Batch ZIP processing ----
    def _batch_zip(self):
        """Open a ZIP file containing Scene_1/, Scene_2/... folders,
        each with image.png + prompt.txt. Process them sequentially."""
        if self._chat_busy.get("grok"):
            self._enqueue_log("⚠ Tunggu proses sebelumnya selesai dulu.")
            return

        zip_path = filedialog.askopenfilename(
            title="Select Batch ZIP (Scene_1, Scene_2, ...)",
            filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")]
        )
        if not zip_path:
            return

        import re
        import zipfile
        import tempfile

        # Extract ZIP
        try:
            extract_dir = Path(tempfile.mkdtemp(prefix="batch_imagine_"))
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to extract ZIP: {e}")
            return

        # Find scene folders (Scene_1, Scene_2, ...)
        scenes = []
        for root_dir in extract_dir.rglob("*"):
            if root_dir.is_dir() and re.match(r"Scene_\d+$", root_dir.name):
                scene_num = int(root_dir.name.split("_")[1])
                image_file = None
                prompt_text = ""

                # Find image
                for ext in [".png", ".jpg", ".jpeg", ".webp"]:
                    img = root_dir / f"image{ext}"
                    if img.exists():
                        image_file = img
                        break
                if not image_file:
                    # Try any image in the folder
                    for f in root_dir.iterdir():
                        if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                            image_file = f
                            break

                # Read prompt and extract LIP-SYNC text
                prompt_file = root_dir / "prompt.txt"
                if prompt_file.exists():
                    raw = prompt_file.read_text(encoding="utf-8")
                    # Extract LIP-SYNC content: "Model is speaking: '...'"
                    match = re.search(r"LIP-SYNC:.*?speaking:\s*['\"](.+?)['\"]", raw, re.DOTALL)
                    if match:
                        prompt_text = match.group(1).strip()
                    else:
                        # Fallback: use full CONTEXT line
                        match2 = re.search(r"CONTEXT:\s*(.+?)(?:\n|$)", raw)
                        if match2:
                            prompt_text = match2.group(1).strip()
                        else:
                            prompt_text = raw.strip()

                if image_file and prompt_text:
                    scenes.append({
                        "num": scene_num,
                        "image": image_file,
                        "prompt": prompt_text,
                        "folder": root_dir,
                    })

        scenes.sort(key=lambda s: s["num"])

        if not scenes:
            messagebox.showinfo("No scenes",
                "No valid Scene_N/ folders found in ZIP.\n"
                "Expected: Scene_1/image.png + Scene_1/prompt.txt")
            return

        # Confirm
        scene_list = "\n".join(
            f"  Scene {s['num']}: {s['prompt'][:60]}..." for s in scenes
        )
        if not messagebox.askyesno("Batch Imagine",
            f"Found {len(scenes)} scene(s):\n{scene_list}\n\n"
            f"Mode: {self.imagine_mode_var.get()}\n"
            f"Quality: {self.imagine_res_var.get()}\n"
            f"Duration: {self.imagine_dur_var.get()}\n\n"
            f"Start batch processing?"):
            return

        # Enable Imagine mode if not already
        self.imagine_enabled_var.set(True)
        self._on_imagine_toggle()
        self.chat_platform_var.set("grok")

        # Lock UI
        self._chat_busy["grok"] = True
        self.send_btn.configure(state="disabled")
        self.batch_btn.configure(state="disabled", text="⏳ Batch...")

        self._append_chat_divider(
            f"═══ BATCH START: {len(scenes)} scenes from {Path(zip_path).name} ═══"
        )

        imagine_opts = self._get_imagine_opts()
        platform = "grok"
        label = self.chat_label_var.get().strip() or "default"

        gui = self

        def batch_worker():
            results = []
            for i, scene in enumerate(scenes):
                scene_tag = f"Scene {scene['num']}"
                gui.root.after(0, gui._append_chat_divider,
                    f"─── {scene_tag} ({i+1}/{len(scenes)}) ───")
                gui.root.after(0, gui._enqueue_log,
                    f"🎬 Batch: Processing {scene_tag}...")

                try:
                    bridge = gui._ensure_bridge()

                    # Force new chat for each scene
                    if i > 0:
                        try:
                            bridge.start_new_chat(platform, label)
                            import time; time.sleep(2)
                        except Exception:
                            pass

                    result = bridge.chat(
                        platform, scene["prompt"],
                        label=label, timeout=360,
                        attachments=[scene["image"]],
                        imagine_opts=imagine_opts,
                        force_new_chat=True,
                    )
                    results.append({"scene": scene_tag, "result": result})
                    gui.root.after(0, gui._on_chat_result, result, platform)

                    if result.get("ok"):
                        gui.root.after(0, gui._enqueue_log,
                            f"✓ {scene_tag} done! Media: {len(result.get('media', []))} file(s)")
                    else:
                        gui.root.after(0, gui._enqueue_log,
                            f"✗ {scene_tag} failed: {result.get('error', 'Unknown')}")

                except Exception as e:
                    gui.root.after(0, gui._enqueue_log,
                        f"✗ {scene_tag} error: {e}")
                    results.append({"scene": scene_tag,
                                    "result": {"ok": False, "error": str(e)}})

            # Done
            ok_count = sum(1 for r in results if r["result"].get("ok"))
            gui.root.after(0, gui._append_chat_divider,
                f"═══ BATCH DONE: {ok_count}/{len(scenes)} succeeded ═══")
            gui.root.after(0, gui._enqueue_log,
                f"📦 Batch complete: {ok_count}/{len(scenes)} scenes succeeded")
            gui.root.after(0, gui._unlock_send_button)

        threading.Thread(target=batch_worker, daemon=True).start()

    def _append_chat_status(self, text: str):
        """Legacy shim — appends status to Grok display."""
        display = self._platform_panels.get("grok", {}).get("display")
        if display:
            try:
                display.configure(state="normal")
                display.insert("end", f"  · {text}\n", "system")
                display.see("end")
                display.configure(state="disabled")
            except Exception:
                pass

    def _on_chat_result(self, result, platform):
        """Legacy shim — routes to per-platform result handler."""
        self._on_chat_result_for(platform, result)

    def _insert_image_thumbnail(self, path: str):
        """Legacy shim — inserts thumbnail to Grok display."""
        display = self._platform_panels.get("grok", {}).get("display")
        if display:
            self._insert_image_thumbnail_to(display, path)

    # ---- API Server ----
    def _start_api_server(self):
        port = self.api_port_var.get().strip()
        key = self.api_key_var.get().strip()
        self.cfg["api_port"] = int(port)
        self.cfg["api_key"] = key
        save_config(self.cfg)

        env = os.environ.copy()
        if key:
            env["API_KEY"] = key

        cmd = [sys.executable, str(BASE_DIR / "api_server.py"),
               "--port", port, "--host", "0.0.0.0"]

        try:
            self._api_process = subprocess.Popen(cmd, env=env,
                                                  stdout=subprocess.PIPE,
                                                  stderr=subprocess.STDOUT)
            self.api_start_btn.configure(state="disabled")
            self.api_stop_btn.configure(state="normal")
            self.api_status_var.set(f"● Running on http://0.0.0.0:{port}/v1")
            self._log(f"API server started on port {port}", "success")

            def stream_logs():
                for line in iter(self._api_process.stdout.readline, b''):
                    self._enqueue_log(f"[API] {line.decode().strip()}")
            threading.Thread(target=stream_logs, daemon=True).start()
        except Exception as e:
            self._log(f"Failed to start API: {e}", "error")

    def _stop_api_server(self):
        if self._api_process:
            self._api_process.terminate()
            self._api_process = None
        self.api_start_btn.configure(state="normal")
        self.api_stop_btn.configure(state="disabled")
        self.api_status_var.set("● Stopped")
        self._log("API server stopped", "info")

    # ---- Log ----
    def _enqueue_log(self, msg):
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                lower = msg.lower()
                tag = "info"
                if "error" in lower or "fail" in lower or "✗" in msg:
                    tag = "error"
                elif "warning" in lower or "missing" in lower or "⚠" in msg:
                    tag = "warn"
                elif "ok" in lower or "saved" in lower or "✓" in msg or "success" in lower:
                    tag = "success"
                self._log(msg, tag)
        except queue.Empty:
            pass
        finally:
            self.root.after(150, self._poll_log_queue)

    def _log(self, msg, tag="info"):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] ", "muted")
        self.log_text.insert("end", f"{msg}\n", tag)
        self.log_text.see("end")


def main():
    root = tk.Tk()
    app = AIChatBridgeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
