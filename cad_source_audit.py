"""Phase 10: evidence-oriented *preview* CAD inventory and dimension cross-check.

Source inspection is descriptive, not an interpretation of manufacturing semantics.
No cutting release is granted, even if the measurements match.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import math
import os
from pathlib import Path
import tempfile

import ezdxf

from dxf_import import parse_dxf_patterns, DxfImportError, MAX_FILE_BYTES

UNIT_NAMES = {0: 'unitless', 1: 'inches', 2: 'feet', 4: 'millimetres',
              5: 'centimetres', 6: 'metres', 7: 'kilometres', 8: 'microinches',
              9: 'mils', 10: 'yards', 11: 'angstroms', 12: 'nanometres',
              13: 'microns', 14: 'decimetres'}
MAX_INSPECT_ENTITIES = 30000


def _read_dxf(raw: bytes):
    # ezdxf.readfile is compatible with legacy CAD block exports.
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as fh:
            fh.write(raw)
            path = fh.name
        return ezdxf.readfile(path)
    except Exception as exc:
        raise DxfImportError('Cannot read source DXF for CAD inventory') from exc
    finally:
        if path is not None:
            try: os.unlink(path)
            except OSError: pass


def audit_cad_source(*, raw: bytes, filename: str, unitless_units: str = 'mm',
                     source_units_override: str = 'auto',
                     reference_pattern_index: int | None = None,
                     reference_axis: str = 'width', reference_mm: float | None = None,
                     tolerance_mm: float = 1.0) -> dict:
    """Report candidate pattern extraction + independent source-entity inventory.

    Reference measurements are USER-SUPPLIED, not verified CAD authority.
    """
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise DxfImportError('DXF must be non-empty and at most 6MB')
    if reference_axis not in ('width', 'height'):
        raise DxfImportError('reference_axis must be width or height')
    if not math.isfinite(tolerance_mm) or not 0 < tolerance_mm <= 25:
        raise DxfImportError('tolerance_mm must be > 0 and <= 25')
    if reference_mm is not None and (not math.isfinite(reference_mm) or not 0 < reference_mm < 20000):
        raise DxfImportError('reference_mm must be between 0 and 20,000 mm')
    if reference_mm is not None and reference_pattern_index is None:
        raise DxfImportError('reference_pattern_index is required when reference_mm is provided')

    parsed = parse_dxf_patterns(raw, filename, unitless_units, source_units_override)
    doc = _read_dxf(raw)
    header_code = int(doc.header.get('$INSUNITS', 0))
    source_entities = Counter()
    block_entities = Counter()
    layers = Counter()
    block_names = Counter()
    notes = []
    inspected = 0

    def _add(entity, counts):
        nonlocal inspected
        inspected += 1
        if inspected > MAX_INSPECT_ENTITIES:
            raise DxfImportError('DXF contains too many entities to inventory safely')
        kind = entity.dxftype()
        counts[kind] += 1
        layers[str(entity.dxf.get('layer', '0'))[:70]] += 1

    for entity in doc.modelspace():
        _add(entity, source_entities)
        if entity.dxftype() == 'INSERT':
            block_names[str(entity.dxf.name)[:70]] += 1
            try:
                for virtual_entity in entity.virtual_entities():
                    _add(virtual_entity, block_entities)
            except Exception:
                notes.append('Some INSERT content cannot be expanded. Check original CAD.')

    patterns = []
    for i, pat in enumerate(parsed['patterns'], 1):
        xs = [float(p[0]) for p in pat['points']]
        ys = [float(p[1]) for p in pat['points']]
        patterns.append({
            'index': i, 'name': pat['name'],
            'width_mm': round(max(xs) - min(xs), 3),
            'height_mm': round(max(ys) - min(ys), 3),
            'fold_label_present': bool(pat.get('fold_required', False)),
            'size_label': pat.get('source_size'),
            'quantity_label': pat.get('source_quantity'),
        })

    reference = {'status': 'not_provided', 'note': 'No external reference dimension provided.'}
    if reference_mm is not None:
        if not isinstance(reference_pattern_index, int) or not 1 <= reference_pattern_index <= len(patterns):
            raise DxfImportError('reference_pattern_index is outside imported patterns')
        selected = patterns[reference_pattern_index-1]
        measured = selected[f'{reference_axis}_mm']
        difference = abs(measured - reference_mm)
        reference = {
            'status': 'matched' if difference <= tolerance_mm + 1e-9 else 'mismatch',
            'pattern_index': reference_pattern_index, 'pattern_name': selected['name'],
            'axis': reference_axis, 'user_supplied_reference_mm': reference_mm,
            'imported_dimension_mm': measured, 'absolute_difference_mm': round(difference, 4),
            'tolerance_mm': tolerance_mm,
            'note': 'Comparison against a manually entered dimension only. It is not authenticated CAD evidence.',
        }

    flags = []
    def flag(code, message): flags.append({'code': code, 'message': message})
    if source_units_override != 'auto':
        flag('UNIT_OVERRIDE', f'Header specifies {UNIT_NAMES.get(header_code, str(header_code))}, importer used manual {source_units_override}. Verify in CAD.')
    if reference['status'] == 'not_provided':
        flag('NO_REFERENCE', 'Check a physical/source-CAD dimension before trusting scale.')
    if reference['status'] == 'mismatch':
        flag('DIMENSION_MISMATCH', 'User-supplied dimension differs from imported geometry beyond tolerance.')
    ignored = parsed.get('ignored_entities', {})
    if ignored:
        flag('CAD_ENTITY_LOSS', 'Importer does not transfer all source CAD entities. Source inventory below is diagnostic, not preserved export.')
    if any(p['fold_label_present'] for p in patterns):
        flag('FOLD_NOT_VERIFIED', 'Fold Y metadata is advisory; fold axis and fabric alignment not established.')
    flag('SEMANTICS_NOT_VERIFIED', 'Grainlines, notches, cut vs seam contours, grading, material and production allowances are not validated.')

    return {
        'schema': 'autonester.cad_source_review.v1',
        'source_filename': Path(filename.replace('\\', '/')).name[:120],
        'source_dxf_sha256_server_calculated': sha256(raw).hexdigest(),
        'header_insunits_code': header_code,
        'header_units': UNIT_NAMES.get(header_code, f'unknown({header_code})'),
        'import_interpretation': parsed['source_units'],
        'imported_pattern_count': len(patterns),
        'patterns': patterns,
        'source_inventory': {
            'modelspace_entity_counts': dict(sorted(source_entities.items())),
            'insert_content_entity_counts': dict(sorted(block_entities.items())),
            'layer_entity_counts': dict(sorted(layers.items())),
            'block_insert_counts': dict(sorted(block_names.items())),
            'importer_skipped_entity_counts': ignored,
        },
        'reference_check': reference,
        'flags': flags,
        'inspection_notes': notes,
        'cutting_approved': False,
        'note': 'Source inventory + manually asserted dimension check only; no CAD semantic certification or cutter-ready DXF.',
    }
