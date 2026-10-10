"""Phase 8 automated regression tests using bundled synthetic DXF fixtures."""
from __future__ import annotations

import unittest
from pathlib import Path
from math import isfinite
from copy import deepcopy

from cad_preflight import inspect_import
from dxf_import import parse_dxf_patterns
from phase8_compactor import refine_geometry
from layout_audit import audit_layout
from main import GeometricOptimizeRequest, refine_nesting, import_dxf, app

SAMPLES = Path(__file__).resolve().parents[2] / 'samples'


class Phase8Tests(unittest.TestCase):
    def load(self, filename='synthetic_tee_demo.dxf'):
        f=SAMPLES / filename
        return parse_dxf_patterns(f.read_bytes(), f.name, source_units_override='mm')

    def test_preflight_never_approves_cutting(self):
        preflight=inspect_import(self.load())
        self.assertEqual(preflight['status'], 'review_required')
        self.assertFalse(preflight['cutting_approved'])
        self.assertEqual(len(preflight['patterns']),4)
        self.assertTrue(all(p['width_mm']>0 and p['height_mm']>0 for p in preflight['patterns']))
        self.assertIn('UNITS_OVERRIDE',[e['code'] for e in preflight['issues']])
        self.assertIn('FOLD_UNVERIFIED',[e['code'] for e in preflight['issues']])
        self.assertIn('CUTTING_METADATA_UNVERIFIED',[e['code'] for e in preflight['issues']])

    def test_dxf_preflight_does_not_modify_original_import(self):
        imported=self.load()
        snapshot=deepcopy(imported)
        preflight=inspect_import(imported)
        self.assertEqual(imported,snapshot)
        self.assertEqual(preflight['pattern_count'],imported['pattern_count'])
        self.assertGreater(preflight['candidate_area_mm2'],0)

    def test_refinement_layout_is_audited_and_never_worsens(self):
        patterns=self.load()['patterns']
        result=refine_geometry(patterns,1500,2,effort='quick')
        self.assertFalse(result['production_ready'])
        self.assertLessEqual(result['best_length_mm'],result['baseline_length_mm']+.0001)
        self.assertTrue(result['baseline_audit']['passed'])
        self.assertTrue(result['best_audit']['passed'])
        audit=audit_layout(patterns,result['best_layout'],1500,2)
        self.assertTrue(audit['passed'],audit['issues'])
        self.assertEqual(audit['instances_checked'],4)

    def test_local_results_reproducible(self):
        pts=self.load()['patterns'][:3]
        a=refine_geometry(pts,1500,2,effort='quick',seed=43)
        b=refine_geometry(pts,1500,2,effort='quick',seed=43)
        self.assertEqual(a['best_audit']['layout_fingerprint'],b['best_audit']['layout_fingerprint'])

    def test_large_batch_fallback_is_explicit_and_audited(self):
        pts=self.load('synthetic_11_piece_demo.dxf')['patterns']
        for p in pts[:8]:p['quantity']=2
        result=refine_geometry(pts,1500,2,effort='quick')
        self.assertEqual(result['pieces'],19)
        self.assertTrue(result['compaction_skipped'])
        self.assertTrue(result['best_audit']['passed'])
        self.assertAlmostEqual(result['best_length_mm'],result['baseline_length_mm'])

    def test_verified_fold_restrictions_never_relaxed(self):
        pts=self.load()['patterns'][:2]
        for p in pts:
            p.update(fold_verified=True,fold_edge='left',grainline_verified=True,fold_status_verified=True)
        result=refine_geometry(pts,1500,2,enforce_constraints=True,
                               fabric_mode='folded',fabric_fold_side='left',effort='quick')
        self.assertTrue(result['best_audit']['passed'])
        for item in result['best_layout']['placed_patterns']:
            self.assertAlmostEqual(min(x for x,y in item['points']),0,delta=.005)

    def test_no_false_verified_flag(self):
        pts=self.load()['patterns'][:2]
        with self.assertRaisesRegex(ValueError,'verify Fold'):
            refine_geometry(pts,1500,2,enforce_constraints=True,
                            fabric_mode='folded',fabric_fold_side='left')

    def test_reject_invalid_seed_and_effort(self):
        pts=self.load()['patterns'][:1]
        for bad in (True,-1,1000000,'42'):
            with self.assertRaises(ValueError):refine_geometry(pts,1500,2,seed=bad)
        with self.assertRaises(ValueError):refine_geometry(pts,1500,2,effort='infinite')

    def test_new_api_route_present(self):
        self.assertIn('/api/nesting/refine',{route.path for route in app.routes})
        self.assertEqual(GeometricOptimizeRequest.model_fields['effort'].default,'standard')


if __name__ == '__main__':
    unittest.main()
