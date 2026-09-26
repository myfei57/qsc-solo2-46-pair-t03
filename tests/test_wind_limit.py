"""Unit tests for wind handling and limit arbitration."""

from __future__ import annotations

import unittest

from cranefleet.audit import Audit
from cranefleet.errors import SlewStopNotAcknowledged, StaleRelease
from cranefleet.hoist import Hoist, lower as hoist_lower
from cranefleet.limit import (
    AXIS_HOIST,
    AXIS_SLEW,
    AXIS_TROLLEY,
    Arbitration,
    Envelope,
    Limit,
    arbitrate,
    enforce_envelopes,
    envelope_margins,
    latch,
    position_envelope,
    release,
    reset_arbitration,
    slew_position,
    trip,
    trolley_position,
)
from cranefleet.slew import Slew
from cranefleet.trolley import Trolley
from cranefleet.wind import (
    ALARM_THRESHOLD,
    Gust,
    Wind,
    alarm,
    clear,
    latch_limits,
    set_level,
)
from cranefleet.geometry import Point
from cranefleet.store import MemoryStore
from cranefleet.zone import ZoneState
from cranefleet.slew import start as slew_start

BOUNDARY = [Point(0.0, 0.0), Point(100.0, 0.0), Point(100.0, 100.0), Point(0.0, 100.0)]


class WindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wind = Wind()
        self.gust = Gust()
        self.slew = Slew("crane-1")
        self.hoist = Hoist("crane-1")
        self.limit = Limit()
        self.audit = Audit(50)
        self.zone = ZoneState("zone-1", "zone", BOUNDARY)
        self.store = MemoryStore()

    def test_set_level_and_gust(self) -> None:
        set_level(self.wind, 4.0)
        self.assertEqual(self.wind.current_level(), 4.0)
        self.assertFalse(self.wind.is_active())
        set_level(self.wind, ALARM_THRESHOLD)
        self.assertTrue(self.wind.is_active())
        self.gust.observe(4.0)
        self.gust.observe(14.0)
        self.assertEqual(self.gust.samples(), 2)
        self.assertEqual(self.gust.peak(), 14.0)

    def test_alarm_stops_slew_before_hoist(self) -> None:
        slew_start(self.slew, self.zone, self.store, self.audit)
        hoist_lower(self.hoist)
        set_level(self.wind, 14.0)
        sequence = alarm(self.wind, self.slew, self.hoist, self.audit)
        self.assertEqual(sequence, 1)
        self.assertFalse(self.slew.is_moving())
        self.assertTrue(self.slew.held_by_wind())
        self.assertFalse(self.hoist.is_moving())
        actions = [entry.action for entry in self.audit.snapshot()]
        self.assertLess(actions.index("wind-slew-stop"), actions.index("hoist-stop"))
        self.assertEqual(self.wind.history(), [14.0])

    def test_replayed_alarm_is_recorded(self) -> None:
        set_level(self.wind, 14.0)
        alarm(self.wind, self.slew, self.hoist, self.audit)
        alarm(self.wind, self.slew, self.hoist, self.audit)
        actions = [entry.action for entry in self.audit.snapshot()]
        self.assertIn("wind-slew-replay", actions)
        self.assertEqual(self.wind.alarm_sequence(), 2)

    def test_latch_and_clear_round_trip(self) -> None:
        latch_limits(self.wind, self.limit)
        self.assertTrue(self.wind.is_latched())
        self.assertTrue(self.limit.is_latched())
        self.assertEqual(self.wind.generation_now(), 1)
        result = clear(self.wind, self.limit)
        self.assertTrue(result.latch_released)
        self.assertFalse(self.wind.is_latched())
        self.assertFalse(self.limit.is_latched())
        self.assertEqual(result.to_dict()["generation"], 2)
        self.assertEqual(clear(self.wind, None).latch_released, False)

    def test_stale_release_is_rejected(self) -> None:
        latch(self.limit)
        latch(self.limit)
        with self.assertRaises(StaleRelease):
            release(self.limit, 1)
        self.assertTrue(release(self.limit, 2))
        self.assertEqual(self.limit.generation_now(), 2)


class LimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limit = Limit()
        self.slew = Slew("crane-1")
        self.trolley = Trolley("crane-1")
        self.hoist = Hoist("crane-1")

    def test_envelope_and_margins(self) -> None:
        inside = Envelope(axis=AXIS_SLEW, minimum=-100.0, maximum=100.0, value=lambda: 40.0)
        outside = Envelope(axis=AXIS_TROLLEY, minimum=-100.0, maximum=100.0, value=lambda: 140.0)
        self.assertFalse(inside.exceeded())
        self.assertEqual(inside.margin(), 0.0)
        self.assertTrue(outside.exceeded())
        self.assertEqual(outside.margin(), 40.0)
        self.assertEqual(
            envelope_margins([inside, outside]),
            {AXIS_SLEW: 0.0, AXIS_TROLLEY: 40.0},
        )

    def test_enforce_trips_axes(self) -> None:
        envelopes = [
            position_envelope(AXIS_SLEW, slew_position(150.0)),
            position_envelope(AXIS_TROLLEY, trolley_position(0.0)),
        ]
        exceeded = enforce_envelopes(self.limit, envelopes)
        self.assertEqual(exceeded, [AXIS_SLEW])
        self.assertTrue(self.limit.is_tripped(AXIS_SLEW))
        self.assertFalse(self.limit.is_tripped(AXIS_TROLLEY))

    def test_arbitrate_stops_every_axis(self) -> None:
        lower = position_envelope(AXIS_HOIST, lambda: 0.0, -50.0, 50.0)
        exceed = position_envelope(AXIS_SLEW, lambda: 500.0)
        exceeded = enforce_envelopes(self.limit, [lower, exceed])
        trip(self.limit, AXIS_TROLLEY)
        self.assertEqual(exceeded, [AXIS_SLEW])
        arbitration = arbitrate(self.limit, self.slew, self.trolley, self.hoist)
        self.assertEqual(arbitration.axes, (AXIS_SLEW, AXIS_TROLLEY))
        self.assertEqual(set(arbitration.stops), {AXIS_SLEW, AXIS_TROLLEY})
        self.assertEqual(arbitration.sequence, 1)
        self.assertIn("stops", arbitration.to_dict())
        self.assertTrue(self.limit.is_stopped(AXIS_SLEW))
        self.assertTrue(self.limit.is_stopped(AXIS_TROLLEY))
        self.assertFalse(self.slew.is_moving())
        self.assertFalse(self.trolley.is_moving())
        reset_arbitration(self.limit)
        self.assertFalse(self.limit.is_stopped(AXIS_SLEW))

    def test_arbitration_of_hoist_axis(self) -> None:
        self.hoist.moving = True
        trip(self.limit, AXIS_HOIST)
        arbitrate(self.limit, self.slew, self.trolley, self.hoist)
        self.assertFalse(self.hoist.is_moving())

    def test_empty_arbitration(self) -> None:
        arbitration = arbitrate(self.limit, self.slew, self.trolley, self.hoist)
        self.assertEqual(arbitration.axes, ())
        arbitration_dataclass = Arbitration(axes=(), stops={}, sequence=0)
        self.assertEqual(arbitration_dataclass.to_dict()["axes"], [])

    def test_alarm_requires_acknowledged_stop(self) -> None:
        from cranefleet.slew import set_fault

        set_fault(self.slew, True)
        self.assertTrue(self.slew.is_faulted())
        with self.assertRaises(SlewStopNotAcknowledged):
            alarm(Wind(), self.slew, self.hoist, None)
        set_fault(self.slew, False)
        self.assertFalse(self.slew.is_faulted())


if __name__ == "__main__":
    unittest.main()
