"""Phase 11 self-contained synthetic CAD tests; never depend on private DXF."""
from __future__ import annotations
import io
import json
import unittest
from zipfile import ZipFile

import ezdxf
from fastapi.testclient import TestClient

from cad_metadata import inspect_cad_metadata, build_metadata_preview_bundle
from dxf_import import DxfImportError
from main import app


def cad_example(*, with_attdef=True):
    doc = ezdxf.new('R2010')
    doc.header['$INSUNITS'] = 6 # deliberately dubious source declaration
    block = doc.blocks.new(name='P001')
    block.add_text('Piece Name: Test Pocket',dxfattribs={'layer':'1', 'height':3, 'insert':(8,8)})
    block.add_text('Size: 28',dxfattribs={'layer':'1', 'height':3, 'insert':(8,14)})
    block.add_text('Quantity: 2',dxfattribs={'layer':'1', 'height':3, 'insert':(8,20)})
    block.add_text('Fold: N',dxfattribs={'layer':'1', 'height':3, 'insert':(8,26)})
    block.add_lwpolyline([(0,0),(100,0),(100,45),(0,45)],close=True,dxfattribs={'layer':'1'})
    block.add_lwpolyline([(10,12),(90,12),(90,30),(10,30)],close=True,dxfattribs={'layer':'14'})
    block.add_line((3,4),(20,10),dxfattribs={'layer':'7'})
    block.add_point((10,30),dxfattribs={'layer':'2'})
    if with_attdef:
        block.add_attdef('EXAMPLE', insert=(8,20))
    doc.modelspace().add_blockref('P001',(0,0))
    f = io.StringIO(); doc.write(f)
    return f.getvalue().encode('utf8')


class Phase11Tests(unittest.TestCase):
    def setUp(self):
        self.raw=cad_example()

    def inspect(self, **kw):
        return inspect_cad_metadata(raw=self.raw, filename='sample.dxf',source_units_override='mm',**kw)

    def test_block_entities_and_handles_mapped(self):
        report=self.inspect()
        self.assertEqual(report['piece_count'],1)
        p=report['pieces'][0]
        self.assertEqual(p['source_quantity_label'],2)
        self.assertEqual(p['source_size_label'],'28')
        self.assertIn('INSERT:',p['candidate_cut_source_id'])
        self.assertTrue(all('HANDLE:' in e['source_id'] for e in p['entities']))
        self.assertEqual(len(set(e['source_id'] for e in p['entities'])),p['entity_count'])
        self.assertFalse(report['production_approved'])

    def test_secondary_polylines_unverified(self):
        p=self.inspect()['pieces'][0]
        self.assertEqual(sum(e['role']=='candidate_outline_unverified' for e in p['entities']),1)
        self.assertEqual(sum(e['role']=='secondary_contour_unverified' for e in p['entities']),1)
        self.assertEqual(sum(e['role']=='point_unverified' for e in p['entities']),1)
        self.assertEqual(sum(e['role']=='line_unverified' for e in p['entities']),1)

    def test_unsupported_attributes_are_reported(self):
        report=self.inspect()
        self.assertEqual(report['unsupported_entity_count'],1)
        self.assertIn('ATTDEF',report['entity_type_counts'])
        self.assertIn('Manual source-units override', ' '.join(report['warnings']))

    def test_diagnostic_zip_round_trip_and_complete_mapping(self):
        zip_bytes, report=build_metadata_preview_bundle(
            raw=self.raw,filename='sample.dxf',source_units_override='mm')
        self.assertEqual(report['exported_entities'],8)
        self.assertEqual(report['skipped_entities'],1)
        self.assertTrue(report['round_trip_geometry_validated'])
        self.assertEqual(report['max_round_trip_coordinate_error_mm'],0)
        self.assertEqual(len(report['mapping']),9)
        self.assertFalse(report['production_approved'])
        with ZipFile(io.BytesIO(zip_bytes)) as z:
            self.assertIn('METADATA_INSPECTION_NOT_FOR_CUTTING.dxf', z.namelist())
            self.assertEqual(json.loads(z.read('source_to_preview_mapping.json'))['exported_entities'],8)
            self.assertIn(b'NOT FOR CUTTING', z.read('READ_ME_FIRST.txt'))

    def test_units_auto_not_silently_overridden(self):
        with self.assertRaises(DxfImportError):
            inspect_cad_metadata(raw=self.raw,filename='sample.dxf',source_units_override='auto')

    def test_invalid_input_rejected(self):
        with self.assertRaises(DxfImportError):
            inspect_cad_metadata(raw=b'invalid',filename='bad.dxf')
        with self.assertRaises(DxfImportError):
            inspect_cad_metadata(raw=self.raw,filename='x.dwg')

    def test_api_inspect_preview_and_http400(self):
        client=TestClient(app)
        files={'file':('sample.dxf',self.raw,'application/dxf')}
        ok=client.post('/api/cad/metadata-inspect',data={'source_units_override':'mm'},files=files)
        self.assertEqual(ok.status_code,200,ok.text)
        self.assertEqual(ok.json()['entity_count'],9)
        export=client.post('/api/cad/metadata-preview-bundle',data={'source_units_override':'mm'},files=files)
        self.assertEqual(export.status_code,200,export.text)
        self.assertEqual(export.headers['content-type'],'application/zip')
        fail=client.post('/api/cad/metadata-inspect',data={'source_units_override':'auto'},files=files)
        self.assertEqual(fail.status_code,400)

if __name__=='__main__': unittest.main()
