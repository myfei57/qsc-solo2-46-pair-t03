"""Planar geometry primitives used by the anti-collision logic.

The module covers the small amount of analytic geometry the control service
needs: distance between cranes, membership inside a working zone, the area of
a zone, and the safe-separation margin used by the conflict checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

EPSILON = 1e-9


@dataclass(frozen=True)
class Point:
    """A point in the site coordinate system, in metres."""

    x: float
    y: float

    def distance(self, other: "Point") -> float:
        dx = self.x - other.x
        dy = self.y - other.y
        return math.sqrt(dx * dx + dy * dy)

    def midpoint(self, other: "Point") -> "Point":
        return Point((self.x + other.x) / 2.0, (self.y + other.y) / 2.0)

    def add(self, other: "Point") -> "Point":
        return Point(self.x + other.x, self.y + other.y)

    def scale(self, factor: float) -> "Point":
        return Point(self.x * factor, self.y * factor)

    def equal(self, other: "Point") -> bool:
        return abs(self.x - other.x) < EPSILON and abs(self.y - other.y) < EPSILON

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class Line:
    """A straight segment between two crane positions."""

    start: Point
    end: Point

    def length(self) -> float:
        return self.start.distance(self.end)

    def project(self, point: Point) -> Point:
        abx = self.end.x - self.start.x
        aby = self.end.y - self.start.y
        length_squared = abx * abx + aby * aby
        if length_squared == 0:
            return self.start
        factor = ((point.x - self.start.x) * abx + (point.y - self.start.y) * aby) / length_squared
        if factor < 0:
            factor = 0.0
        if factor > 1:
            factor = 1.0
        return Point(self.start.x + factor * abx, self.start.y + factor * aby)

    def distance_to(self, point: Point) -> float:
        return point.distance(self.project(point))

    def center(self) -> Point:
        return self.start.midpoint(self.end)

    def to_dict(self) -> dict[str, dict[str, float]]:
        return {"start": self.start.to_dict(), "end": self.end.to_dict()}


@dataclass(frozen=True)
class Bounds:
    """An axis-aligned bounding box."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def contains(self, point: Point) -> bool:
        return self.min_x <= point.x <= self.max_x and self.min_y <= point.y <= self.max_y

    def overlaps(self, other: "Bounds") -> bool:
        return (
            self.min_x <= other.max_x
            and self.max_x >= other.min_x
            and self.min_y <= other.max_y
            and self.max_y >= other.min_y
        )

    def width(self) -> float:
        return self.max_x - self.min_x

    def height(self) -> float:
        return self.max_y - self.min_y

    def center(self) -> Point:
        return Point((self.min_x + self.max_x) / 2.0, (self.min_y + self.max_y) / 2.0)

    def to_dict(self) -> dict[str, float]:
        return {
            "min_x": self.min_x,
            "min_y": self.min_y,
            "max_x": self.max_x,
            "max_y": self.max_y,
        }


def bounds_of(points: Sequence[Point]) -> Bounds:
    """Return the bounding box that covers every supplied point."""

    if not points:
        return Bounds(0.0, 0.0, 0.0, 0.0)
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    return Bounds(min(xs), min(ys), max(xs), max(ys))


@dataclass(frozen=True)
class Polygon:
    """A simple polygon described by its boundary points."""

    points: tuple[Point, ...]

    @classmethod
    def from_points(cls, points: Iterable[Point]) -> "Polygon":
        return cls(tuple(points))

    def bounds(self) -> Bounds:
        return bounds_of(self.points)

    def contains(self, point: Point) -> bool:
        """Test membership with a ray-cast crossing count."""

        if len(self.points) < 3:
            return False
        inside = False
        count = len(self.points)
        j = count - 1
        for i in range(count):
            a = self.points[i]
            b = self.points[j]
            if (a.y > point.y) != (b.y > point.y):
                cross = (b.x - a.x) * (point.y - a.y) / (b.y - a.y) + a.x
                if point.x < cross:
                    inside = not inside
            j = i
        return inside

    def area(self) -> float:
        total = 0.0
        count = len(self.points)
        for i in range(count):
            a = self.points[i]
            b = self.points[(i + 1) % count]
            total += a.x * b.y - b.x * a.y
        return abs(total) / 2.0

    def centroid(self) -> Point:
        if not self.points:
            return Point(0.0, 0.0)
        total = Point(0.0, 0.0)
        for point in self.points:
            total = total.add(point)
        return total.scale(1.0 / len(self.points))

    def expand(self, delta: float) -> "Polygon":
        """Push every vertex outward from the centroid by ``delta`` metres."""

        center = self.centroid()
        expanded = []
        for point in self.points:
            direction = point.add(center.scale(-1.0))
            length = direction.distance(Point(0.0, 0.0))
            if length == 0:
                expanded.append(point)
                continue
            unit = direction.scale(1.0 / length)
            expanded.append(point.add(unit.scale(delta)))
        return Polygon(tuple(expanded))


@dataclass(frozen=True)
class Separation:
    """The clearance between two cranes against the configured safe distance."""

    distance: float
    below_safe: bool
    margin: float

    def safe_margin(self) -> float:
        return self.margin if self.margin > 0 else 0.0

    def to_dict(self) -> dict[str, float | bool]:
        return {
            "distance": self.distance,
            "below_safe": self.below_safe,
            "margin": self.margin,
            "safe_margin": self.safe_margin(),
        }


def compute_separation(a: Point, b: Point, safe: float) -> Separation:
    distance = a.distance(b)
    return Separation(distance=distance, below_safe=distance < safe, margin=distance - safe)


def is_below_safe(a: Point, b: Point, safe: float) -> bool:
    return compute_separation(a, b, safe).below_safe
