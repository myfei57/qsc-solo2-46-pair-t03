"""Key/value persistence for durable control state.

The control service keeps a small amount of state that must survive a restart:
the collision-zone snapshot, the hoist snapshot, the slew speed feedback, the
cluster baseline, the site configuration and the audit log. All of them go
through this thin key/value interface so the service can run on disk in
production and in memory during tests.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from .errors import StoreError, StoreNotFound

SNAPSHOT_VERSION = 1


class Store(ABC):
    """A durable key/value store keyed by a slash-separated string."""

    @abstractmethod
    def save(self, key: str, value: bytes) -> None:
        """Persist ``value`` under ``key``, replacing any previous value."""

    @abstractmethod
    def load(self, key: str) -> bytes:
        """Return the stored value or raise :class:`StoreNotFound`."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove ``key``; deleting a missing key is not an error."""

    @abstractmethod
    def keys(self) -> list[str]:
        """Return every key currently held, in ascending order."""

    def snapshot(self) -> dict[str, bytes]:
        """Return a shallow copy of every stored key/value pair."""

        return {key: self.load(key) for key in self.keys()}

    def restore(self, snapshot: dict[str, bytes]) -> None:
        """Overwrite the store with the supplied snapshot."""

        for key, value in snapshot.items():
            self.save(key, value)


class FileStore(Store):
    """A store that maps each key to one file on disk."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        return self._directory

    def _path(self, key: str) -> Path:
        return self._directory / (key.replace("/", "_") + ".bin")

    def save(self, key: str, value: bytes) -> None:
        target = self._path(key)
        with self._lock:
            temporary = target.with_suffix(".bin.tmp")
            try:
                temporary.write_bytes(bytes(value))
                os.replace(temporary, target)
            except OSError as exc:
                raise StoreError(f"cannot persist {key}") from exc

    def load(self, key: str) -> bytes:
        with self._lock:
            try:
                return self._path(key).read_bytes()
            except FileNotFoundError as exc:
                raise StoreNotFound(key) from exc
            except OSError as exc:
                raise StoreError(f"cannot read {key}") from exc

    def delete(self, key: str) -> None:
        with self._lock:
            try:
                self._path(key).unlink()
            except FileNotFoundError:
                return

    def keys(self) -> list[str]:
        with self._lock:
            names = sorted(entry.name for entry in self._directory.iterdir() if entry.name.endswith(".bin"))
        return [name[: -len(".bin")].replace("_", "/") for name in names]


class MemoryStore(Store):
    """An in-process store used for ephemeral runs and tests."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def save(self, key: str, value: bytes) -> None:
        with self._lock:
            self._data[key] = bytes(value)

    def load(self, key: str) -> bytes:
        with self._lock:
            try:
                return self._data[key]
            except KeyError as exc:
                raise StoreNotFound(key) from exc

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._data)


def encode_snapshot(snapshot: dict[str, bytes]) -> bytes:
    """Encode a snapshot into a single JSON document."""

    payload = {
        "version": SNAPSHOT_VERSION,
        "data": {key: base64.b64encode(value).decode("ascii") for key, value in snapshot.items()},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def decode_snapshot(encoded: bytes) -> dict[str, bytes]:
    """Decode a snapshot produced by :func:`encode_snapshot`."""

    payload = json.loads(encoded.decode("utf-8"))
    data = payload.get("data") or {}
    return {key: base64.b64decode(value) for key, value in data.items()}


def store_size(store: Store) -> int:
    """Return the total number of payload bytes held by ``store``."""

    return sum(len(value) for value in store.snapshot().values())


def store_count(store: Store) -> int:
    """Return the number of keys held by ``store``."""

    return len(store.keys())
