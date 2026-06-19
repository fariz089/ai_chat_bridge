"""
Profile manager — the heart of the unified CDP architecture.

Old model: cookie session files (chatgpt/grok) + direct-CDP (gemini/aistudio).
New model: EVERYTHING is a Chrome profile reached over CDP. One account =
one profile = one Chrome instance = one CDP port.

    profiles.json
    [
      {"id": "grok_main",   "platform": "grok",    "label": "main",  "port": 9302},
      {"id": "gpt_work",    "platform": "chatgpt", "label": "work",  "port": 9301},
      {"id": "grok_alt",    "platform": "grok",    "label": "alt",   "port": 9304}
    ]

To add an account, add a row (the UI does this) and (re)start the Chrome
supervisor — a new Chrome boots with --user-data-dir=profiles/<id> and
--remote-debugging-port=<port>. Log in once via noVNC; the profile persists
on the mounted volume.

The web layer keeps ONE BridgeWorker per profile (each pinned to that
profile's CDP url), so multiple accounts of the same platform coexist.
chat_engine.py is unchanged: we just hand each worker a different cdp_url.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


# Ports are allocated from this base upward as profiles are added.
PORT_BASE = 9301
PORT_MAX = 9400


@dataclass
class Profile:
    id: str                 # folder name under profiles/, e.g. "grok_main"
    platform: str           # chatgpt | grok | gemini | aistudio
    label: str              # human label, e.g. "main"
    port: int               # CDP remote-debugging-port for this profile's Chrome

    # CDP host is configurable so the web container can reach the chrome
    # container by service name in docker-compose (default: localhost).
    def cdp_url(self, host: str) -> str:
        return f"http://{host}:{self.port}"


class ProfileStore:
    def __init__(self, path: Path, profiles_dir: Path):
        self.path = Path(path)
        self.profiles_dir = Path(profiles_dir)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._profiles: dict[str, Profile] = {}
        self.load()

    # ---- persistence ----
    def load(self):
        with self._lock:
            self._profiles = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    for row in raw:
                        p = Profile(id=row["id"], platform=row["platform"],
                                    label=row.get("label", "default"),
                                    port=int(row["port"]))
                        self._profiles[p.id] = p
                except Exception:  # noqa: BLE001
                    self._profiles = {}

    def save(self):
        with self._lock:
            rows = [asdict(p) for p in self._profiles.values()]
        self.path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    # ---- queries ----
    def list(self) -> list[Profile]:
        with self._lock:
            return list(self._profiles.values())

    def get(self, profile_id: str) -> Optional[Profile]:
        with self._lock:
            return self._profiles.get(profile_id)

    def find(self, platform: str, label: str) -> Optional[Profile]:
        with self._lock:
            for p in self._profiles.values():
                if p.platform == platform and p.label == label:
                    return p
            return None

    def _next_port(self) -> int:
        used = {p.port for p in self._profiles.values()}
        for port in range(PORT_BASE, PORT_MAX):
            if port not in used:
                return port
        raise RuntimeError("No free CDP ports left (9301-9400 exhausted).")

    # ---- mutations ----
    def add(self, platform: str, label: str) -> Profile:
        label = (label or "default").strip()
        if self.find(platform, label):
            raise ValueError(f"Profil {platform}:{label} sudah ada.")
        pid = "".join(c for c in f"{platform}_{label}"
                      if c.isalnum() or c in "-_").lower()[:40]
        with self._lock:
            if pid in self._profiles:
                raise ValueError(f"ID profil '{pid}' bentrok.")
            port = self._next_port()
            prof = Profile(id=pid, platform=platform, label=label, port=port)
            self._profiles[pid] = prof
            (self.profiles_dir / pid).mkdir(parents=True, exist_ok=True)
        self.save()
        return prof

    def remove(self, profile_id: str) -> bool:
        with self._lock:
            existed = self._profiles.pop(profile_id, None) is not None
        if existed:
            self.save()
        return existed

    def as_supervisor_config(self) -> list[dict]:
        """What the Chrome supervisor reads to launch one Chrome per profile."""
        return [{"id": p.id, "port": p.port,
                 "user_data_dir": str((self.profiles_dir / p.id).resolve())}
                for p in self.list()]
