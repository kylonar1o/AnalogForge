"""Background job manager used by the web server."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from .flows.job import run_job, validate_config
from .store import Store


class JobManager:
    def __init__(self, store: Store, max_concurrent_jobs: int = 2):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=max_concurrent_jobs, thread_name_prefix="afjob")
        self.cancel_flags: dict[str, threading.Event] = {}
        self.futures = {}
        # jobs left "running" by a previous server process cannot resume
        for job in store.list_jobs(1000):
            if job["status"] in ("queued", "running"):
                store.update_job(job["id"], status="interrupted", error="server restarted")

    def submit(self, cfg: dict) -> str:
        cfg = validate_config(cfg)
        job_id = self.store.create_job(cfg.get("kind", "simulate"), cfg, cfg.get("name", ""))
        flag = threading.Event()
        self.cancel_flags[job_id] = flag
        self.futures[job_id] = self.pool.submit(run_job, cfg, self.store, job_id, flag)
        return job_id

    def cancel(self, job_id: str) -> bool:
        flag = self.cancel_flags.get(job_id)
        if flag is None:
            return False
        flag.set()
        job = self.store.get_job(job_id)
        if job and job["status"] == "queued":
            self.store.update_job(job_id, status="cancelled")
        return True

    def wait(self, job_id: str, timeout: float | None = None):
        fut = self.futures.get(job_id)
        return fut.result(timeout) if fut else None

    def shutdown(self):
        for f in self.cancel_flags.values():
            f.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
