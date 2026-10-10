"""Phase 14 diagnostic TUKAcad-first source exchange review."""
import unittest
from io import StringIO
from pathlib import Path

import ezdxf
from fastapi.testclient import TestClient
from main import app
from tuka_compatibility import inspect_tuka_exchange


def make_dxf(size='M',quantity=2,units=4):
    doc=ezdxf.new('R2010')
    doc.header['$INSUNITS']=units
    block=doc.blocks.new('GARMENT_1')
    block.add_lwpolyline([(0,0),(90,0),(90,50),(0,50)],close=True,dxfattribs={'layer':'1'})
    block.add_text('Piece Name: Front',dxfattribs={'layer':'1','height':2,'insert':(5,5)})
    block.add_text(f'Size: {size}',dxfattribs={'layer':'1','height':2,'insert':(5,10)})
    block.add_text(f'Quantity: {quantity}',dxfattribs={'layer':'1','height':2,'insert':(5,15)})
    doc.modelspace().add_blockref('GARMENT_1',(0,0))
    fh=StringIO(); doc.write(fh)
    return fh.getvalue().encode('utf-8')

class TukaExchangeTests(unittest.TestCase):
    def test_inventory_and_quantities(self):
        data=inspect_tuka_exchange(raw=make_dxf(),filename='tuka_source.dxf')
        self.assertTrue(data['import_readable_for_preview'])
        self.assertEqual(data['candidate_pattern_count'],1)
        self.assertEqual(data['labelled_quantity_sum'],2)
        self.assertEqual(data['distinct_size_labels'],['M'])
        self.assertFalse(data['aama_astm_compliant'])
        self.assertFalse(data['tukamark_reimport_verified'])
        self.assertFalse(data['factory_cutting_approved'])

    def test_no_false_grading_or_vendor_claim(self):
        data=inspect_tuka_exchange(raw=make_dxf(),filename='simple.dxf')
        self.assertEqual(data['source_hints_unverified'],[])
        self.assertIn('GRADING_NOT_DEMONSTRATED',[i['code'] for i in data['issues']])
        self.assertFalse(data['native_tuka_file_supported'])

    def test_unit_override_warning(self):
        data=inspect_tuka_exchange(raw=make_dxf(units=6),filename='sample.dxf',source_units_override='mm')
        codes=[i['code'] for i in data['issues']]
        self.assertIn('METRE_HEADER_MM_OVERRIDE',codes)
        self.assertTrue(data['source_units']['manual_override'])
        self.assertFalse(data['source_units']['independently_verified'])

    def test_cannot_import_has_blocked_report(self):
        doc=ezdxf.new('R2010')
        doc.modelspace().add_line((0,0),(10,10))
        fh=StringIO();doc.write(fh)
        data=inspect_tuka_exchange(raw=fh.getvalue().encode(),filename='line_only.dxf')
        self.assertFalse(data['import_readable_for_preview'])
        self.assertEqual(data['candidate_pattern_count'],0)
        self.assertIn('DXF_IMPORT_BLOCKED',[i['code'] for i in data['issues']])

    def test_api_success_and_validation(self):
        client=TestClient(app)
        dxf=make_dxf()
        resp=client.post('/api/cad/tuka-compatibility-preflight',files={'file':('sample.dxf',dxf,'application/dxf')})
        self.assertEqual(resp.status_code,200,resp.text)
        self.assertEqual(resp.json()['candidate_pattern_count'],1)
        self.assertEqual(client.post('/api/cad/tuka-compatibility-preflight', files={'file':('x.tud',dxf,'application/octet-stream')}).status_code,400)
        self.assertEqual(client.post('/api/cad/tuka-compatibility-preflight', files={'file':('x.dxf',b'invalid','text/plain')}).status_code,400)

if __name__=='__main__':unittest.main()
