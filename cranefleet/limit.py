"""Travel limits, multi-axis arbitration and latch release generations."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from .errors import StaleRelease
from .hoist import Hoist, limit_stop as hoist_limit_stop
from .slew import Slew, limit_stop as slew_limit_stop
from .trolley import Trolley, limit_stop as trolley_limit_stop

AXIS_SLEW = "slew"
AXIS_TROLLEY = "trolley"
AXIS_HOIST = "hoist"


class Limit:
    """Mutable state of the travel limits of every axis."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tripped: dict[str, bool] = {}
        self.stopped: dict[str, bool] = {}
        self.latch = False
        self.generation = 0
        self.sequence = 0

    def is_tripped(self, axis: str) -> bool:
        with self._lock:
            return self.tripped.get(axis, False)

    def is_stopped(self, axis: str) -> bool:
        with self._lock:
            return self.stopped.get(axis, False)

    def is_latched(self) -> bool:
        with self._lock:
            return self.latch

    def generation_now(self) -> int:
        with self._lock:
            return self.generation

    def sequence_now(self) -> int:
        with self._lock:
            return self.sequence


@dataclass(frozen=True)
class Envelope:
    """A single axis envelope described by a live value reader."""

    axis: str
    minimum: float
    maximum: float
    value: Callable[[], float]

    def exceeded(self) -> bool:
        current = self.value()
        return current < self.minimum or current > self.maximum

    def margin(self) -> float:
        current = self.value()
        if current < self.minimum:
            return self.minimum - current
        if current > self.maximum:
            return current - self.maximum
        return 0.0


@dataclass(frozen=True)
class Arbitration:
    """The result of arbitrating every tripped axis in one pass."""

    axes: tuple[str, ...]
    stops: dict[str, bool]
    sequence: int

    def to_dict(self) -> dict[str, object]:
        return {"axes": list(self.axes), "stops": self.stops, "sequence": self.sequence}


def trip(limit: Limit, axis: str) -> None:
    with limit._lock:  # noqa: SLF001 - module-level functions share limit state
        limit.tripped[axis] = True


def latch(limit: Limit) -> int:
    with limit._lock:  # noqa: SLF001
        limit.latch = True
        limit.generation += 1
        return limit.generation


def release(limit: Limit, generation: int) -> bool:
    """Release the latch for ``generation``; older generations are rejected."""

    with limit._lock:  # noqa: SLF001
        if generation < limit.generation:
            raise StaleRelease("stale release generation")
        limit.latch = False
        limit.generation = generation
        return True


def arbitrate(limit: Limit, slew: Slew, trolley: Trolley, hoist: Hoist) -> Arbitration:
    """Stop every axis that has tripped a limit in a single pass."""

    with limit._lock:  # noqa: SLF001
        limit.sequence += 1
        sequence = limit.sequence
        axes = sorted(limit.tripped)
        stops: dict[str, bool] = {}
        for axis in axes:
            if axis == AXIS_SLEW:
                slew_limit_stop(slew, axis, sequence)
            elif axis == AXIS_TROLLEY:
                trolley_limit_stop(trolley)
            elif axis == AXIS_HOIST:
                hoist_limit_stop(hoist)
            stops[axis] = True
            limit.stopped[axis] = True
            del limit.tripped[axis]
    return Arbitration(axes=tuple(axes), stops=stops, sequence=sequence)


def reset_arbitration(limit: Limit) -> None:
    with limit._lock:  # noqa: SLF001
        limit.stopped = {}


def position_envelope(axis: str, value: Callable[[], float], minimum: float = -100.0, maximum: float = 100.0) -> Envelope:
    return Envelope(axis=axis, minimum=minimum, maximum=maximum, value=value)


def enforce_envelopes(limit: Limit, envelopes: list[Envelope]) -> list[str]:
    """Trip every envelope that is out of range and return the axes."""

    exceeded: list[str] = []
    for envelope in envelopes:
        if envelope.exceeded():
            trip(limit, envelope.axis)
            exceeded.append(envelope.axis)
    return exceeded


def envelope_margins(envelopes: list[Envelope]) -> dict[str, float]:
    return {envelope.axis: envelope.margin() for envelope in envelopes}


def slew_position(x: float) -> Callable[[], float]:
    return lambda: x


def trolley_position(y: float) -> Callable[[], float]:
    return lambda: y
