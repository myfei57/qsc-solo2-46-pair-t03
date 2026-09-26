"""Unit tests for the anti-collision zone state machine."""

from __future__ import annotations

import unittest

from cranefleet.geometry import Point
from cranefleet.store import MemoryStore
from cranefleet.zone import (
    Conflict,
    ZoneState,
    boundary_area,
    clear_conflict,
    coverage_check,
    detect_conflicts,
    is_latched,
    latch_generation,
    latest_registration_seq,
    lock_zone,
    occupancy_version,
    occupy,
    occupants_snapshot,
    pending_releases,
    persist,
    register_conflict,
    registration_count,
    registration_seq_of,
    restore,
    separation,
    vacate,
)

BOUNDARY = [Point(0.0, 0.0), Point(100.0, 0.0), Point(100.0, 100.0), Point(0.0, 100.0)]


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], int, tuple[str, ...]]] = []

    def on_conflict_cleared(self, zone_id, crane_ids, generation, conflicts) -> None:
        self.calls.append((zone_id, tuple(crane_ids), generation, tuple(conflicts)))


class ZoneOccupancyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zone = ZoneState("zone-1", "吊装作业区", BOUNDARY)

    def test_occupy_and_vacate(self) -> None:
        self.assertEqual(occupy(self.zone, ""), 0)
        self.assertEqual(occupy(self.zone, "crane-1"), 1)
        self.assertEqual(occupy(self.zone, "crane-2"), 2)
        self.assertEqual(occupy(self.zone, "crane-1"), 3)
        self.assertTrue(self.zone.occupied_by("crane-1"))
        self.assertEqual(self.zone.occupant_count(), 2)
        self.assertEqual(registration_count(self.zone), 3)
        self.assertEqual(latest_registration_seq(self.zone), 3)
        self.assertEqual(registration_seq_of(self.zone, "crane-1"), 3)
        self.assertEqual(occupancy_version(self.zone), 3)
        vacate(self.zone, "crane-1")
        self.assertEqual(occupants_snapshot(self.zone), ["crane-2"])
        self.assertEqual(occupancy_version(self.zone), 4)
        vacate(self.zone, "crane-1")
        self.assertEqual(occupancy_version(self.zone), 4)

    def test_persist_and_restore(self) -> None:
        store = MemoryStore()
        occupy(self.zone, "crane-1")
        self.assertFalse(self.zone.is_durable())
        persist(self.zone, store)
        self.assertTrue(self.zone.is_durable())
        self.assertEqual(self.zone.durable_revision(), 1)
        occupy(self.zone, "crane-2")
        persist(self.zone, store)
        reloaded = ZoneState("zone-1", "stale", [])
        restore(reloaded, store)
        self.assertEqual(reloaded.name, "吊装作业区")
        self.assertTrue(reloaded.is_durable())
        self.assertEqual(sorted(occupants_snapshot(reloaded)), ["crane-1", "crane-2"])
        self.assertEqual(reloaded.boundary_copy(), BOUNDARY)

    def test_restore_without_snapshot_is_not_durable(self) -> None:
        zone = ZoneState("zone-9", "empty", BOUNDARY)
        restore(zone, MemoryStore())
        self.assertFalse(zone.is_durable())

    def test_conflict_detection_and_registration(self) -> None:
        occupy(self.zone, "crane-1")
        occupy(self.zone, "crane-2")
        positions = {"crane-1": Point(0.0, 0.0), "crane-2": Point(5.0, 0.0)}
        conflicts = detect_conflicts(self.zone, positions, 12.0)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].crane_ids, ("crane-1", "crane-2"))
        self.assertEqual(conflicts[0].to_dict()["zone_id"], "zone-1")
        self.assertEqual(detect_conflicts(self.zone, {"crane-1": Point(0.0, 0.0)}, 12.0), [])

    def test_register_and_clear_conflict(self) -> None:
        occupy(self.zone, "crane-1")
        occupy(self.zone, "crane-2")
        register_conflict(self.zone, ("crane-1", "crane-2", ""))
        self.assertTrue(self.zone.is_conflict())
        self.assertTrue(is_latched(self.zone))
        lock_zone(self.zone)
        self.assertEqual(latch_generation(self.zone), 1)
        notifier = RecordingNotifier()
        generation = clear_conflict(self.zone, notifier)
        self.assertEqual(generation, 2)
        self.assertFalse(self.zone.is_conflict())
        self.assertFalse(is_latched(self.zone))
        self.assertEqual(notifier.calls[0][0], "zone-1")
        self.assertEqual(notifier.calls[0][2], 2)
        self.assertEqual(notifier.calls[0][3], ("crane-1", "crane-2"))
        self.assertEqual(len(pending_releases(self.zone)), 2)
        self.assertEqual(pending_releases(self.zone)[0].to_dict()["generation"], 2)

    def test_clear_without_notifier(self) -> None:
        self.assertEqual(clear_conflict(self.zone), 1)

    def test_coverage_and_area(self) -> None:
        positions = {"crane-1": Point(50.0, 50.0), "crane-2": Point(150.0, 50.0)}
        coverage = coverage_check(self.zone, positions)
        self.assertEqual(coverage.inside, ("crane-1",))
        self.assertEqual(coverage.outside, ("crane-2",))
        self.assertEqual(coverage.inside_bounds, ("crane-1",))
        self.assertAlmostEqual(coverage.area, 10000.0)
        self.assertGreater(coverage.expanded_area, coverage.area)
        self.assertTrue(coverage.expanded_overlap)
        self.assertIn("center", coverage.to_dict())
        self.assertAlmostEqual(boundary_area(self.zone), 10000.0)

    def test_separation_report(self) -> None:
        occupy(self.zone, "crane-1")
        occupy(self.zone, "crane-2")
        report = separation(
            self.zone, {"crane-1": Point(0.0, 0.0), "crane-2": Point(3.0, 4.0)}
        )
        self.assertAlmostEqual(report.distance, 5.0)
        self.assertTrue(report.fresh)
        self.assertIn("positions", report.to_dict())
        lone = separation(self.zone, {"crane-1": Point(0.0, 0.0)})
        self.assertEqual(lone.distance, -1.0)

    def test_conflict_dataclass_serialisation(self) -> None:
        conflict = Conflict(zone_id="zone-1", crane_ids=("a", "b"))
        self.assertEqual(conflict.to_dict(), {"zone_id": "zone-1", "crane_ids": ["a", "b"]})


if __name__ == "__main__":
    unittest.main()
