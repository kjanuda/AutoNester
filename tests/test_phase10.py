"""Phase 10 checks: inventory, measured dimensions, and preview guardrails."""
from pathlib import Path
import unittest

from cad_source_audit import audit_cad_source
from dxf_import import DxfImportError

ASSET_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
# Standalone unit tests generate a small reproducible DXF, no user CAD required.
import ezdxf
from io import StringIO


def sample_dxf():
    doc = ezdxf.new('R2010')
    doc.header['$INSUNITS'] = 4
    doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
    doc.modelspace().add_line((0, 0), (5, 5), dxfattribs={'layer': 'SEWING'})
    s = StringIO()
    doc.write(s)
    return s.getvalue().encode('utf-8')


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.raw = sample_dxf()

    def call(self, **kwargs):
        return audit_cad_source(raw=self.raw, filename='example.dxf', **kwargs)

    def test_detect_source_inventory_and_non_transfer(self):
        data = self.call()
        self.assertEqual(data['header_insunits_code'], 4)
        self.assertEqual(data['imported_pattern_count'], 1)
        self.assertEqual(data['source_inventory']['modelspace_entity_counts']['LINE'], 1)
        self.assertIn('CAD_ENTITY_LOSS', [flag['code'] for flag in data['flags']])
        self.assertFalse(data['cutting_approved'])

    def test_reference_dimension_matches(self):
        data = self.call(reference_pattern_index=1, reference_axis='width', reference_mm=100.3)
        self.assertEqual(data['reference_check']['status'], 'matched')

    def test_reference_dimension_mismatch_is_visible(self):
        data = self.call(reference_pattern_index=1, reference_axis='height', reference_mm=63)
        self.assertEqual(data['reference_check']['status'], 'mismatch')
        self.assertIn('DIMENSION_MISMATCH', [flag['code'] for flag in data['flags']])

    def test_bad_reference_index_rejected(self):
        with self.assertRaises(DxfImportError):
            self.call(reference_pattern_index=5, reference_mm=100)

    def test_bad_reference_axis_rejected(self):
        with self.assertRaises(DxfImportError):
            self.call(reference_pattern_index=1, reference_axis='diagonal', reference_mm=100)

    def test_bad_tolerance_rejected(self):
        with self.assertRaises(DxfImportError):
            self.call(tolerance_mm=0)

    def test_override_is_explicitly_flagged(self):
        data = self.call(source_units_override='mm')
        self.assertIn('UNIT_OVERRIDE', [flag['code'] for flag in data['flags']])

    def test_external_measurement_claim_not_auto_approval(self):
        data = self.call(reference_pattern_index=1, reference_mm=100)
        self.assertEqual(data['reference_check']['status'], 'matched')
        self.assertFalse(data['cutting_approved'])

    def test_api_200_and_validation_400(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)
        valid = client.post('/api/cad/source-audit', data={'reference_pattern_index': '1', 'reference_mm': '100'},
                            files={'file': ('x.dxf', self.raw, 'application/dxf')})
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(valid.json()['reference_check']['status'], 'matched')
        invalid = client.post('/api/cad/source-audit', data={'reference_pattern_index': '10', 'reference_mm': '100'},
                              files={'file': ('x.dxf', self.raw, 'application/dxf')})
        self.assertEqual(invalid.status_code, 400)


if __name__ == '__main__':
    unittest.main()
