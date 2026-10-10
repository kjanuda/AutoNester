"""Phase 13 bounded hybrid NFP regression and API safety tests."""
import unittest
from shapely.affinity import translate
from shapely.geometry import Point, Polygon
from fastapi.testclient import TestClient
from main import app
from phase13_nfp import _nfp, optimize_nfp_hybrid


SAMPLE = [
    {'name':'L','points':[[0,0],[80,0],[80,22],[19,22],[19,75],[0,75]],'quantity':1,'allowed_rotations':[0]},
    {'name':'B','points':[[0,0],[54,0],[54,23],[0,23]],'quantity':2,'allowed_rotations':[0]},
]

class Phase13Tests(unittest.TestCase):
    def test_convex_nfp_minkowski_bounds(self):
        a = Polygon([(10,20),(14,20),(14,23),(10,23)])
        b = Polygon([(0,0),(2,0),(2,1),(0,1)])
        region = _nfp(a,b)
        self.assertIsNotNone(region)
        self.assertEqual(tuple(round(x, 6) for x in region.bounds), (8.,19.,14.,23.))

    def test_concave_nfp_collision_membership(self):
        a=Polygon([(0,0),(5,0),(5,1),(1,1),(1,5),(0,5)])
        b=Polygon([(0,0),(2,0),(2,2),(0,2)])
        region=_nfp(a,b)
        self.assertIsNotNone(region)
        for x,y in [(1,1),(3,3),(8,8),(-5,-5),(3,0),(0,3)]:
            intersects=a.intersection(translate(b, xoff=x, yoff=y)).area>1e-8
            self.assertEqual(intersects, region.contains(Point(x,y)), (x,y))

    def test_non_worsening_and_audit(self):
        report=optimize_nfp_hybrid(SAMPLE,150,2,effort='quick',seed=42)
        self.assertTrue(report['baseline_audit']['passed'])
        self.assertTrue(report['best_audit']['passed'])
        self.assertLessEqual(report['best_length_mm'],report['baseline_length_mm'])
        self.assertEqual(report['pattern_instances'],3)
        self.assertFalse(report['production_ready'])
        self.assertGreater(report['search_stats']['candidate_checks'],0)

    def test_replay_is_reproducible(self):
        a=optimize_nfp_hybrid(SAMPLE,150,2,effort='quick',seed=5)
        b=optimize_nfp_hybrid(SAMPLE,150,2,effort='quick',seed=5)
        self.assertEqual(a['best_audit']['layout_fingerprint'],b['best_audit']['layout_fingerprint'])

    def test_constraints_and_bad_requests(self):
        with self.assertRaises(ValueError):
            optimize_nfp_hybrid(SAMPLE, 150, 2, effort='expert')
        with self.assertRaises(ValueError):
            optimize_nfp_hybrid(SAMPLE, 150, 2, seed=-1)
        with self.assertRaises(ValueError):
            optimize_nfp_hybrid(SAMPLE, 150, 2, enforce_constraints=True)

    def test_api_success_and_error(self):
        client=TestClient(app)
        payload={'patterns':SAMPLE,'fabric_width':150,'gap':2,'effort':'quick','seed':42}
        result=client.post('/api/nesting/optimize-nfp', json=payload)
        self.assertEqual(result.status_code,200, result.text)
        self.assertIn('best_layout',result.json())
        payload['effort']='invalid'
        self.assertEqual(client.post('/api/nesting/optimize-nfp',json=payload).status_code,400)

if __name__=='__main__': unittest.main()
