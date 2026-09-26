"""Unit tests for slewing, trolley travel and hoisting."""

from __future__ import annotations

import unittest

from cranefleet.audit import Audit
from cranefleet.errors import (
    FeedbackNotConfirmed,
    HoistBusy,
    HookNotLowered,
    SlewBlocked,
    SlewBusy,
    SlewIdle,
    StoreError,
)
from cranefleet.geometry import Point
from cranefleet.hoist import (
    Drum,
    Hoist,
    engage_brake,
    limit_stop as hoist_limit_stop,
    lower,
    persist_hoist,
    raise_hook,
    release_brake,
    resume_release,
    stop as hoist_stop,
)
from cranefleet.slew import (
    Ramp,
    Slew,
    SpeedFeedback,
    is_axis_stopped,
    limit_stop,
    persist_feedback,
    ramp_reached,
    release_latch_hold,
    release_wind_hold,
    restore_feedback,
    start,
    stop,
    stop_for_wind,
    stop_reason,
    stop_sequence,
    swing,
)
from cranefleet.store import MemoryStore
from cranefleet.trolley import Speedometer, Trolley
from cranefleet.trolley import limit_stop as trolley_limit_stop
from cranefleet.trolley import move, move_resume, stop as trolley_stop
from cranefleet.zone import ZoneState

BOUNDARY = [Point(0.0, 0.0), Point(100.0, 0.0), Point(100.0, 100.0), Point(0.0, 100.0)]


class RampTests(unittest.TestCase):
    def test_ramp_walks_to_target(self) -> None:
        ramp = Ramp()
        ramp.set_target(5.0)
        for _ in range(25):
            ramp.step(0.1)
        self.assertTrue(ramp_reached(ramp))
        self.assertEqual(ramp.target_now(), 5.0)
        ramp.set_target(0.0)
        ramp.step(0.1)
        self.assertAlmostEqual(ramp.velocity_now(), 4.8)


class SlewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.slew = Slew("crane-1")
        self.zone = ZoneState("zone-1", "zone", BOUNDARY)
        self.store = MemoryStore()
        self.audit = Audit(50)

    def test_start_persists_the_zone_first(self) -> None:
        start(self.slew, self.zone, self.store, self.audit)
        self.assertTrue(self.slew.is_moving())
        self.assertTrue(self.zone.is_durable())
        with self.assertRaises(SlewBusy):
            start(self.slew, self.zone, self.store, self.audit)

    def test_blocked_when_zone_cannot_persist(self) -> None:
        class BrokenStore(MemoryStore):
            def save(self, key: str, value: bytes) -> None:
                raise StoreError("disk full")

        with self.assertRaises(StoreError):
            start(self.slew, self.zone, BrokenStore(), self.audit)
        self.assertFalse(self.slew.is_moving())
        self.assertFalse(self.zone.is_durable())

    def test_swing_requires_moving(self) -> None:
        with self.assertRaises(SlewIdle):
            swing(self.slew, 5.0, self.audit)
        start(self.slew, self.zone, self.store, self.audit)
        swing(self.slew, 5.0, self.audit)
        self.assertAlmostEqual(self.slew.angle_now(), 5.0)

    def test_stop_clears_motion(self) -> None:
        start(self.slew, self.zone, self.store, self.audit)
        stop(self.slew, self.audit)
        self.assertFalse(self.slew.is_moving())
        self.assertTrue(self.slew.is_stopped())

    def test_wind_hold_blocks_start(self) -> None:
        status = stop_for_wind(self.slew, self.audit, 1)
        self.assertTrue(status.acknowledged())
        self.assertTrue(status.already_stopped)
        with self.assertRaises(SlewBlocked):
            start(self.slew, self.zone, self.store, self.audit)
        release_wind_hold(self.slew)
        start(self.slew, self.zone, self.store, self.audit)
        self.assertTrue(self.slew.is_moving())

    def test_wind_stop_reports_motion(self) -> None:
        start(self.slew, self.zone, self.store, self.audit)
        status = stop_for_wind(self.slew, self.audit, 2)
        self.assertTrue(status.stopped_now)
        self.assertFalse(status.already_stopped)
        self.assertEqual(self.slew.stop_count_now(), 1)
        self.assertEqual(self.slew.last_stop_sequence_now(), 2)

    def test_latch_hold_blocks_until_released(self) -> None:
        self.slew.latch_held = True
        with self.assertRaises(SlewBlocked):
            start(self.slew, self.zone, self.store, self.audit)
        release_latch_hold(self.slew)
        start(self.slew, self.zone, self.store, self.audit)

    def test_limit_stop_records_reason(self) -> None:
        start(self.slew, self.zone, self.store, self.audit)
        limit_stop(self.slew, "slew", 7)
        self.assertTrue(is_axis_stopped(self.slew, "slew"))
        self.assertEqual(stop_reason(self.slew, "slew"), "limit")
        self.assertEqual(stop_sequence(self.slew, "slew"), 7)
        self.assertFalse(is_axis_stopped(self.slew, "trolley"))

    def test_feedback_round_trip(self) -> None:
        feedback = SpeedFeedback("crane-1")
        self.assertFalse(feedback.is_confirmed())
        feedback.confirm(1.5)
        self.assertTrue(feedback.is_confirmed())
        self.assertEqual(feedback.speed_now(), 1.5)
        persist_feedback(feedback, self.store)
        reloaded = SpeedFeedback("crane-1")
        restore_feedback(reloaded, self.store)
        self.assertTrue(reloaded.is_confirmed())
        self.assertEqual(reloaded.speed_now(), 1.5)
        missing = SpeedFeedback("crane-9")
        restore_feedback(missing, self.store)
        self.assertFalse(missing.is_confirmed())


class TrolleyTests(unittest.TestCase):
    def test_move_requires_lowered_hook(self) -> None:
        trolley = Trolley("crane-1")
        hoist = Hoist("crane-1")
        with self.assertRaises(HookNotLowered):
            move(trolley, hoist)
        lower(hoist)
        move(trolley, hoist)
        self.assertTrue(trolley.is_moving())
        trolley_stop(trolley)
        self.assertFalse(trolley.is_moving())
        trolley_limit_stop(trolley)
        self.assertFalse(trolley.is_moving())

    def test_resume_reads_hoist_snapshot(self) -> None:
        trolley = Trolley("crane-1")
        hoist = Hoist("crane-1")
        store = MemoryStore()
        lower(hoist)
        persist_hoist(hoist, store)
        resumed = Hoist("crane-1")
        move_resume(trolley, resumed, store)
        self.assertTrue(resumed.hook_lowered())
        self.assertTrue(trolley.is_moving())

    def test_speedometer(self) -> None:
        speedometer = Speedometer()
        self.assertEqual(speedometer.sample(0.0, 0.0), 0.0)
        self.assertAlmostEqual(speedometer.sample(5.0, 2.0), 2.5)
        self.assertAlmostEqual(speedometer.last_speed(), 2.5)


class HoistTests(unittest.TestCase):
    def test_lower_and_busy(self) -> None:
        hoist = Hoist("crane-1")
        lower(hoist)
        self.assertTrue(hoist.hook_lowered())
        self.assertTrue(hoist.is_moving())
        with self.assertRaises(HoistBusy):
            lower(hoist)

    def test_brake_requires_feedback(self) -> None:
        hoist = Hoist("crane-1")
        feedback = SpeedFeedback("crane-1")
        with self.assertRaises(FeedbackNotConfirmed):
            release_brake(hoist, feedback)
        feedback.confirm(1.0)
        release_brake(hoist, feedback)
        self.assertFalse(hoist.brake_on())
        engage_brake(hoist)
        self.assertTrue(hoist.brake_on())

    def test_resume_release_restores_feedback(self) -> None:
        store = MemoryStore()
        feedback = SpeedFeedback("crane-1")
        feedback.confirm(2.0)
        persist_feedback(feedback, store)
        hoist = Hoist("crane-1")
        fresh = SpeedFeedback("crane-1")
        resume_release(hoist, fresh, store)
        self.assertFalse(hoist.brake_on())

    def test_raise_and_stop(self) -> None:
        hoist = Hoist("crane-1")
        raise_hook(hoist)
        self.assertTrue(hoist.is_moving())
        hoist_stop(hoist)
        self.assertFalse(hoist.is_moving())
        hoist_limit_stop(hoist)
        self.assertFalse(hoist.is_moving())

    def test_drum_winding(self) -> None:
        drum = Drum(0.8)
        drum.pay_out(1.5)
        self.assertGreater(drum.wound_total(), 0.0)
        self.assertGreaterEqual(drum.layer_count(), 1)
        self.assertGreater(drum.height_from_cable(10.0), 0.0)


if __name__ == "__main__":
    unittest.main()
