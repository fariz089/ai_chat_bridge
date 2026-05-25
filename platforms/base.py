"""
Base class for AI chat platform session capture.

Adapted from multi_capture's LoginCapture. Same architecture:
  - Playwright headed browser with stealth patches
  - Persistent Chrome profile per label (anti-bot)
  - Manual login → auto-detect → save storage_state JSON
  - Chrome extension bridge (recommended) via extension_server.py

Platforms supported:
  - ChatGPT (chatgpt.com)
  - Grok    (grok.com / x.com/i/grok)

NEW: Real Chrome support
  - Set REAL_CHROME_EXE + REAL_CHROME_USER_DATA in config/env
    to launch the user's actual Chrome installation with a chosen profile.
  - Profile is specified by folder name (e.g. "Profile 25") OR
    by profile number (25 → "Profile 25").
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

from playwright.sync_api import sync_playwright, Page, BrowserContext

logger = logging.getLogger(__name__)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-default-browser-check",
    "--no-first-run",
    "--no-service-autorun",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
    "--enable-webgl",
    "--use-gl=angle",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
]

_STEALTH_INIT_JS = r"""
(() => {
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => false, configurable: true,
    });
  } catch (e) {}

  try {
    const makeMime = (type, suffixes, description) => ({
      type, suffixes, description, enabledPlugin: null,
    });
    const makePlugin = (name, filename, description, mimeTypes) => {
      const plugin = { name, filename, description, length: mimeTypes.length };
      mimeTypes.forEach((mt, i) => { plugin[i] = mt; plugin[mt.type] = mt; mt.enabledPlugin = plugin; });
      return plugin;
    };
    const pdf = makeMime('application/pdf', 'pdf', 'Portable Document Format');
    const arr = [
      makePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdf]),
      makePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdf]),
      makePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdf]),
    ];
    arr.item = (i) => arr[i] || null;
    arr.namedItem = (n) => arr.find(p => p.name === n) || null;
    arr.refresh = () => {};
    Object.setPrototypeOf(arr, PluginArray.prototype);
    Object.defineProperty(Navigator.prototype, 'plugins', { get: () => arr, configurable: true });
  } catch (e) {}

  try {
    Object.defineProperty(Navigator.prototype, 'languages', {
      get: () => ['en-US', 'en', 'id-ID', 'id'], configurable: true,
    });
  } catch (e) {}

  try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
      if (parameter === 37445) return 'Google Inc. (Intel)';
      if (parameter === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return getParameter.apply(this, arguments);
    };
    if (typeof WebGL2RenderingContext !== 'undefined') {
      const gp2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Google Inc. (Intel)';
        if (parameter === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return gp2.apply(this, arguments);
      };
    }
  } catch (e) {}

  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', UPDATE: 'update' },
        PlatformOs: { WIN: 'win', MAC: 'mac', LINUX: 'linux' },
        connect: () => ({ onMessage: { addListener: () => {} }, postMessage: () => {} }),
        sendMessage: () => {},
      };
    }
    if (!window.chrome.loadTimes) {
      window.chrome.loadTimes = () => ({
        commitLoadTime: Date.now()/1000, connectionInfo:'h2',
        finishDocumentLoadTime: Date.now()/1000, finishLoadTime: Date.now()/1000,
        firstPaintAfterLoadTime:0, firstPaintTime: Date.now()/1000,
        navigationType:'Other', npnNegotiatedProtocol:'h2',
        requestTime: Date.now()/1000-1, startLoadTime: Date.now()/1000-1,
        wasAlternateProtocolAvailable:false, wasFetchedViaSpdy:true, wasNpnNegotiated:true,
      });
    }
    if (!window.chrome.csi) {
      window.chrome.csi = () => ({ onloadT: Date.now(), pageT: Date.now()-1, startE: Date.now()-2, tran: 15 });
    }
  } catch (e) {}

  try {
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission, onchange: null })
          : origQuery(parameters)
      );
    }
  } catch (e) {}

  try {
    delete window.__playwright__;
    delete window.__pwInitScripts__;
    delete window.__PLAYWRIGHT_GUID__;
  } catch (e) {}
})();
"""


# ---------------------------------------------------------------------------
# Real Chrome detection helpers
# ---------------------------------------------------------------------------

def _find_real_chrome() -> Optional[str]:
    """Auto-detect Chrome executable path on Windows / macOS / Linux."""
    candidates: list[str] = []

    if sys.platform == "win32":
        import winreg
        # Try registry first
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (
                r"SOFTWARE\Google\Chrome\BLBeacon",
                r"SOFTWARE\Wow6432Node\Google\Chrome\BLBeacon",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
            ):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        candidates.append(str(val))
                except Exception:
                    pass
        # Fallback paths
        for base in (
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))

    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]

    else:  # Linux
        candidates = [
            shutil.which("google-chrome") or "",
            shutil.which("google-chrome-stable") or "",
            shutil.which("chromium-browser") or "",
            shutil.which("chromium") or "",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _default_chrome_user_data() -> Optional[str]:
    """Return the default Chrome User Data directory for this OS."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        return os.path.join(local, "Google", "Chrome", "User Data") if local else None
    elif sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    else:
        return os.path.expanduser("~/.config/google-chrome")


def _resolve_profile_dir(chrome_user_data: str, profile: str) -> str:
    """
    Resolve a profile to a folder name inside Chrome's User Data dir.

    Accepts:
      - A folder name like "Profile 25" or "Default"
      - A plain integer like 25  →  "Profile 25"
      - A full path (returned as-is if it looks absolute)
    """
    profile = str(profile).strip()
    # Plain integer → "Profile N"
    if profile.isdigit():
        profile = f"Profile {profile}"
    # If it's already an absolute path, return the user_data dir itself
    # and let the caller use the folder name separately
    return profile


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CaptureResult:
    success: bool
    platform: str
    label: str
    session_path: Optional[Path] = None
    cookie_count: int = 0
    has_required_cookies: bool = False
    missing_cookies: list = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success, "platform": self.platform,
            "label": self.label,
            "session_path": str(self.session_path) if self.session_path else None,
            "cookie_count": self.cookie_count,
            "has_required_cookies": self.has_required_cookies,
            "missing_cookies": self.missing_cookies,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# LoginCapture
# ---------------------------------------------------------------------------

class LoginCapture:
    PLATFORM: str = "override-me"
    LOGIN_URL: str = ""
    POST_LOGIN_HINT: str = ""
    REQUIRED_COOKIES: tuple = ()
    LOCALE: str = "en-US"
    TIMEZONE: str = "Asia/Jakarta"
    VIEWPORT: dict = None
    USE_PERSISTENT_PROFILE: bool = True
    CLEAR_PROFILE_AFTER_SAVE: bool = False

    # --- Real Chrome config (set via config or subclass) ---
    # These are read from ai_chat_bridge_config.json at runtime by AIChatBridgeApp
    # and passed through status_callback; but you can also hard-code them here
    # or override per-platform subclass.
    # Class-level flag: set True to skip cookie capture and use direct CDP control
    DIRECT_CDP = False
    # Will hold the CDP URL after launch for chat_engine to use
    _cdp_url_result: Optional[str] = None

    REAL_CHROME_EXE: Optional[str] = None          # path to chrome.exe / google-chrome
    REAL_CHROME_USER_DATA: Optional[str] = None    # path to User Data dir
    REAL_CHROME_PROFILE: Optional[str] = None      # e.g. "Profile 25" or "25"

    def __init__(self, label: str, sessions_dir: Path,
                 status_callback: Optional[Callable[[str], None]] = None,
                 real_chrome_exe: Optional[str] = None,
                 real_chrome_user_data: Optional[str] = None,
                 real_chrome_profile: Optional[str] = None):
        self.label = label.strip() or "default"
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._status = status_callback or (lambda msg: logger.info(msg))
        if self.VIEWPORT is None:
            self.VIEWPORT = {"width": 1366, "height": 900}

        # Real Chrome overrides (constructor args take priority over class attrs)
        if real_chrome_exe:
            self.REAL_CHROME_EXE = real_chrome_exe
        if real_chrome_user_data:
            self.REAL_CHROME_USER_DATA = real_chrome_user_data
        if real_chrome_profile:
            self.REAL_CHROME_PROFILE = real_chrome_profile

    def _use_real_chrome(self) -> bool:
        """Return True if real Chrome mode is requested and executable found."""
        if self.REAL_CHROME_PROFILE:
            return True
        return False

    def _get_chrome_exe(self) -> str:
        exe = self.REAL_CHROME_EXE or _find_real_chrome()
        if not exe or not os.path.isfile(exe):
            raise FileNotFoundError(
                "Chrome tidak ditemukan! Set 'real_chrome_exe' di config "
                "atau pastikan Google Chrome sudah terinstall."
            )
        return exe

    def _get_chrome_user_data(self) -> str:
        udata = self.REAL_CHROME_USER_DATA or _default_chrome_user_data()
        if not udata:
            raise FileNotFoundError("Chrome User Data directory tidak ditemukan.")
        return udata

    def session_path(self) -> Path:
        return self.sessions_dir / f"{self.PLATFORM}_{self._safe_label()}.json"

    def profile_dir(self) -> Path:
        return self.sessions_dir / ".profiles" / f"{self.PLATFORM}_{self._safe_label()}"

    def run(self, finished_event=None) -> CaptureResult:
        if self.DIRECT_CDP and self._use_real_chrome():
            return self._run_direct_cdp(finished_event)
        if self._use_real_chrome():
            return self._run_real_chrome(finished_event)
        if self.USE_PERSISTENT_PROFILE:
            return self._run_persistent(finished_event)
        return self._run_ephemeral(finished_event)

    # ------------------------------------------------------------------
    # DIRECT CDP: Launch Chrome with CDP, skip login/cookie capture.
    # The chat_engine connects directly via CDP to automate the browser.
    # ------------------------------------------------------------------

    def _run_direct_cdp(self, finished_event=None) -> CaptureResult:
        """
        Launch the user's real Chrome with --remote-debugging-port so
        chat_engine can connect via CDP and control it directly.

        No cookies are captured or saved.  The browser stays open and
        chat_engine talks to it over CDP.
        """
        import subprocess
        import socket
        import time

        try:
            chrome_exe = self._get_chrome_exe()
            user_data  = self._get_chrome_user_data()
            profile_name = _resolve_profile_dir(user_data, self.REAL_CHROME_PROFILE or "Default")
        except FileNotFoundError as e:
            return CaptureResult(success=False, platform=self.PLATFORM,
                                 label=self.label, error=str(e))

        # Kill existing Chrome (required — Chrome ignores CDP flag otherwise)
        self._status(f"[{self.PLATFORM}] 🔴 Menutup Chrome yang sedang berjalan...")
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/IM", "chrome.exe"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.run(["pkill", "-9", "-f", "google-chrome"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3.0)
        except Exception:
            pass

        # Remove stale lock files
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_file = Path(user_data) / lock_name
            try:
                if lock_file.exists():
                    lock_file.unlink(missing_ok=True)
            except Exception:
                pass

        # Chrome refuses CDP from default data dir — use a separate dir with junction
        cdp_data_dir = Path(user_data).parent / "ChromeCDP"
        cdp_data_dir.mkdir(exist_ok=True)

        # Junction the profile
        cdp_profile_dir = cdp_data_dir / profile_name
        source_profile_dir = Path(user_data) / profile_name
        if source_profile_dir.exists():
            if cdp_profile_dir.exists() or cdp_profile_dir.is_symlink():
                try:
                    if sys.platform == "win32":
                        # Remove junction
                        subprocess.run(["cmd", "/c", "rmdir", str(cdp_profile_dir)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        cdp_profile_dir.unlink()
                except Exception:
                    shutil.rmtree(cdp_profile_dir, ignore_errors=True)

            if sys.platform == "win32":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(cdp_profile_dir), str(source_profile_dir)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                try:
                    cdp_profile_dir.symlink_to(source_profile_dir)
                except Exception:
                    shutil.copytree(source_profile_dir, cdp_profile_dir, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns("Cache", "Code Cache",
                                                                   "Service Worker", "GPUCache"))

        # Copy Local State
        for fname in ("Local State",):
            src = Path(user_data) / fname
            dst = cdp_data_dir / fname
            if src.exists():
                try: shutil.copy2(src, dst)
                except Exception: pass

        # Pick CDP port
        cdp_port = 9222
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", cdp_port))
        except OSError:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                cdp_port = s.getsockname()[1]

        cdp_user_data = str(cdp_data_dir)

        cmd = [
            chrome_exe,
            f"--remote-debugging-port={cdp_port}",
            f"--profile-directory={profile_name}",
            f"--user-data-dir={cdp_user_data}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-features=ChromeWhatsNewUI",
            "--no-service-autorun",
            self.LOGIN_URL or "about:blank",
        ]

        # Launch Chrome
        if sys.platform == "win32":
            def _wquote(arg: str) -> str:
                return f'"{arg}"' if " " in arg else arg
            cmd_str = " ".join(_wquote(c) for c in cmd)
            self._status(f"[{self.PLATFORM}] 🟢 Launching Chrome (Direct CDP)\n  CMD: {cmd_str}")
        else:
            cmd_str = None

        try:
            if cmd_str:
                proc = subprocess.Popen(cmd_str, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, shell=True)
            else:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        except Exception as e:
            return CaptureResult(success=False, platform=self.PLATFORM,
                                 label=self.label, error=f"Chrome launch failed: {e}")

        # Wait for CDP
        self._status(f"[{self.PLATFORM}] Menunggu CDP port {cdp_port}...")
        deadline = time.time() + 45
        connected = False
        while time.time() < deadline:
            if proc.poll() is not None:
                return CaptureResult(success=False, platform=self.PLATFORM,
                                     label=self.label,
                                     error=f"Chrome exited immediately (code {proc.returncode})")
            try:
                with socket.create_connection(("127.0.0.1", cdp_port), timeout=1):
                    connected = True
                    break
            except OSError:
                time.sleep(0.5)

        if not connected:
            try: proc.terminate()
            except Exception: pass
            return CaptureResult(success=False, platform=self.PLATFORM,
                                 label=self.label,
                                 error=f"CDP port {cdp_port} tidak terbuka dalam 45s.")

        cdp_url = f"http://127.0.0.1:{cdp_port}"
        self._cdp_url_result = cdp_url
        self._status(
            f"[{self.PLATFORM}] ✅ Chrome ready! CDP: {cdp_url}\n"
            f"  Browser langsung dikontrol via CDP — no cookies needed."
        )

        # Create a dummy session file so chat_engine doesn't complain
        out_path = self.session_path()
        dummy_state = {"cookies": [], "origins": [], "_direct_cdp": True,
                       "_cdp_url": cdp_url}
        out_path.write_text(json.dumps(dummy_state, indent=2), encoding="utf-8")

        return CaptureResult(success=True, platform=self.PLATFORM,
                             label=self.label, session_path=str(out_path),
                             cookie_count=0, has_required_cookies=True)

    # ------------------------------------------------------------------
    # NEW: Real Chrome mode  (subprocess + CDP connect)
    # ------------------------------------------------------------------

    def _run_real_chrome(self, finished_event=None) -> CaptureResult:
        """
        Launch Chrome via subprocess with --remote-debugging-port, then connect
        Playwright via connect_over_cdp().

        Key constraint: Chrome refuses --remote-debugging-port when another
        Chrome instance is already running with the same user-data-dir — the new
        process simply delegates to the existing instance and exits immediately,
        so the port never opens.

        Strategy:
          1. Kill any existing chrome.exe processes (Windows: taskkill, POSIX: pkill)
          2. Wait a moment for profile locks to release
          3. Launch fresh Chrome with --remote-debugging-port
          4. Connect Playwright via CDP
        """
        import subprocess
        import socket
        import time

        out_path = self.session_path()

        try:
            chrome_exe = self._get_chrome_exe()
            user_data  = self._get_chrome_user_data()
            profile_name = _resolve_profile_dir(user_data, self.REAL_CHROME_PROFILE or "Default")
        except FileNotFoundError as e:
            return CaptureResult(success=False, platform=self.PLATFORM,
                                 label=self.label, error=str(e))

        # ------------------------------------------------------------------
        # Step 1: Kill existing Chrome so our new instance owns the port
        # ------------------------------------------------------------------
        self._status(f"[{self.PLATFORM}] 🔴 Menutup Chrome yang sedang berjalan...")
        try:
            if sys.platform == "win32":
                # /T = kill entire process tree, ensures background children die too
                subprocess.run(
                    ["taskkill", "/F", "/T", "/IM", "chrome.exe"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                # Also kill any lingering crashpad/update handlers
                for proc_name in ("GoogleCrashHandler.exe", "GoogleCrashHandler64.exe"):
                    subprocess.run(
                        ["taskkill", "/F", "/IM", proc_name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
            else:
                subprocess.run(
                    ["pkill", "-9", "-f", "google-chrome"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            # Wait longer for profile lock files (SingletonLock, lockfile) to release
            time.sleep(4.0)
        except Exception as e:
            self._status(f"[{self.PLATFORM}] Kill Chrome warning: {e} (continuing)")

        # Remove stale lock files that prevent Chrome from using the profile
        try:
            profile_path = Path(user_data) / profile_name
            for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                lock_file = Path(user_data) / lock_name
                if lock_file.exists():
                    lock_file.unlink(missing_ok=True)
                    self._status(f"[{self.PLATFORM}] 🗑️ Removed stale {lock_name}")
        except Exception as e:
            self._status(f"[{self.PLATFORM}] Lock cleanup warning: {e} (continuing)")

        # Diagnostic: check if any chrome.exe survived the kill
        if sys.platform == "win32":
            try:
                check = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                    capture_output=True, text=True, timeout=5
                )
                chrome_lines = [l for l in check.stdout.strip().splitlines()
                                if "chrome.exe" in l.lower()]
                if chrome_lines:
                    self._status(
                        f"[{self.PLATFORM}] ⚠️ WARNING: {len(chrome_lines)} chrome.exe "
                        f"process(es) STILL ALIVE after taskkill!\n"
                        f"  {chrome_lines[0]}"
                    )
                    # Try one more aggressive kill
                    subprocess.run(
                        ["wmic", "process", "where", "name='chrome.exe'", "delete"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    time.sleep(2.0)
                else:
                    self._status(f"[{self.PLATFORM}] ✅ No surviving chrome.exe processes")
            except Exception:
                pass

        # ------------------------------------------------------------------
        # Step 2: Pick a CDP port — use fixed 9222 if available, else ephemeral
        # ------------------------------------------------------------------
        cdp_port = 9222
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", cdp_port))
        except OSError:
            # 9222 is taken, fall back to ephemeral
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                cdp_port = s.getsockname()[1]

        cdp_url = f"http://127.0.0.1:{cdp_port}"

        self._status(
            f"[{self.PLATFORM}] 🟢 Launching REAL Chrome via subprocess\n"
            f"  exe        : {chrome_exe}\n"
            f"  user data  : {user_data}\n"
            f"  profile    : {profile_name}\n"
            f"  CDP port   : {cdp_port}"
        )

        # ------------------------------------------------------------------
        # Step 3: Launch Chrome fresh with CDP port
        # ------------------------------------------------------------------
        # CRITICAL: Chrome refuses --remote-debugging-port when --user-data-dir
        # points to the DEFAULT Chrome data directory (even if quoted correctly).
        # This is a Chrome security restriction, not a quoting bug.
        #
        # Workaround: Create a separate user-data-dir that is NOT the default
        # Chrome location. We symlink/junction the chosen profile into it
        # so login cookies are preserved.
        # ------------------------------------------------------------------

        # Create a dedicated data dir for CDP-enabled Chrome
        cdp_data_dir = Path(user_data).parent / "ChromeCDP"
        cdp_data_dir.mkdir(exist_ok=True)

        # Copy (or re-link) the target profile into the CDP data dir
        cdp_profile_dir = cdp_data_dir / profile_name
        source_profile_dir = Path(user_data) / profile_name

        if source_profile_dir.exists():
            # Sync profile: remove old copy/link and re-create
            if cdp_profile_dir.exists() or cdp_profile_dir.is_symlink():
                try:
                    if cdp_profile_dir.is_symlink() or cdp_profile_dir.is_junction() if hasattr(cdp_profile_dir, 'is_junction') else False:
                        cdp_profile_dir.unlink()
                    else:
                        shutil.rmtree(cdp_profile_dir, ignore_errors=True)
                except Exception:
                    shutil.rmtree(cdp_profile_dir, ignore_errors=True)

            # Try junction/symlink first (fast, saves disk), fall back to copy
            linked = False
            if sys.platform == "win32":
                try:
                    # Windows junction — doesn't need admin rights unlike symlinks
                    subprocess.run(
                        ["cmd", "/c", "mklink", "/J",
                         str(cdp_profile_dir), str(source_profile_dir)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=True
                    )
                    linked = True
                    self._status(f"[{self.PLATFORM}] 🔗 Junction: {cdp_profile_dir} → {source_profile_dir}")
                except Exception:
                    pass

            if not linked:
                try:
                    cdp_profile_dir.symlink_to(source_profile_dir)
                    linked = True
                    self._status(f"[{self.PLATFORM}] 🔗 Symlink: {cdp_profile_dir} → {source_profile_dir}")
                except Exception:
                    pass

            if not linked:
                self._status(f"[{self.PLATFORM}] 📋 Copying profile (this may take a moment)...")
                shutil.copytree(source_profile_dir, cdp_profile_dir, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("Cache", "Code Cache",
                                                               "Service Worker", "GPUCache"))
                self._status(f"[{self.PLATFORM}] ✅ Profile copied to {cdp_profile_dir}")

        # Also copy essential top-level files (Local State, etc.)
        for fname in ("Local State",):
            src = Path(user_data) / fname
            dst = cdp_data_dir / fname
            if src.exists():
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass

        cdp_user_data = str(cdp_data_dir)

        cmd = [
            chrome_exe,
            f"--remote-debugging-port={cdp_port}",
            f"--profile-directory={profile_name}",
            f"--user-data-dir={cdp_user_data}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-features=ChromeWhatsNewUI",
            "--no-service-autorun",
            self.LOGIN_URL or "about:blank",
        ]

        # On Windows, build a properly quoted command string
        if sys.platform == "win32":
            def _wquote(arg: str) -> str:
                return f'"{arg}"' if " " in arg else arg
            cmd_str = " ".join(_wquote(c) for c in cmd)
            self._status(f"[{self.PLATFORM}] 🔧 CMD (shell): {cmd_str}")
        else:
            cmd_str = None

        proc = None
        chrome_log = Path(user_data).parent / "chrome_cdp_debug.log"
        try:
            log_fh = open(chrome_log, "w")
            self._status(f"[{self.PLATFORM}] 📝 Chrome stderr → {chrome_log}")
            if cmd_str:
                proc = subprocess.Popen(
                    cmd_str,
                    stdout=subprocess.DEVNULL,
                    stderr=log_fh,
                    shell=True,
                )
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=log_fh,
                )
        except Exception as e:
            return CaptureResult(success=False, platform=self.PLATFORM,
                                 label=self.label,
                                 error=f"Chrome subprocess failed to start: {e}")

        # ------------------------------------------------------------------
        # Step 4: Wait for CDP port to open (up to 45s)
        # ------------------------------------------------------------------
        self._status(f"[{self.PLATFORM}] Menunggu Chrome CDP pada port {cdp_port}...")
        deadline = time.time() + 45
        connected = False
        check_count = 0
        while time.time() < deadline:
            # Check if process died immediately (delegated to existing instance)
            if proc.poll() is not None:
                # Read stderr log for clues
                stderr_content = ""
                try:
                    log_fh.close()
                    stderr_content = chrome_log.read_text(errors="replace")[:500]
                except Exception:
                    pass
                return CaptureResult(
                    success=False, platform=self.PLATFORM, label=self.label,
                    error=(
                        f"Chrome process exited immediately (code {proc.returncode}). "
                        f"stderr: {stderr_content or '(empty)'}. "
                        "Mungkin masih ada proses Chrome tersisa. "
                        "Coba tutup Chrome manual dulu, lalu klik Open Browser & Login lagi."
                    )
                )
            try:
                with socket.create_connection(("127.0.0.1", cdp_port), timeout=1):
                    connected = True
                    break
            except OSError:
                check_count += 1
                if check_count % 10 == 0:  # every ~5s
                    self._status(f"[{self.PLATFORM}] ⏳ Still waiting... ({check_count * 0.5:.0f}s, pid={proc.pid})")
                time.sleep(0.5)

        if not connected:
            # Read stderr log for clues
            stderr_content = ""
            try:
                log_fh.close()
                stderr_content = chrome_log.read_text(errors="replace")[:1000]
            except Exception:
                pass
            try: proc.terminate()
            except Exception: pass
            return CaptureResult(success=False, platform=self.PLATFORM,
                                 label=self.label,
                                 error=(
                                     f"Chrome CDP port {cdp_port} tidak terbuka dalam 45s. "
                                     f"PID={proc.pid}. Chrome stderr:\n{stderr_content or '(empty)'}"
                                 ))

        self._status(f"[{self.PLATFORM}] Chrome ready — connecting Playwright via CDP...")
        time.sleep(1.5)  # give Chrome a moment to finish initialising tabs

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                proc.terminate()
                return CaptureResult(success=False, platform=self.PLATFORM,
                                     label=self.label,
                                     error=f"Playwright CDP connect failed: {e}")

            try:
                # Use existing context (the real Chrome profile context)
                contexts = browser.contexts
                context = contexts[0] if contexts else browser.new_context()

                try:
                    context.add_init_script(_STEALTH_INIT_JS)
                except Exception:
                    pass

                # Find or open the target tab
                pages = context.pages
                # Find a tab already on the login URL domain, or use first tab
                login_domain = self.LOGIN_URL.split("/")[2] if self.LOGIN_URL else ""
                page = None

                # Close any Google sign-in/rejected tabs that Chrome auto-opened
                for p in pages:
                    try:
                        purl = p.url or ""
                        if "accounts.google.com" in purl and ("signin" in purl or "rejected" in purl):
                            self._status(f"[{self.PLATFORM}] 🗑️ Closing Google sign-in tab")
                            p.close()
                            continue
                    except Exception:
                        pass

                # Re-fetch pages after closing
                pages = context.pages

                for p in pages:
                    try:
                        if login_domain and login_domain in (p.url or ""):
                            page = p
                            break
                    except Exception:
                        pass
                if page is None:
                    page = pages[0] if pages else context.new_page()

                # Navigate to login URL if not already there
                try:
                    if login_domain and login_domain not in (page.url or ""):
                        page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                except Exception as e:
                    self._status(f"[{self.PLATFORM}] Nav soft-error: {e} (continuing)")

                if self.POST_LOGIN_HINT:
                    self._status(f"[{self.PLATFORM}] {self.POST_LOGIN_HINT}")

                # Wait for login
                self._wait_for_login(page, finished_event)

                # --- Fast cookie capture (avoid storage_state hang) ---
                self._status(f"[{self.PLATFORM}] Collecting cookies...")
                try:
                    all_cookies = context.cookies()
                    if self.COOKIE_DOMAINS:
                        domains = tuple(self.COOKIE_DOMAINS)
                        cookies = [
                            c for c in all_cookies
                            if any(
                                c.get("domain", "").lstrip(".").endswith(d.lstrip("."))
                                for d in domains
                            )
                        ]
                        self._status(
                            f"[{self.PLATFORM}] Got {len(cookies)} cookies "
                            f"(from {len(all_cookies)} total) for {domains}"
                        )
                    else:
                        cookies = all_cookies

                    state = {"cookies": cookies, "origins": []}
                except Exception as e:
                    self._status(f"[{self.PLATFORM}] Cookie grab failed: {e}")
                    return CaptureResult(success=False, platform=self.PLATFORM,
                                         label=self.label, error=str(e))

                cookie_names = [c.get("name", "") for c in cookies]
                missing = [c for c in self.REQUIRED_COOKIES if c not in cookie_names]

                if missing and out_path.exists():
                    try:
                        existing = json.loads(out_path.read_text(encoding="utf-8"))
                        existing_names = {
                            c.get("name") for c in (existing.get("cookies") or [])
                            if isinstance(c, dict) and c.get("name") and c.get("value")
                        }
                        existing_missing = [c for c in self.REQUIRED_COOKIES
                                            if c not in existing_names]
                        if not existing_missing:
                            self._status(
                                f"[{self.PLATFORM}] Using existing valid session (skip overwrite)")
                            return CaptureResult(
                                success=True, platform=self.PLATFORM, label=self.label,
                                session_path=out_path,
                                cookie_count=len(existing.get("cookies") or []),
                                has_required_cookies=True, missing_cookies=[])
                    except Exception:
                        pass

                self._status(f"[{self.PLATFORM}] Saving session...")
                self._post_process_state(state)
                out_path.write_text(
                    json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
                self._status(
                    f"[{self.PLATFORM}] ✅ Session saved! "
                    f"({len(cookie_names)} cookies → {out_path.name})")
                return CaptureResult(
                    success=True, platform=self.PLATFORM, label=self.label,
                    session_path=out_path, cookie_count=len(cookie_names),
                    has_required_cookies=not missing, missing_cookies=missing)

            except Exception as e:
                logger.exception(f"[{self.PLATFORM}] Real Chrome capture failed")
                return CaptureResult(success=False, platform=self.PLATFORM,
                                     label=self.label, error=str(e))
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                # Do NOT kill proc — leave Chrome open for the user

    # ------------------------------------------------------------------
    # Existing modes (unchanged)
    # ------------------------------------------------------------------

    def _run_persistent(self, finished_event=None) -> CaptureResult:
        out_path = self.session_path()
        profile = self.profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            self._status(f"[{self.PLATFORM}] Launching Chromium (persistent: {profile.name}/)")
            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile), headless=False,
                    args=CHROME_ARGS, user_agent=DESKTOP_UA,
                    viewport=self.VIEWPORT, locale=self.LOCALE,
                    timezone_id=self.TIMEZONE,
                    ignore_default_args=["--enable-automation"],
                )
            except Exception as e:
                self._status(f"[{self.PLATFORM}] Persistent failed ({e}), fallback ephemeral")
                return self._run_ephemeral(finished_event)
            try:
                context.add_init_script(_STEALTH_INIT_JS)
            except Exception as e:
                self._status(f"[{self.PLATFORM}] Stealth warning: {e}")
            pages = context.pages
            page = pages[0] if pages else context.new_page()
            try:
                return self._do_capture(context, page, out_path, finished_event)
            finally:
                try: context.close()
                except: pass
                if self.CLEAR_PROFILE_AFTER_SAVE:
                    shutil.rmtree(profile, ignore_errors=True)

    def _run_ephemeral(self, finished_event=None) -> CaptureResult:
        out_path = self.session_path()
        storage_state = str(out_path) if out_path.exists() else None
        with sync_playwright() as pw:
            self._status(f"[{self.PLATFORM}] Launching Chromium (ephemeral)...")
            browser = pw.chromium.launch(headless=False, args=CHROME_ARGS,
                                         ignore_default_args=["--enable-automation"])
            context = browser.new_context(user_agent=DESKTOP_UA, viewport=self.VIEWPORT,
                                          locale=self.LOCALE, timezone_id=self.TIMEZONE,
                                          storage_state=storage_state)
            try: context.add_init_script(_STEALTH_INIT_JS)
            except: pass
            page = context.new_page()
            try:
                return self._do_capture(context, page, out_path, finished_event)
            finally:
                try: browser.close()
                except: pass

    # Override in subclass to list relevant domains for cookie filtering
    # e.g. ["chatgpt.com", "openai.com"] — used in real Chrome mode to avoid
    # calling storage_state() which hangs on Chrome with thousands of cookies.
    COOKIE_DOMAINS: tuple = ()

    def _do_capture(self, context, page, out_path, finished_event) -> CaptureResult:
        try:
            self._status(f"[{self.PLATFORM}] Opening {self.LOGIN_URL}")
            try:
                page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                self._status(f"[{self.PLATFORM}] Nav soft-error: {e} (continuing)")
            if self.POST_LOGIN_HINT:
                self._status(f"[{self.PLATFORM}] {self.POST_LOGIN_HINT}")
            self._wait_for_login(page, finished_event)

            # ------------------------------------------------------------------
            # Real Chrome mode: storage_state() hangs because Chrome has
            # thousands of cookies from hundreds of domains. Instead, grab only
            # the cookies we actually need via page.context.cookies() which is
            # fast and never blocks, then build a minimal storage_state dict.
            # ------------------------------------------------------------------
            if self._use_real_chrome() and self.COOKIE_DOMAINS:
                self._status(f"[{self.PLATFORM}] Collecting cookies (fast mode)...")
                try:
                    all_cookies = page.context.cookies()
                    # Filter to relevant domains only
                    domains = tuple(self.COOKIE_DOMAINS)
                    filtered = [
                        c for c in all_cookies
                        if any(
                            c.get("domain", "").lstrip(".").endswith(d.lstrip("."))
                            for d in domains
                        )
                    ]
                    self._status(
                        f"[{self.PLATFORM}] Got {len(filtered)} cookies "
                        f"(from {len(all_cookies)} total) for {domains}"
                    )
                    state = {"cookies": filtered, "origins": []}
                except Exception as e:
                    self._status(f"[{self.PLATFORM}] Fast cookie grab failed ({e}), trying storage_state...")
                    state = context.storage_state()
            else:
                # Standard mode — storage_state() is fine for Playwright Chromium
                state = context.storage_state()

            cookie_names = [c.get("name", "") for c in (state.get("cookies") or [])]
            missing = [c for c in self.REQUIRED_COOKIES if c not in cookie_names]

            if missing and out_path.exists():
                try:
                    existing = json.loads(out_path.read_text(encoding="utf-8"))
                    existing_names = {c.get("name") for c in (existing.get("cookies") or [])
                                      if isinstance(c, dict) and c.get("name") and c.get("value")}
                    existing_missing = [c for c in self.REQUIRED_COOKIES if c not in existing_names]
                    if not existing_missing:
                        self._status(f"[{self.PLATFORM}] Using existing valid session (skip overwrite)")
                        return CaptureResult(success=True, platform=self.PLATFORM, label=self.label,
                                             session_path=out_path,
                                             cookie_count=len(existing.get("cookies") or []),
                                             has_required_cookies=True, missing_cookies=[])
                except: pass

            self._status(f"[{self.PLATFORM}] Saving session...")
            self._post_process_state(state)
            out_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            self._status(f"[{self.PLATFORM}] ✅ Session saved! ({len(cookie_names)} cookies → {out_path.name})")
            return CaptureResult(success=True, platform=self.PLATFORM, label=self.label,
                                 session_path=out_path, cookie_count=len(cookie_names),
                                 has_required_cookies=not missing, missing_cookies=missing)
        except Exception as e:
            logger.exception(f"[{self.PLATFORM}] Capture failed")
            return CaptureResult(success=False, platform=self.PLATFORM, label=self.label, error=str(e))

    def is_logged_in(self, page: Page) -> bool:
        try:
            cookies = page.context.cookies()
            names = {c["name"] for c in cookies}
            return all(c in names for c in self.REQUIRED_COOKIES) if self.REQUIRED_COOKIES else False
        except: return False

    def _post_process_state(self, state: dict) -> None:
        pass

    def _wait_for_login(self, page, finished_event=None,
                        max_seconds=600, poll_interval=2.0):
        import time
        deadline = time.time() + max_seconds
        last_print = 0
        while time.time() < deadline:
            if finished_event is not None and finished_event.is_set():
                self._status(f"[{self.PLATFORM}] User confirmed login manually.")
                return
            try:
                if page.is_closed():
                    raise RuntimeError("Browser closed before login confirmed.")
            except: pass
            try:
                if self.is_logged_in(page):
                    self._status(f"[{self.PLATFORM}] Auto-detected: logged in.")
                    try: page.wait_for_timeout(2_000)
                    except: pass
                    return
            except: pass
            now = time.time()
            if now - last_print > 15:
                self._status(f"[{self.PLATFORM}] Waiting login... ({int(deadline-now)}s remaining)")
                last_print = now
            try: page.wait_for_timeout(int(poll_interval * 1000))
            except: time.sleep(poll_interval)
        raise TimeoutError(f"Timeout {max_seconds}s — login not detected.")

    def _safe_label(self) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in self.label)[:40]
