"""
Background job manager.

The desktop app ran long tasks (Generate, Capture, Live) on threads and
streamed their log into a Tk widget. The web app does the same, but the log
goes into a per-job ring buffer that the browser tails over Server-Sent
Events (see /api/jobs/<id>/stream in app.py).

Nothing here is FakeFluencer-specific — it's a tiny generic job runner so
every long action in the UI behaves the same way: start, watch the log,
see a final status, optionally collect a result (e.g. a zip path).
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Job:
    id: str
    kind: str                       # "generate" | "capture" | "live" | ...
    title: str
    status: str = "running"         # running | done | error | cancelled
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    result: dict = field(default_factory=dict)
    error: str = ""
    # Log lines, capped so a runaway job can't eat memory.
    _log: deque = field(default_factory=lambda: deque(maxlen=2000))
    _seq: int = 0                   # monotonically increasing line counter
    _cancel: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # ---- mid-job user confirmation (e.g. confirm product before drawing) ----
    # When a worker needs an answer from the user it parks here: it publishes a
    # `prompt` payload, flips status to "awaiting_input", and blocks on _ack.
    # The browser shows a dialog, POSTs the answer, and the worker resumes.
    prompt: Optional[dict] = None   # what to ask the user (surfaced in to_dict)
    answer: Optional[dict] = None   # what the user sent back (None = cancelled)
    _ack: threading.Event = field(default_factory=threading.Event)

    # ---- logging ----
    def log(self, msg: str):
        with self._lock:
            self._seq += 1
            self._log.append((self._seq, time.strftime("%H:%M:%S"), str(msg)))

    def lines_since(self, after_seq: int):
        with self._lock:
            return [(s, t, m) for (s, t, m) in self._log if s > after_seq]

    # ---- cancellation (cooperative) ----
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def request_cancel(self):
        self._cancel.set()
        # Unblock anything waiting on a user answer so the worker can exit.
        self._ack.set()

    # ---- mid-job confirmation (cooperative, blocking on the worker thread) ----
    def ask_user(self, prompt: dict) -> Optional[dict]:
        """Park the job, surface `prompt` to the browser, and block until the
        user answers (via submit_answer) or the job is cancelled.

        Returns the answer dict on confirm, or None if cancelled.
        """
        with self._lock:
            self.prompt = dict(prompt or {})
            self.answer = None
            self.status = "awaiting_input"
        self._ack.clear()
        self._ack.wait()
        with self._lock:
            self.prompt = None
            if self._cancel.is_set():
                return None
            self.status = "running"
            return self.answer

    def submit_answer(self, answer: Optional[dict]) -> bool:
        """Called from the request thread when the user responds. answer=None
        means the user cancelled. Returns False if the job wasn't waiting."""
        with self._lock:
            if self.status != "awaiting_input":
                return False
            self.answer = answer
        if answer is None:
            self._cancel.set()
        self._ack.set()
        return True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "created": self.created,
            "finished": self.finished,
            "result": self.result,
            "error": self.error,
            "prompt": self.prompt,
            "last_seq": self._seq,
        }


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, title: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, kind: Optional[str] = None) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        if kind:
            jobs = [j for j in jobs if j.kind == kind]
        jobs.sort(key=lambda j: j.created, reverse=True)
        return [j.to_dict() for j in jobs]

    def run(self, kind: str, title: str,
            target: Callable[[Job], Any]) -> Job:
        """Spawn `target(job)` on a daemon thread; manages status + errors."""
        job = self.create(kind, title)

        def _wrap():
            try:
                target(job)
                if job.cancelled:
                    job.status = "cancelled"
                elif job.status == "running":
                    job.status = "done"
            except Exception as e:  # noqa: BLE001
                job.status = "error"
                job.error = str(e)
                job.log(f"\u2717 Error: {e}")
            finally:
                job.finished = time.time()

        threading.Thread(target=_wrap, daemon=True, name=f"job-{job.id}").start()
        return job


# A single process-wide manager.
manager = JobManager()
