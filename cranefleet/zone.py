"""Anti-collision zone state.

Every zone owns the occupancy registrations reported by the cranes working
inside it, the conflict flag raised when two cranes get too close, and the
latch that keeps a swing blocked until the conflict is cleared. The zone is
the component whose state must be durable before any swing is allowed, so the
persistence helpers live here rather than in the console.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from .geometry import Point, Polygon
from .errors import StoreNotFound
from .store import Store


@dataclass(frozen=True)
class ReleaseEntry:
    """One crane that was released from the latch at a given generation."""

    crane_id: str
    generation: int

    def to_dict(self) -> dict[str, object]:
        return {"crane_id": self.crane_id, "generation": self.generation}


@dataclass(frozen=True)
class Conflict:
    """A pair of cranes whose clearance fell below the safe distance."""

    zone_id: str
    crane_ids: tuple[str, str]

    def to_dict(self) -> dict[str, object]:
        return {"zone_id": self.zone_id, "crane_ids": list(self.crane_ids)}


@dataclass(frozen=True)
class Coverage:
    """How the current crane positions sit inside the zone boundary."""

    inside: tuple[str, ...]
    outside: tuple[str, ...]
    inside_bounds: tuple[str, ...]
    area: float
    bounds_width: float
    bounds_height: float
    center: Point
    expanded_area: float
    expanded_overlap: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "inside": list(self.inside),
            "outside": list(self.outside),
            "inside_bounds": list(self.inside_bounds),
            "area": self.area,
            "bounds_width": self.bounds_width,
            "bounds_height": self.bounds_height,
            "center": self.center.to_dict(),
            "expanded_area": self.expanded_area,
            "expanded_overlap": self.expanded_overlap,
        }


@dataclass(frozen=True)
class DistanceReport:
    """The closest clearance between the cranes currently inside the zone."""

    distance: float
    positions: dict[str, Point]
    revision: int
    fresh: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "distance": self.distance,
            "positions": {key: point.to_dict() for key, point in self.positions.items()},
            "revision": self.revision,
            "fresh": self.fresh,
        }


class ConflictNotifier(Protocol):
    """Receives the release notification once a zone conflict clears."""

    def on_conflict_cleared(
        self,
        zone_id: str,
        crane_ids: Sequence[str],
        generation: int,
        conflicts: Sequence[str],
    ) -> None:
        ...


class ZoneState:
    """Mutable state of a single anti-collision zone."""

    def __init__(self, zone_id: str, name: str, boundary: Sequence[Point]) -> None:
        self._lock = threading.Lock()
        self.id = zone_id
        self.name = name
        self.boundary: list[Point] = list(boundary)
        self.revision = 0
        self.durable = False
        self.occupants: dict[str, bool] = {}
        self.registrations: list[str] = []
        self.conflict = False
        self.latch = False
        self.latch_generation = 0
        self.conflict_pairs: list[str] = []
        self.cached_positions: dict[str, Point] = {}
        self.registration_seq = 0
        self.registration_seq_by_crane: dict[str, int] = {}
        self.release_ledger: list[ReleaseEntry] = []
        self.occupancy_version = 0

    def occupied_by(self, crane_id: str) -> bool:
        with self._lock:
            return self.occupants.get(crane_id, False)

    def occupant_count(self) -> int:
        with self._lock:
            return len(self.occupants)

    def boundary_copy(self) -> list[Point]:
        with self._lock:
            return list(self.boundary)

    def durable_revision(self) -> int:
        with self._lock:
            return self.revision

    def is_durable(self) -> bool:
        with self._lock:
            return self.durable

    def is_conflict(self) -> bool:
        with self._lock:
            return self.conflict


def _zone_key(zone_id: str) -> str:
    return "zone/" + zone_id


def occupy(zone: ZoneState, crane_id: str) -> int:
    """Register a crane inside the zone and return its registration sequence."""

    if not crane_id:
        return 0
    with zone._lock:  # noqa: SLF001 - module-level functions share zone state
        zone.occupants.setdefault(crane_id, True)
        zone.registrations.append(crane_id)
        zone.registration_seq += 1
        zone.registration_seq_by_crane[crane_id] = zone.registration_seq
        zone.occupancy_version += 1
        return zone.registration_seq


def vacate(zone: ZoneState, crane_id: str) -> None:
    """Remove a crane from the zone once it leaves."""

    with zone._lock:  # noqa: SLF001
        if zone.occupants.pop(crane_id, None) is not None:
            zone.occupancy_version += 1


def occupants_snapshot(zone: ZoneState) -> list[str]:
    with zone._lock:  # noqa: SLF001
        return list(zone.occupants)


def registration_count(zone: ZoneState) -> int:
    with zone._lock:  # noqa: SLF001
        return len(zone.registrations)


def registration_seq_of(zone: ZoneState, crane_id: str) -> int:
    with zone._lock:  # noqa: SLF001
        return zone.registration_seq_by_crane.get(crane_id, 0)


def latest_registration_seq(zone: ZoneState) -> int:
    with zone._lock:  # noqa: SLF001
        return zone.registration_seq


def is_latched(zone: ZoneState) -> bool:
    with zone._lock:  # noqa: SLF001
        return zone.latch


def latch_generation(zone: ZoneState) -> int:
    with zone._lock:  # noqa: SLF001
        return zone.latch_generation


def pending_releases(zone: ZoneState) -> list[ReleaseEntry]:
    with zone._lock:  # noqa: SLF001
        return list(zone.release_ledger)


def occupancy_version(zone: ZoneState) -> int:
    with zone._lock:  # noqa: SLF001
        return zone.occupancy_version


def persist(zone: ZoneState, store: Store) -> None:
    """Write the zone snapshot and mark the zone durable.

    The revision is bumped only after the store accepts the payload, so a
    failed write leaves the zone non-durable and blocks any swing.
    """

    with zone._lock:  # noqa: SLF001
        snapshot = {
            "id": zone.id,
            "name": zone.name,
            "boundary": [point.to_dict() for point in zone.boundary],
            "revision": zone.revision,
            "occupants": list(zone.occupants),
            "registrations": list(zone.registrations),
            "conflict": zone.conflict,
            "latch": zone.latch,
        }
        payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        store.save(_zone_key(zone.id), payload)
        zone.revision += 1
        zone.durable = True


def restore(zone: ZoneState, store: Store) -> None:
    """Reload the newest persisted zone snapshot on boot."""

    with zone._lock:  # noqa: SLF001
        try:
            payload = store.load(_zone_key(zone.id))
        except StoreNotFound:
            zone.durable = False
            return
        snapshot = json.loads(payload.decode("utf-8"))
        zone.name = snapshot["name"]
        zone.boundary = [Point(point["x"], point["y"]) for point in snapshot["boundary"]]
        zone.revision = snapshot["revision"]
        zone.occupants = {crane_id: True for crane_id in snapshot["occupants"]}
        zone.registrations = list(snapshot["registrations"])
        zone.conflict = snapshot["conflict"]
        zone.latch = snapshot["latch"]
        zone.durable = True


def detect_conflicts(zone: ZoneState, positions: Mapping[str, Point], safe: float) -> list[Conflict]:
    """Return every pair of occupying cranes closer than ``safe`` metres."""

    with zone._lock:  # noqa: SLF001
        cranes = sorted(zone.occupants)
    conflicts: list[Conflict] = []
    for index, first in enumerate(cranes):
        for second in cranes[index + 1 :]:
            point_a = positions.get(first)
            point_b = positions.get(second)
            if point_a is None or point_b is None:
                continue
            if point_a.distance(point_b) < safe:
                conflicts.append(Conflict(zone_id=zone.id, crane_ids=(first, second)))
    return conflicts


def register_conflict(zone: ZoneState, crane_ids: Sequence[str]) -> None:
    """Raise the conflict flag, latch the zone and record the pair."""

    with zone._lock:  # noqa: SLF001
        zone.conflict = True
        zone.latch = True
        for crane_id in crane_ids:
            if crane_id:
                zone.occupants[crane_id] = True
                zone.conflict_pairs.append(crane_id)


def lock_zone(zone: ZoneState) -> None:
    """Advance the latch generation and hold the zone."""

    with zone._lock:  # noqa: SLF001
        zone.latch = True
        zone.latch_generation += 1


def clear_conflict(zone: ZoneState, notifier: ConflictNotifier | None = None) -> int:
    """Clear the conflict and release the latch, notifying the fleet.

    Returns the generation the release was published under so callers can
    correlate the notification with the release ledger.
    """

    with zone._lock:  # noqa: SLF001
        zone.conflict = False
        zone.latch = False
        zone.latch_generation += 1
        generation = zone.latch_generation
        crane_ids = list(zone.occupants)
        conflicts = list(zone.conflict_pairs)
        zone.conflict_pairs = []
        for crane_id in crane_ids:
            zone.release_ledger.append(ReleaseEntry(crane_id=crane_id, generation=generation))
    if notifier is not None:
        notifier.on_conflict_cleared(zone.id, crane_ids, generation, conflicts)
    return generation


def coverage_check(zone: ZoneState, positions: Mapping[str, Point]) -> Coverage:
    """Split the supplied crane positions into inside/outside groups."""

    with zone._lock:  # noqa: SLF001
        boundary = list(zone.boundary)
    polygon = Polygon.from_points(boundary)
    bounds = polygon.bounds()
    expanded = polygon.expand(2.0)
    inside: list[str] = []
    outside: list[str] = []
    inside_bounds: list[str] = []
    for crane_id, point in positions.items():
        if polygon.contains(point):
            inside.append(crane_id)
        else:
            outside.append(crane_id)
        if bounds.contains(point):
            inside_bounds.append(crane_id)
    return Coverage(
        inside=tuple(sorted(inside)),
        outside=tuple(sorted(outside)),
        inside_bounds=tuple(sorted(inside_bounds)),
        area=polygon.area(),
        bounds_width=bounds.width(),
        bounds_height=bounds.height(),
        center=bounds.center(),
        expanded_area=expanded.area(),
        expanded_overlap=expanded.bounds().overlaps(bounds),
    )


def boundary_area(zone: ZoneState) -> float:
    with zone._lock:  # noqa: SLF001
        boundary = list(zone.boundary)
    return Polygon.from_points(boundary).area()


def separation(zone: ZoneState, positions: Mapping[str, Point]) -> DistanceReport:
    """Compute the closest clearance between every occupying crane pair."""

    with zone._lock:  # noqa: SLF001
        revision = zone.revision
        zone.cached_positions = {key: point for key, point in positions.items()}
    cached = {key: point for key, point in positions.items()}
    crane_ids = occupants_snapshot(zone)
    closest = -1.0
    if len(crane_ids) >= 2:
        for index, first in enumerate(crane_ids):
            for second in crane_ids[index + 1 :]:
                point_a = cached.get(first)
                point_b = cached.get(second)
                if point_a is None or point_b is None:
                    continue
                distance = point_a.distance(point_b)
                if closest < 0 or distance < closest:
                    closest = distance
    return DistanceReport(distance=closest, positions=cached, revision=revision, fresh=True)
