"""Fleet geometry: survey, baseline, nearest pair and conflict registration."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .errors import NoCranesSurveyed, StoreNotFound
from .geometry import Line, Point, compute_separation
from .store import Store
from .zone import Conflict, ZoneState, detect_conflicts, latest_registration_seq
from .zone import occupants_snapshot, registration_count, registration_seq_of

BASELINE_KEY = "cluster/baseline"


@dataclass(frozen=True)
class ReleaseRecord:
    """One crane released from a latch, with its cumulative count."""

    crane_id: str
    generation: int
    count: int

    def to_dict(self) -> dict[str, object]:
        return {"crane_id": self.crane_id, "generation": self.generation, "count": self.count}


@dataclass(frozen=True)
class Pair:
    """The closest pair of cranes in the fleet."""

    first: str
    second: str
    distance: float
    line: Line

    def center(self) -> Point:
        return self.line.center()

    def to_dict(self) -> dict[str, object]:
        return {
            "first": self.first,
            "second": self.second,
            "distance": self.distance,
            "line": self.line.to_dict(),
        }


class Cluster:
    """Mutable state of the whole crane fleet."""

    def __init__(self, cranes: Mapping[str, Point]) -> None:
        self._lock = threading.Lock()
        self.cranes: dict[str, Point] = dict(cranes)
        self.baseline: dict[str, Point] = dict(cranes)
        self.revision = 0
        self.released: dict[str, bool] = {}
        self.release_generation = 0
        self.release_counts: dict[str, int] = {}
        self.release_ledger: list[ReleaseRecord] = []

    def on_conflict_cleared(
        self,
        zone_id: str,
        crane_ids: Sequence[str],
        generation: int,
        conflicts: Sequence[str],
    ) -> None:
        """Notifier callback invoked by the zone once a conflict clears."""

        with self._lock:
            if generation < self.release_generation:
                return
            self.release_generation = generation
            for crane_id in crane_ids:
                self.released[crane_id] = True
                self.release_counts[crane_id] = self.release_counts.get(crane_id, 0) + 1
                self.release_ledger.append(
                    ReleaseRecord(
                        crane_id=crane_id,
                        generation=generation,
                        count=self.release_counts[crane_id],
                    )
                )


def survey(cluster: Cluster, points: Mapping[str, Point]) -> None:
    """Record fresh surveyed coordinates for the supplied cranes."""

    with cluster._lock:  # noqa: SLF001 - module-level functions share cluster state
        for crane_id, point in points.items():
            cluster.cranes[crane_id] = point


def refresh_baseline(cluster: Cluster, store: Store) -> int:
    """Freeze the current coordinates as the new separation baseline."""

    with cluster._lock:  # noqa: SLF001
        if not cluster.cranes:
            raise NoCranesSurveyed("no cranes surveyed")
        cluster.baseline = dict(cluster.cranes)
        cluster.revision += 1
        revision = cluster.revision
        payload = json.dumps(
            {
                "revision": revision,
                "baseline": {key: point.to_dict() for key, point in cluster.baseline.items()},
                "surveyed_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ).encode("utf-8")
    store.save(BASELINE_KEY, payload)
    return revision


def restore_baseline(cluster: Cluster, store: Store) -> None:
    try:
        payload = store.load(BASELINE_KEY)
    except StoreNotFound:
        return
    snapshot = json.loads(payload.decode("utf-8"))
    with cluster._lock:  # noqa: SLF001
        cluster.revision = snapshot["revision"]
        cluster.baseline = {
            key: Point(point["x"], point["y"]) for key, point in snapshot["baseline"].items()
        }


def baseline_revision(cluster: Cluster) -> int:
    with cluster._lock:  # noqa: SLF001
        return cluster.revision


def current_position(cluster: Cluster, crane_id: str) -> Point | None:
    with cluster._lock:  # noqa: SLF001
        return cluster.cranes.get(crane_id)


def current_positions(cluster: Cluster) -> dict[str, Point]:
    with cluster._lock:  # noqa: SLF001
        return dict(cluster.cranes)


def baseline_positions(cluster: Cluster) -> dict[str, Point]:
    with cluster._lock:  # noqa: SLF001
        return dict(cluster.baseline)


def revision_now(cluster: Cluster) -> int:
    with cluster._lock:  # noqa: SLF001
        return cluster.revision


def reconcile_registrations(zone: ZoneState) -> list[str]:
    """Report occupancy registrations that never produced a sequence."""

    mismatches: list[str] = []
    if latest_registration_seq(zone) != registration_count(zone):
        mismatches.append("registration-sequence-gap")
    for crane_id in occupants_snapshot(zone):
        if registration_seq_of(zone, crane_id) == 0:
            mismatches.append(crane_id)
    return mismatches


def check_conflicts(cluster: Cluster, zone: ZoneState, safe: float) -> tuple[list[Conflict], list[str]]:
    """Detect zone conflicts from the live coordinates and audit registration."""

    conflicts = detect_conflicts(zone, current_positions(cluster), safe)
    return conflicts, reconcile_registrations(zone)


def nearest_pair(cluster: Cluster) -> Pair | None:
    with cluster._lock:  # noqa: SLF001
        cranes = dict(cluster.cranes)
    best: Pair | None = None
    identifiers = sorted(cranes)
    for index, first in enumerate(identifiers):
        for second in identifiers[index + 1 :]:
            point_a = cranes[first]
            point_b = cranes[second]
            if point_a.equal(point_b):
                continue
            distance = point_a.distance(point_b)
            if best is None or distance < best.distance:
                best = Pair(
                    first=first,
                    second=second,
                    distance=distance,
                    line=Line(start=point_a, end=point_b),
                )
    return best


def fleet_center(cluster: Cluster) -> Point:
    with cluster._lock:  # noqa: SLF001
        cranes = list(cluster.cranes.values())
    if not cranes:
        return Point(0.0, 0.0)
    total = Point(0.0, 0.0)
    for point in cranes:
        total = total.add(point)
    return total.scale(1.0 / len(cranes))


def spread(cluster: Cluster) -> float:
    """Return the distance from the fleet centre to the farthest crane."""

    center = fleet_center(cluster)
    with cluster._lock:  # noqa: SLF001
        cranes = list(cluster.cranes.values())
    widest = 0.0
    for point in cranes:
        widest = max(widest, center.distance(point))
    return widest


def is_released(cluster: Cluster, crane_id: str) -> bool:
    with cluster._lock:  # noqa: SLF001
        return cluster.released.get(crane_id, False)


def released_snapshot(cluster: Cluster) -> dict[str, bool]:
    with cluster._lock:  # noqa: SLF001
        return dict(cluster.released)


def release_generation_now(cluster: Cluster) -> int:
    with cluster._lock:  # noqa: SLF001
        return cluster.release_generation


def release_count(cluster: Cluster, crane_id: str) -> int:
    with cluster._lock:  # noqa: SLF001
        return cluster.release_counts.get(crane_id, 0)


def release_ledger_now(cluster: Cluster) -> list[ReleaseRecord]:
    with cluster._lock:  # noqa: SLF001
        return list(cluster.release_ledger)


def pair_separation(pair: Pair, safe: float) -> float:
    """Return the safe margin of the pair, never negative."""

    return compute_separation(pair.line.start, pair.line.end, safe).safe_margin()


def pair_clearance_from_center(cluster: Cluster, pair: Pair) -> float:
    """Distance from the fleet centre to the line between the pair."""

    return pair.line.distance_to(fleet_center(cluster))


def grid_spacing(cluster: Cluster) -> float:
    """Mean spacing between neighbouring cranes, used by the overview page."""

    with cluster._lock:  # noqa: SLF001
        cranes = dict(cluster.cranes)
    identifiers = sorted(cranes)
    if len(identifiers) < 2:
        return 0.0
    total = 0.0
    pairs = 0
    for index, first in enumerate(identifiers):
        for second in identifiers[index + 1 :]:
            total += cranes[first].distance(cranes[second])
            pairs += 1
    return total / pairs if pairs else 0.0


def spacing_deviation(cluster: Cluster) -> float:
    """Standard deviation of the neighbour spacing, a rough grid regularity."""

    with cluster._lock:  # noqa: SLF001
        cranes = dict(cluster.cranes)
    identifiers = sorted(cranes)
    distances: list[float] = []
    for index, first in enumerate(identifiers):
        for second in identifiers[index + 1 :]:
            distances.append(cranes[first].distance(cranes[second]))
    if not distances:
        return 0.0
    mean = sum(distances) / len(distances)
    variance = sum((value - mean) ** 2 for value in distances) / len(distances)
    return math.sqrt(variance)
