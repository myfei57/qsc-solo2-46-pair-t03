"""Hoist drum, hook lowering and the speed-feedback brake interlock."""

from __future__ import annotations

import json
import math
import threading
from typing import TYPE_CHECKING, Protocol

from .audit import Audit
from .errors import FeedbackNotConfirmed, HoistBusy
from .errors import StoreNotFound
from .store import Store

if TYPE_CHECKING:
    from .slew import SpeedFeedback

CABLE_DIAMETER = 0.02


class FeedbackReader(Protocol):
    """The part of the speed feedback the brake interlock depends on."""

    def is_confirmed(self) -> bool:
        ...


class Hoist:
    """Mutable state of the hoisting gear."""

    def __init__(self, hoist_id: str) -> None:
        self._lock = threading.Lock()
        self.id = hoist_id
        self.lowered = False
        self.brake_engaged = True
        self.moving = False

    def hook_lowered(self) -> bool:
        with self._lock:
            return self.lowered

    def is_moving(self) -> bool:
        with self._lock:
            return self.moving

    def brake_on(self) -> bool:
        with self._lock:
            return self.brake_engaged


class Drum:
    """Winding geometry used to convert paid-out cable into hook height."""

    def __init__(self, diameter: float) -> None:
        self._lock = threading.Lock()
        self.diameter = diameter
        self.layers = 1
        self.wound = 0.0

    def pay_out(self, length: float) -> None:
        with self._lock:
            self.wound += length
            self.layers = 1 + int(self.wound / (self.diameter * math.pi * CABLE_DIAMETER))

    def height_from_cable(self, cable_out: float) -> float:
        with self._lock:
            effective = self.diameter + self.layers * CABLE_DIAMETER
            return cable_out / math.pi / effective

    def layer_count(self) -> int:
        with self._lock:
            return self.layers

    def wound_total(self) -> float:
        with self._lock:
            return self.wound


def _hoist_key(hoist_id: str) -> str:
    return "hoist/" + hoist_id


def lower(hoist: Hoist) -> None:
    """Lower the hook; refused while the drum is already turning."""

    with hoist._lock:  # noqa: SLF001 - module-level functions share hoist state
        if hoist.moving:
            raise HoistBusy("drum is busy")
        hoist.lowered = True
        hoist.moving = True


def persist_hoist(hoist: Hoist, store: Store) -> None:
    with hoist._lock:  # noqa: SLF001
        payload = json.dumps(
            {
                "lowered": hoist.lowered,
                "moving": hoist.moving,
                "brake_engaged": hoist.brake_engaged,
            }
        ).encode("utf-8")
    store.save(_hoist_key(hoist.id), payload)


def resume_lower(hoist: Hoist, store: Store) -> None:
    """Reload the hoist snapshot so a resumed cycle keeps its hook state."""

    try:
        payload = store.load(_hoist_key(hoist.id))
    except StoreNotFound:
        with hoist._lock:  # noqa: SLF001
            hoist.lowered = False
        return
    snapshot = json.loads(payload.decode("utf-8"))
    with hoist._lock:  # noqa: SLF001
        hoist.lowered = snapshot["lowered"]
        hoist.moving = snapshot["moving"]
        hoist.brake_engaged = snapshot["brake_engaged"]


def release_brake(hoist: Hoist, feedback: FeedbackReader) -> None:
    """Release the brake only once the speed feedback is confirmed."""

    with hoist._lock:  # noqa: SLF001
        if not feedback.is_confirmed():
            raise FeedbackNotConfirmed("speed feedback not confirmed")
        hoist.brake_engaged = False
        hoist.moving = True


def engage_brake(hoist: Hoist) -> None:
    with hoist._lock:  # noqa: SLF001
        hoist.brake_engaged = True
        hoist.moving = False


def resume_release(hoist: Hoist, feedback: "SpeedFeedback", store: Store) -> None:
    """Restore the feedback snapshot and then release the brake."""

    from .slew import restore_feedback

    restore_feedback(feedback, store)
    release_brake(hoist, feedback)


def stop(hoist: Hoist, recorder: Audit | None = None) -> None:
    with hoist._lock:  # noqa: SLF001
        hoist.moving = False
    if recorder is not None:
        recorder.record(hoist.id, "hoist-stop", "hoist stopped")


def raise_hook(hoist: Hoist, recorder: Audit | None = None) -> None:
    with hoist._lock:  # noqa: SLF001
        if hoist.moving:
            raise HoistBusy("drum is busy")
        hoist.moving = True
        hoist.brake_engaged = False
    if recorder is not None:
        recorder.record(hoist.id, "hoist-raise", "raising")


def limit_stop(hoist: Hoist) -> None:
    with hoist._lock:  # noqa: SLF001
        hoist.moving = False
