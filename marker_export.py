"""Phase 9: geometry-audited preview marker export and deterministic traceability.

This exporter deliberately omits production approvals and does NOT preserve full
CAD semantics (seam lines, notches, grading, grainline, folds, material metadata).
It MUST NOT be used to drive cutters. The only import/export fidelity asserted is
closed candidate polygon coordinates in millimetres, in the chosen layout.
"""
from __future__ import annotations

from hashlib import sha256
import io
import json
import math
import re
from zipfile import ZipFile, ZIP_DEFLATED

import ezdxf

from layout_audit import audit_layout

OUTLINE_LAYER = 'AN_PREVIEW_CONTOURS'
NOTE_LAYER = 'AN_READ_ME'
APP_ID = 'AUTONESTER'
WARNING = 'PREVIEW ONLY - NOT APPROVED FOR CUTTING'


def _hash_json(obj):
    return sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'),
                             allow_nan=False, ensure_ascii=True).encode('utf-8')).hexdigest()


def _make_dxf(layout):
    doc = ezdxf.new('R2010', setup=True)
    doc.header['$INSUNITS'] = 4  # export coordinates are millimetres
    doc.header['$MEASUREMENT'] = 1
    doc.layers.new(OUTLINE_LAYER, dxfattribs={'color': 4})
    doc.layers.new(NOTE_LAYER, dxfattribs={'color': 1})
    if APP_ID not in doc.appids:
        doc.appids.new(APP_ID)
    space = doc.modelspace()
    for piece in layout['placed_patterns']:
        poly = space.add_lwpolyline(piece['points'], close=True,
                                    dxfattribs={'layer': OUTLINE_LAYER})
        poly.set_xdata(APP_ID, [
            (1000, str(piece['name'])[:120]),
            (1070, int(piece['instance'])),
            (1070, int(piece['rotation'])),
        ])
    space.add_text(WARNING, dxfattribs={'layer': NOTE_LAYER, 'height': 12,
                                       'insert': (0, -30)})
    space.add_text('Pattern geometry preview: CAD grainlines/folds/notches NOT transferred',
                   dxfattribs={'layer': NOTE_LAYER, 'height': 7, 'insert': (0, -50)})
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode('utf-8')


def _verify_roundtrip(raw_dxf: bytes, layout: dict) -> dict:
    """Read exported DXF back through an independent ezdxf parser."""
    doc = ezdxf.read(io.StringIO(raw_dxf.decode('utf-8')))
    if int(doc.header.get('$INSUNITS', 0)) != 4:
        raise ValueError('Round-trip units mismatch: expected mm')
    exported = list(doc.modelspace().query('LWPOLYLINE'))
    originals = layout['placed_patterns']
    if len(exported) != len(originals):
        raise ValueError('Round-trip piece count mismatch')
    max_error = 0.0
    for i, (entity, source) in enumerate(zip(exported, originals), 1):
        if entity.dxf.layer != OUTLINE_LAYER or not entity.closed:
            raise ValueError(f'Round-trip piece {i}: incorrect cut layer or open contour')
        points = list(entity.get_points('xy'))
        if len(points) != len(source['points']):
            raise ValueError(f'Round-trip piece {i}: vertex count differs')
        meta = list(entity.get_xdata(APP_ID))
        metadata = [tag.value for tag in meta]
        if metadata != [str(source['name'])[:120], int(source['instance']), int(source['rotation'])]:
            raise ValueError(f'Round-trip piece {i}: identity metadata differs')
        for (x, y), (sx, sy) in zip(points, source['points']):
            error = math.hypot(float(x)-float(sx), float(y)-float(sy))
            max_error = max(max_error, error)
            if not math.isfinite(error) or error > 0.001:
                raise ValueError(f'Round-trip piece {i}: coordinate mismatch')
    return {'passed': True, 'count': len(exported),
            'max_coordinate_error_mm': round(max_error, 6),
            'entities_closed': True, 'export_units': 'mm',
            'scope': 'placed outline coordinates and piece identity only'}


def build_preview_bundle(*, patterns: list[dict], layout: dict, fabric_width: float,
                         gap: float, enforce_constraints: bool = False,
                         fabric_mode: str = 'flat', fabric_fold_side: str = 'left',
                         source_file_name: str | None = None,
                         source_dxf_sha256: str | None = None) -> tuple[bytes, dict]:
    """Reject altered/invalid layouts; return ZIP of DXF, validation and manifest."""
    if not isinstance(layout, dict):
        raise ValueError('Expected a nesting layout object')
    if source_dxf_sha256 is not None and not re.fullmatch(r'[0-9a-fA-F]{64}', source_dxf_sha256):
        raise ValueError('Invalid client-reported source DXF SHA-256')
    audit = audit_layout(patterns, layout, fabric_width, gap,
                         enforce_constraints, fabric_mode, fabric_fold_side)
    if not audit['passed']:
        raise ValueError('Refusing preview export: geometry audit failed: ' +
                         '; '.join(audit['issues'][:3]))
    dxf = _make_dxf(layout)
    roundtrip = _verify_roundtrip(dxf, layout)
    source = source_file_name or '(synthetic input; no uploaded DXF)'
    manifest = {
        'schema': 'autonester.preview_bundle.v1',
        'phase': 9, 'production_approved': False, 'cutting_allowed': False,
        'warning': WARNING,
        'source_file_name_client_reported': str(source)[:120],
        'source_dxf_sha256_client_reported': source_dxf_sha256,
        'source_checksum_attestation': 'Client reported; no secure source-document association or signature',
        'input_pattern_geometry_sha256': _hash_json(patterns),
        'audited_layout_fingerprint': audit['layout_fingerprint'],
        'export_dxf_sha256': sha256(dxf).hexdigest(),
        'fabric_width_mm': fabric_width, 'fabric_length_mm': layout['fabric_length'],
        'required_gap_mm': gap, 'fabric_mode': fabric_mode,
        'fold_side': fabric_fold_side,
        'constraints_checked_for_geometry_only': enforce_constraints,
        'pattern_instances': [
            {'name': p['name'], 'instance': p['instance'], 'rotation_degrees': p['rotation'],
             'vertex_count': len(p['points']), 'placed_polygon_sha256': _hash_json(p['points'])}
            for p in layout['placed_patterns']
        ],
        'unverified_cad_semantics': [
            'source drawing units and layer meaning', 'cut vs seam boundary',
            'notches', 'grainline', 'fold placement', 'seam allowance',
            'graded size and source quantities', 'fabric nap and cutting equipment'
        ],
        'roundtrip': roundtrip,
    }
    validation = {
        'geometry_audit': audit,
        'dxf_roundtrip': roundtrip,
        'manufacturing_approval': 'NOT APPROVED',
        'note': 'Only placement geometry audited. Preview DXF intentionally loses most native CAD semantics.'
    }
    stream = io.BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as bundle:
        bundle.writestr('PREVIEW_marker_NOT_FOR_CUTTING.dxf', dxf)
        bundle.writestr('manifest.json', json.dumps(manifest, indent=2, ensure_ascii=False))
        bundle.writestr('geometry_validation.json', json.dumps(validation, indent=2, ensure_ascii=False))
        bundle.writestr('READ_ME_FIRST.txt',
            WARNING + '\nThis is a polygon-only, geometry-audited prototype output.\n'
            'Source CAD, material, fabric fold, notch, grade and grainline must be independently verified.\n'
            'NEVER send the included preview DXF directly to cutting equipment.\n')
    return stream.getvalue(), manifest
