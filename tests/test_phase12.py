"""Source CAD + nested instance transformation checks; synthetic fixtures only."""
import copy
import io
import json
import unittest
from zipfile import ZipFile

import ezdxf
from fastapi.testclient import TestClient

from cad_aware_export import build_cad_aware_nested_preview
from dxf_import import parse_dxf_patterns
from nesting_engine import generate_nesting
from main import app
from test_phase11 import cad_example


class Phase12Tests(unittest.TestCase):
    def setUp(self):
        self.raw = cad_example(with_attdef=True)
        self.p = parse_dxf_patterns(self.raw, 'cad.dxf', source_units_override='mm')['patterns'][0]
        self.selection = {'name':self.p['name'],'quantity':2,'allowed_rotations':[0]}
        self.layout = generate_nesting([{**self.p, 'quantity':2,'allowed_rotations':[0]}],
                                       fabric_width=120,gap=2,strategy='fast')

    def build(self, layout=None, selection=None, raw=None, **kw):
        return build_cad_aware_nested_preview(
            raw=self.raw if raw is None else raw, filename='cad.dxf',
            selected_patterns=[self.selection] if selection is None else selection,
            layout=self.layout if layout is None else layout,
            fabric_width=120, gap=2, source_units_override='mm', **kw)

    def test_two_instances_mapping_counts_and_zip(self):
        blob,man=self.build()
        self.assertEqual(man['included_piece_instances'],2)
        self.assertEqual(man['source_entity_mappings'],18)
        self.assertEqual(man['exported_entities'],16)
        self.assertEqual(man['skipped_unsupported_instances'],2)
        self.assertTrue(man['roundtrip']['passed'])
        self.assertFalse(man['production_approved'])
        with ZipFile(io.BytesIO(blob)) as archive:
            names=archive.namelist()
            self.assertIn('CAD_AWARE_NESTED_PREVIEW_NOT_FOR_CUTTING.dxf',names)
            mapping=json.loads(archive.read('source_to_nested_entity_mapping.json'))
            self.assertEqual(len({(x['source_id'],x['instance']) for x in mapping}),18)
            self.assertEqual({x['instance'] for x in mapping},{1,2})
            self.assertIn(b'NOT FOR CUTTING',archive.read('READ_ME_FIRST.txt'))
            doc=ezdxf.read(io.StringIO(archive.read('CAD_AWARE_NESTED_PREVIEW_NOT_FOR_CUTTING.dxf').decode('utf8')))
            self.assertEqual(len(list(doc.modelspace().query('LWPOLYLINE'))),4)

    def test_rotation_transforms_internal_point_and_line(self):
        selection={'name':self.p['name'],'quantity':1,'allowed_rotations':[90]}
        layout=generate_nesting([{**self.p,'quantity':1,'allowed_rotations':[90]}],
                                fabric_width=60,gap=2,strategy='fast')
        self.assertEqual(layout['placed_patterns'][0]['rotation'],90)
        blob,man=build_cad_aware_nested_preview(raw=self.raw,filename='cad.dxf',
                   selected_patterns=[selection],layout=layout,fabric_width=60,gap=2,source_units_override='mm')
        with ZipFile(io.BytesIO(blob)) as z:
            mapping=json.loads(z.read('source_to_nested_entity_mapping.json'))
            doc=ezdxf.read(io.StringIO(z.read('CAD_AWARE_NESTED_PREVIEW_NOT_FOR_CUTTING.dxf').decode()))
        point=next(m for m in mapping if m['source_type']=='POINT')
        got=doc.entitydb[point['output_handle']].dxf.location
        # Original point (10,30) rotated 90° around (0,0) => (-30,10)
        # normalized rotated bbox has min (-45,0) => (15,10) placed at origin.
        self.assertAlmostEqual(got.x,15,5)
        self.assertAlmostEqual(got.y,10,5)
        self.assertTrue(man['roundtrip']['passed'])

    def test_wrong_position_and_wrong_metrics_rejected(self):
        bad=copy.deepcopy(self.layout)
        bad['placed_patterns'][0]['points'][0][0]+=5
        with self.assertRaisesRegex(ValueError,'audit failed'):
            self.build(layout=bad)
        bad=copy.deepcopy(self.layout)
        bad['utilization_percent']=99
        with self.assertRaisesRegex(ValueError,'audit failed'):
            self.build(layout=bad)

    def test_missing_piece_and_wrong_source_rejected(self):
        bad=copy.deepcopy(self.layout)
        bad['placed_patterns'].pop()
        with self.assertRaisesRegex(ValueError,'audit failed'):
            self.build(layout=bad)
        with self.assertRaises((ValueError,Exception)):
            self.build(raw=b'bad-dxf')

    def test_invalid_units_and_unverified_constraint_rejected(self):
        with self.assertRaises(ValueError):
            self.build(enforce_constraints=True)
        with self.assertRaises(Exception):
            build_cad_aware_nested_preview(raw=self.raw,filename='cad.dxf',
                selected_patterns=[self.selection],layout=self.layout,
                fabric_width=120,gap=2,source_units_override='auto')

    def test_http_200_and_400(self):
        client=TestClient(app)
        def post(layout):
            return client.post('/api/cad/export-nested-metadata-preview',files={
                'file':('cad.dxf',self.raw,'application/dxf')},data={
                'source_units_override':'mm',
                'selected_patterns_json':json.dumps([self.selection]),
                'layout_json':json.dumps(layout),'fabric_width':'120','gap':'2'})
        ok=post(self.layout)
        self.assertEqual(ok.status_code,200,ok.text)
        self.assertEqual(ok.headers['content-type'],'application/zip')
        changed=copy.deepcopy(self.layout)
        changed['placed_patterns'][0]['rotation']=90
        self.assertEqual(post(changed).status_code,400)


if __name__=='__main__':unittest.main()
