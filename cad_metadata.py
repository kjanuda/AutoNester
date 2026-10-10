"""AutoNester Phase 11: evidence-preserving garment DXF *preview* inspection.

No CAD meaning is inferred from layer numbers. Rebuilds a separate diagnostic DXF
with supported source entities and an auditable mapping of source to output IDs.
It is NOT a nested marker, manufacturing-ready exporter, or a validated AAMA parser.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from io import BytesIO, StringIO
import json
import math
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import ezdxf
from ezdxf.path import make_path

from cad_source_audit import _read_dxf, UNIT_NAMES
from dxf_import import (
    parse_dxf_patterns, DxfImportError, UNIT_TO_MM, UNITLESS_TO_MM,
    MAX_FILE_BYTES, _is_closed,
)

SCHEMA = 'autonester.cad_metadata_preview.v1'
MAX_SOURCE_ENTITIES = 15000
MAX_PIECE_ENTITIES = 2400
MAX_PREVIEW_VERTICES = 500
MAX_TEXT_LENGTH = 300
SUPPORTED = {'LINE', 'POINT', 'TEXT', 'POLYLINE', 'LWPOLYLINE'}
MAX_BUNDLE_BYTES = 12 * 1024 * 1024


def _round(v: float) -> float:
    v = float(v)
    if not math.isfinite(v) or abs(v) > 1e8:
        raise DxfImportError('Non-finite or excessive CAD coordinate')
    return round(v, 6)


def _pair(x: float, y: float, factor: float) -> list[float]:
    return [_round(x * factor), _round(y * factor)]


def _coords(entity, factor: float):
    """Extract inspectable 2D geometry, not fabric semantics."""
    typ = entity.dxftype()
    if typ == 'POINT':
        v = entity.dxf.location
        return {'kind': 'point', 'xy': _pair(v.x, v.y, factor)}
    if typ == 'LINE':
        a, b = entity.dxf.start, entity.dxf.end
        return {'kind': 'line', 'start': _pair(a.x,a.y,factor), 'end': _pair(b.x,b.y,factor)}
    if typ == 'TEXT':
        v = entity.dxf.insert
        txt = str(entity.dxf.get('text',''))
        if len(txt) > MAX_TEXT_LENGTH:
            # We never silently truncate source labels.
            raise DxfImportError('CAD TEXT exceeds preview limit; inspection cannot preserve full label')
        return {'kind': 'text', 'xy': _pair(v.x,v.y,factor), 'text': txt,
                'height_mm': round(max(0.1, min(100, float(entity.dxf.get('height', 2.5))*factor)), 5),
                'rotation_deg': float(entity.dxf.get('rotation', 0))}
    if typ in ('POLYLINE', 'LWPOLYLINE'):
        if typ == 'POLYLINE' and not entity.is_2d_polyline:
            return None
        path = make_path(entity)
        pts = []
        for pos in path.flattening(distance=max(0.1 / factor, 1e-8), segments=8):
            p = _pair(pos.x,pos.y,factor)
            if not pts or math.dist(pts[-1],p) > 1e-6:
                pts.append(p)
            if len(pts) > MAX_PREVIEW_VERTICES:
                return None  # recorded as unsupported in caller
        closed = bool(_is_closed(entity))
        if len(pts)>1 and closed and math.dist(pts[0],pts[-1])<0.005:
            pts.pop()
        return {'kind': 'polyline', 'closed': closed, 'points': pts}
    return None


def _unit_factor(doc, unitless_units, source_units_override):
    if source_units_override not in ('auto','mm','cm','in') or unitless_units not in UNITLESS_TO_MM:
        raise DxfImportError('Invalid DXF source units selection')
    code = int(doc.header.get('$INSUNITS', 0))
    if source_units_override != 'auto':
        return UNITLESS_TO_MM[source_units_override], code
    if code == 0:
        return UNITLESS_TO_MM[unitless_units], code
    if code not in UNIT_TO_MM:
        raise DxfImportError('Unsupported source $INSUNITS; supply an explicit verified override')
    return UNIT_TO_MM[code], code


def inspect_cad_metadata(*, raw: bytes, filename: str = 'pattern.dxf',
                         unitless_units: str = 'mm', source_units_override: str = 'auto') -> dict:
    """Inspect source CAD with explicit source-entity IDs and diagnostics.

    Piece sequence follows the Phase 10 importer's candidate order. Each nested
    entity is associated with a source INSERT and BLOCK handle; virtual INSERT
    transformations are applied by ezdxf. Its original handle is never faked.
    """
    parsed = parse_dxf_patterns(raw, filename, unitless_units, source_units_override)
    doc = _read_dxf(raw)
    factor, header_code = _unit_factor(doc, unitless_units, source_units_override)
    summary = Counter()
    pieces = []
    source_entity_total = 0
    unsupported_total = 0
    pattern_index = 0
    warnings = [
        'PREVIEW ONLY. Layer numbers, fold, grainline, notches and seams have NOT been certified.',
        'Entity presence and traceability do not establish garment/cutter semantics.',
    ]
    if source_units_override != 'auto':
        warnings.append('Manual source-units override: independently confirm true source CAD dimensions.')

    for insert in doc.modelspace():
        if insert.dxftype() != 'INSERT':
            continue
        name = str(insert.dxf.name)
        try:
            block = doc.blocks[name]
            virt = list(insert.virtual_entities())
        except Exception as e:
            raise DxfImportError(f'Unable to inspect source block {name}') from e
        original = list(block)
        # ezdxf excludes ATTDEF from virtual_entities(). Keep ATTDEF in the
        # manifest explicitly as unsupported rather than shifting handle mapping.
        matched = []
        k = 0
        for src_ent in original:
            if src_ent.dxftype() == 'ATTDEF':
                matched.append((src_ent, None, None))
                continue
            if k >= len(virt) or src_ent.dxftype() != virt[k].dxftype():
                raise DxfImportError('Cannot establish reliable per-entity source-to-virtual mapping')
            matched.append((src_ent, virt[k], k))
            k += 1
        if k != len(virt):
            raise DxfImportError('Unmapped virtual CAD entities')
        if len(original) > MAX_PIECE_ENTITIES:
            raise DxfImportError('Too many entities inside one pattern block')
        candidate = [i for i, e in enumerate(virt) if e.dxftype() in ('POLYLINE','LWPOLYLINE')
                     and str(e.dxf.get('layer','')) == '1' and _is_closed(e)]
        all_closed = [i for i,e in enumerate(virt) if e.dxftype() in ('POLYLINE','LWPOLYLINE') and _is_closed(e)]
        if len(candidate) == 1:
            cut_idx = candidate[0]
        elif len(all_closed) == 1:
            cut_idx = all_closed[0]
        else:
            raise DxfImportError(f'Cannot uniquely match candidate outline in BLOCK {name}')
        if pattern_index >= len(parsed['patterns']):
            raise DxfImportError('CAD source and importer pattern counts disagree')
        imported = parsed['patterns'][pattern_index]
        pattern_index += 1
        bx = [p[0] for p in imported['points']]; by = [p[1] for p in imported['points']]
        origin = [round(min(bx),6), round(min(by),6)]
        ent_items = []
        counts = Counter()
        layer_counts = Counter()
        preserved_count = 0
        metadata_labels = {}
        for j,(src,flat,virtual_index) in enumerate(matched):
            source_entity_total += 1
            if source_entity_total > MAX_SOURCE_ENTITIES:
                raise DxfImportError('Too many entities to inspect safely')
            typ = src.dxftype()
            layer = str(src.dxf.get('layer','0'))[:80]
            counts[typ]+=1; layer_counts[layer]+=1; summary[typ]+=1
            src_handle = str(src.dxf.get('handle','') or '')
            if not src_handle:
                raise DxfImportError('Missing source BLOCK entity handle')
            kind = 'candidate_outline_unverified' if virtual_index == cut_idx else (
                'secondary_contour_unverified' if typ in ('POLYLINE','LWPOLYLINE') else
                'point_unverified' if typ == 'POINT' else
                'line_unverified' if typ == 'LINE' else
                'annotation_unverified' if typ == 'TEXT' else 'unsupported')
            try:
                geom = _coords(flat, factor) if flat is not None and typ in SUPPORTED else None
            except Exception as e:
                raise DxfImportError(f'Cannot decode entity {src_handle} in block {name}') from e
            retained = geom is not None
            if retained:
                preserved_count += 1
                if typ == 'TEXT':
                    val = geom['text']
                    if ':' in val:
                        k,v = val.split(':',1)
                        if k.strip().lower() in ('piece name','size','quantity','fold','rotation','tilt','material','annotation','category'):
                            metadata_labels.setdefault(k.strip().lower(),v.strip())
            else:
                unsupported_total += 1
            ent_items.append({
                'source_id': f'INSERT:{insert.dxf.handle}/BLOCK:{name}/HANDLE:{src_handle}',
                'source_handle': src_handle,
                'source_insert_handle': str(insert.dxf.handle),
                'source_block_name': name,
                'source_type': typ,
                'source_layer': layer,
                'role': kind,
                'preview_exportable': retained,
                'geometry': geom,
            })
        pieces.append({
            'pattern_index': pattern_index,
            'pattern_name': imported['name'],
            'block_name': name,
            'insert_handle': str(insert.dxf.handle),
            'candidate_cut_source_id': next(ent['source_id'] for ent in ent_items if ent['role']=='candidate_outline_unverified'),
            'source_size_label': imported.get('source_size'),
            'source_quantity_label': imported.get('source_quantity'),
            'source_fold_label': imported.get('fold_text'),
            'source_fields': metadata_labels,
            'candidate_bounds_mm': {'min_x':origin[0], 'min_y':origin[1],
                     'width':round(max(bx)-min(bx),3), 'height':round(max(by)-min(by),3)},
            'entity_count': len(ent_items),
            'preview_exportable_count': preserved_count,
            'counts_by_type': dict(sorted(counts.items())),
            'counts_by_layer': dict(sorted(layer_counts.items())),
            'entities': ent_items,
        })
    if pattern_index != len(parsed['patterns']):
        raise DxfImportError('CAD outlines outside INSERT blocks require separate explicit mapping; refusing to guess')
    if unsupported_total:
        warnings.append(f'{unsupported_total} entities are not preview-exportable; inspect the report.')
    warnings.append('Source metadata is evidence only. Grainline, notches and fold-line geometry remain unverified.')
    return {
        'schema': SCHEMA,
        'file_name': Path(filename.replace('\\','/')).name[:120],
        'source_sha256': sha256(raw).hexdigest(),
        'source_insunits': header_code,
        'source_units': UNIT_NAMES.get(header_code, f'unknown({header_code})'),
        'import_interpretation': parsed['source_units'],
        'output_units': 'mm',
        'piece_count': len(pieces),
        'entity_count': source_entity_total,
        'exportable_entity_count': source_entity_total-unsupported_total,
        'unsupported_entity_count': unsupported_total,
        'entity_type_counts': dict(sorted(summary.items())),
        'pieces': pieces,
        'warnings': warnings,
        'production_approved': False,
    }


def _move(pt, dx, dy):
    return (_round(pt[0]+dx), _round(pt[1]+dy))


def build_metadata_preview_bundle(*, raw: bytes, filename: str,
                                 unitless_units: str = 'mm',
                                 source_units_override: str = 'auto') -> tuple[bytes,dict]:
    """Create diagnostic sample sheet, source/export mapping and checks.

    Geometry is only translated onto a visual inspection grid; this is NOT the
    optimized nesting layout. Unsupported entities remain explicitly reported.
    """
    data = inspect_cad_metadata(raw=raw, filename=filename,
                                unitless_units=unitless_units,
                                source_units_override=source_units_override)
    document = ezdxf.new('R2010', setup=True)
    document.header['$INSUNITS'] = 4
    msp = document.modelspace()
    layers = {
        'candidate_outline_unverified':'AN_CANDIDATE',
        'secondary_contour_unverified':'AN_SECONDARY',
        'point_unverified':'AN_POINT',
        'line_unverified':'AN_LINE',
        'annotation_unverified':'AN_TEXT',
    }
    for layer in layers.values():
        document.layers.new(name=layer)
    manifest = []
    geometry_expectations = {}
    for piece in data['pieces']:
        index = piece['pattern_index']-1
        # Deliberately *not* a tight nesting. Each pattern has its own workspace.
        dx = (index % 3)*2200 - piece['candidate_bounds_mm']['min_x']
        dy = (index // 3)*2500 - piece['candidate_bounds_mm']['min_y']
        for ent in piece['entities']:
            geom = ent['geometry']
            item = {'source_id':ent['source_id'], 'source_type':ent['source_type'],
                    'source_layer':ent['source_layer'], 'role':ent['role'],
                    'pattern_index':piece['pattern_index'], 'export_dxf_handle':None,
                    'status':'skipped_unsupported' if geom is None else 'exported_preview'}
            if geom is not None:
                typ = geom['kind']; layer = layers[ent['role']]
                if typ == 'polyline':
                    output = msp.add_lwpolyline([_move(p,dx,dy) for p in geom['points']],
                                                close=geom['closed'],dxfattribs={'layer':layer})
                elif typ == 'line':
                    output = msp.add_line(_move(geom['start'],dx,dy),_move(geom['end'],dx,dy),dxfattribs={'layer':layer})
                elif typ == 'point':
                    output = msp.add_point(_move(geom['xy'],dx,dy),dxfattribs={'layer':layer})
                elif typ == 'text':
                    output = msp.add_text(geom['text'],dxfattribs={'layer':layer,'height':geom['height_mm'],
                                           'insert':_move(geom['xy'],dx,dy)})
                else:
                    raise AssertionError('Unexpected inspected geometry type')
                item['export_dxf_handle'] = str(output.dxf.handle)
                geometry_expectations[str(output.dxf.handle)] = (geom, dx, dy)
            manifest.append(item)
    with StringIO() as data_stream:
        document.write(data_stream)
        dxf = data_stream.getvalue().encode('utf-8')
    if len(dxf) > MAX_BUNDLE_BYTES:
        raise DxfImportError('Metadata preview DXF exceeds safe ZIP limit')
    # Independently re-read the generated preview in memory; entity fidelity
    # counts and geometry are compared with the source manifest geometries.
    read_back = ezdxf.read(StringIO(dxf.decode('utf-8', errors='replace')))
    all_back = {e.dxf.handle:e for e in read_back.modelspace()}
    verification_errors = []
    max_diff = 0.0
    def _diff(got, expected):
        nonlocal max_diff
        d = math.dist(tuple(got)[:2], expected)
        max_diff = max(max_diff, d)
        if d > 1e-5:
            verification_errors.append(f'CAD preview coordinate mismatch ({d:.6f} mm)')

    for item in manifest:
        h = item['export_dxf_handle']
        if not h:
            continue
        if h not in all_back or all_back[h].dxftype() not in (
                'LWPOLYLINE','LINE','POINT','TEXT'):
            verification_errors.append(f'Missing/mismatched preview entity {h}')
            continue
        exported = all_back[h]
        geom, dx, dy = geometry_expectations[h]
        typ = geom['kind']
        if typ == 'polyline':
            got = list(exported.get_points('xy'))
            expected = [_move(p,dx,dy) for p in geom['points']]
            if len(got) != len(expected) or bool(exported.closed) != geom['closed']:
                verification_errors.append(f'Polyline vertex count/closure mismatch {h}')
            else:
                for ap,bp in zip(got,expected): _diff(ap,bp)
        elif typ == 'point':
            _diff(exported.dxf.location.xy,_move(geom['xy'],dx,dy))
        elif typ == 'line':
            _diff(exported.dxf.start.xy,_move(geom['start'],dx,dy))
            _diff(exported.dxf.end.xy,_move(geom['end'],dx,dy))
        elif typ == 'text':
            _diff(exported.dxf.insert.xy,_move(geom['xy'],dx,dy))
            if exported.dxf.text != geom['text']:
                verification_errors.append(f'Text label mismatch {h}')
    if verification_errors:
        raise DxfImportError('DXF export round-trip failed: ' + verification_errors[0])
    report = {
        'schema':'autonester.cad_metadata_export_preview.v1',
        'preview_only':True, 'production_approved':False,
        'source_filename':data['file_name'],'source_sha256':data['source_sha256'],
        'source_units_interpretation':data['import_interpretation'],
        'output_dxf_sha256':sha256(dxf).hexdigest(),
        'output_units':'millimetres',
        'exported_entities':sum(bool(x['export_dxf_handle']) for x in manifest),
        'skipped_entities':sum(not x['export_dxf_handle'] for x in manifest),
        'round_trip_handles_verified':len(verification_errors)==0,
        'round_trip_geometry_validated':True,
        'max_round_trip_coordinate_error_mm':round(max_diff,8),
        'mapping':manifest,
        'note':'DIAGNOSTIC ENTITY SHEET ONLY. NOT A NESTED MARKER. NOT FOR CUTTING. Original CAD semantics are unverified.',
    }
    readme = ('AutoNester Phase 11 — DIAGNOSTIC PREVIEW ONLY, NOT FOR CUTTING.\n'
              'Each source INSERT is laid out on a separated inspection grid, NOT nested.\n'
              'Layer roles are tentative, not verified cut/seam/notch/grainline meanings.\n'
              'Inspect source_to_preview_mapping.json for every supported/skipped entity.\n'
              'Physical dimensions require independent CAD/measurement verification.\n')
    with BytesIO() as buf:
        with ZipFile(buf, 'w', compression=ZIP_DEFLATED) as z:
            z.writestr('METADATA_INSPECTION_NOT_FOR_CUTTING.dxf',dxf)
            z.writestr('source_to_preview_mapping.json',json.dumps(report,indent=2,ensure_ascii=False))
            z.writestr('CAD_METADATA_INSPECTION.json',json.dumps(data,indent=2,ensure_ascii=False))
            z.writestr('READ_ME_FIRST.txt',readme)
        return buf.getvalue(), report
