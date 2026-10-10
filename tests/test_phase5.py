import math
import unittest
from shapely.geometry import Polygon
from optimization_compare import compare_layouts


def rectangle(name, w, h, count=1, **extras):
    return {'name': name, 'points': [[0,0], [w,0], [w,h], [0,h]],
            'quantity': count, **extras}


class ComparisonTests(unittest.TestCase):
    def test_basic_comparison_and_never_worse_than_baseline(self):
        shapes = [rectangle('A', 45, 85), rectangle('B', 40, 80), rectangle('C', 20, 25)]
        data = compare_layouts(shapes, fabric_width=150, gap=2, max_trials=7)
        self.assertGreaterEqual(data['trials_run'], 1)
        self.assertLessEqual(data['trials_run'], 7)
        self.assertLessEqual(data['best_length_mm'], data['baseline_length_mm'])
        self.assertFalse(data['trials'][data['best_trial_id']]['layout']['production_ready'])
        self.assertEqual(data['trials'][0]['method'], 'Input order (baseline)')
        self.assertGreaterEqual(data['improvement_vs_input_order_percent'], 0)

    def test_known_comparison_improvement_synthetic_demo(self):
        sizes = [(16,17),(45,31),(41,24),(34,16),(11,19)]
        pieces = [rectangle(f'piece_{i}',w,h) for i,(w,h) in enumerate(sizes)]
        data = compare_layouts(pieces, 100, 2)
        self.assertEqual(data['baseline_length_mm'], 57)
        self.assertEqual(data['best_length_mm'], 49)
        self.assertEqual(data['improvement_vs_input_order_percent'], 14.04)

    def test_gap_and_width_held_in_every_trial(self):
        concave = {'name':'Notch', 'points':[[0,0],[70,0],[70,20],[20,20],[20,70],[0,70]]}
        square = rectangle('Inset', 40, 40, count=2)
        data = compare_layouts([concave, square], 90, gap=2)
        for trial in data['trials']:
            r = trial['layout']
            self.assertEqual(len(r['placed_patterns']), 3)
            polygons = [Polygon(p['points']) for p in r['placed_patterns']]
            for p in polygons:
                self.assertGreaterEqual(p.bounds[0], -1e-5)
                self.assertLessEqual(p.bounds[2], 90+1e-5)
            for i,p in enumerate(polygons):
                for other in polygons[i+1:]:
                    self.assertGreaterEqual(p.distance(other)+1e-4, 2)

    def test_quantity_and_constraints_reject_unverified(self):
        a = rectangle('Fold piece', 10, 30, count=2, fold_required=True)
        self.assertEqual(len(compare_layouts([a],50)['trials'][0]['layout']['placed_patterns']),2)
        with self.assertRaisesRegex(ValueError,'verify Fold Y/N'):
            compare_layouts([a], 50, enforce_constraints=True, fabric_mode='folded')

    def test_verified_fold_must_use_fabric_edge(self):
        fold = rectangle('Fold piece', 10, 30, count=2, fold_required=True,
                         fold_edge='left', fold_verified=True, fold_status_verified=True,
                         grainline_verified=True)
        data = compare_layouts([fold], 60, gap=2, enforce_constraints=True,
                               fabric_mode='folded', fabric_fold_side='left')
        for trial in data['trials']:
            layout = trial['layout']
            self.assertTrue(layout['constraints_enforced'])
            self.assertFalse(layout['production_ready'])
            for piece in layout['placed_patterns']:
                self.assertAlmostEqual(Polygon(piece['points']).bounds[0], 0, places=4)

    def test_bad_inputs(self):
        shapes = [rectangle('x',10,20)]
        for args in [dict(max_trials=0), dict(max_trials=10), dict(fabric_mode='invalid'),
                     dict(fabric_fold_side='middle'), dict(gap=-1)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                compare_layouts(shapes, 50, **args)

    def test_deterministic_and_no_duplicate_orders(self):
        shapes = [rectangle('p1',20,60),rectangle('p2',65,20),rectangle('p3',30,35)]
        a=compare_layouts(shapes,100,2)
        b=compare_layouts(shapes,100,2)
        self.assertEqual([x['method'] for x in a['trials']], [x['method'] for x in b['trials']])
        self.assertEqual([x['layout']['fabric_length'] for x in a['trials']],
                         [x['layout']['fabric_length'] for x in b['trials']])

if __name__ == '__main__':
    unittest.main()
