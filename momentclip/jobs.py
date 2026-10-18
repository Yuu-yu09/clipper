"""In-process job manager. Threaded, in-memory, good enough for a local tool."""

from __future__ import annotations

import logging
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Config, get_config

log = logging.getLogger("momentclip.jobs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"           # queued | running | done | error | cancelled
    progress: int = 0
    step: str = ""
    created: str = field(default_factory=_now)
    started: str | None = None
    finished: str | None = None
    result: dict | None = None
    error: str | None = None
    cancelled: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "progress": self.progress, "step": self.step,
            "created": self.created, "started": self.started,
            "finished": self.finished, "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=self.cfg.max_workers, thread_name_prefix="clipper"
        )

    # -- lookup -------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    # -- control ------------------------------------------------------
    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in {"done", "error", "cancelled"}:
                return False
            job.cancelled = True
            job.status = "cancelled"
            job.finished = _now()
            job.step = "cancelled"
            return True

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return False
        shutil_dir = Path(self.cfg.jobs_dir) / job_id
        if shutil_dir.exists():
            import shutil
            shutil.rmtree(shutil_dir, ignore_errors=True)
        return True

    # -- submit -------------------------------------------------------
    def submit(self, kind: str, fn: Callable[[Callable[[int, str]], dict], "Job"],
               job_id: str | None = None) -> Job:
        job = Job(id=job_id or uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
        self._pool.submit(self._run, job, fn)
        return job

    def _run(self, job: Job, fn) -> None:
        job.status = "running"
        job.started = _now()

        def progress(pct: int, msg: str) -> None:
            if job.cancelled:
                raise JobCancelled()
            job.progress = max(0, min(100, int(pct)))
            job.step = msg

        try:
            job.result = fn(progress)
            job.status = "done"
            job.progress = 100
            job.step = "complete"
        except JobCancelled:
            job.status = "cancelled"
            job.step = "cancelled"
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", job.id)
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.step = "failed"
            job.result = {"traceback": traceback.format_exc()[-2000:]}
        finally:
            job.finished = _now()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class JobCancelled(Exception):
    pass


_manager: JobManager | None = None


def get_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager