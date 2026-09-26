"""Unit tests for the planar geometry helpers."""

from __future__ import annotations

import unittest

from cranefleet.geometry import (
    Bounds,
    Line,
    Point,
    Polygon,
    bounds_of,
    compute_separation,
    is_below_safe,
)


class PointTests(unittest.TestCase):
    def test_distance_midpoint_and_equal(self) -> None:
        a = Point(0.0, 0.0)
        b = Point(3.0, 4.0)
        self.assertAlmostEqual(a.distance(b), 5.0)
        self.assertTrue(a.midpoint(b).equal(Point(1.5, 2.0)))
        self.assertTrue(a.equal(Point(0.0, 0.0)))
        self.assertFalse(a.equal(Point(0.001, 0.0)))

    def test_add_scale_and_dict(self) -> None:
        self.assertEqual(Point(1.0, 2.0).add(Point(3.0, 4.0)), Point(4.0, 6.0))
        self.assertEqual(Point(2.0, 3.0).scale(2.0), Point(4.0, 6.0))
        self.assertEqual(Point(1.0, 2.0).to_dict(), {"x": 1.0, "y": 2.0})


class LineTests(unittest.TestCase):
    def test_length_projection_and_distance(self) -> None:
        line = Line(Point(0.0, 0.0), Point(10.0, 0.0))
        self.assertAlmostEqual(line.length(), 10.0)
        self.assertTrue(line.project(Point(5.0, 8.0)).equal(Point(5.0, 0.0)))
        self.assertAlmostEqual(line.distance_to(Point(5.0, 8.0)), 8.0)
        self.assertTrue(line.center().equal(Point(5.0, 0.0)))
        self.assertEqual(line.to_dict()["start"], {"x": 0.0, "y": 0.0})

    def test_degenerate_line_projects_to_start(self) -> None:
        line = Line(Point(2.0, 2.0), Point(2.0, 2.0))
        self.assertTrue(line.project(Point(9.0, 9.0)).equal(Point(2.0, 2.0)))


class BoundsTests(unittest.TestCase):
    def test_bounds_of_and_membership(self) -> None:
        bounds = bounds_of([Point(1.0, 2.0), Point(5.0, 8.0), Point(-3.0, 4.0)])
        self.assertTrue(bounds.contains(Point(0.0, 3.0)))
        self.assertFalse(bounds.contains(Point(9.0, 3.0)))
        self.assertTrue(bounds.overlaps(Bounds(4.0, 3.0, 6.0, 5.0)))
        self.assertAlmostEqual(bounds.width(), 8.0)
        self.assertAlmostEqual(bounds.height(), 6.0)
        self.assertTrue(bounds.center().equal(Point(1.0, 5.0)))
        self.assertIn("min_x", bounds.to_dict())

    def test_empty_bounds(self) -> None:
        bounds = bounds_of([])
        self.assertEqual(bounds, Bounds(0.0, 0.0, 0.0, 0.0))


class PolygonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.square = Polygon.from_points(
            [Point(0.0, 0.0), Point(10.0, 0.0), Point(10.0, 10.0), Point(0.0, 10.0)]
        )

    def test_contains_and_area(self) -> None:
        self.assertTrue(self.square.contains(Point(5.0, 5.0)))
        self.assertFalse(self.square.contains(Point(15.0, 5.0)))
        self.assertAlmostEqual(self.square.area(), 100.0)
        self.assertTrue(self.square.centroid().equal(Point(5.0, 5.0)))

    def test_degenerate_polygon_is_empty(self) -> None:
        self.assertFalse(Polygon.from_points([Point(0.0, 0.0), Point(1.0, 1.0)]).contains(Point(0.5, 0.5)))
        self.assertEqual(Polygon.from_points([]).centroid(), Point(0.0, 0.0))

    def test_expand_grows_boundary(self) -> None:
        expanded = self.square.expand(2.0)
        self.assertGreater(expanded.area(), self.square.area())
        self.assertTrue(expanded.bounds().overlaps(self.square.bounds()))


class SeparationTests(unittest.TestCase):
    def test_compute_separation(self) -> None:
        separation = compute_separation(Point(0.0, 0.0), Point(5.0, 0.0), 10.0)
        self.assertTrue(separation.below_safe)
        self.assertAlmostEqual(separation.margin, -5.0)
        self.assertEqual(separation.safe_margin(), 0.0)
        self.assertIn("below_safe", separation.to_dict())
        self.assertTrue(is_below_safe(Point(0.0, 0.0), Point(5.0, 0.0), 10.0))

    def test_clear_separation_keeps_margin(self) -> None:
        separation = compute_separation(Point(0.0, 0.0), Point(30.0, 0.0), 12.0)
        self.assertFalse(separation.below_safe)
        self.assertAlmostEqual(separation.safe_margin(), 18.0)


if __name__ == "__main__":
    unittest.main()
