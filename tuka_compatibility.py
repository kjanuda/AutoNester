"""Phase 14: conservative TUKAcad/TUKAmark-oriented DXF exchange preflight.

The report inventories source evidence; it never declares proprietary file support,
AAMA/ASTM compliance, verified cut/grade/seam semantics, or TUKAmark re-import.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from pathlib import Path

from cad_source_audit import UNIT_NAMES, _read_dxf
from cad_metadata import inspect_cad_metadata
from dxf_import import DxfImportError, MAX_FILE_BYTES, parse_dxf_patterns

MAX_SCAN_ENTITIES = 30000


def inspect_tuka_exchange(*, raw: bytes, filename: str,
                          unitless_units: str = 'mm',
                          source_units_override: str = 'auto') -> dict:
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise DxfImportError('DXF must be nonempty and at most 6 MB')
    safe_name = Path(filename.replace('\\', '/')).name[:120]
    if not safe_name.lower().endswith('.dxf'):
        raise DxfImportError('This exchange preflight accepts ASCII .dxf files only; native TUKA formats are unsupported')
    if unitless_units not in ('mm', 'cm', 'in'):
        raise DxfImportError('unitless_units must be mm, cm or in')
    if source_units_override not in ('auto', 'mm', 'cm', 'in'):
        raise DxfImportError('source_units_override must be auto, mm, cm or in')

    doc = _read_dxf(raw)
    header_code = int(doc.header.get('$INSUNITS', 0))
    source_bytes = raw.upper()
    # Mere string markers are hints only; do not assert exporter provenance.
    hints = [label for label, needle in (
        ('TUKACAD', b'TUKACAD'), ('TUKATECH', b'TUKATECH'),
        ('TUKAMARK', b'TUKAMARK'), ('AAMA', b'AAMA'), ('ASTM', b'ASTM'))
        if needle in source_bytes]
    model = Counter()
    inserts = Counter()
    block_content = Counter()
    layers = Counter()
    inspected = 0
    failed_insert_expansions = 0
    for entity in doc.modelspace():
        inspected += 1
        if inspected > MAX_SCAN_ENTITIES:
            raise DxfImportError('CAD entity inventory exceeds preflight limit')
        typ = entity.dxftype()
        model[typ] += 1
        layers[str(entity.dxf.get('layer', '0'))[:80]] += 1
        if typ == 'INSERT':
            inserts[str(entity.dxf.name)[:80]] += 1
            try:
                expanded = list(entity.virtual_entities())
            except Exception:
                failed_insert_expansions += 1
                expanded = []
            if len(expanded) + inspected > MAX_SCAN_ENTITIES:
                raise DxfImportError('CAD block content exceeds preflight limit')
            for item in expanded:
                inspected += 1
                block_content[item.dxftype()] += 1
                layers[str(item.dxf.get('layer', '0'))[:80]] += 1

    parsed = None
    import_issue = None
    try:
        parsed = parse_dxf_patterns(raw, safe_name, unitless_units, source_units_override)
    except DxfImportError as exc:
        import_issue = str(exc)

    metadata = None
    metadata_issue = None
    if parsed is not None:
        try:
            metadata = inspect_cad_metadata(raw=raw, filename=safe_name,
                                            unitless_units=unitless_units,
                                            source_units_override=source_units_override)
        except DxfImportError as exc:
            metadata_issue = str(exc)

    pieces = []
    if parsed is not None:
        for i, piece in enumerate(parsed['patterns'], 1):
            xs = [p[0] for p in piece['points']]
            ys = [p[1] for p in piece['points']]
            pieces.append({
                'index': i,
                'name': piece['name'],
                'source_size_label': piece.get('source_size'),
                'source_quantity_label': piece.get('source_quantity'),
                'source_material_label': piece.get('source_material'),
                'source_fold_label': piece.get('fold_text'),
                'bounds_mm': {'width': round(max(xs)-min(xs), 3),
                              'height': round(max(ys)-min(ys), 3)},
                'dimensions_independently_verified': False,
            })

    sizes = sorted({str(p['source_size_label']) for p in pieces if p['source_size_label']})
    missing_size = sum(not p['source_size_label'] for p in pieces)
    missing_quantity = sum(p['source_quantity_label'] is None for p in pieces)
    known_quantities = [p['source_quantity_label'] for p in pieces if isinstance(p['source_quantity_label'], int)]

    issues = []
    def issue(level, code, text):
        issues.append({'level': level, 'code': code, 'message': text})
    if failed_insert_expansions:
        issue('review', 'INSERT_EXPANSION_FAILED', f'{failed_insert_expansions} INSERT references could not be expanded; entity inventory may be incomplete.')
    if import_issue:
        issue('blocked', 'DXF_IMPORT_BLOCKED', import_issue)
    if metadata_issue:
        issue('review', 'METADATA_MAPPING_PARTIAL', f'Source entities cannot be completely associated with pattern blocks: {metadata_issue}')
    if source_units_override != 'auto':
        issue('review', 'MANUAL_UNIT_OVERRIDE', 'Header units were overridden. Independently confirm a pattern dimension in the originating CAD software.')
    elif header_code == 0:
        issue('review', 'UNITLESS_SOURCE', 'DXF does not declare units. Confirm scale from the source CAD system.')
    if header_code == 6 and source_units_override == 'mm':
        issue('review', 'METRE_HEADER_MM_OVERRIDE', 'DXF header declares metres, while geometry is interpreted as millimetres; factory CAD measurement is required.')
    if not hints:
        issue('review', 'EXPORTER_NOT_IDENTIFIED', 'No obvious Tukatech/AAMA/ASTM text markers detected; origin and exchange profile are unconfirmed.')
    else:
        issue('review', 'STRING_HINTS_ONLY', 'Vendor/standard terms found as strings; they do not prove software origin or standards compliance.')
    if missing_size:
        issue('review', 'SIZE_LABELS_MISSING', f'{missing_size} candidate pieces have no readable size label.')
    if missing_quantity:
        issue('review', 'QUANTITIES_MISSING', f'{missing_quantity} candidate pieces have no readable quantity label.')
    if len(sizes) < 2:
        issue('review', 'GRADING_NOT_DEMONSTRATED', 'One or zero distinct readable size labels; grading/size nest equivalence cannot be established.')
    else:
        issue('review', 'GRADING_UNVERIFIED', 'Multiple size labels exist, but grading rules and cross-size correspondence remain unverified.')
    if metadata is not None and metadata['unsupported_entity_count']:
        issue('review', 'UNSUPPORTED_SOURCE_ENTITIES', f"{metadata['unsupported_entity_count']} source entities are unsupported in Phase 11 preview export.")
    issue('review', 'CAD_SEMANTICS_UNVERIFIED', 'Cut/seam lines, grainlines, fold axes, notches, drill holes, fabric nap and shrinkage are not authenticated.')
    issue('review', 'REIMPORT_NOT_TESTED', 'A CAD operator must re-import the exported sample in TUKAmark and inspect each size and mark.')

    ready_for_experiment = parsed is not None and len(pieces) > 0
    return {
        'schema': 'autonester.tuka_exchange_preflight.v1',
        'phase': 14,
        'source_file': safe_name,
        'source_sha256': sha256(raw).hexdigest(),
        'profile': 'TUKAcad-first ASCII DXF diagnostic preflight',
        'source_hints_unverified': hints,
        'source_units': {
            'header_insunits_code': header_code,
            'header_name': UNIT_NAMES.get(header_code, f'unknown({header_code})'),
            'interpretation': parsed['source_units'] if parsed else None,
            'manual_override': source_units_override != 'auto',
            'independently_verified': False,
        },
        'inventory': {
            'modelspace': dict(sorted(model.items())),
            'insert_blocks': dict(sorted(inserts.items())),
            'block_entities': dict(sorted(block_content.items())),
            'layers': dict(sorted(layers.items())),
            'entities_scanned': inspected,
        },
        'patterns': pieces,
        'candidate_pattern_count': len(pieces),
        'distinct_size_labels': sizes,
        'labelled_quantity_sum': sum(known_quantities) if not missing_quantity and pieces else None,
        'metadata_preview_exportable_entities': metadata['exportable_entity_count'] if metadata else None,
        'metadata_preview_unsupported_entities': metadata['unsupported_entity_count'] if metadata else None,
        'import_readable_for_preview': ready_for_experiment,
        'import_failure': import_issue,
        'metadata_mapping_issue': metadata_issue,
        'issues': issues,
        'operator_checklist': [
            {'item': 'Original CAD verified physical dimensions and units', 'status': 'pending'},
            {'item': 'Size/grading and piece quantities verified by CAD operator', 'status': 'pending'},
            {'item': 'Cut/seam contours, notches, grainline and fold geometry reviewed', 'status': 'pending'},
            {'item': 'TUKAmark import/export compatibility independently checked', 'status': 'pending'},
            {'item': 'Production marker compared with factory reference', 'status': 'pending'},
        ],
        'aama_astm_compliant': False,
        'native_tuka_file_supported': False,
        'tukamark_reimport_verified': False,
        'factory_cutting_approved': False,
        'notes': 'Diagnostic review only, NOT FOR CUTTING. No vendor endorsement, proprietary support or standards compliance is implied.',
    }
