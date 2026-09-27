"""Audit log for every control action taken by the service.

The log is a bounded ring buffer: once it reaches capacity the oldest entry is
dropped so a long-running console cannot grow without limit. Entries can be
filtered by crane, by action and by recency.

Every entry carries a monotonic ``seq`` and a ``digest`` chained to the
previous entry's digest. The chain makes two failure modes explicit instead of
silent: an entry whose fields were altered after the fact fails its digest
check, and a missing entry (a hand-deleted row, or a truncated buffer) shows up
as a gap between two surviving sequence numbers. Callers never treat the log as
trustworthy on its own -- :mod:`cranefleet.evidence` replays the chain and
reconciles it against the machine counters.
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
GENESIS_DIGEST = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    """A single recorded action."""

    entry_id: str
    recorded_at: str
    crane_id: str
    action: str
    detail: str
    seq: int = 0
    digest: str = ""
    prev_digest: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.entry_id,
            "at": self.recorded_at,
            "crane_id": self.crane_id,
            "action": self.action,
            "detail": self.detail,
            "seq": self.seq,
            "digest": self.digest,
            "prev_digest": self.prev_digest,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "AuditEntry":
        # Tolerate the five-field rows written before the chain existed: they
        # simply have no sequence number or digest and verify as "legacy".
        return cls(
            entry_id=str(payload["id"]),
            recorded_at=str(payload["at"]),
            crane_id=str(payload["crane_id"]),
            action=str(payload["action"]),
            detail=str(payload["detail"]),
            seq=int(payload.get("seq") or 0),
            digest=str(payload.get("digest") or ""),
            prev_digest=str(payload.get("prev_digest") or ""),
        )


def compute_digest(
    seq: int,
    entry_id: str,
    recorded_at: str,
    crane_id: str,
    action: str,
    detail: str,
    prev_digest: str,
) -> str:
    """Recompute the SHA-256 chain digest for one entry.

    Keys are sorted so the digest is independent of insertion order and every
    field is covered, including the previous digest that anchors the chain.
    """

    body = json.dumps(
        {
            "seq": seq,
            "id": entry_id,
            "at": recorded_at,
            "crane_id": crane_id,
            "action": action,
            "detail": detail,
            "prev": prev_digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def verify_entry(entry: AuditEntry, expected_prev: str) -> str:
    """Return ``"ok"``/``"bad-digest"``/``"chain-broken"`` for one entry."""

    if not entry.digest:
        return "legacy"
    if entry.prev_digest != expected_prev:
        return "chain-broken"
    expected = compute_digest(
        entry.seq,
        entry.entry_id,
        entry.recorded_at,
        entry.crane_id,
        entry.action,
        entry.detail,
        entry.prev_digest,
    )
    return "ok" if constant_time_equal(expected, entry.digest) else "bad-digest"


def constant_time_equal(left: str, right: str) -> bool:
    """Constant-time hex comparison so verification cannot be timed."""

    if len(left) != len(right):
        return False
    result = 0
    for char_a, char_b in zip(left, right):
        result |= ord(char_a) ^ ord(char_b)
    return result == 0


class Audit:
    """A thread-safe bounded audit log with a hash chain."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity if capacity > 0 else DEFAULT_CAPACITY
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()
        self._next_seq = 1
        self._last_digest = GENESIS_DIGEST

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(self, crane_id: str, action: str, detail: str) -> AuditEntry:
        entry_id = uuid.uuid4().hex
        recorded_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            seq = self._next_seq
            prev_digest = self._last_digest
            digest = compute_digest(
                seq, entry_id, recorded_at, crane_id, action, detail, prev_digest
            )
            entry = AuditEntry(
                entry_id=entry_id,
                recorded_at=recorded_at,
                crane_id=crane_id,
                action=action,
                detail=detail,
                seq=seq,
                digest=digest,
                prev_digest=prev_digest,
            )
            self._entries.append(entry)
            self._next_seq = seq + 1
            self._last_digest = digest
            if len(self._entries) > self._capacity:
                del self._entries[: len(self._entries) - self._capacity]
        return entry

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

    def head_seq(self) -> int:
        """Sequence number of the oldest entry still held (0 when empty)."""

        with self._lock:
            return self._entries[0].seq if self._entries else 0

    def tail_seq(self) -> int:
        """Sequence number of the newest entry held (0 when empty)."""

        with self._lock:
            return self._entries[-1].seq if self._entries else 0

    def verify_chain(self) -> list[dict[str, object]]:
        """Replay every held entry and report each anomaly.

        Each reported row names the entry (``seq``/``id``) and one of
        ``bad-digest`` (content altered), ``chain-broken`` (previous digest
        does not link), ``seq-gap`` (a sequence number is missing) or
        ``seq-order`` (numbers are out of order). An empty result means the
        held window is internally intact.
        """

        with self._lock:
            entries = list(self._entries)
        findings: list[dict[str, object]] = []
        prev_digest = GENESIS_DIGEST
        expected_seq = 0
        for entry in entries:
            status = verify_entry(entry, prev_digest)
            if status in ("bad-digest", "chain-broken"):
                findings.append(
                    {"seq": entry.seq, "id": entry.entry_id, "kind": status}
                )
            if entry.digest:
                if expected_seq and entry.seq != expected_seq:
                    kind = "seq-gap" if entry.seq > expected_seq else "seq-order"
                    findings.append(
                        {
                            "seq": entry.seq,
                            "id": entry.entry_id,
                            "kind": kind,
                            "expected": expected_seq,
                        }
                    )
                expected_seq = entry.seq + 1
                # Continue the replay from what the entry itself declares so a
                # single tampered row does not mask every row after it.
                prev_digest = entry.digest if status == "ok" else entry.digest
            elif expected_seq:
                # A legacy row in the middle of a chained window cannot anchor
                # the sequence; subsequent rows are judged from the gap on.
                findings.append(
                    {"seq": entry.seq, "id": entry.entry_id, "kind": "legacy-row"}
                )
        return findings

    def snapshot(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)

    def restore(self, entries: Sequence[AuditEntry]) -> None:
        with self._lock:
            self._entries = list(entries)
            if len(self._entries) > self._capacity:
                del self._entries[: len(self._entries) - self._capacity]
            chained = [entry for entry in self._entries if entry.digest]
            if chained:
                self._last_digest = chained[-1].digest
                self._next_seq = max(entry.seq for entry in chained) + 1
            else:
                self._last_digest = GENESIS_DIGEST
                self._next_seq = 1
