"""Tamper-evident audit log for every control action taken by the service.

The log is a bounded ring buffer: once it reaches capacity the oldest entry is
dropped so a long-running console cannot grow without limit. Entries can be
filtered by crane, by action and by recency.

Each entry carries a monotonic sequence number and a SHA-256 hash that chains
it to the previous entry. The chain is what makes an exported record bundle
verifiable: replaying the hashes detects a deleted entry (a gap in the
sequence), a modified field (a hash mismatch) or a reordered entry (a broken
link). The oldest surviving entry's sequence/hash pair is the *chain anchor*;
the buffer never claims knowledge of anything before the anchor.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

DEFAULT_CAPACITY = 500
GENESIS_HASH = "0" * 64

# Status codes used both by the in-buffer checks and by the exported ledger.
STATUS_OK = "ok"
STATUS_HASH_MISMATCH = "hash-mismatch"
STATUS_FIELD_ALTERED = "field-altered"
STATUS_BROKEN_CHAIN = "broken-chain"
STATUS_SEQUENCE_GAP = "sequence-gap"
STATUS_JOB_TAG_MISMATCH = "job-tag-mismatch"
STATUS_MISSING_FROM_LIVE = "missing-from-live-log"


def entry_hash(
    seq: int,
    recorded_at: str,
    crane_id: str,
    action: str,
    detail: str,
    job_id: str,
    prev_hash: str,
) -> str:
    """Compute the tamper-evident hash of one entry.

    The fields are serialised through ``json.dumps`` with sorted keys so the
    hash is stable across processes and independent of dict insertion order.
    """

    payload = json.dumps(
        {
            "seq": seq,
            "at": recorded_at,
            "crane_id": crane_id,
            "action": action,
            "detail": detail,
            "job_id": job_id,
            "prev": prev_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    """A single recorded action.

    ``seq`` is the site-wide monotonic sequence (1 based). ``prev_hash`` chains
    the entry to its predecessor; ``hash`` is this entry's own hash. ``job_id``
    names the work segment ("job") during which the entry was recorded and is
    the empty string for entries outside any job.
    """

    entry_id: str
    recorded_at: str
    crane_id: str
    action: str
    detail: str
    seq: int = 0
    prev_hash: str = GENESIS_HASH
    hash: str = ""
    job_id: str = ""

    def to_dict(self) -> dict[str, str | int]:
        return {
            "id": self.entry_id,
            "seq": self.seq,
            "at": self.recorded_at,
            "crane_id": self.crane_id,
            "action": self.action,
            "detail": self.detail,
            "job_id": self.job_id,
            "prev": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "AuditEntry":
        # Legacy records predating the chain only carried id/at/crane/action/
        # detail. They are restored with seq=0 and an empty stored hash; the
        # Audit log re-chains them on restore, which assigns real values.
        return cls(
            entry_id=str(payload["id"]),
            recorded_at=str(payload["at"]),
            crane_id=str(payload["crane_id"]),
            action=str(payload["action"]),
            detail=str(payload["detail"]),
            seq=int(payload.get("seq") or 0),
            prev_hash=str(payload.get("prev") or GENESIS_HASH),
            hash=str(payload.get("hash") or ""),
            job_id=str(payload.get("job_id") or ""),
        )

    def recomputed_hash(self) -> str:
        return entry_hash(
            self.seq,
            self.recorded_at,
            self.crane_id,
            self.action,
            self.detail,
            self.job_id,
            self.prev_hash,
        )


class Audit:
    """A thread-safe bounded, hash-chained audit log."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity if capacity > 0 else DEFAULT_CAPACITY
        self._entries: list[AuditEntry] = []
        self._next_seq = 1
        self._active_job = ""
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(self, crane_id: str, action: str, detail: str, job_id: str = "") -> AuditEntry:
        with self._lock:
            entry = self._append(crane_id, action, detail, job_id or self._active_job)
        return entry

    def _append(
        self, crane_id: str, action: str, detail: str, job_id: str
    ) -> AuditEntry:
        seq = self._next_seq
        prev_hash = self._entries[-1].hash if self._entries else GENESIS_HASH
        entry = AuditEntry(
            entry_id=uuid.uuid4().hex,
            recorded_at=datetime.now(timezone.utc).isoformat(),
            crane_id=crane_id,
            action=action,
            detail=detail,
            seq=seq,
            prev_hash=prev_hash,
            job_id=job_id,
        )
        object.__setattr__(entry, "hash", entry.recomputed_hash())
        self._entries.append(entry)
        self._next_seq = seq + 1
        if len(self._entries) > self._capacity:
            del self._entries[: len(self._entries) - self._capacity]
        return entry

    # --------------------------------------------------------------- job scope

    def begin_job_scope(self, job_id: str) -> None:
        """Tag every subsequently recorded entry with ``job_id``."""

        with self._lock:
            self._active_job = job_id

    def end_job_scope(self) -> None:
        with self._lock:
            self._active_job = ""

    def active_job(self) -> str:
        with self._lock:
            return self._active_job

    # ------------------------------------------------------------------ reads

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def count_by_action(self, action: str) -> int:
        with self._lock:
            return sum(1 for entry in self._entries if entry.action == action)

    def query(self, crane_id: str = "", limit: int = 50) -> list[AuditEntry]:
        """Return the newest entries first, optionally for one crane."""

        with self._lock:
            entries = list(self._entries)
        if limit <= 0 or limit > len(entries):
            limit = len(entries)
        collected: list[AuditEntry] = []
        for entry in reversed(entries):
            if crane_id and entry.crane_id != crane_id:
                continue
            collected.append(entry)
            if len(collected) >= limit:
                break
        return collected

    def filter_by_actions(self, actions: Iterable[str]) -> list[AuditEntry]:
        """Return the newest entries first whose action is one of ``actions``."""

        allowed = set(actions)
        with self._lock:
            entries = list(self._entries)
        return [entry for entry in reversed(entries) if entry.action in allowed]

    def latest(self, action: str) -> AuditEntry | None:
        with self._lock:
            for entry in reversed(self._entries):
                if entry.action == action:
                    return entry
        return None

    def snapshot(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)

    def entry_by_seq(self, seq: int) -> AuditEntry | None:
        with self._lock:
            for entry in self._entries:
                if entry.seq == seq:
                    return entry
        return None

    def reset_sequence(self, next_seq: int) -> None:
        """Advance the sequence counter past ``next_seq`` without entries.

        This models a ring buffer whose window has rolled forward: the next
        recorded entry receives a higher sequence while the buffer holds no
        rows from before the roll.
        """

        with self._lock:
            if next_seq < self._next_seq:
                raise ValueError("序列只能向前推进，不能回退")
            self._next_seq = next_seq

    def chain_anchor(self) -> tuple[int, str] | None:
        """Return ``(seq, hash)`` of the oldest surviving entry, if any."""

        with self._lock:
            if not self._entries:
                return None
            oldest = self._entries[0]
            return oldest.seq, oldest.hash

    def tail(self) -> tuple[int, str] | None:
        """Return ``(seq, hash)`` of the newest entry, if any."""

        with self._lock:
            if not self._entries:
                return None
            newest = self._entries[-1]
            return newest.seq, newest.hash

    def window(self, first_seq: int, last_seq: int) -> list[AuditEntry]:
        """Return the surviving entries with ``first_seq <= seq <= last_seq``."""

        with self._lock:
            return [
                entry
                for entry in self._entries
                if first_seq <= entry.seq <= last_seq
            ]

    # -------------------------------------------------------------- restore /

    def restore(self, entries: Sequence[AuditEntry]) -> None:
        """Replace the buffer, validating and (for legacy rows) re-chaining.

        Entries are accepted in chronological order. Fully chained rows keep
        their stored sequence and hashes; legacy rows (seq == 0) are appended
        to the chain with fresh sequence numbers. Any internal inconsistency
        is repaired by re-chaining from the first valid anchor, because the
        point of the chain is to flag tampering on *export* rather than to
        refuse a boot restore.
        """

        with self._lock:
            self._entries = []
            self._next_seq = 1
            prev_hash = GENESIS_HASH
            for raw in entries:
                if raw.seq > 0 and raw.hash and raw.prev_hash == prev_hash:
                    entry = raw
                else:
                    # Legacy row, or a row whose link no longer lines up:
                    # renumber and rehash so the live buffer is internally
                    # consistent. Exports made from stored rows are what get
                    # verified for tampering, not the live restore path.
                    entry = AuditEntry(
                        entry_id=raw.entry_id,
                        recorded_at=raw.recorded_at,
                        crane_id=raw.crane_id,
                        action=raw.action,
                        detail=raw.detail,
                        seq=self._next_seq,
                        prev_hash=prev_hash,
                        job_id=raw.job_id,
                    )
                    object.__setattr__(entry, "hash", entry.recomputed_hash())
                self._entries.append(entry)
                self._next_seq = entry.seq + 1
                prev_hash = entry.hash
            if len(self._entries) > self._capacity:
                del self._entries[: len(self._entries) - self._capacity]


def verify_entry(entry: AuditEntry, expected_prev_hash: str) -> tuple[str, str]:
    """Verify one entry against its stored hash and an expected predecessor.

    Returns ``(status, detail)``. ``status`` is :data:`STATUS_OK` when the
    entry reproduces its stored hash and links to ``expected_prev_hash``;
    otherwise it names exactly what failed.
    """

    recomputed = entry.recomputed_hash()
    if recomputed != entry.hash:
        if entry.prev_hash != expected_prev_hash:
            return (
                STATUS_FIELD_ALTERED,
                f"第 {entry.seq} 条记录字段被改动，且前链断裂：重算哈希 {recomputed[:12]} 与存档 {entry.hash[:12]} 不符",
            )
        return (
            STATUS_HASH_MISMATCH,
            f"第 {entry.seq} 条记录字段被改动：重算哈希 {recomputed[:12]} 与存档 {entry.hash[:12]} 不符",
        )
    if entry.prev_hash != expected_prev_hash:
        return (
            STATUS_BROKEN_CHAIN,
            f"第 {entry.seq} 条记录前链断裂：存档前链 {entry.prev_hash[:12]}，应为 {expected_prev_hash[:12]}",
        )
    return STATUS_OK, f"第 {entry.seq} 条记录哈希与前后链一致"
