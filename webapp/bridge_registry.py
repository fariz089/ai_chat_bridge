"""
Bridge registry — one BridgeWorker per Chrome profile, each pinned to that
profile's CDP url.

chat_engine.BridgeWorker holds a single cdp_url for its whole pool, so to run
several accounts at once we simply keep several workers, one per profile, and
route each request to the right one. No change to chat_engine.py.

A worker is created lazily the first time a profile is used and reused after.
Because every worker is in CDP mode, it connects to the already-running Chrome
managed by the supervisor — it never launches its own browser and never needs
a cookie session file.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from chat_engine import BridgeWorker


class BridgeRegistry:
    def __init__(self, sessions_dir: Path, media_dir: Path, cdp_host: str):
        self.sessions_dir = sessions_dir
        self.media_dir = media_dir
        self.cdp_host = cdp_host
        self._workers: dict[str, BridgeWorker] = {}     # profile_id -> worker
        self._lock = threading.Lock()

    def worker_for(self, profile) -> BridgeWorker:
        """Get/create the CDP-pinned worker for a Profile."""
        with self._lock:
            w = self._workers.get(profile.id)
            if w is not None and w.is_running():
                return w
            w = BridgeWorker(self.sessions_dir, media_dir=self.media_dir)
            # IMPORTANT: start the worker thread FIRST. set_cdp_url() runs as an
            # operation on the worker's internal queue, so the thread must be
            # alive before we call it — otherwise it raises "BridgeWorker is not
            # running". headless is irrelevant in CDP mode (browser is external).
            w.start(headless=False)
            # Pin this worker to the profile's Chrome via CDP.
            w.set_cdp_url(profile.cdp_url(self.cdp_host))
            self._workers[profile.id] = w
            return w

    def close(self, profile_id: str) -> bool:
        with self._lock:
            w = self._workers.pop(profile_id, None)
        if w is not None:
            try:
                w.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            return True
        return False

    def close_all(self):
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            try:
                w.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass

    def active_ids(self) -> list[str]:
        with self._lock:
            return [pid for pid, w in self._workers.items() if w.is_running()]
