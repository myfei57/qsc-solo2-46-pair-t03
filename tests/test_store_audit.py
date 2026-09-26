"""Unit tests for the persistence layer and the audit ring buffer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cranefleet.audit import Audit, AuditEntry
from cranefleet.errors import StoreNotFound
from cranefleet.store import (
    FileStore,
    MemoryStore,
    decode_snapshot,
    encode_snapshot,
    store_count,
    store_size,
)


class MemoryStoreTests(unittest.TestCase):
    def test_round_trip_and_missing_key(self) -> None:
        store = MemoryStore()
        store.save("zone/z1", b"payload")
        self.assertEqual(store.load("zone/z1"), b"payload")
        with self.assertRaises(StoreNotFound):
            store.load("missing")

    def test_delete_is_idempotent(self) -> None:
        store = MemoryStore()
        store.save("a", b"1")
        store.delete("a")
        store.delete("a")
        self.assertEqual(store.keys(), [])

    def test_snapshot_and_restore(self) -> None:
        store = MemoryStore()
        store.save("one", b"1")
        store.save("two", b"2")
        clone = MemoryStore()
        clone.restore(store.snapshot())
        self.assertEqual(clone.load("two"), b"2")


class FileStoreTests(unittest.TestCase):
    def test_file_round_trip_and_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileStore(directory)
            store.save("b/c", b"2")
            store.save("a", b"1")
            self.assertEqual(store.load("a"), b"1")
            self.assertEqual(store.keys(), ["a", "b/c"])
            store.delete("a")
            self.assertEqual(store.keys(), ["b/c"])
            store.delete("a")

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileStore(directory)
            with self.assertRaises(StoreNotFound):
                store.load("nope")

    def test_directory_property(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileStore(directory)
            self.assertEqual(store.directory, Path(directory))


class SnapshotEncodingTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        encoded = encode_snapshot({"a": b"\x00\x01", "b": b"text"})
        self.assertEqual(decode_snapshot(encoded), {"a": b"\x00\x01", "b": b"text"})

    def test_store_metrics(self) -> None:
        store = MemoryStore()
        store.save("a", b"1234")
        store.save("b", b"12")
        self.assertEqual(store_count(store), 2)
        self.assertEqual(store_size(store), 6)


class AuditTests(unittest.TestCase):
    def test_record_query_and_filter(self) -> None:
        audit = Audit(100)
        audit.record("crane-1", "slew-start", "allowed")
        audit.record("crane-2", "hoist-stop", "stopped")
        audit.record("crane-1", "wind-alarm", "active")
        entries = audit.query("crane-1", 10)
        self.assertEqual([entry.action for entry in entries], ["wind-alarm", "slew-start"])
        self.assertEqual(len(audit.filter_by_actions(["hoist-stop"])), 1)
        self.assertEqual(audit.count_by_action("wind-alarm"), 1)
        self.assertIsNotNone(audit.latest("hoist-stop"))
        self.assertIsNone(audit.latest("missing"))

    def test_ring_capacity(self) -> None:
        audit = Audit(3)
        for index in range(10):
            audit.record("crane-1", "tick", str(index))
        self.assertEqual(audit.count(), 3)
        self.assertEqual(audit.capacity, 3)
        self.assertEqual(audit.query(limit=10)[0].detail, "9")

    def test_named_zero_capacity_defaults(self) -> None:
        audit = Audit(0)
        self.assertEqual(audit.capacity, 500)

    def test_snapshot_restore_and_serialisation(self) -> None:
        audit = Audit(20)
        audit.record("crane-1", "start", "one")
        audit.record("crane-1", "stop", "two")
        payload = audit.snapshot()
        clone = Audit(20)
        clone.restore([AuditEntry.from_dict(entry.to_dict()) for entry in payload])
        self.assertEqual(clone.latest("stop").detail, "two")
        self.assertEqual(clone.query(limit=1)[0].crane_id, "crane-1")


if __name__ == "__main__":
    unittest.main()
