"""后台任务：采集、批量印标等耗时操作，前端轮询进度。"""
from __future__ import annotations

import threading
import time
import uuid

_jobs: dict[str, "Job"] = {}
_lock = threading.Lock()


class Job:
    def __init__(self, kind: str, title: str, register: bool = True):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.title = title
        self.status = "pending"  # pending / running / done / failed
        self.total = 0
        self.done = 0
        self.log: list[str] = []
        self.results: list[str] = []
        self.cancelled = False
        self.calls = {"total": 0, "image": 0, "text": 0, "failed": 0}  # 本任务发给中转站的请求数
        self.created = time.time()
        if register:  # 批量任务里的子任务不单独显示
            with _lock:
                _jobs[self.id] = self

    def note(self, msg: str) -> None:
        if not self.log or self.log[-1] != msg:
            self.log.append(msg)
            del self.log[:-200]
            from . import runlog
            runlog.log("任务", f"{self.title} | {msg}", job=self.id, progress=f"{self.done}/{self.total}")

    def finish(self, msg: str = "完成") -> None:
        self.status = "done"
        self.note(msg)

    def fail(self, msg: str) -> None:
        from . import runlog
        runlog.log("任务", f"{self.title} | 任务失败", job=self.id)
        self.status = "failed"
        self.note("失败：" + msg)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("id", "kind", "title", "status", "total", "done", "log", "results", "created", "calls")}


def run_in_thread(job: Job, fn, *args) -> Job:
    def wrap():
        job.status = "running"
        try:
            fn(*args, job)
            if job.status == "running":
                job.finish()
        except Exception as e:  # noqa: BLE001
            job.fail(str(e))

    threading.Thread(target=wrap, daemon=True).start()
    return job


def list_jobs() -> list[dict]:
    cutoff = time.time() - 3600
    with _lock:
        for k in [k for k, j in _jobs.items() if j.created < cutoff and j.status in ("done", "failed")]:
            del _jobs[k]
        return [j.to_dict() for j in sorted(_jobs.values(), key=lambda j: j.created, reverse=True)]


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)
