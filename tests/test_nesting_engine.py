"""Run from backend: python -m unittest discover -s tests -v"""
import unittest
from shapely.geometry import Polygon
from nesting_engine import generate_nesting


def validate_layout(response, min_gap):
    polygons = [Polygon(p["points"]) for p in response["placed_patterns"]]
    for polygon in polygons:
        assert polygon.is_valid and polygon.area > 0
        x0, y0, x1, y1 = polygon.bounds
        assert x0 >= -1e-4 and y0 >= -1e-4
        assert x1 <= response["fabric_width"] + 1e-4
        assert y1 <= response["fabric_length"] + 0.015
    for i, left in enumerate(polygons):
        for right in polygons[i + 1:]:
            if min_gap:
                assert left.distance(right) >= min_gap - 1e-5
            else:
                assert left.intersection(right).area < 1e-5


class NestingTests(unittest.TestCase):
    def test_original_sample_backward_compatible(self):
        patterns = [
            {"name": "Trouser Front", "points": [[0, 0], [40, 0], [40, 80], [0, 80]]},
            {"name": "Trouser Back", "points": [[0, 0], [45, 0], [45, 85], [0, 85]]},
            {"name": "Pocket", "points": [[0, 0], [20, 0], [20, 25], [0, 25]]},
        ]
        out = generate_nesting(patterns, 150, 2)
        validate_layout(out, 2)
        self.assertEqual(len(out["placed_patterns"]), 3)
        self.assertEqual(out["fabric_length"], 85)
        self.assertEqual(out["utilization_percent"], 59.02)

    def test_concave_piece_interlocking(self):
        patterns = [
            {"name": "L", "points": [[0, 0], [70, 0], [70, 20], [20, 20], [20, 70], [0, 70]]},
            {"name": "Inner piece", "points": [[0, 0], [40, 0], [40, 40], [0, 40]]},
        ]
        out = generate_nesting(patterns, 70, 5, strategy="quick")
        validate_layout(out, 5)
        self.assertEqual(out["fabric_length"], 70)

    def test_rotation_enabled(self):
        out = generate_nesting([
            {"name": "Strip", "points": [[0, 0], [80, 0], [80, 30], [0, 30]],
             "allowed_rotations": [0, 90]}
        ], 50, 2)
        validate_layout(out, 2)
        self.assertEqual(out["placed_patterns"][0]["rotation"], 90)

    def test_disallowed_rotation_rejected(self):
        with self.assertRaisesRegex(ValueError, "no allowed rotation fits"):
            generate_nesting([{"name": "Strip", "points": [[0, 0], [80, 0], [80, 30], [0, 30]]}], 50)

    def test_quantity(self):
        out = generate_nesting([{"name": "Pocket", "points": [[0, 0], [15, 0], [15, 20], [0, 20]], "quantity": 3}], 50, 2)
        validate_layout(out, 2)
        self.assertEqual({p["instance"] for p in out["placed_patterns"]}, {1, 2, 3})

    def test_invalid_polygon_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid or self-intersecting"):
            generate_nesting([{"name": "Bowtie", "points": [[0, 0], [20, 20], [0, 20], [20, 0]]}], 50)

    def test_invalid_gap_rejected(self):
        with self.assertRaisesRegex(ValueError, "gap"):
            generate_nesting([{"name": "Square", "points": [[0, 0], [10, 0], [10, 10], [0, 10]]}], 50, -1)

    def test_zero_gap_no_area_overlap(self):
        out = generate_nesting([{"name": "Square", "points": [[0, 0], [10, 0], [10, 10], [0, 10]], "quantity": 3}], 20, 0)
        validate_layout(out, 0)


if __name__ == "__main__":
    unittest.main()
