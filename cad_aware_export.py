"""Phase 12: server-side source-aware *diagnostic* nesting DXF export.

This is NOT a cutter file. The server reimports original DXF bytes, verifies
client layout against source-derived polygon data, and translates/rotates
inspectable CAD markers with each placement. Geometry may be flattened.
Source layer semantics, true fabric units, material/fold/notch/grainline and
AAMA/ASTM compliance remain unverified.
"""
from __future__ import annotations

import io
import json
import math
from hashlib import sha256
from zipfile import ZipFile, ZIP_DEFLATED

import ezdxf
from shapely.affinity import rotate
from shapely.geometry import Polygon

from cad_metadata import inspect_cad_metadata, MAX_BUNDLE_BYTES
from dxf_import import parse_dxf_patterns, DxfImportError
from layout_audit import audit_layout
from nesting_engine import _normalise

SCHEMA = 'autonester.cad_aware_nesting_preview.v1'
MAX_EXPORT_ENTITIES = 12000
LAYERS = {
    'candidate_outline_unverified': 'AN12_CANDIDATE',
    'secondary_contour_unverified': 'AN12_SECONDARY',
    'point_unverified': 'AN12_POINT',
    'line_unverified': 'AN12_LINE',
    'annotation_unverified': 'AN12_TEXT',
}


def _finite(v):
    x = float(v)
    if not math.isfinite(x) or abs(x) > 1e8:
        raise ValueError('Invalid or excessive coordinate')
    return x


def _point(pt, transform):
    return [round(v, 6) for v in transform(pt)]


def _transform_for(source_polygon: dict, placement: dict):
    """Replicate nesting_engine normalization/rotation/translation exactly.

    The original polygon is normalized by its bounding-box minimum, rotated
    CCW about the origin and normalized again. A placement's bbox minimum is
    its translated origin. Source CAD entity coordinates are in original
    global coordinates; derive that transform from source geometry, never
    from client-reported offset fields.
    """
    coords = source_polygon['points']
    x0 = min(_finite(p[0]) for p in coords)
    y0 = min(_finite(p[1]) for p in coords)
    angle = int(placement['rotation'])
    rad = math.radians(angle)
    cos, sin = math.cos(rad), math.sin(rad)
    rotated = _normalise(rotate(_normalise(Polygon(coords)), angle, origin=(0, 0)))
    # To normalize the raw rotated source polygon, determine the unnormalized
    # rotated bounds (before applying the second normalization).
    raw_xs = [cos*(x-x0)-sin*(y-y0) for x,y in coords]
    raw_ys = [sin*(x-x0)+cos*(y-y0) for x,y in coords]
    rx0, ry0 = min(raw_xs), min(raw_ys)
    px0 = min(_finite(p[0]) for p in placement['points'])
    py0 = min(_finite(p[1]) for p in placement['points'])
    def fn(pt):
        sx,sy = _finite(pt[0])-x0, _finite(pt[1])-y0
        return (cos*sx-sin*sy-rx0+px0, sin*sx+cos*sy-ry0+py0)
    return fn, rotated


def _write_dxf(source: dict, selected: list[dict], layout: dict, audit: dict):
    mapping = []
    expectations = {}
    doc = ezdxf.new('R2010', setup=True)
    doc.header['$INSUNITS'] = 4  # coordinates output in mm
    doc.layers.new('AN12_WARNING')
    for layer in LAYERS.values():
        doc.layers.new(layer)
    space = doc.modelspace()
    space.add_text('AUTONESTER PHASE 12 - DIAGNOSTIC ONLY - NOT FOR CUTTING',
                   dxfattribs={'layer':'AN12_WARNING','height':15,'insert':(0,-45)})
    by_name = {p['pattern_name']: p for p in source['pieces']}
    src_by_name = {p['name']:p for p in selected}
    n = 0
    skipped = 0
    for placement in layout['placed_patterns']:
        name = str(placement['name'])
        meta = by_name[name]
        poly = src_by_name[name]
        fn, _ = _transform_for(poly, placement)
        for entity in meta['entities']:
            n += 1
            if n > MAX_EXPORT_ENTITIES:
                raise ValueError('Preview entity expansion exceeds safety limit')
            g = entity['geometry']
            entry = {'source_id':entity['source_id'], 'source_layer':entity['source_layer'],
                     'source_type':entity['source_type'], 'source_role_unverified':entity['role'],
                     'pattern_name':name, 'instance':int(placement['instance']),
                     'rotation_deg':int(placement['rotation']),
                     'output_handle':None, 'status':'skipped_unsupported' if g is None else 'exported',
                     'source_outline_replaced_by_placement':False}
            if g is None:
                skipped += 1
                mapping.append(entry)
                continue
            layer = LAYERS[entity['role']]
            kind = g['kind']
            if entity['source_id'] == meta['candidate_cut_source_id']:
                # The placement contour is independently audited against the
                # imported source polygon. Its sampling may differ from the
                # original native CAD curve sampling.
                points = [[round(_finite(x),6), round(_finite(y),6)] for x,y in placement['points']]
                out = space.add_lwpolyline(points, close=True, dxfattribs={'layer':layer})
                expected = {'kind':'polyline','closed':True,'points':points}
                entry['source_outline_replaced_by_placement'] = True
            elif kind == 'polyline':
                points = [_point(p,fn) for p in g['points']]
                out = space.add_lwpolyline(points, close=bool(g['closed']), dxfattribs={'layer':layer})
                expected = {'kind':'polyline','closed':bool(g['closed']),'points':points}
            elif kind == 'line':
                a,b = _point(g['start'],fn), _point(g['end'],fn)
                out = space.add_line(a,b,dxfattribs={'layer':layer})
                expected = {'kind':'line','start':a,'end':b}
            elif kind == 'point':
                pt = _point(g['xy'],fn)
                out = space.add_point(pt,dxfattribs={'layer':layer})
                expected = {'kind':'point','xy':pt}
            elif kind == 'text':
                pt = _point(g['xy'],fn)
                new_angle = (float(g.get('rotation_deg',0)) + int(placement['rotation'])) % 360
                # TEXT insertion and rotation transformed; fonts, alignment
                # and proprietary drawing presentation not guaranteed.
                out = space.add_text(g['text'],dxfattribs={
                    'layer':layer,'height':g['height_mm'], 'insert':pt, 'rotation':new_angle})
                expected = {'kind':'text','xy':pt,'text':g['text'],
                            'height_mm':g['height_mm'], 'rotation_deg':new_angle}
            else:
                raise ValueError('Unknown entity geometry kind')
            handle = str(out.dxf.handle)
            entry['output_handle']=handle
            expectations[handle]=expected
            mapping.append(entry)
    stream = io.StringIO()
    doc.write(stream)
    dxf_bytes = stream.getvalue().encode('utf-8')
    if len(dxf_bytes) > MAX_BUNDLE_BYTES:
        raise ValueError('Export DXF exceeds preview size limit')
    return dxf_bytes, mapping, expectations, skipped


def _roundtrip(dxf_bytes, mapping, expected):
    doc = ezdxf.read(io.StringIO(dxf_bytes.decode('utf-8')))
    if int(doc.header.get('$INSUNITS',0)) != 4:
        raise ValueError('DXF output unit mismatch')
    by_handle = {str(e.dxf.handle):e for e in doc.modelspace()}
    errs = 0.0
    count = 0
    for item in mapping:
        h=item['output_handle']
        if h is None:
            continue
        if h not in by_handle or h not in expected:
            raise ValueError('Missing output handle in DXF roundtrip')
        g=expected[h]; ent=by_handle[h]
        def verify(a,b):
            nonlocal errs
            difference=math.dist(tuple(a)[:2],tuple(b)[:2])
            errs=max(errs,difference)
            if not math.isfinite(difference) or difference > 1e-5:
                raise ValueError('Transformed geometry changed during DXF export')
        if g['kind']=='polyline':
            if ent.dxftype()!='LWPOLYLINE' or bool(ent.closed)!=g['closed']:
                raise ValueError('Polyline type/closed flag mismatch')
            pts=list(ent.get_points('xy'))
            if len(pts)!=len(g['points']):
                raise ValueError('Polyline vertices missing')
            for a,b in zip(pts,g['points']):verify(a,b)
        elif g['kind']=='line':
            if ent.dxftype()!='LINE':raise ValueError('LINE type changed')
            verify(ent.dxf.start.xy,g['start']);verify(ent.dxf.end.xy,g['end'])
        elif g['kind']=='point':
            if ent.dxftype()!='POINT':raise ValueError('POINT type changed')
            verify(ent.dxf.location.xy,g['xy'])
        elif g['kind']=='text':
            if ent.dxftype()!='TEXT' or ent.dxf.text!=g['text']:
                raise ValueError('TEXT missing or changed')
            verify(ent.dxf.insert.xy,g['xy'])
            if abs(float(ent.dxf.get('rotation',0))-g['rotation_deg'])>1e-5:
                raise ValueError('TEXT rotation changed')
        count+=1
    if count!=len(expected):
        raise ValueError('Export entity count mismatch')
    return {'passed':True,'exported_entities_checked':count,
            'max_coordinate_roundtrip_difference_mm':round(errs,6),
            'scope':'re-read exported preview geometry, not source CAD semantic certification'}


def build_cad_aware_nested_preview(*, raw:bytes, filename:str,
                                   selected_patterns:list[dict], layout:dict,
                                   fabric_width:float, gap:float,
                                   unitless_units:str='mm', source_units_override:str='auto',
                                   fabric_mode:str='flat', enforce_constraints:bool=False):
    if enforce_constraints:
        raise ValueError('Phase 12 cannot independently verify factory cutting constraints; disable strict manufacturing claims')
    if fabric_mode not in ('flat','folded'):
        raise ValueError('Unknown fabric mode')
    if not isinstance(selected_patterns,list) or not selected_patterns or len(selected_patterns)>24:
        raise ValueError('Select 1-24 source CAD pattern definitions')
    if not isinstance(layout,dict):
        raise ValueError('Missing layout')
    parsed=parse_dxf_patterns(raw,filename,unitless_units,source_units_override)
    meta=inspect_cad_metadata(raw=raw,filename=filename,unitless_units=unitless_units,
                              source_units_override=source_units_override)
    sources={p['name']:p for p in parsed['patterns']}
    if len(sources)!=len(parsed['patterns']):
        raise ValueError('Ambiguous duplicate source pattern names')
    meta_names=[p['pattern_name'] for p in meta['pieces']]
    if meta_names!=[p['name'] for p in parsed['patterns']]:
        raise ValueError('Source entity/polygon mapping differs from importer')
    # Rebuild input geometry from source bytes, not from client-provided points.
    trusted=[]
    seen=set()
    for item in selected_patterns:
        if not isinstance(item,dict):raise ValueError('Invalid pattern selection')
        name=item.get('name')
        if not isinstance(name,str) or name not in sources or name in seen:
            raise ValueError('Unknown/duplicate CAD pattern selection')
        seen.add(name)
        try:
            qty=item.get('quantity',1)
            if isinstance(qty,bool) or not isinstance(qty,int):
                raise ValueError('Quantity must be integer')
            rot=item.get('allowed_rotations',[0])
            if not isinstance(rot,list):raise ValueError('Invalid allowed rotations')
            trusted.append({'name':name,'points':sources[name]['points'],
                'quantity':qty,'allowed_rotations':rot,
                'fold_required':bool(sources[name].get('fold_required',False)),
                'fold_edge':None,'fold_verified':False,
                'grainline_verified':False,'fold_status_verified':False})
        except (TypeError,ValueError) as e:
            raise ValueError('Invalid selected pattern quantity or rotations') from e
    audit=audit_layout(trusted,layout,float(fabric_width),float(gap),
                       enforce_constraints=False,fabric_mode=fabric_mode)
    if not audit['passed']:
        raise ValueError('Source-aware geometry audit failed: '+'; '.join(audit['issues'][:3]))
    dxf,mapping,expected,skipped=_write_dxf(meta,trusted,layout,audit)
    verified=_roundtrip(dxf,mapping,expected)
    manifest={
        'schema':SCHEMA,'phase':12,'production_approved':False,'cutting_allowed':False,
        'source_file_name':meta['file_name'],
        'source_dxf_sha256_server_calculated':sha256(raw).hexdigest(),
        'source_declared_units':meta['source_units'],
        'used_import_interpretation':meta['import_interpretation'],
        'fabric_width_mm':float(fabric_width),
        'fabric_length_mm':float(layout['fabric_length']),
        'layout_fingerprint':audit['layout_fingerprint'],
        'included_piece_instances':len(layout['placed_patterns']),
        'source_entity_mappings':len(mapping),
        'exported_entities':len(expected),
        'skipped_unsupported_instances':skipped,
        'output_dxf_sha256':sha256(dxf).hexdigest(),
        'roundtrip':verified,
        'notes':[
            'NOT FOR CUTTING. Source units, cut/seam/grade, fold/grainline and notch semantics are not verified.',
            'Source candidate cut contours are represented by independently audited engine placement contours; source curve sampling and other CAD attributes may differ.',
            'POINT/TEXT/LINE/secondary contours are transformed as diagnostic geometry without assigning garment manufacturing roles.',
            'Original CAD layer names are retained in mapping JSON only; diagnostic entity types use AN12_* layers in DXF.',
            'Duplicate piece copies use source CAD labels without rewriting quantity text for each instance.',
        ],
    }
    report={'geometry_audit':audit, 'roundtrip':verified,
            'manufacturing_approval':'NOT APPROVED'}
    readme=('AUTONESTER PHASE 12 - DIAGNOSTIC NESTED CAD PREVIEW ONLY. NOT FOR CUTTING.\n'
            'An original DXF upload was server-reparsed. Selected source polygons were used to audit the submitted layout.\n'
            'Supporting POINT, LINE, TEXT and secondary polygon geometry were transformed per nested piece placement.\n'
            'The main outline is taken from the audited nesting geometry, not a native source CAD curve.\n'
            'True source units, cut/seam distinction, notch/grainline/fold interpretation, text alignment, grading and production CAD fidelity remain UNVERIFIED.\n'
            'Do not load this preview file into any fabric cutter or manufacturing process.\n')
    with io.BytesIO() as buff:
        with ZipFile(buff,'w',compression=ZIP_DEFLATED) as z:
            z.writestr('CAD_AWARE_NESTED_PREVIEW_NOT_FOR_CUTTING.dxf',dxf)
            z.writestr('source_to_nested_entity_mapping.json',json.dumps(mapping,indent=2,ensure_ascii=False))
            z.writestr('manifest.json',json.dumps(manifest,indent=2,ensure_ascii=False))
            z.writestr('geometry_validation.json',json.dumps(report,indent=2,ensure_ascii=False))
            z.writestr('READ_ME_FIRST.txt',readme)
        contents=buff.getvalue()
    if len(contents)>MAX_BUNDLE_BYTES:
        raise ValueError('Phase 12 preview ZIP exceeds size limit')
    return contents,manifest
