"""Trolley travel with the hook-lowered interlock and a speed sampler."""

from __future__ import annotations

import threading

from .audit import Audit
from .errors import HookNotLowered
from .hoist import Hoist, resume_lower
from .store import Store


class Trolley:
    """Mutable state of the trolley that moves the load along the jib."""

    def __init__(self, trolley_id: str) -> None:
        self._lock = threading.Lock()
        self.id = trolley_id
        self.position = 0.0
        self.moving = False

    def is_moving(self) -> bool:
        with self._lock:
            return self.moving

    def position_now(self) -> float:
        with self._lock:
            return self.position


class Speedometer:
    """Derives trolley speed from consecutive position samples."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_position = 0.0
        self._last_speed = 0.0

    def sample(self, position: float, seconds: float) -> float:
        if seconds <= 0:
            return 0.0
        with self._lock:
            speed = (position - self._last_position) / seconds
            self._last_position = position
            self._last_speed = speed
            return speed

    def last_speed(self) -> float:
        with self._lock:
            return self._last_speed


def move(trolley: Trolley, hoist: Hoist) -> None:
    """Move the trolley only after the hook has been lowered."""

    with trolley._lock:  # noqa: SLF001 - module-level functions share trolley state
        if not hoist.hook_lowered():
            raise HookNotLowered("hook is not lowered")
        trolley.moving = True


def move_resume(trolley: Trolley, hoist: Hoist, store: Store) -> None:
    """Resume a trolley move by reloading the hoist snapshot first."""

    resume_lower(hoist, store)
    move(trolley, hoist)


def stop(trolley: Trolley, recorder: Audit | None = None) -> None:
    with trolley._lock:  # noqa: SLF001
        trolley.moving = False
    if recorder is not None:
        recorder.record(trolley.id, "trolley-stop", "stopped")


def limit_stop(trolley: Trolley) -> None:
    """Hold the trolley after its axis trips a limit."""

    with trolley._lock:  # noqa: SLF001
        trolley.moving = False
