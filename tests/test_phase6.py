"""Offline Phase 6 regression suite; no user CAD files or web services required."""
import sys
import unittest
from pathlib import Path

from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geometric_optimizer import optimize_geometry


def rect(name, w, h, **kwargs):
    return {'name': name, 'points': [[0, 0], [w, 0], [w, h], [0, h]], **kwargs}


class Phase6Tests(unittest.TestCase):
    @staticmethod
    def fixture():
        return [rect(f'P{i}', w, h) for i,(w,h) in enumerate(
            [(16,17),(45,31),(41,24),(34,16),(11,19)])]

    def assert_safe_layout(self, result, width, gap, count):
        self.assertFalse(result['production_ready'])
        items=result['placed_patterns']
        self.assertEqual(len(items), count)
        polys=[Polygon(x['points']) for x in items]
        for p in polys:
            self.assertTrue(p.is_valid)
            self.assertGreaterEqual(p.bounds[0],-0.001)
            self.assertGreaterEqual(p.bounds[1],-0.001)
            self.assertLessEqual(p.bounds[2],width+0.001)
            self.assertLessEqual(p.intersection(p).area,p.area+0.0001)
        for i,poly in enumerate(polys):
            for other in polys[i+1:]:
                self.assertLessEqual(poly.intersection(other).area,0.0001)
                self.assertGreaterEqual(poly.distance(other)+0.003,gap)

    def test_comparison_shape_and_best_never_worse_than_input(self):
        r=optimize_geometry(self.fixture(),100,2,effort='quick')
        self.assertEqual(r['version'],'0.6.0')
        self.assertEqual(r['trials'][0]['method'],'Input order (baseline)')
        self.assertGreaterEqual(r['attempts_run'],1)
        self.assertLessEqual(r['best_length_mm'],r['baseline_length_mm'])
        self.assertEqual(r['trials'][r['best_trial_id']]['layout']['fabric_length'],r['best_length_mm'])
        for trial in r['trials']:
            self.assert_safe_layout(trial['layout'],100,2,5)

    def test_concave_geom_gap_and_quantity(self):
        patterns=[{'name':'L','points':[[0,0],[70,0],[70,20],[20,20],[20,70],[0,70]]},
                  rect('Inner',20,20,quantity=2)]
        r=optimize_geometry(patterns,85,3,effort='standard')
        for trial in r['trials']:
            self.assert_safe_layout(trial['layout'],85,3,3)

    def test_rotation_not_invented(self):
        pieces=[rect('strip',80,30,allowed_rotations=[90]),
                rect('square',18,18,allowed_rotations=[0])]
        result=optimize_geometry(pieces,50,2,effort='quick')
        for trial in result['trials']:
            for item in trial['layout']['placed_patterns']:
                self.assertEqual(item['rotation'],90 if item['name']=='strip' else 0)

    def test_strict_constraints_reject_unverified(self):
        with self.assertRaisesRegex(ValueError,'verify Fold Y/N'):
            optimize_geometry([rect('fold',20,30,fold_required=True)],60,2,
                              enforce_constraints=True,fabric_mode='folded')

    def test_strict_verified_fold_stays_at_selected_edge(self):
        r=optimize_geometry([rect('fold',12,25,quantity=2,fold_required=True,
                                  fold_edge='right',fold_verified=True,
                                  fold_status_verified=True,grainline_verified=True)],
                            60,2,True,'folded','right','quick')
        for trial in r['trials']:
            self.assert_safe_layout(trial['layout'],60,2,2)
            for p in trial['layout']['placed_patterns']:
                self.assertAlmostEqual(Polygon(p['points']).bounds[2],60,places=3)
                self.assertTrue(trial['layout']['constraints_enforced'])

    def test_deterministic_fixed_seed(self):
        a=optimize_geometry(self.fixture(),100,2,effort='quick',seed=4)
        b=optimize_geometry(self.fixture(),100,2,effort='quick',seed=4)
        self.assertEqual(a['best_length_mm'],b['best_length_mm'])
        self.assertEqual([(x['method'],x['layout']['placed_patterns']) for x in a['trials']],
                         [(x['method'],x['layout']['placed_patterns']) for x in b['trials']])

    def test_invalid_mode_and_seed(self):
        for kwargs in [{'effort':'forever'},{'seed':-1},{'seed':True},
                       {'fabric_mode':'bad'},{'fabric_fold_side':'middle'},{'gap':-1}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                optimize_geometry([rect('x',10,10)],50,**kwargs)

    def test_integrates_with_realistic_synthetic_dxf(self):
        # Phase 4 supplies this synthetic fixture; never package customer's CAD.
        from dxf_import import parse_dxf_patterns
        sample=Path(__file__).resolve().parents[2]/'samples'/'synthetic_tee_demo.dxf'
        p=parse_dxf_patterns(sample.read_bytes(),sample.name,source_units_override='mm')['patterns']
        r=optimize_geometry(p,1500,2,effort='standard')
        self.assertLessEqual(r['best_length_mm'],r['baseline_length_mm'])
        for trial in r['trials']:
            self.assert_safe_layout(trial['layout'],1500,2,4)


if __name__ == '__main__':
    unittest.main()
