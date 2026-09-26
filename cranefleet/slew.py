"""Slewing control: start gate, swing, stop and speed feedback.

The swing is only allowed once the collision-zone state is durable, so the
start gate deliberately reaches into the zone module. A wind alarm and a zone
latch both hold the slew until the corresponding release arrives.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass

from .audit import Audit
from .errors import (
    SlewBlocked,
    SlewBusy,
    SlewIdle,
    StoreNotFound,
    ZoneNotDurable,
)
from .store import Store
from . import zone as zone_module
from .zone import ZoneState

RAMP_RATE = 2.0


class Ramp:
    """A rate-limited ramp that walks the slew velocity toward a target."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.target = 0.0
        self.velocity = 0.0
        self.accelerated = False

    def set_target(self, target: float) -> None:
        with self._lock:
            self.target = target
            self.accelerated = True

    def step(self, dt: float) -> float:
        with self._lock:
            remaining = self.target - self.velocity
            step = RAMP_RATE * dt
            if abs(remaining) < step:
                self.velocity = self.target
            elif remaining > 0:
                self.velocity += step
            else:
                self.velocity -= step
            return self.velocity

    def velocity_now(self) -> float:
        with self._lock:
            return self.velocity

    def target_now(self) -> float:
        with self._lock:
            return self.target


class Slew:
    """Mutable state of one crane's slewing gear."""

    def __init__(self, slew_id: str) -> None:
        self._lock = threading.Lock()
        self.id = slew_id
        self.angle = 0.0
        self.moving = False
        self.stopped = False
        self.wind_held = False
        self.latch_held = False
        self.axis_stopped: dict[str, bool] = {}
        self.stop_reasons: dict[str, str] = {}
        self.stop_sequences: dict[str, int] = {}
        self.stop_count = 0
        self.last_stop_sequence = 0
        self.faulted = False

    def angle_now(self) -> float:
        with self._lock:
            return self.angle

    def is_moving(self) -> bool:
        with self._lock:
            return self.moving

    def is_stopped(self) -> bool:
        with self._lock:
            return self.stopped

    def held_by_wind(self) -> bool:
        with self._lock:
            return self.wind_held

    def held_by_latch(self) -> bool:
        with self._lock:
            return self.latch_held

    def stop_count_now(self) -> int:
        with self._lock:
            return self.stop_count

    def last_stop_sequence_now(self) -> int:
        with self._lock:
            return self.last_stop_sequence

    def is_faulted(self) -> bool:
        with self._lock:
            return self.faulted


@dataclass(frozen=True)
class StopStatus:
    """The outcome of a wind stop, used to detect a replayed alarm."""

    already_stopped: bool
    stopped_now: bool
    sequence: int

    def acknowledged(self) -> bool:
        return self.already_stopped or self.stopped_now


class SpeedFeedback:
    """The speed feedback that must confirm before the brake is released."""

    def __init__(self, crane_id: str) -> None:
        self._lock = threading.Lock()
        self.crane_id = crane_id
        self.confirmed = False
        self.speed = 0.0

    def confirm(self, speed: float) -> None:
        with self._lock:
            self.confirmed = True
            self.speed = speed

    def is_confirmed(self) -> bool:
        with self._lock:
            return self.confirmed

    def speed_now(self) -> float:
        with self._lock:
            return self.speed


def format_angle(angle: float) -> str:
    return f"{angle:.2f}"


def start(slew: Slew, zone: ZoneState, store: Store, recorder: Audit | None = None) -> None:
    """Allow a swing once the interlocks and the durable zone state agree."""

    with slew._lock:  # noqa: SLF001 - module-level functions share slew state
        if slew.stopped or slew.wind_held or slew.latch_held:
            raise SlewBlocked("slew blocked by interlock")
        if not zone.is_durable():
            zone_module.persist(zone, store)
            if not zone.is_durable():
                raise ZoneNotDurable("collision-zone state is not durable")
        if slew.moving:
            raise SlewBusy("slew is already moving")
        slew.moving = True
        if recorder is not None:
            recorder.record(slew.id, "slew-start", "swing allowed")


def swing(slew: Slew, angle: float, recorder: Audit | None = None) -> None:
    with slew._lock:  # noqa: SLF001
        if not slew.moving:
            raise SlewIdle("slew is not moving")
        slew.angle += angle
        if recorder is not None:
            recorder.record(slew.id, "slew-swing", "angle=" + format_angle(slew.angle))


def stop(slew: Slew, recorder: Audit | None = None) -> None:
    with slew._lock:  # noqa: SLF001
        was_moving = slew.moving
        slew.moving = False
        slew.stopped = True
    if recorder is not None:
        recorder.record(slew.id, "slew-stop", "moving=" + str(was_moving).lower())


def stop_for_wind(slew: Slew, recorder: Audit | None, sequence: int) -> StopStatus:
    """Stop the slew as the first half of the wind stop order."""

    with slew._lock:  # noqa: SLF001
        if slew.faulted:
            return StopStatus(already_stopped=False, stopped_now=False, sequence=sequence)
        was_moving = slew.moving
        slew.moving = False
        slew.wind_held = True
        slew.stop_count += 1
        slew.last_stop_sequence = sequence
    if recorder is not None:
        recorder.record(
            slew.id,
            "wind-slew-stop",
            f"seq={sequence} moving={str(was_moving).lower()}",
        )
    return StopStatus(already_stopped=not was_moving, stopped_now=was_moving, sequence=sequence)


def limit_stop(slew: Slew, axis: str, sequence: int) -> None:
    with slew._lock:  # noqa: SLF001
        slew.axis_stopped[axis] = True
        slew.moving = False
        slew.stop_reasons[axis] = "limit"
        slew.stop_sequences[axis] = sequence


def is_axis_stopped(slew: Slew, axis: str) -> bool:
    with slew._lock:  # noqa: SLF001
        return slew.axis_stopped.get(axis, False)


def stop_reason(slew: Slew, axis: str) -> str:
    with slew._lock:  # noqa: SLF001
        return slew.stop_reasons.get(axis, "")


def stop_sequence(slew: Slew, axis: str) -> int:
    with slew._lock:  # noqa: SLF001
        return slew.stop_sequences.get(axis, 0)


def release_wind_hold(slew: Slew) -> None:
    with slew._lock:  # noqa: SLF001
        slew.wind_held = False


def release_latch_hold(slew: Slew) -> None:
    with slew._lock:  # noqa: SLF001
        slew.latch_held = False


def set_fault(slew: Slew, faulted: bool) -> None:
    """Flag a slewing drive fault that prevents the wind stop from landing."""

    with slew._lock:  # noqa: SLF001
        slew.faulted = faulted
        if faulted:
            slew.moving = False


def _feedback_key(crane_id: str) -> str:
    return "feedback/" + crane_id


def persist_feedback(feedback: SpeedFeedback, store: Store) -> None:
    with feedback._lock:  # noqa: SLF001
        payload = json.dumps(
            {"confirmed": feedback.confirmed, "speed": feedback.speed}
        ).encode("utf-8")
    store.save(_feedback_key(feedback.crane_id), payload)


def restore_feedback(feedback: SpeedFeedback, store: Store) -> None:
    try:
        payload = store.load(_feedback_key(feedback.crane_id))
    except StoreNotFound:
        with feedback._lock:  # noqa: SLF001
            feedback.confirmed = False
        return
    snapshot = json.loads(payload.decode("utf-8"))
    with feedback._lock:  # noqa: SLF001
        feedback.confirmed = snapshot["confirmed"]
        feedback.speed = snapshot["speed"]


def ramp_reached(ramp: Ramp) -> bool:
    """Report whether the ramp has finished walking to its target."""

    return math.isclose(ramp.velocity_now(), ramp.target_now(), abs_tol=1e-9)
