"""Work-segment ("job") book keeping.

A *job* is one stretch of on-site work that must be auditable as a unit: it
opens with a ``job-start`` audit entry and closes with a ``job-end`` entry. At
both boundaries an independent *site fingerprint* is captured — counters read
straight from the interlock components rather than from the audit log. An
exported ledger can then prove two things independently:

1. its own entries have not been deleted or edited (the audit hash chain);
2. the entries account for everything the machinery itself says happened
   (counter deltas versus entry counts).
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable

from .audit import Audit
from .errors import JobAlreadyClosed, JobAlreadyOpen, JobNotFound
from .store import Store, StoreError

JOBS_KEY = "jobs/ledger"
JOB_STATUS_OPEN = "open"
JOB_STATUS_CLOSED = "closed"

FingerprintProvider = Callable[[], dict[str, int]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRecord:
    """The boundaries and fingerprints of one work segment."""

    def __init__(
        self,
        job_id: str,
        title: str,
        site: str,
        started_at: str,
        start_seq: int,
        start_fingerprint: dict[str, int] | None = None,
        ended_at: str = "",
        end_seq: int = 0,
        end_fingerprint: dict[str, int] | None = None,
        status: str = JOB_STATUS_OPEN,
    ) -> None:
        self.job_id = job_id
        self.title = title
        self.site = site
        self.started_at = started_at
        self.ended_at = ended_at
        self.start_seq = start_seq
        self.end_seq = end_seq
        self.start_fingerprint = dict(start_fingerprint or {})
        self.end_fingerprint = dict(end_fingerprint or {})
        self.status = status

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "site": self.site,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "start_seq": self.start_seq,
            "end_seq": self.end_seq,
            "start_fingerprint": self.start_fingerprint,
            "end_fingerprint": self.end_fingerprint,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "JobRecord":
        def integers(mapping: object) -> dict[str, int]:
            if not isinstance(mapping, dict):
                return {}
            return {str(key): int(value) for key, value in mapping.items()}

        return cls(
            job_id=str(payload["job_id"]),
            title=str(payload.get("title") or ""),
            site=str(payload.get("site") or ""),
            started_at=str(payload.get("started_at") or ""),
            ended_at=str(payload.get("ended_at") or ""),
            start_seq=int(payload.get("start_seq") or 0),
            end_seq=int(payload.get("end_seq") or 0),
            start_fingerprint=integers(payload.get("start_fingerprint")),
            end_fingerprint=integers(payload.get("end_fingerprint")),
            status=str(payload.get("status") or JOB_STATUS_OPEN),
        )


class JobBook:
    """Thread-safe registry of work segments, at most one open at a time."""

    def __init__(self) -> None:
        self._jobs: list[JobRecord] = []
        self._lock = threading.Lock()

    def start(
        self,
        audit: Audit,
        fingerprints: FingerprintProvider,
        title: str,
        site: str = "",
        job_id: str = "",
    ) -> JobRecord:
        job_id = job_id or uuid.uuid4().hex
        with self._lock:
            for job in self._jobs:
                if job.job_id == job_id:
                    raise JobAlreadyOpen(f"作业 {job_id} 已存在")
            if any(job.status == JOB_STATUS_OPEN for job in self._jobs):
                raise JobAlreadyOpen("已有未收尾的作业，请先收尾")
            audit.begin_job_scope(job_id)
            entry = audit.record(
                "system", "job-start", f"作业开始：{title}", job_id=job_id
            )
            record = JobRecord(
                job_id=job_id,
                title=title,
                site=site,
                started_at=entry.recorded_at,
                start_seq=entry.seq,
                start_fingerprint=fingerprints(),
            )
            self._jobs.append(record)
        return record

    def finish(
        self,
        audit: Audit,
        fingerprints: FingerprintProvider,
        job_id: str = "",
    ) -> JobRecord:
        with self._lock:
            record = self._open_record(job_id)
            entry = audit.record(
                "system", "job-end", f"作业收尾：{record.title}", job_id=record.job_id
            )
            audit.end_job_scope()
            record.ended_at = entry.recorded_at
            record.end_seq = entry.seq
            record.end_fingerprint = fingerprints()
            record.status = JOB_STATUS_CLOSED
        return record

    def _open_record(self, job_id: str) -> JobRecord:
        if job_id:
            for job in self._jobs:
                if job.job_id == job_id:
                    if job.status != JOB_STATUS_OPEN:
                        raise JobAlreadyClosed(f"作业 {job_id} 已收尾")
                    return job
            raise JobNotFound(f"作业 {job_id} 不存在")
        for job in reversed(self._jobs):
            if job.status == JOB_STATUS_OPEN:
                return job
        raise JobNotFound("当前没有进行中的作业")

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            for job in self._jobs:
                if job.job_id == job_id:
                    return job
        raise JobNotFound(f"作业 {job_id} 不存在")

    def active(self) -> JobRecord | None:
        with self._lock:
            for job in reversed(self._jobs):
                if job.status == JOB_STATUS_OPEN:
                    return job
        return None

    def list(self) -> list[JobRecord]:
        with self._lock:
            return list(reversed(self._jobs))

    def snapshot(self) -> list[JobRecord]:
        with self._lock:
            return list(self._jobs)

    def restore(self, records: list[JobRecord]) -> None:
        with self._lock:
            self._jobs = list(records)


def save_jobs(store: Store, book: JobBook) -> None:
    payload = json.dumps(
        [record.to_dict() for record in book.snapshot()], ensure_ascii=False
    ).encode("utf-8")
    try:
        store.save(JOBS_KEY, payload)
    except StoreError:
        raise


def restore_jobs(store: Store, book: JobBook) -> None:
    from .errors import StoreNotFound

    try:
        payload = store.load(JOBS_KEY)
    except StoreNotFound:
        return
    records = [JobRecord.from_dict(item) for item in json.loads(payload.decode("utf-8"))]
    book.restore(records)
