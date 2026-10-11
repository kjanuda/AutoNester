"""Phase 15: source-verified *diagnostic* material-marker previews.

No fabric semantics are inferred from CAD labels. The operator must explicitly
map each original source label to a fabric group; nothing is factory approved.
Layouts use only geometry re-parsed from the uploaded DXF, never client vertices.
"""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
from math import isfinite
import re

from dxf_import import parse_dxf_patterns
from layout_audit import audit_layout
from nesting_engine import generate_nesting, MAX_PIECES

MAX_GROUPS = 12
GROUP_ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9 _.-]{0,39}$')


def source_label(piece: dict) -> str:
    """Display grouping labels as found in CAD, not semantic classifications."""
    label = str(piece.get('source_material') or '').strip()
    return label if label else 'UNLABELED'


def _finite_positive(value, label: str, maximum=20000.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a numeric value')
    val = float(value)
    if not isfinite(val) or val <= 0 or val > maximum:
        raise ValueError(f'{label} must be greater than zero and at most {maximum:g}')
    return val


def _read_source(raw, filename, unitless_units, source_units_override):
    imported = parse_dxf_patterns(raw, filename, unitless_units, source_units_override)
    labels = defaultdict(lambda: {'patterns': 0, 'labelled_quantity': 0, 'pattern_names': []})
    for p in imported['patterns']:
        info = labels[source_label(p)]
        info['patterns'] += 1
        info['labelled_quantity'] += p.get('source_quantity', 1)
        info['pattern_names'].append(p['name'])
    return imported, labels


def inspect_materials(raw, filename, unitless_units='mm', source_units_override='auto',
                      reference_pattern_index=None, reference_axis='width', reference_mm=None,
                      tolerance_mm=1.0):
    imported, labels = _read_source(raw, filename, unitless_units, source_units_override)
    reference = {'status': 'not_provided', 'note': 'Provide an independently measured dimension from original CAD.'}
    if reference_mm is not None or reference_pattern_index is not None:
        if reference_mm is None or reference_pattern_index is None:
            raise ValueError('Reference index and measurement must be supplied together')
        if type(reference_pattern_index) is not int or not 1 <= reference_pattern_index <= len(imported['patterns']):
            raise ValueError('Invalid reference pattern index')
        if reference_axis not in ('width', 'height'):
            raise ValueError('Reference axis must be width or height')
        ref = _finite_positive(reference_mm, 'Reference dimension')
        tol = _finite_positive(tolerance_mm, 'Tolerance', 100.0)
        selected = imported['patterns'][reference_pattern_index-1]
        coords = selected['points']
        index = 0 if reference_axis == 'width' else 1
        measured = max(pt[index] for pt in coords) - min(pt[index] for pt in coords)
        delta = abs(measured - ref)
        reference = {'status': 'matched' if delta <= tol else 'mismatch',
                     'pattern_name': selected['name'], 'axis': reference_axis,
                     'candidate_mm': round(measured, 5), 'independent_reference_mm': ref,
                     'absolute_difference_mm': round(delta, 5), 'tolerance_mm': tol,
                     'note': 'A single dimension match does not certify DXF units or other patterns.'}
    return {
        'schema': 'autonester.material_review.v1', 'phase': 15,
        'source_file': imported['file_name'], 'source_sha256': imported['source_dxf_sha256'],
        'source_units': imported['source_units'],
        'manual_unit_override': source_units_override != 'auto',
        'reference_check': reference,
        'pattern_count': imported['pattern_count'],
        'source_material_labels': [{ 'label': label, **summary }
                                  for label, summary in sorted(labels.items())],
        'patterns': [{'name': p['name'], 'source_material': source_label(p),
                      'source_size': p.get('source_size'),
                      'source_quantity': p.get('source_quantity'),
                      'preview_quantity': 1}
                     for p in imported['patterns']],
        'issues': [
            'Source material codes are unverified text labels, not authenticated fabric types.',
            'Operator must explicitly confirm mappings before grouping pieces.',
            *(['Source units manually overridden; independently verify dimensions in CAD.'] if source_units_override != 'auto' else []),
            *(['Reference dimension mismatch; do not proceed as if units were validated.'] if reference['status'] == 'mismatch' else []),
            'Cut/seam/grainline/fold/notch meanings and production rules are not certified.'
        ],
        'material_semantics_verified': False,
        'factory_cutting_approved': False,
    }


def build_material_previews(raw, filename, unitless_units, source_units_override,
                            selected, assignments, fabric_width, gap,
                            widths_by_group=None, reference_pattern_index=None,
                            reference_axis='width', reference_mm=None, tolerance_mm=1.0):
    review = inspect_materials(raw, filename, unitless_units, source_units_override,
                               reference_pattern_index, reference_axis, reference_mm, tolerance_mm)
    if review['reference_check']['status'] == 'mismatch':
        raise ValueError('Independent CAD reference dimension does not match source interpretation')
    if not isinstance(selected, list) or not selected:
        raise ValueError('Select one or more source pattern instances')
    if not isinstance(assignments, list):
        raise ValueError('Material assignments must be a list')
    if not isinstance(widths_by_group, dict) and widths_by_group is not None:
        raise ValueError('Group widths must be a JSON object')
    default_width = _finite_positive(fabric_width, 'Fabric width')
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not isfinite(float(gap)) or not 0 <= gap <= 100:
        raise ValueError('Gap must be between 0 and 100 mm')
    imported, _ = _read_source(raw, filename, unitless_units, source_units_override)
    source_patterns = {p['name']: p for p in imported['patterns']}
    wanted = {}
    total_instances = 0
    for item in selected:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            raise ValueError('Each selected pattern requires a source name')
        name, count = item['name'], item.get('quantity')
        if name not in source_patterns or name in wanted:
            raise ValueError('Unknown or duplicated pattern source name')
        if type(count) is not int or not 1 <= count <= MAX_PIECES:
            raise ValueError('Pattern quantity must be an integer between 1 and 60')
        wanted[name] = count
        total_instances += count
    if total_instances > MAX_PIECES:
        raise ValueError(f'At most {MAX_PIECES} total instances per request')

    originals_used = {source_label(source_patterns[name]) for name in wanted}
    confirmed = {}
    if len(assignments) > MAX_GROUPS or len(originals_used) > MAX_GROUPS:
        raise ValueError('Too many material labels for a preview')
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise ValueError('Malformed material assignment')
        origin = assignment.get('source_label')
        group = assignment.get('group_id')
        if not isinstance(origin, str) or origin not in originals_used or origin in confirmed:
            raise ValueError('Unexpected or duplicated source material label')
        if not isinstance(group, str) or not GROUP_ID.fullmatch(group):
            raise ValueError('Use 1-40 safe characters for fabric group names')
        if assignment.get('operator_confirmed') is not True:
            raise ValueError('Confirm source material labels with a CAD operator before generating previews')
        confirmed[origin] = group
    if set(confirmed) != originals_used:
        raise ValueError('Every selected source material label needs an explicit confirmed group')

    groups = defaultdict(list)
    for name, count in wanted.items():
        original = source_patterns[name]
        # Never use client-supplied points, rotations or fold verification flags.
        safe = {**original, 'quantity': count, 'allowed_rotations': [0],
                'fold_verified': False, 'grainline_verified': False, 'fold_status_verified': False}
        groups[confirmed[source_label(original)]].append(safe)
    widths_by_group = widths_by_group or {}
    if set(widths_by_group) - set(groups):
        raise ValueError('Width provided for an unknown fabric group')
    results = []
    for group, pieces in sorted(groups.items()):
        width = _finite_positive(widths_by_group.get(group, default_width), f'Width for {group}')
        try:
            layout = generate_nesting(patterns=pieces, fabric_width=width, gap=gap,
                                      strategy='fast', fabric_mode='flat', enforce_constraints=False)
            audit = audit_layout(patterns=pieces, layout=layout, fabric_width=width,
                                 gap=gap, enforce_constraints=False, fabric_mode='flat')
        except ValueError as error:
            raise ValueError(f'Group {group}: {error}') from error
        if not audit['passed']:
            raise ValueError(f'Group {group}: independent geometry audit failed')
        results.append({
            'group_id': group, 'source_material_labels': sorted(label for label, g in confirmed.items() if g == group),
            'fabric_width_mm': width, 'fabric_length_mm': layout['fabric_length'],
            'utilization_percent': layout['utilization_percent'],
            'waste_percent': layout['waste_percent'],
            'instances': len(layout['placed_patterns']),
            'geometry_audit': {'passed': audit['passed'],
                               'minimum_clearance_mm': audit['minimum_clearance_mm'],
                               'layout_fingerprint': audit['layout_fingerprint']},
            'layout': layout,
            'cutting_approved': False,
        })
    return {
        'schema': 'autonester.material_preview.v1', 'phase': 15,
        'source_file': imported['file_name'], 'source_sha256': sha256(raw).hexdigest(),
        'source_units': imported['source_units'],
        'manual_unit_override': source_units_override != 'auto',
        'reference_check': review['reference_check'],
        'selected_pattern_types': len(wanted), 'selected_instances': total_instances,
        'group_count': len(results), 'groups': results,
        'warnings': [
            'DIAGNOSTIC PREVIEW ONLY — NOT FOR CUTTING.',
            'Source material label interpretation was manually asserted; independent source CAD review is still required.',
            'All rotations constrained to 0 degrees for this preview; grainline/fold rules not certified.',
            'No combined utilization percentage across distinct fabric groups.',
            *(['Source units were manually overridden; independently verify original CAD measurements.'] if source_units_override != 'auto' else []),
        ],
        'production_approved': False,
    }
