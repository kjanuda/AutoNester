"""AutoNester DXF preview importer v0.4.

Supports 2D explicitly-closed polylines in modelspace and candidate cutting
outlines in simple INSERT blocks, including endpoint-closed but unflagged
legacy POLYLINEs. Not intended for production cutting, grading, or AAMA.
"""
from __future__ import annotations

import tempfile
import os
import math
from hashlib import sha256
from collections import Counter

import ezdxf
from ezdxf.path import make_path
from shapely.geometry import Polygon

MAX_FILE_BYTES = 6 * 1024 * 1024
MAX_PATTERNS = 24
MAX_VERTICES = 80
MAX_ENTITIES = 5000

UNIT_TO_MM = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0,
              7: 1_000_000.0, 8: 0.0000254, 9: 0.0254, 10: 914.4,
              11: 1e-7, 12: 1e-6, 13: 0.001, 14: 100.0}
UNITLESS_TO_MM = {'mm': 1.0, 'cm': 10.0, 'in': 25.4}


class DxfImportError(ValueError):
    pass


def _safe_name(s):
    return (''.join(c if c.isprintable() else ' ' for c in str(s)).strip() or 'Pattern')[:60]


def _is_closed(entity):
    typ = entity.dxftype()
    if typ == 'LWPOLYLINE':
        if entity.closed:
            return True
        coords = list(entity.get_points('xy'))
    elif typ == 'POLYLINE' and entity.is_2d_polyline:
        if entity.is_closed:
            return True
        coords = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
    else:
        return False
    return len(coords) >= 4 and math.dist(coords[0], coords[-1]) < 0.005


def _polygon(entity, factor):
    if not _is_closed(entity):
        return None
    try:
        path = make_path(entity)
        pts = []
        for v in path.flattening(distance=max(0.5 / factor, 1e-9), segments=8):
            xy = (float(v.x) * factor, float(v.y) * factor)
            if not all(math.isfinite(k) and abs(k) < 1e9 for k in xy):
                raise DxfImportError('Non-finite or out-of-range DXF vertex')
            if not pts or math.dist(pts[-1], xy) > 1e-6:
                pts.append(xy)
            if len(pts) > MAX_VERTICES+1:
                raise DxfImportError(f'Contour exceeds {MAX_VERTICES} vertices; simplify CAD outline')
        if len(pts) > 1 and math.dist(pts[-1], pts[0]) < 0.005 * factor:
            pts.pop()
        if len(pts) < 3:
            raise DxfImportError('A cutting contour has fewer than three vertices')
        p = Polygon(pts)
        if not p.is_valid or p.area <= 1e-7:
            raise DxfImportError('Self-intersecting or empty pattern contour')
        return p
    except DxfImportError:
        raise
    except Exception as exc:
        raise DxfImportError('Could not interpret pattern polyline') from exc


def parse_dxf_patterns(raw: bytes, filename: str = 'pattern.dxf',
                       unitless_units: str = 'mm', source_units_override: str = 'auto') -> dict:
    """Read candidate cut boundaries only. Source-unit override must be deliberate."""
    if unitless_units not in UNITLESS_TO_MM:
        raise DxfImportError('unitless_units must be mm, cm or in')
    if source_units_override not in ('auto', 'mm', 'cm', 'in'):
        raise DxfImportError('source_units_override must be auto, mm, cm or in')
    safe_name = (filename.replace('\\', '/').split('/')[-1] or 'pattern.dxf')[:120]
    if not safe_name.lower().endswith('.dxf'):
        raise DxfImportError('Only .dxf files are supported')
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise DxfImportError('DXF must be nonempty and at most 6 MB')
    if raw.startswith(b'AutoCAD Binary DXF') or b'SECTION' not in raw[:10000]:
        raise DxfImportError('Not a recognized ASCII DXF file')
    # ezdxf.readfile handles some legacy R12 block exports that ezdxf.read(StringIO)
    # does not resolve into modelspace entities. Use a temporary file, then remove it.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as temp:
            temp.write(raw)
            temp_path = temp.name
        doc = ezdxf.readfile(temp_path)
    except Exception as exc:
        raise DxfImportError('DXF could not be parsed') from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    header_units = int(doc.header.get('$INSUNITS', 0))
    warnings = ['Preview only. Verify contour layer, size, units, grainline and notches against the original CAD before cutting.']
    if source_units_override != 'auto':
        factor = UNITLESS_TO_MM[source_units_override]
        unit_label = f'Manual {source_units_override} override (DXF $INSUNITS={header_units})'
        warnings.append('Units were overridden manually; source $INSUNITS was ignored. Confirm actual dimensions.')
    elif header_units == 0:
        factor = UNITLESS_TO_MM[unitless_units]
        unit_label = f'unitless (interpreted as {unitless_units})'
    elif header_units in UNIT_TO_MM:
        factor = UNIT_TO_MM[header_units]
        unit_label = f'DXF $INSUNITS={header_units}'
    else:
        raise DxfImportError(f'Unsupported $INSUNITS={header_units}; explicitly choose a source-unit override')

    outlines = []
    ignored = Counter()
    index = 0
    for entity in doc.modelspace():
        index += 1
        if index > MAX_ENTITIES:
            raise DxfImportError('Too many modelspace entities')
        typ = entity.dxftype()
        if typ in ('LWPOLYLINE', 'POLYLINE'):
            candidate = _polygon(entity, factor)
            if candidate is None:
                ignored[f'open_or_non2d_{typ}'] += 1
            else:
                outlines.append((_safe_name(entity.dxf.layer), candidate, {}))
        elif typ == 'INSERT':
            try:
                virtual = list(entity.virtual_entities())
            except Exception as exc:
                raise DxfImportError('Unsupported block transform') from exc
            if len(virtual) > MAX_ENTITIES:
                raise DxfImportError('Too many entities in CAD block')
            block_polys = []
            for sub in virtual:
                kind = sub.dxftype()
                if kind in ('LWPOLYLINE', 'POLYLINE'):
                    poly = _polygon(sub, factor)
                    if poly is not None:
                        block_polys.append((sub.dxf.layer, poly))
                    else:
                        ignored[f'open_block_{kind}'] += 1
                else:
                    ignored[f'block_{kind}'] += 1
            if not block_polys:
                ignored['block_without_contour'] += 1
                continue
            selected = [item for item in block_polys if item[0] == '1']
            if len(selected) == 1:
                layer, polygon = selected[0]
            elif len(block_polys) == 1:
                layer, polygon = block_polys[0]
            else:
                # Fail rather than silently interpreting multiple enclosed
                # sewing/lining contours as cutting outlines.
                raise DxfImportError(f'BLOCK {entity.dxf.name} has ambiguous cut contours. Select outer cut layer in CAD first.')
            name = f'Block {entity.dxf.name}'
            metadata = {}
            try:
                text_entities = doc.blocks[entity.dxf.name].query('TEXT')
                for txt in text_entities:
                    content = txt.dxf.text.strip()
                    if content.startswith('Piece Name:'):
                        name = content.partition(':')[2].strip() or name
                    for k, field in [('Quantity:', 'source_quantity'), ('Size:', 'source_size'),
                                     ('Fold:', 'fold_text'), ('Rotation:', 'source_rotation'),
                                     ('Tilt:', 'source_tilt'), ('Material:', 'source_material')]:
                        if content.startswith(k):
                            value = content.partition(':')[2].strip()
                            if k == 'Quantity:':
                                if value.isdigit():
                                    metadata[field] = int(value)
                            else:
                                metadata[field] = _safe_name(value)

            except Exception:
                pass
            outlines.append((_safe_name(name), polygon, metadata))
            ignored['nested_or_internal_block_contours_skipped'] += max(0,len(block_polys)-1)
        else:
            ignored[typ] += 1
        if len(outlines) > MAX_PATTERNS:
            raise DxfImportError(f'More than {MAX_PATTERNS} cutting outlines (prototype limit)')

    if not outlines:
        raise DxfImportError('No recognized closed 2D contours in Model Space or INSERT blocks')

    for i, (_,a,_) in enumerate(outlines):
        for j, (_,b,_) in enumerate(outlines):
            if i!=j and a.contains(b):
                raise DxfImportError('A contour is enclosed by another contour; cut vs seam/hole selection is ambiguous')

    # An absurd garment dimension is often a source-unit declaration error.
    biggest = max(max(p.bounds[2]-p.bounds[0],p.bounds[3]-p.bounds[1]) for _,p,_ in outlines)
    if biggest > 20000:
        raise DxfImportError(
            f'Largest outline is {biggest:.0f} mm based on DXF $INSUNITS={header_units}. '
            'Verify the unit mismatch and select the source-unit override (mm/in) if confirmed.'
        )

    patterns = []
    for number,(name,p,metadata) in enumerate(outlines,1):
        obj = {
            'name': f'{name} #{number}',
            'points': [[round(x,5),round(y,5)] for x,y in list(p.exterior.coords)[:-1]],
            'quantity': 1,
            'allowed_rotations':[0],
            # Fold markers are imported as data, NOT assumed geometrically verified.
            'fold_required':metadata.get('fold_text', '').strip().upper() in ('Y', 'YES', 'TRUE'),
            'fold_verified':False,
            'fold_edge':None,
            'grainline_verified':False,
            'fold_status_verified':False,
        }
        for field in ('source_quantity','source_size','source_rotation','source_tilt','source_material','fold_text'):
            if field in metadata:
                obj[field] = metadata[field]
        patterns.append(obj)
    if any(meta.get("source_quantity", 1) != 1 for _,_,meta in outlines):
        warnings.append('CAD source quantities were read as metadata, but nesting quantity defaults to 1 until confirmed.')
    if ignored['nested_or_internal_block_contours_skipped']:
        warnings.append('Ignored internal/sewing contours in BLOCKs; used layer 1 as candidate outer cut outline. Confirm layer meaning.')
    if ignored:
        warnings.append('Skipped support entities: ' + ', '.join(f'{k}={v}' for k,v in sorted(ignored.items())))
    warnings.append('Imported Fold and Rotation metadata are advisory only; no fold line/grainline was geometrically verified. 0° rotation remains the default.')

    return {
        'file_name':safe_name,'source_dxf_sha256':sha256(raw).hexdigest(),
        'source_units':unit_label,'output_units':'mm',
        'pattern_count':len(patterns),'patterns':patterns,'warnings':warnings,
        'ignored_entities':dict(ignored),
    }
