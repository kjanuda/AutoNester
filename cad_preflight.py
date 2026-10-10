"""Phase 8: independent preview-level measurements and source CAD risk flags.

This does not discover grainline or manufacturing metadata that the DXF importer
cannot read. It NEVER authorizes cutting, even when geometry checks pass.
"""
from __future__ import annotations

from collections import Counter
from math import isfinite
from shapely.geometry import Polygon


def inspect_import(imported: dict) -> dict:
    patterns = imported.get('patterns', [])
    items: list[dict] = []
    issues: list[dict] = []
    src = str(imported.get('source_units', ''))
    if 'Manual' in src:
        issues.append({'level': 'warning', 'code': 'UNITS_OVERRIDE',
                       'message': 'Source units were manually overridden. Confirm at least one physical reference measurement in source CAD.'})
    if 'unitless' in src:
        issues.append({'level': 'warning', 'code': 'UNITLESS_FILE',
                       'message': 'Source DXF lacks authoritative units; confirm actual measurement before any cutting.'})

    by_name = Counter()
    total_area = 0.0
    for i, item in enumerate(patterns, 1):
        name = str(item.get('name', f'Piece #{i}'))
        by_name[name.casefold()] += 1
        p = Polygon(item['points'])
        x0, y0, x1, y1 = p.bounds
        w, h = x1-x0, y1-y0
        area = p.area
        total_area += area
        severe = not (p.is_valid and area > 0 and isfinite(w) and isfinite(h))
        if severe:
            issues.append({'level': 'error', 'code': 'INVALID_CONTOUR', 'message': f'{name}: invalid contour'})
        if min(w,h) < 1.0:
            issues.append({'level': 'warning', 'code': 'TINY_DIMENSION',
                           'message': f'{name}: a bounding-box dimension is below 1 mm.'})
        if max(w,h) > 3000:
            issues.append({'level': 'warning', 'code': 'OVERSIZE_PATTERN',
                           'message': f'{name}: dimension exceeds 3000 mm. Check unit conversion.'})
        fold = bool(item.get('fold_required', False))
        if fold:
            issues.append({'level':'warning','code':'FOLD_UNVERIFIED',
                           'message': f'{name}: source indicates Fold Y; actual fold line and fabric alignment are unverified.'})
        items.append({'name': name, 'width_mm': round(w,2), 'height_mm': round(h,2),
                      'area_mm2': round(area,2),
                      'source_quantity_label': item.get('source_quantity'),
                      'source_size_label': item.get('source_size'),
                      'source_fold_label': item.get('fold_text'),
                      'candidate_layer_only': True})
    if any(count > 1 for count in by_name.values()):
        issues.append({'level': 'warning', 'code': 'DUPLICATE_NAMES',
                       'message': 'Duplicate source pattern names; validate sizes and identities manually.'})
    if not patterns:
        issues.append({'level':'error','code':'NO_PATTERNS','message':'No pattern outlines imported.'})
    issues.append({'level': 'warning', 'code': 'CUTTING_METADATA_UNVERIFIED',
                   'message': 'Grainline, notches, seam/cut-line semantics, grade sizes, fabric nap and cut tolerances were not validated.'})
    return {'version': '0.8.0', 'status': 'review_required', 'cutting_approved': False,
            'pattern_count': len(items), 'candidate_area_mm2': round(total_area, 2),
            'source_units': src, 'patterns': items, 'issues': issues,
            'note': 'Candidate outline geometry only. A CAD operator must check against original source drawing and material specifications.'}
