"""Standalone unittest suite: no external API server or browser needed."""
import sys
import unittest
from pathlib import Path
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nesting_engine import generate_nesting, MAX_PIECES
from dxf_import import parse_dxf_patterns

SAMPLES = Path(__file__).resolve().parents[2] / 'samples'

class Phase4Tests(unittest.TestCase):
    def read(self, name):
        p=SAMPLES / name
        return parse_dxf_patterns(p.read_bytes(), p.name, source_units_override='mm')

    def safe_layout(self, result, gap):
        shapes=[Polygon(x['points']) for x in result['placed_patterns']]
        for geom in shapes:
            self.assertTrue(geom.is_valid)
            self.assertGreaterEqual(geom.bounds[0], -1e-3)
            self.assertLessEqual(geom.bounds[2], result['fabric_width']+1e-3)
        for i,p in enumerate(shapes):
            for q in shapes[i+1:]:
                self.assertGreaterEqual(p.distance(q), gap-0.005)
                self.assertLessEqual(p.intersection(q).area, 0.0001)

    def test_import_synthetic_shirt_and_advisory_fold(self):
        d=self.read('synthetic_tee_demo.dxf')
        self.assertEqual(d['pattern_count'],4)
        self.assertEqual(sum(x['fold_required'] for x in d['patterns']),3)
        self.assertTrue(all(x['allowed_rotations']==[0] for x in d['patterns']))
        self.assertTrue(all(not x['grainline_verified'] for x in d['patterns']))
        self.assertTrue(all(x['source_size']=='S' for x in d['patterns']))

    def test_full_4_shirt_fast(self):
        p=self.read('synthetic_tee_demo.dxf')['patterns']
        r=generate_nesting(p,1500,2,strategy='fast')
        self.assertEqual(len(r['placed_patterns']),4)
        self.assertFalse(r['constraints_enforced'])
        self.safe_layout(r,2)

    def test_11_piece_synthetic_dxf(self):
        q=self.read('synthetic_11_piece_demo.dxf')
        self.assertEqual(q['pattern_count'],11)
        r=generate_nesting(q['patterns'],1500,2,strategy='balanced')
        self.assertEqual(len(r['placed_patterns']),11)
        self.safe_layout(r,2)

    def test_quantities_create_multiple_instances(self):
        p=self.read('synthetic_tee_demo.dxf')['patterns']
        p[0]['quantity']=3
        p[1]['quantity']=2
        p[2]['quantity']=2
        r=generate_nesting(p,1500,2,strategy='fast')
        self.assertEqual(len(r['placed_patterns']),8)
        self.assertEqual(len([x for x in r['placed_patterns'] if x['name']==p[0]['name']]),3)
        self.safe_layout(r,2)

    def test_reject_over_60_instances(self):
        p=self.read('synthetic_tee_demo.dxf')['patterns']
        p[0]['quantity']=MAX_PIECES
        with self.assertRaisesRegex(ValueError,'maximum'):
            generate_nesting(p,1500,2,strategy='fast')

    def test_preview_is_not_production_ready(self):
        p=self.read('synthetic_tee_demo.dxf')['patterns']
        r=generate_nesting(p,1500,2,strategy='fast')
        self.assertFalse(r['production_ready'])
        self.assertTrue(any('NOT enforced' in w for w in r['warnings']))

    def test_strict_requires_verified_grainline(self):
        p=self.read('synthetic_tee_demo.dxf')['patterns'][:2]
        for x in p: x['fold_status_verified']=True
        with self.assertRaisesRegex(ValueError,'grainline'):
            generate_nesting(p,1500,2,strategy='fast',enforce_constraints=True)

    def test_strict_rejects_unverified_fold(self):
        p=self.read('synthetic_tee_demo.dxf')['patterns'][:2]
        for x in p: x['grainline_verified']=True; x['fold_status_verified']=True
        with self.assertRaisesRegex(ValueError,'fold required'):
            generate_nesting(p,1500,2,strategy='fast',enforce_constraints=True)

    def test_verified_fold_aligned_at_left(self):
        p=self.read('synthetic_tee_demo.dxf')['patterns'][:2]
        for x in p:
            x.update(fold_verified=True,fold_edge='left',grainline_verified=True,fold_status_verified=True)
        r=generate_nesting(p,1500,2,strategy='fast',enforce_constraints=True,
                           fabric_mode='folded',fabric_fold_side='left')
        self.assertTrue(r['constraints_enforced'])
        self.assertFalse(r['production_ready'])
        for piece in r['placed_patterns']:
            self.assertAlmostEqual(Polygon(piece['points']).bounds[0],0,delta=.005)
        self.safe_layout(r,2)

    def test_unverified_diagonal_edge_rejected(self):
        p=[dict(name='triangle_fold',points=[[0,0],[40,0],[20,20]],quantity=1,
                allowed_rotations=[0],fold_required=True,fold_verified=True,
                grainline_verified=True,fold_edge='left',fold_status_verified=True)]
        with self.assertRaisesRegex(ValueError,'continuous straight'):
            generate_nesting(p,70,2,'fast',enforce_constraints=True,fabric_mode='folded')

    def test_legacy_sample_remains(self):
        p=[dict(name='Front',points=[[0,0],[40,0],[40,80],[0,80]]),
           dict(name='Back',points=[[0,0],[45,0],[45,85],[0,85]]),
           dict(name='Pocket',points=[[0,0],[20,0],[20,25],[0,25]])]
        r=generate_nesting(p,150,2,'quick')
        self.assertEqual(r['fabric_length'],85)
        self.assertEqual(r['utilization_percent'],59.02)

if __name__=='__main__': unittest.main()
