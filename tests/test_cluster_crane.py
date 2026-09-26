"""Unit tests for fleet geometry, command merging and configuration."""

from __future__ import annotations

import unittest

from cranefleet import cluster as fleet
from cranefleet.config import SiteConfig, config_view, load_config, save_config
from cranefleet.crane import (
    ACTION_LOWER,
    ACTION_RAISE,
    ACTION_STOP,
    ACTION_SWING,
    ACTION_TROLLEY,
    AXIS_HOIST,
    AXIS_SLEW,
    SOURCE_CAB,
    SOURCE_REMOTE,
    Command,
    Crane,
    Load,
    apply_cab,
    apply_remote,
    execute,
    merge,
)
from cranefleet.errors import ConfigError, HookNotLowered, NoCranesSurveyed, SlewIdle
from cranefleet.geometry import Point
from cranefleet.hoist import Hoist, lower as hoist_lower
from cranefleet.slew import Slew, start as slew_start
from cranefleet.store import MemoryStore
from cranefleet.trolley import Trolley
from cranefleet.zone import ZoneState, clear_conflict, occupy, register_conflict

BOUNDARY = [Point(0.0, 0.0), Point(100.0, 0.0), Point(100.0, 100.0), Point(0.0, 100.0)]


class ClusterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster = fleet.Cluster({"crane-1": Point(0.0, 0.0), "crane-2": Point(80.0, 0.0)})
        self.store = MemoryStore()

    def test_survey_and_baseline(self) -> None:
        fleet.survey(self.cluster, {"crane-2": Point(20.0, 0.0), "crane-3": Point(40.0, 40.0)})
        self.assertEqual(len(fleet.current_positions(self.cluster)), 3)
        self.assertEqual(fleet.current_position(self.cluster, "crane-3"), Point(40.0, 40.0))
        self.assertIsNone(fleet.current_position(self.cluster, "missing"))
        revision = fleet.refresh_baseline(self.cluster, self.store)
        self.assertEqual(revision, 1)
        self.assertEqual(fleet.baseline_revision(self.cluster), 1)
        self.assertEqual(fleet.baseline_positions(self.cluster)["crane-2"], Point(20.0, 0.0))
        fleet.survey(self.cluster, {"crane-2": Point(30.0, 0.0)})
        self.assertEqual(fleet.current_positions(self.cluster)["crane-2"], Point(30.0, 0.0))
        self.assertEqual(fleet.baseline_positions(self.cluster)["crane-2"], Point(20.0, 0.0))

    def test_baseline_restore(self) -> None:
        fleet.survey(self.cluster, {"crane-1": Point(1.0, 1.0)})
        fleet.refresh_baseline(self.cluster, self.store)
        restored = fleet.Cluster({"crane-1": Point(9.0, 9.0)})
        fleet.restore_baseline(restored, self.store)
        self.assertEqual(fleet.baseline_revision(restored), 1)
        self.assertEqual(fleet.baseline_positions(restored)["crane-1"], Point(1.0, 1.0))
        fleet.restore_baseline(fleet.Cluster({}), MemoryStore())

    def test_refresh_without_cranes(self) -> None:
        with self.assertRaises(NoCranesSurveyed):
            fleet.refresh_baseline(fleet.Cluster({}), self.store)

    def test_geometry_summaries(self) -> None:
        pair = fleet.nearest_pair(self.cluster)
        self.assertIsNotNone(pair)
        self.assertEqual((pair.first, pair.second), ("crane-1", "crane-2"))
        self.assertAlmostEqual(pair.distance, 80.0)
        self.assertEqual(pair.center(), Point(40.0, 0.0))
        self.assertEqual(fleet.fleet_center(self.cluster), Point(40.0, 0.0))
        self.assertAlmostEqual(fleet.spread(self.cluster), 40.0)
        self.assertAlmostEqual(fleet.grid_spacing(self.cluster), 80.0)
        self.assertAlmostEqual(fleet.spacing_deviation(self.cluster), 0.0)
        self.assertAlmostEqual(fleet.pair_separation(pair, 12.0), 68.0)
        self.assertAlmostEqual(fleet.pair_clearance_from_center(self.cluster, pair), 0.0)
        self.assertIn("line", pair.to_dict())

    def test_single_crane_has_no_pair(self) -> None:
        cluster = fleet.Cluster({"crane-1": Point(0.0, 0.0)})
        self.assertIsNone(fleet.nearest_pair(cluster))
        self.assertEqual(fleet.grid_spacing(cluster), 0.0)
        self.assertEqual(fleet.spacing_deviation(cluster), 0.0)
        self.assertEqual(fleet.spread(fleet.Cluster({})), 0.0)

    def test_check_conflicts_reports_mismatches(self) -> None:
        zone = ZoneState("zone-1", "zone", BOUNDARY)
        occupy(zone, "crane-1")
        occupy(zone, "crane-2")
        conflicts, mismatches = fleet.check_conflicts(self.cluster, zone, 100.0)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(mismatches, [])
        zone.occupants["crane-9"] = True
        _, mismatches = fleet.check_conflicts(self.cluster, zone, 100.0)
        self.assertIn("crane-9", mismatches)

    def test_conflict_clear_notifies_cluster(self) -> None:
        zone = ZoneState("zone-1", "zone", BOUNDARY)
        occupy(zone, "crane-1")
        occupy(zone, "crane-2")
        register_conflict(zone, ("crane-1", "crane-2"))
        clear_conflict(zone, self.cluster)
        self.assertTrue(fleet.is_released(self.cluster, "crane-1"))
        self.assertEqual(fleet.release_count(self.cluster, "crane-1"), 1)
        self.assertEqual(fleet.release_generation_now(self.cluster), 1)
        self.assertEqual(len(fleet.release_ledger_now(self.cluster)), 2)
        self.assertEqual(fleet.released_snapshot(self.cluster)["crane-2"], True)
        self.assertEqual(fleet.release_ledger_now(self.cluster)[0].to_dict()["count"], 1)


class CraneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.crane = Crane("crane-1")
        self.slew = Slew("crane-1")
        self.trolley = Trolley("crane-1")
        self.hoist = Hoist("crane-1")
        self.zone = ZoneState("zone-1", "zone", BOUNDARY)
        self.store = MemoryStore()

    def test_stop_from_cab_blocks_later_move(self) -> None:
        apply_cab(self.crane, Command(SOURCE_CAB, ACTION_STOP, AXIS_SLEW))
        merged = apply_remote(self.crane, Command(SOURCE_REMOTE, ACTION_SWING, angle=30.0))
        self.assertEqual(merged.action, ACTION_STOP)
        self.assertEqual(self.crane.blocked_moves_now(), [ACTION_SWING])
        self.assertTrue(self.crane.stops_pending())
        self.assertEqual(self.crane.command_sequence(), 2)
        self.assertEqual(self.crane.stop_sequence_now(), 1)

    def test_stop_from_remote_blocks_later_cab_move(self) -> None:
        apply_remote(self.crane, Command(SOURCE_REMOTE, ACTION_STOP, AXIS_SLEW))
        merged = apply_cab(self.crane, Command(SOURCE_CAB, ACTION_TROLLEY))
        self.assertEqual(merged.action, ACTION_STOP)
        self.crane.clear_stop()
        self.assertFalse(self.crane.stops_pending())
        merged = merge(self.crane, Command(SOURCE_CAB, ACTION_TROLLEY))
        self.assertEqual(merged.action, ACTION_TROLLEY)
        self.assertEqual(self.crane.last_command().action, ACTION_TROLLEY)

    def test_execute_dispatch(self) -> None:
        slew_start(self.slew, self.zone, self.store)
        apply_cab(self.crane, Command(SOURCE_CAB, ACTION_SWING, angle=10.0))
        execute(self.crane, self.slew, self.trolley, self.hoist)
        self.assertAlmostEqual(self.slew.angle_now(), 10.0)

        apply_cab(self.crane, Command(SOURCE_CAB, ACTION_TROLLEY))
        with self.assertRaises(HookNotLowered):
            execute(self.crane, self.slew, self.trolley, self.hoist)
        hoist_lower(self.hoist)
        execute(self.crane, self.slew, self.trolley, self.hoist)
        self.assertTrue(self.trolley.is_moving())

        apply_cab(self.crane, Command(SOURCE_CAB, ACTION_STOP, AXIS_HOIST))
        execute(self.crane, self.slew, self.trolley, self.hoist)
        self.assertFalse(self.hoist.is_moving())

    def test_execute_raise_and_stop_axes(self) -> None:
        apply_cab(self.crane, Command(SOURCE_CAB, ACTION_RAISE))
        execute(self.crane, self.slew, self.trolley, self.hoist)
        self.assertTrue(self.hoist.is_moving())
        apply_cab(self.crane, Command(SOURCE_CAB, ACTION_STOP, AXIS_SLEW))
        execute(self.crane, self.slew, self.trolley, self.hoist)
        self.assertTrue(self.slew.is_stopped())

    def test_execute_without_command_is_noop(self) -> None:
        result = execute(self.crane, self.slew, self.trolley, self.hoist)
        self.assertEqual(result.action, "")

    def test_execute_idle_swing_raises(self) -> None:
        apply_cab(self.crane, Command(SOURCE_CAB, ACTION_SWING, angle=5.0))
        with self.assertRaises(SlewIdle):
            execute(self.crane, self.slew, self.trolley, self.hoist)

    def test_command_serialisation(self) -> None:
        command = Command.from_dict({"source": "remote", "action": "swing", "angle": 12})
        self.assertEqual(command.angle, 12.0)
        self.assertEqual(command.to_dict()["source"], "remote")
        fallback = Command.from_dict({})
        self.assertEqual(fallback.source, SOURCE_CAB)

    def test_lower_via_execute(self) -> None:
        apply_cab(self.crane, Command(SOURCE_CAB, ACTION_LOWER))
        execute(self.crane, self.slew, self.trolley, self.hoist)
        self.assertTrue(self.hoist.hook_lowered())

    def test_load_model(self) -> None:
        load = Load(2.4)
        load.update(0.3, 0.1)
        self.assertGreater(load.sway_angle(), 0.0)
        self.assertGreater(load.momentum(), 0.0)
        load.set_vertical(8.0)
        self.assertEqual(load.height(), 8.0)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self) -> None:
        config = SiteConfig()
        self.assertEqual(config.to_dict()["safe_separation"], 12.0)
        parsed = SiteConfig.from_dict({"site_name": "工地", "safe_separation": "15"})
        self.assertEqual(parsed.safe_separation, 15.0)
        with self.assertRaises(ConfigError):
            SiteConfig.from_dict({"safe_separation": 0})
        with self.assertRaises(ConfigError):
            SiteConfig.from_dict({"safe_separation": "wide"})

    def test_save_load_and_view(self) -> None:
        store = MemoryStore()
        self.assertEqual(load_config(store), SiteConfig())
        save_config(store, SiteConfig(site_name="华东一号", safe_separation=18.0))
        loaded = load_config(store)
        self.assertEqual(loaded.site_name, "华东一号")
        cluster = fleet.Cluster({"crane-1": Point(0.0, 0.0)})
        view = config_view(loaded, cluster)
        self.assertEqual(view["surveyed_cranes"], 1)
        self.assertEqual(view["alarm_threshold"], 10.0)


if __name__ == "__main__":
    unittest.main()
