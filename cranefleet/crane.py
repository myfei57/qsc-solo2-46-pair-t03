"""Crane command merging, load sway and command execution."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .audit import Audit
from .hoist import Hoist, lower as hoist_lower, raise_hook, stop as hoist_stop
from .slew import Slew, swing, stop as slew_stop
from .trolley import Trolley, move as trolley_move, stop as trolley_stop

SOURCE_CAB = "cab"
SOURCE_REMOTE = "remote"

ACTION_STOP = "stop"
ACTION_SWING = "swing"
ACTION_TROLLEY = "trolley"
ACTION_RAISE = "raise"
ACTION_LOWER = "lower"

AXIS_SLEW = "slew"
AXIS_TROLLEY = "trolley"
AXIS_HOIST = "hoist"


@dataclass(frozen=True)
class Command:
    """One command received from the cab or the radio remote."""

    source: str
    action: str
    axis: str = ""
    angle: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "action": self.action,
            "axis": self.axis,
            "angle": self.angle,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Command":
        return cls(
            source=str(payload.get("source", SOURCE_CAB)),
            action=str(payload.get("action", "")),
            axis=str(payload.get("axis", "")),
            angle=float(payload.get("angle", 0.0)),
        )


class Crane:
    """Mutable command state of a single crane."""

    def __init__(self, crane_id: str) -> None:
        self._lock = threading.Lock()
        self.id = crane_id
        self.cab_stop = False
        self.remote_stop = False
        self.last = Command(source=SOURCE_CAB, action="")
        self.sequence = 0
        self.stop_sequence = 0
        self.blocked_moves: list[str] = []

    def last_command(self) -> Command:
        with self._lock:
            return self.last

    def stops_pending(self) -> bool:
        with self._lock:
            return self.cab_stop or self.remote_stop

    def clear_stop(self) -> None:
        with self._lock:
            self.cab_stop = False
            self.remote_stop = False

    def command_sequence(self) -> int:
        with self._lock:
            return self.sequence

    def stop_sequence_now(self) -> int:
        with self._lock:
            return self.stop_sequence

    def blocked_moves_now(self) -> list[str]:
        with self._lock:
            return list(self.blocked_moves)


class Load:
    """A first-order sway model for the suspended load."""

    def __init__(self, mass: float) -> None:
        self._lock = threading.Lock()
        self.mass = mass
        self.angle = 0.0
        self.horizontal = 0.0
        self.vertical = 0.0

    def update(self, acceleration: float, seconds: float) -> None:
        with self._lock:
            self.horizontal += acceleration * seconds
            self.angle = self.horizontal / (self.mass + 1.0)

    def sway_angle(self) -> float:
        with self._lock:
            return self.angle

    def momentum(self) -> float:
        with self._lock:
            return self.mass * self.horizontal

    def set_vertical(self, height: float) -> None:
        with self._lock:
            self.vertical = height

    def height(self) -> float:
        with self._lock:
            return self.vertical


def merge(crane: Crane, command: Command) -> Command:
    """Merge an incoming command, keeping any stop ahead of later moves."""

    with crane._lock:  # noqa: SLF001 - module-level functions share crane state
        crane.sequence += 1
        sequence = crane.sequence
        if command.action == ACTION_STOP:
            if command.source == SOURCE_CAB:
                crane.cab_stop = True
            else:
                crane.remote_stop = True
            crane.stop_sequence = sequence
            crane.last = command
            return command
        if crane.cab_stop or crane.remote_stop:
            crane.blocked_moves.append(command.action)
            return crane.last
        crane.last = command
        return command


def apply_cab(crane: Crane, command: Command) -> Command:
    return merge(crane, command)


def apply_remote(crane: Crane, command: Command) -> Command:
    return merge(crane, command)


def execute(
    crane: Crane,
    slew: Slew,
    trolley: Trolley,
    hoist: Hoist,
    recorder: Audit | None = None,
) -> Command:
    """Execute the currently merged command against the machine modules."""

    command = crane.last_command()
    if command.action == ACTION_STOP:
        if command.axis == AXIS_TROLLEY:
            trolley_stop(trolley, recorder)
        elif command.axis == AXIS_HOIST:
            hoist_stop(hoist, recorder)
        else:
            slew_stop(slew, recorder)
    elif command.action == ACTION_SWING:
        swing(slew, command.angle, recorder)
    elif command.action == ACTION_TROLLEY:
        trolley_move(trolley, hoist)
    elif command.action == ACTION_RAISE:
        raise_hook(hoist, recorder)
    elif command.action == ACTION_LOWER:
        hoist_lower(hoist)
    return command
