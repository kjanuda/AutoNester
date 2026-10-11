import json
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from main import app
from material_aware import inspect_materials, build_material_previews

SAMPLES = Path(__file__).resolve().parents[2] / 'samples'

class Phase15Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = (SAMPLES / 'synthetic_11_piece_demo.dxf').read_bytes()
        cls.client = TestClient(app)
        cls.review = inspect_materials(cls.raw,'synthetic_11_piece_demo.dxf','mm','mm')
        cls.patterns = cls.review['patterns']

    def _payload(self, selected=None, assignments=None, **kw):
        selected = selected if selected is not None else [{'name':p['name'],'quantity':1} for p in self.patterns[:3]]
        origin = sorted({p['source_material'] for p in self.patterns if p['name'] in {v['name'] for v in selected}})
        assignments = assignments if assignments is not None else [{'source_label':l,'group_id':'TEST FABRIC','operator_confirmed':True} for l in origin]
        return build_material_previews(self.raw, 'synthetic_11_piece_demo.dxf','mm','mm', selected,assignments,1500,2,**kw)

    def test_review_is_explicitly_unverified(self):
        self.assertEqual(self.review['pattern_count'],11)
        self.assertTrue(self.review['manual_unit_override'])
        self.assertFalse(self.review['factory_cutting_approved'])
        self.assertTrue(self.review['source_material_labels'])

    def test_layout_audit_and_quantity(self):
        selected = [{'name':p['name'],'quantity':2 if i==0 else 1} for i,p in enumerate(self.patterns[:3])]
        result=self._payload(selected)
        self.assertEqual(result['selected_instances'],4)
        self.assertEqual(sum(g['instances'] for g in result['groups']),4)
        self.assertTrue(all(g['geometry_audit']['passed'] for g in result['groups']))
        self.assertFalse(result['production_approved'])

    def test_no_confirmation_no_output(self):
        selected=[{'name':self.patterns[0]['name'],'quantity':1}]
        label=self.patterns[0]['source_material']
        with self.assertRaisesRegex(ValueError,'Confirm source material'):
            self._payload(selected,[{'source_label':label,'group_id':'A','operator_confirmed':False}])

    def test_rejects_client_fabrication_and_excess_quantity(self):
        with self.assertRaisesRegex(ValueError,'Unknown or duplicated'):
            self._payload([{'name':'FAKE','quantity':1}])
        with self.assertRaisesRegex(ValueError,'integer'):
            self._payload([{'name':self.patterns[0]['name'],'quantity':1.5}])
        with self.assertRaisesRegex(ValueError,'integer'):
            self._payload([{'name':self.patterns[0]['name'],'quantity':True}])

    def test_independent_cad_reference_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError,'dimension does not match'):
            self._payload(reference_pattern_index=1,reference_mm=0.00001)

    def test_endpoints_and_bad_request(self):
        r=self.client.post('/api/cad/material-review', files={'file':('test.dxf',self.raw,'application/dxf')}, data={'source_units_override':'mm'})
        self.assertEqual(r.status_code,200,r.text)
        selected=[{'name':self.patterns[0]['name'],'quantity':2}]
        label=self.patterns[0]['source_material']
        data={'source_units_override':'mm','fabric_width':'1500','gap':'2',
              'selected_patterns_json':json.dumps(selected),
              'assignments_json':json.dumps([{'source_label':label,'group_id':'Fabric 1','operator_confirmed':True}])}
        r=self.client.post('/api/nesting/material-preview',files={'file':('test.dxf',self.raw,'application/dxf')},data=data)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['selected_instances'],2)
        data['assignments_json']='[]'
        r=self.client.post('/api/nesting/material-preview',files={'file':('test.dxf',self.raw,'application/dxf')},data=data)
        self.assertEqual(r.status_code,400)

if __name__=='__main__': unittest.main()
