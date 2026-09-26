"""Wind monitoring: alarm, gust tracking, stop order and latch release."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .audit import Audit
from .errors import SlewStopNotAcknowledged
from .hoist import Hoist, stop as hoist_stop
from .limit import Limit, latch as limit_latch, release as limit_release
from .slew import Slew, stop_for_wind

ALARM_THRESHOLD = 10.0


@dataclass(frozen=True)
class ClearResult:
    """Outcome of clearing a wind alarm and releasing the limit latch."""

    latch_released: bool
    generation: int

    def to_dict(self) -> dict[str, object]:
        return {"latch_released": self.latch_released, "generation": self.generation}


class Wind:
    """Mutable state of the anemometer attached to the fleet."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.level = 0.0
        self.active = False
        self.latch = False
        self.sequence = 0
        self.level_history: list[float] = []
        self.generation = 0

    def current_level(self) -> float:
        with self._lock:
            return self.level

    def is_active(self) -> bool:
        with self._lock:
            return self.active

    def is_latched(self) -> bool:
        with self._lock:
            return self.latch

    def alarm_sequence(self) -> int:
        with self._lock:
            return self.sequence

    def history(self) -> list[float]:
        with self._lock:
            return list(self.level_history)

    def generation_now(self) -> int:
        with self._lock:
            return self.generation


class Gust:
    """Tracks the peak and sample count of the recent wind readings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.maximum = 0.0
        self.count = 0

    def observe(self, level: float) -> None:
        with self._lock:
            self.count += 1
            if level > self.maximum:
                self.maximum = level

    def peak(self) -> float:
        with self._lock:
            return self.maximum

    def samples(self) -> int:
        with self._lock:
            return self.count


def set_level(wind: Wind, level: float) -> None:
    with wind._lock:  # noqa: SLF001 - module-level functions share wind state
        wind.level = level
        if level >= ALARM_THRESHOLD:
            wind.active = True


def alarm(wind: Wind, slew: Slew, hoist: Hoist, recorder: Audit | None = None) -> int:
    """Raise a wind alarm and stop the slew before the hoist.

    Returns the alarm sequence so callers can correlate the stop with the
    matching audit entries.
    """

    with wind._lock:  # noqa: SLF001
        wind.active = True
        wind.sequence += 1
        sequence = wind.sequence
        wind.level_history.append(wind.level)
    status = stop_for_wind(slew, recorder, sequence)
    if not status.acknowledged():
        raise SlewStopNotAcknowledged("slew stop was not acknowledged")
    if status.already_stopped and recorder is not None:
        recorder.record(slew.id, "wind-slew-replay", "sequence replay after clear")
    hoist_stop(hoist, recorder)
    return sequence


def latch_limits(wind: Wind, limit: Limit) -> int:
    """Latch the limits as part of raising a wind alarm."""

    with wind._lock:  # noqa: SLF001
        wind.latch = True
        wind.generation += 1
        generation = wind.generation
    limit_latch(limit)
    return generation


def clear(wind: Wind, limit: Limit | None) -> ClearResult:
    """Clear the alarm and release the limit latch.

    A stale generation is rejected by :func:`limit.release` so an out-of-order
    clear cannot unlock a newer alarm.
    """

    with wind._lock:  # noqa: SLF001
        wind.active = False
        wind.level = 0.0
        wind.latch = False
        wind.generation += 1
        generation = wind.generation
    if limit is None:
        return ClearResult(latch_released=False, generation=generation)
    released = limit_release(limit, generation)
    return ClearResult(latch_released=released, generation=generation)
