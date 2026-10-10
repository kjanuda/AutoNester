"""Offline Phase 7 regression tests; no customer files needed."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from layout_audit import audit_layout, replay_benchmark
from geometric_optimizer import optimize_geometry


def rect(name, width, height, **kwargs):
    return {'name': name, 'points': [[0, 0], [width, 0], [width, height], [0, height]], **kwargs}


class Phase7AuditTests(unittest.TestCase):
    def setUp(self):
        self.patterns = [rect('front', 32, 41, quantity=2), rect('back', 28, 39)]
        self.response = optimize_geometry(self.patterns, 100, 2, effort='quick', seed=42)
        self.layout = self.response['trials'][self.response['best_trial_id']]['layout']

    def test_safe_result_passes_geometry_audit(self):
        r = audit_layout(self.patterns, self.layout, 100, 2)
        self.assertTrue(r['passed'], r['issues'])
        self.assertEqual(r['instances_checked'], 3)
        self.assertFalse(r['production_approved'])
        self.assertEqual(len(r['layout_fingerprint']), 64)

    def test_overlap_is_rejected(self):
        tampered = copy.deepcopy(self.layout)
        tampered['placed_patterns'][1]['points'] = tampered['placed_patterns'][0]['points']
        r = audit_layout(self.patterns, tampered, 100, 2)
        self.assertFalse(r['passed'])
        self.assertTrue(any('Overlap' in i or 'gap' in i for i in r['issues']))

    def test_missing_and_modified_geometry_rejected(self):
        tampered = copy.deepcopy(self.layout)
        tampered['placed_patterns'].pop()
        self.assertFalse(audit_layout(self.patterns, tampered, 100, 2)['passed'])
        tampered = copy.deepcopy(self.layout)
        tampered['placed_patterns'][0]['points'][1][0] += 5
        self.assertFalse(audit_layout(self.patterns, tampered, 100, 2)['passed'])

    def test_disallowed_rotation_rejected(self):
        tampered = copy.deepcopy(self.layout)
        tampered['placed_patterns'][0]['rotation'] = 90
        r = audit_layout(self.patterns, tampered, 100, 2)
        self.assertTrue(any('unapproved rotation' in i for i in r['issues']))

    def test_boundary_escape_rejected(self):
        tampered = copy.deepcopy(self.layout)
        p = tampered['placed_patterns'][0]
        p['points'] = [[x+100, y] for x, y in p['points']]
        self.assertTrue(any('bounds' in i for i in audit_layout(self.patterns, tampered, 100, 2)['issues']))

    def test_inaccurate_length_rejected(self):
        tampered = copy.deepcopy(self.layout)
        tampered['fabric_length'] += 12
        self.assertTrue(any('Fabric length metric' in i for i in audit_layout(self.patterns, tampered, 100, 2)['issues']))

    def test_forged_utilization_rejected(self):
        tampered = copy.deepcopy(self.layout)
        tampered['utilization_percent'] = 99.99
        self.assertTrue(any('Utilization metric' in i for i in
                            audit_layout(self.patterns, tampered, 100, 2)['issues']))

    def test_replay_reproducibility_and_alternate_seed(self):
        r = replay_benchmark(self.patterns, 100, 2, effort='quick', seed=42)
        self.assertTrue(r['same_seed_reproducible'])
        self.assertTrue(r['all_geometry_audits_passed'], r['runs'])
        self.assertEqual(len(r['runs']), 3)
        self.assertEqual([x['seed'] for x in r['runs']], [42, 42, 43])
        self.assertFalse(r['production_approved'])
        self.assertIsNotNone(r['best_layout'])

    def test_seed_validation(self):
        for seed in (-1, True, 999999):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                replay_benchmark([rect('a', 5, 7)], 30, seed=seed)

    def test_concave_rotation_and_positive_gap(self):
        patterns = [
            {'name': 'L', 'points': [[0,0],[60,0],[60,20],[20,20],[20,60],[0,60]]},
            rect('strip', 24, 38, allowed_rotations=[0,90]),
        ]
        r = replay_benchmark(patterns, 100, 3, effort='quick')
        self.assertTrue(r['all_geometry_audits_passed'], r['runs'])


if __name__ == '__main__':
    unittest.main()
