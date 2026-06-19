"""
Pool router — load-balancing + queueing across multiple accounts.

Problem this solves
-------------------
The old flow pinned each Generate job to ONE profile id the user picked.
If two people generated at once with the same Grok account, their jobs
serialized on that single account's BridgeWorker and one waited for the
other (or worse, collided in the same chat tab). Adding accounts didn't
help because nothing routed traffic to the spare ones.

What this does
--------------
A ``ProfileRouter`` groups profiles by platform. When a job needs (say) a
Grok account it calls ``lease(platform="grok")`` which:

  1. picks the least-busy *reachable & logged-in* profile of that platform,
  2. marks it busy (so the next request goes to a different account),
  3. hands back a leased worker the job uses for its whole turn,
  4. releases it when the ``with`` block exits — even on error.

If every account of that platform is busy, ``lease`` blocks (queues) until
one frees up, up to ``acquire_timeout`` seconds. This gives true N-account
concurrency: with two Grok accounts, two jobs run in parallel; a third
waits for whichever finishes first.

Reachability/login are checked with a short-lived probe callback supplied
by the web app (it already has ``cdp_probe`` + ``infer_login``), so the
router stays decoupled from Flask.

Thread-safety: a single condition variable guards the busy map. ``lease``
blocks on it; ``release`` notifies waiters. BridgeWorkers themselves are
unchanged and remain single-threaded internally.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional


class NoProfileAvailable(RuntimeError):
    """Raised when no reachable/logged-in profile of a platform exists."""


class LeaseTimeout(RuntimeError):
    """Raised when all matching profiles stayed busy past acquire_timeout."""


class Lease:
    """A borrowed (profile, worker) pair, returned by ProfileRouter.lease()."""

    def __init__(self, profile, worker):
        self.profile = profile
        self.worker = worker

    @property
    def profile_id(self) -> str:
        return self.profile.id

    @property
    def label(self) -> str:
        return self.profile.label

    @property
    def platform(self) -> str:
        return self.profile.platform


class ProfileRouter:
    """
    Routes platform requests to the least-busy healthy account.

    Parameters
    ----------
    store : ProfileStore
        Source of truth for the configured profiles.
    registry : BridgeRegistry
        Gives us a BridgeWorker pinned to a profile's CDP url.
    health_check : Callable[[Profile], bool]
        Returns True if the profile's Chrome is reachable AND logged in.
        The web app passes a closure over cdp_probe + infer_login.
    """

    def __init__(self, store, registry, health_check: Callable[[object], bool]):
        self.store = store
        self.registry = registry
        self.health_check = health_check
        self._cond = threading.Condition()
        # profile_id -> int (lease count; >0 means busy). A simple count lets
        # us keep one entry per profile and treat 0 as free.
        self._busy: dict[str, int] = {}
        # short cache of health results so we don't hammer CDP on every retry
        self._health_cache: dict[str, tuple[float, bool]] = {}
        self._health_ttl = 5.0  # seconds

    # ------------------------------------------------------------------
    # Health (cached)
    # ------------------------------------------------------------------
    def _healthy(self, profile) -> bool:
        now = time.time()
        cached = self._health_cache.get(profile.id)
        if cached and (now - cached[0]) < self._health_ttl:
            return cached[1]
        try:
            ok = bool(self.health_check(profile))
        except Exception:
            ok = False
        self._health_cache[profile.id] = (now, ok)
        return ok

    def invalidate_health(self, profile_id: Optional[str] = None):
        if profile_id is None:
            self._health_cache.clear()
        else:
            self._health_cache.pop(profile_id, None)

    # ------------------------------------------------------------------
    # Candidate selection
    # ------------------------------------------------------------------
    def _candidates(self, platform: str) -> list:
        return [p for p in self.store.list() if p.platform == platform]

    def _pick_free(self, platform: str):
        """Return the least-busy healthy profile, or None if none free now.

        'Least-busy' = lowest current lease count; ties broken by id so the
        choice is stable/deterministic. A profile with count 0 is preferred.
        """
        cands = self._candidates(platform)
        if not cands:
            return None
        # Order healthy candidates by (busy_count, id).
        ranked = []
        for p in cands:
            if not self._healthy(p):
                continue
            cnt = self._busy.get(p.id, 0)
            ranked.append((cnt, p.id, p))
        if not ranked:
            return None
        ranked.sort(key=lambda t: (t[0], t[1]))
        cnt, _id, prof = ranked[0]
        # Only hand back a *free* one. If the least-busy is still busy, caller
        # should wait. (Single-tab accounts can't safely run two jobs at once.)
        if cnt > 0:
            return None
        return prof

    def has_capacity(self, platform: str) -> bool:
        """True if at least one healthy profile of the platform exists."""
        with self._cond:
            return any(self._healthy(p) for p in self._candidates(platform))

    # ------------------------------------------------------------------
    # Lease / release
    # ------------------------------------------------------------------
    @contextmanager
    def lease(self, platform: str, *, acquire_timeout: float = 600.0,
              prefer_id: Optional[str] = None,
              on_wait: Optional[Callable[[int], None]] = None):
        """Borrow a healthy, free account of ``platform``.

        prefer_id : if given and that profile is healthy+free, take it first
                    (used when the user explicitly picked an account instead
                    of "Auto"). If it's busy, we still wait for *it*
                    specifically so manual picks aren't silently rerouted.
        on_wait   : optional callback invoked once with the queue position-ish
                    info (seconds waited so far, rounded) so the job log can
                    say "menunggu akun kosong…".

        Yields a Lease. Always releases on exit.
        """
        profile = self._acquire(platform, acquire_timeout, prefer_id, on_wait)
        worker = self.registry.worker_for(profile)
        lease = Lease(profile, worker)
        try:
            yield lease
        finally:
            self._release(profile.id)

    def _acquire(self, platform, acquire_timeout, prefer_id, on_wait):
        deadline = time.time() + acquire_timeout
        notified = False
        with self._cond:
            # Fail fast if the platform has no usable account at all.
            if not any(self._healthy(p) for p in self._candidates(platform)):
                # Force a fresh health pass once before giving up (cache may be
                # stale from a moment ago when Chrome was still booting).
                self.invalidate_health()
                if not any(self._healthy(p) for p in self._candidates(platform)):
                    raise NoProfileAvailable(
                        f"Tidak ada akun {platform} yang siap (Chrome mati / belum login)."
                    )

            while True:
                # Manual pick: only that specific profile.
                if prefer_id:
                    prof = self.store.get(prefer_id)
                    if prof and prof.platform == platform and self._healthy(prof) \
                            and self._busy.get(prof.id, 0) == 0:
                        self._busy[prof.id] = 1
                        return prof
                else:
                    prof = self._pick_free(platform)
                    if prof is not None:
                        self._busy[prof.id] = self._busy.get(prof.id, 0) + 1
                        return prof

                remaining = deadline - time.time()
                if remaining <= 0:
                    raise LeaseTimeout(
                        f"Semua akun {platform} sibuk; timeout menunggu giliran."
                    )
                if on_wait and not notified:
                    notified = True
                    try:
                        on_wait(int(acquire_timeout - remaining))
                    except Exception:
                        pass
                # Wait to be notified by a release, or re-check periodically in
                # case a busy account frees without notify (defensive) or a new
                # Chrome finished booting.
                self._cond.wait(timeout=min(remaining, 2.0))

    def _release(self, profile_id: str):
        with self._cond:
            cur = self._busy.get(profile_id, 0)
            if cur <= 1:
                self._busy.pop(profile_id, None)
            else:
                self._busy[profile_id] = cur - 1
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Introspection (for a /api/status-style view)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._cond:
            per_platform: dict[str, dict] = {}
            for p in self.store.list():
                bucket = per_platform.setdefault(
                    p.platform, {"total": 0, "healthy": 0, "busy": 0, "free": 0,
                                 "accounts": []})
                healthy = self._healthy(p)
                busy = self._busy.get(p.id, 0) > 0
                bucket["total"] += 1
                if healthy:
                    bucket["healthy"] += 1
                    if busy:
                        bucket["busy"] += 1
                    else:
                        bucket["free"] += 1
                bucket["accounts"].append({
                    "id": p.id, "label": p.label,
                    "healthy": healthy, "busy": busy,
                })
            return per_platform
