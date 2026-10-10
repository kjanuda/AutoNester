"""Run: python -m unittest discover -s tests -v"""
import io
import unittest
import ezdxf
from fastapi.testclient import TestClient
from dxf_import import parse_dxf_patterns, DxfImportError
from main import app
from nesting_engine import generate_nesting


def make_dxf(*, units=4, open_line=False, bulge=False, nested=False):
    d = ezdxf.new('R2013')
    d.header['$INSUNITS'] = units
    m = d.modelspace()
    d.layers.new('FRONT')
    if bulge:
        m.add_lwpolyline([(0,0,0),(20,0,0.5),(20,30,0),(0,30,0)], format='xyb', close=True, dxfattribs={'layer':'FRONT'})
    else:
        m.add_lwpolyline([(0,0),(40,0),(40,60),(0,60)], close=True, dxfattribs={'layer':'FRONT'})
    if open_line:
        m.add_lwpolyline([(0,0),(30,30)], close=False)
    if nested:
        m.add_lwpolyline([(10,10),(20,10),(20,20),(10,20)], close=True)
    output = io.StringIO()
    d.write(output)
    return output.getvalue().encode('utf-8')


class DxfTests(unittest.TestCase):
    def test_closed_contour_in_mm(self):
        r = parse_dxf_patterns(make_dxf(), 'sample.dxf')
        self.assertEqual(r['pattern_count'], 1)
        self.assertEqual(r['patterns'][0]['name'], 'FRONT #1')
        self.assertEqual(r['patterns'][0]['allowed_rotations'], [0])
        self.assertEqual(r['patterns'][0]['points'][1], [40.0, 0.0])

    def test_unitless_inch_conversion(self):
        raw = make_dxf(units=0)
        r = parse_dxf_patterns(raw, 'sample.dxf', unitless_units='in')
        self.assertEqual(r['patterns'][0]['points'][1], [1016.0, 0.0])

    def test_header_inches_overrides_unitless_choice(self):
        raw = make_dxf(units=1)
        r = parse_dxf_patterns(raw, 'sample.dxf', unitless_units='mm')
        self.assertEqual(r['patterns'][0]['points'][1], [1016.0, 0.0])

    def test_open_line_ignored_with_warning(self):
        r = parse_dxf_patterns(make_dxf(open_line=True), 'sample.dxf')
        self.assertIn('open_or_non2d_LWPOLYLINE', r['ignored_entities'])
        self.assertEqual(len(r['patterns']), 1)

    def test_bulge_flattens(self):
        r = parse_dxf_patterns(make_dxf(bulge=True), 'curve.dxf')
        self.assertGreater(len(r['patterns'][0]['points']), 4)

    def test_nested_contours_rejected(self):
        with self.assertRaisesRegex(DxfImportError, 'enclosed'):
            parse_dxf_patterns(make_dxf(nested=True), 'nested.dxf')

    def test_wrong_extension_and_corrupt_content(self):
        with self.assertRaises(DxfImportError):
            parse_dxf_patterns(make_dxf(), 'sample.pdf')
        with self.assertRaises(DxfImportError):
            parse_dxf_patterns(b'rubbish', 'sample.dxf')

    def test_imported_pattern_works_with_engine(self):
        r = parse_dxf_patterns(make_dxf(), 'sample.dxf')
        layout = generate_nesting(r['patterns'], fabric_width=80, gap=2)
        self.assertEqual(len(layout['placed_patterns']), 1)
        self.assertEqual(layout['fabric_length'], 60)

    def test_api_upload_and_nesting(self):
        client = TestClient(app)
        response = client.post('/api/patterns/import-dxf', files={'file':('sample.dxf', make_dxf(), 'application/dxf')}, data={'unitless_units':'mm'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['pattern_count'], 1)
        payload = {'fabric_width':80, 'gap':2, 'patterns':response.json()['patterns']}
        nesting = client.post('/api/nesting/generate', json=payload)
        self.assertEqual(nesting.status_code, 200, nesting.text)

    def test_api_rejects_invalid_upload(self):
        client = TestClient(app)
        r = client.post('/api/patterns/import-dxf', files={'file':('bad.dxf', b'hello', 'application/dxf')})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
