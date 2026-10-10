"""Phase 9 audited preview bundle, source checksum and round-trip regression tests."""
import hashlib
import io
import json
import unittest
from pathlib import Path
from zipfile import ZipFile
from copy import deepcopy

import ezdxf

from dxf_import import parse_dxf_patterns
from marker_export import build_preview_bundle, _verify_roundtrip
from nesting_engine import generate_nesting


class Phase9Tests(unittest.TestCase):
    def setUp(self):
        self.patterns = [
            {'name': 'FRONT', 'points': [[0,0],[40,0],[40,75],[0,75]],
             'quantity': 1, 'allowed_rotations':[0]},
            {'name': 'BACK', 'points': [[0,0],[25,0],[25,60],[0,60]],
             'quantity': 2, 'allowed_rotations':[0]},
        ]
        self.width = 150
        self.gap = 2
        self.layout = generate_nesting(self.patterns, self.width, self.gap, 'fast')

    def bundle(self, **kw):
        return build_preview_bundle(patterns=self.patterns, layout=self.layout,
                                    fabric_width=self.width, gap=self.gap, **kw)

    def test_zip_manifest_matches_dxf(self):
        raw, manifest = self.bundle(source_file_name='test.dxf',
                                     source_dxf_sha256='a'*64)
        with ZipFile(io.BytesIO(raw)) as z:
            self.assertEqual(len(z.namelist()), 4)
            dxf = z.read('PREVIEW_marker_NOT_FOR_CUTTING.dxf')
            data = json.loads(z.read('manifest.json'))
            report = json.loads(z.read('geometry_validation.json'))
            self.assertEqual(hashlib.sha256(dxf).hexdigest(), data['export_dxf_sha256'])
            self.assertFalse(data['production_approved'])
            self.assertFalse(data['cutting_allowed'])
            self.assertTrue(report['geometry_audit']['passed'])
            self.assertEqual(data['source_dxf_sha256_client_reported'], 'a'*64)
            self.assertEqual(data['audited_layout_fingerprint'], manifest['audited_layout_fingerprint'])
            exported_doc = ezdxf.read(io.StringIO(dxf.decode('utf-8')))
            self.assertEqual(int(exported_doc.header['$INSUNITS']), 4)
            self.assertEqual(len(list(exported_doc.modelspace().query('LWPOLYLINE'))), 3)
            self.assertEqual(manifest['roundtrip']['max_coordinate_error_mm'], 0)

    def test_reject_moved_piece_and_bad_metrics(self):
        altered = deepcopy(self.layout)
        altered['placed_patterns'][0]['points'][0][0] += 12
        with self.assertRaisesRegex(ValueError, 'audit failed'):
            build_preview_bundle(patterns=self.patterns, layout=altered,
                                 fabric_width=self.width, gap=self.gap)
        altered = deepcopy(self.layout)
        altered['utilization_percent'] = 101.0
        with self.assertRaisesRegex(ValueError, 'audit failed'):
            build_preview_bundle(patterns=self.patterns, layout=altered,
                                 fabric_width=self.width, gap=self.gap)

    def test_reject_missing_piece_and_wrong_gap(self):
        with self.assertRaisesRegex(ValueError, 'audit failed'):
            build_preview_bundle(patterns=self.patterns,
                                 layout={**self.layout, 'placed_patterns':self.layout['placed_patterns'][:-1]},
                                 fabric_width=self.width, gap=self.gap)
        with self.assertRaisesRegex(ValueError, 'audit failed'):
            build_preview_bundle(patterns=self.patterns, layout=self.layout,
                                 fabric_width=self.width, gap=99)

    def test_invalid_source_checksum_rejected(self):
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.bundle(source_dxf_sha256='not-a-sha')

    def test_repeatable_dxf_content(self):
        a, ma = self.bundle()
        b, mb = self.bundle()
        with ZipFile(io.BytesIO(a)) as za, ZipFile(io.BytesIO(b)) as zb:
            self.assertEqual(_verify_roundtrip(za.read('PREVIEW_marker_NOT_FOR_CUTTING.dxf'), self.layout),
                             _verify_roundtrip(zb.read('PREVIEW_marker_NOT_FOR_CUTTING.dxf'), self.layout))
        self.assertEqual(ma['audited_layout_fingerprint'], mb['audited_layout_fingerprint'])

    def test_dxf_import_attaches_actual_byte_checksum(self):
        dxf, _ = self.bundle()
        with ZipFile(io.BytesIO(dxf)) as z:
            source = z.read('PREVIEW_marker_NOT_FOR_CUTTING.dxf')
        parsed = parse_dxf_patterns(source, 'nesting.dxf', source_units_override='auto')
        self.assertEqual(parsed['source_dxf_sha256'], hashlib.sha256(source).hexdigest())
        self.assertEqual(parsed['pattern_count'], 3)

    def test_roundtrip_detects_removed_outline(self):
        raw, _ = self.bundle()
        with ZipFile(io.BytesIO(raw)) as z:
            dxf = z.read('PREVIEW_marker_NOT_FOR_CUTTING.dxf')
        doc = ezdxf.read(io.StringIO(dxf.decode('utf-8')))
        piece = list(doc.modelspace().query('LWPOLYLINE'))[0]
        doc.modelspace().delete_entity(piece)
        text = io.StringIO()
        doc.write(text)
        with self.assertRaisesRegex(ValueError, 'count mismatch'):
            _verify_roundtrip(text.getvalue().encode('utf-8'), self.layout)


if __name__ == '__main__':
    unittest.main()
