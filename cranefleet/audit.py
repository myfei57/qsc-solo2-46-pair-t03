"""Audit log for every control action taken by the service.

The log is a bounded ring buffer: once it reaches capacity the oldest entry is
dropped so a long-running console cannot grow without limit. Entries can be
filtered by crane, by action and by recency.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

DEFAULT_CAPACITY = 500


@dataclass(frozen=True)
class AuditEntry:
    """A single recorded action."""

    entry_id: str
    recorded_at: str
    crane_id: str
    action: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.entry_id,
            "at": self.recorded_at,
            "crane_id": self.crane_id,
            "action": self.action,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> "AuditEntry":
        return cls(
            entry_id=payload["id"],
            recorded_at=payload["at"],
            crane_id=payload["crane_id"],
            action=payload["action"],
            detail=payload["detail"],
        )


class Audit:
    """A thread-safe bounded audit log."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity if capacity > 0 else DEFAULT_CAPACITY
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(self, crane_id: str, action: str, detail: str) -> AuditEntry:
        entry = AuditEntry(
            entry_id=uuid.uuid4().hex,
            recorded_at=datetime.now(timezone.utc).isoformat(),
            crane_id=crane_id,
            action=action,
            detail=detail,
        )
        with self._lock:
            self._entries.append(entry)
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

    def snapshot(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)

    def restore(self, entries: Sequence[AuditEntry]) -> None:
        with self._lock:
            self._entries = list(entries)
            if len(self._entries) > self._capacity:
                del self._entries[: len(self._entries) - self._capacity]
