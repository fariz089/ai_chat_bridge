#!/usr/bin/env python3
"""
Chrome supervisor — runs inside the `chrome` container.

Reads profiles.json and launches ONE Google Chrome per profile:

    google-chrome \
      --user-data-dir=/profiles/<id> \
      --remote-debugging-port=<port> \
      --remote-debugging-address=0.0.0.0 \
      --no-sandbox ...

Each Chrome shows on a shared virtual X display (Xvfb) that noVNC exposes,
so you can log in to each profile once through the browser-based VNC.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PROFILES_JSON = Path(os.environ.get("PROFILES_JSON", "/config/profiles.json"))
PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", "/profiles"))
POLL_SECONDS = 5
LOG_DIR = Path("/tmp/chrome_logs")
LOG_DIR.mkdir(exist_ok=True)


def chrome_flags(user_data_dir: str, internal_port: int) -> list[str]:
    return [
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={internal_port}",
        # NOTE: Chrome 111+ ignores --remote-debugging-address and ALWAYS binds
        # the CDP port to 127.0.0.1 for security. We therefore let Chrome bind
        # to localhost and run a socat bridge (see ManagedChrome.start) that
        # republishes the port on 0.0.0.0 so the `web` container can reach it.
        # We must also allow the cross-origin/host CDP handshake:
        f"--remote-allow-origins=*",
        # REQUIRED in containers: Chrome refuses to start as root without this.
        "--no-sandbox",
        "--disable-setuid-sandbox",
        # /dev/shm is often too small in Docker; use /tmp instead.
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-features=Translate,OptimizationHints",
        "--disable-popup-blocking",
        "--start-maximized",
        "--password-store=basic",
        "--use-mock-keychain",
        "about:blank",
    ]


def find_chrome() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    print("[supervisor] ERROR: no Chrome/Chromium binary found", flush=True)
    sys.exit(1)


class ManagedChrome:
    def __init__(self, chrome_bin: str, profile: dict):
        self.chrome_bin = chrome_bin
        self.id = profile["id"]
        self.port = profile["port"]                 # PUBLIC port (web container hits this)
        self.internal_port = self.port + 1000       # Chrome binds here on 127.0.0.1
        self.user_data_dir = profile["user_data_dir"]
        self.proc: subprocess.Popen | None = None
        self.socat: subprocess.Popen | None = None
        self.log_path = LOG_DIR / f"{self.id}.log"
        Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)

    def alive(self) -> bool:
        chrome_ok = self.proc is not None and self.proc.poll() is None
        socat_ok = self.socat is not None and self.socat.poll() is None
        return chrome_ok and socat_ok

    def clear_singleton_locks(self):
        """Remove stale Chrome profile locks.

        When the container is killed while Chrome is still running, files like
        SingletonLock / SingletonCookie / SingletonSocket are left behind,
        still pointing at the old hostname+PID. On next boot Chrome thinks the
        profile is "in use by another computer" and refuses to start, looping
        forever. These locks are safe to delete because only ONE Chrome ever
        owns this profile inside this container.
        """
        base = Path(self.user_data_dir)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = base / name
            try:
                if p.is_symlink() or p.exists():
                    p.unlink()
                    print(f"[supervisor] cleared stale {name} for {self.id}",
                          flush=True)
            except FileNotFoundError:
                pass
            except Exception as e:  # noqa: BLE001
                print(f"[supervisor] could not remove {p}: {e}", flush=True)

    def start(self):
        if self.alive():
            return
        # Clean up any half-dead pair before restarting.
        self.stop()
        # Remove stale profile locks left by a previously-killed container.
        self.clear_singleton_locks()

        cmd = [self.chrome_bin] + chrome_flags(self.user_data_dir,
                                               self.internal_port)
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":99")
        log_fh = open(self.log_path, "a")
        self.proc = subprocess.Popen(cmd, env=env,
                                     stdout=log_fh, stderr=log_fh)
        print(f"[supervisor] started {self.id} Chrome on 127.0.0.1:"
              f"{self.internal_port} (pid {self.proc.pid}) log={self.log_path}",
              flush=True)

        # Bridge: publish 0.0.0.0:<public> → 127.0.0.1:<internal> so the web
        # container can reach CDP. Chrome 111+ binds CDP only to localhost AND
        # rejects HTTP requests whose Host header isn't an IP/localhost. The proxy
        # rewrites the request Host to "localhost" AND rewrites the
        # webSocketDebuggerUrl in the response (ws://localhost/...) back to
        # ws://<public_host>:<port>/... so Playwright reconnects through us.
        # public_host must be the name the web container uses (the compose
        # service name), default "chrome".
        public_host = os.environ.get("CDP_PUBLIC_HOST", "chrome")
        proxy_script = str(Path(__file__).resolve().parent / "cdp_proxy.py")
        proxy_cmd = [sys.executable, proxy_script,
                     str(self.port), str(self.internal_port), public_host]
        self.socat = subprocess.Popen(proxy_cmd, stdout=log_fh, stderr=log_fh)
        print(f"[supervisor] cdp_proxy {self.id}: 0.0.0.0:{self.port} → "
              f"127.0.0.1:{self.internal_port} ws→{public_host}:{self.port} "
              f"(pid {self.socat.pid})", flush=True)

    def stop(self):
        for proc in (self.socat, self.proc):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.socat = None
        self.proc = None


def load_profiles() -> list[dict]:
    if not PROFILES_JSON.exists():
        return []
    try:
        raw = json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[supervisor] bad profiles.json: {e}", flush=True)
        return []
    out = []
    for row in raw:
        out.append({
            "id": row["id"], "port": int(row["port"]),
            "user_data_dir": str(PROFILES_DIR / row["id"]),
        })
    return out


def main():
    chrome_bin = find_chrome()
    print(f"[supervisor] Chrome: {chrome_bin}", flush=True)
    print(f"[supervisor] profiles.json: {PROFILES_JSON}", flush=True)

    managed: dict[str, ManagedChrome] = {}
    last_sig = None

    def shutdown(*_):
        print("[supervisor] shutting down…", flush=True)
        for m in managed.values():
            m.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        profiles = load_profiles()
        sig = json.dumps(sorted((p["id"], p["port"]) for p in profiles))

        if sig != last_sig:
            want_ids = {p["id"] for p in profiles}
            for pid in list(managed.keys()):
                if pid not in want_ids:
                    print(f"[supervisor] removing {pid}", flush=True)
                    managed[pid].stop()
                    managed.pop(pid, None)
            for p in profiles:
                if p["id"] not in managed:
                    managed[p["id"]] = ManagedChrome(chrome_bin, p)
            last_sig = sig

        for m in managed.values():
            if not m.alive():
                print(f"[supervisor] (re)starting {m.id}", flush=True)
                m.start()

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
