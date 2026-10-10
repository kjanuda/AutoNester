"""Independent polygon-layout audit and deterministic replay benchmark (Phase 7).

This audits geometry represented by the API's rounded DXF/polygon preview, NOT
source CAD semantics. PASS is not approval for manufacturing/cutting.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from json import dumps
from math import isfinite
from time import perf_counter
from typing import Any

from shapely.affinity import rotate
from shapely.geometry import Polygon
from nesting_engine import _normalise, _validate_patterns, _validate_cutting_constraints
from geometric_optimizer import optimize_geometry

# Exported API points are rounded to 4 decimals; compensate for worst-case
# accumulated coordinate roundoff while still rejecting material violations.
GEOM_TOL_MM = 0.002
LENGTH_TOL_MM = 0.02


def _digest(value: Any) -> str:
    return sha256(dumps(value, sort_keys=True, separators=(',', ':'),
                        ensure_ascii=True, allow_nan=False).encode('utf-8')).hexdigest()


def _canonical_layout(layout: dict[str, Any]) -> dict[str, Any]:
    """Geometry-only fingerprint; exclude nondeterministic timing/labels."""
    return {
        'fabric_width': round(float(layout['fabric_width']), 4),
        'fabric_length': round(float(layout['fabric_length']), 2),
        'placements': sorted([
            {
                'name': str(p['name']),
                'instance': int(p['instance']),
                'rotation': int(p['rotation']),
                'points': [[round(float(x), 4), round(float(y), 4)]
                           for x, y in p['points']],
            }
            for p in layout['placed_patterns']
        ], key=lambda p: (p['name'], p['instance'], p['rotation'], p['points']))
    }


def audit_layout(patterns: list[dict[str, Any]], layout: dict[str, Any],
                 fabric_width: float, gap: float = 2.0,
                 enforce_constraints: bool = False, fabric_mode: str = 'flat',
                 fabric_fold_side: str = 'left') -> dict[str, Any]:
    """Verify all output instance identities, shapes, rails and pair distances.

    Avoid trusting fields like area/rotation/constraints_enforced from output.
    Checks are done independently using Shapely, from input polygon geometry.
    """
    pieces = _validate_patterns(patterns, fabric_width, gap)
    if enforce_constraints:
        _validate_cutting_constraints(pieces, fabric_mode, fabric_fold_side)
    errors: list[str] = []
    expected = {(p.name, p.instance): p for p in pieces}
    if len(expected) != len(pieces):
        errors.append('Duplicate (name, instance) identifiers in source patterns; audit cannot uniquely match pieces')

    outlines: list[tuple[Polygon, Any]] = []
    counts: Counter = Counter()
    raw = layout.get('placed_patterns', [])
    if not isinstance(raw, list):
        errors.append('placed_patterns is not a list')
        raw = []
    if len(raw) != len(pieces):
        errors.append(f'Expected {len(pieces)} instances; layout contains {len(raw)}')

    for number, p in enumerate(raw, 1):
        if not isinstance(p, dict):
            errors.append(f'Instance #{number}: expected a placement object')
            continue
        key = (p.get('name'), p.get('instance'))
        counts[key] += 1
        source = expected.get(key)
        if source is None:
            errors.append(f'Instance #{number}: unknown piece identity {key!r}')
        elif counts[key] > 1:
            errors.append(f'Instance #{number}: duplicate placement identity {key!r}')
        try:
            coords = p['points']
            if not isinstance(coords, list) or len(coords) < 3 or len(coords) > 80:
                raise ValueError('invalid polygon vertex count')
            if any(len(xy) != 2 or any(not isfinite(float(a)) for a in xy) for xy in coords):
                raise ValueError('non-finite or invalid polygon point')
            poly = Polygon(coords)
            if not poly.is_valid or poly.area <= 1e-7:
                raise ValueError('invalid/degenerate outline')
            angle = p['rotation']
            if isinstance(angle, bool) or not isinstance(angle, int):
                raise ValueError('rotation must be integer degrees')
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            errors.append(f'Instance #{number}: {exc}')
            continue

        minx, miny, maxx, maxy = poly.bounds
        if minx < -GEOM_TOL_MM or miny < -GEOM_TOL_MM or maxx > fabric_width + GEOM_TOL_MM:
            errors.append(f'Instance #{number}: exceeds usable fabric bounds')
        if source is not None:
            if angle not in source.rotations:
                errors.append(f'Instance #{number}: unapproved rotation {angle} degrees')
            else:
                expected_shape = _normalise(rotate(source.polygon, angle, origin=(0, 0)))
                received_shape = _normalise(poly)
                if expected_shape.hausdorff_distance(received_shape) > GEOM_TOL_MM:
                    errors.append(f'Instance #{number}: geometry differs from original polygon')
            if enforce_constraints and source.fold_required:
                rail = minx if fabric_fold_side == 'left' else maxx
                expected_rail = 0.0 if fabric_fold_side == 'left' else fabric_width
                if abs(rail - expected_rail) > GEOM_TOL_MM:
                    errors.append(f'Instance #{number}: required fold edge is not on verified fabric rail')
        outlines.append((poly, key))

    for key in expected:
        if counts[key] == 0:
            errors.append(f'Missing source piece {key!r}')
    min_clearance: float | None = None
    for index, (a, aid) in enumerate(outlines):
        for b, bid in outlines[index + 1:]:
            intersection = a.intersection(b).area
            if intersection > 0.0001:
                errors.append(f'Overlap between {aid!r} and {bid!r}: {intersection:.4f} mm2')
            distance = a.distance(b)
            min_clearance = distance if min_clearance is None else min(min_clearance, distance)
            if distance < gap - GEOM_TOL_MM:
                errors.append(f'Insufficient gap between {aid!r} and {bid!r}: {distance:.4f} mm vs {gap} mm')

    try:
        reported_length = float(layout['fabric_length'])
        actual_length = max((p.bounds[3] for p, _ in outlines), default=0.0)
        if not isfinite(reported_length) or abs(reported_length - actual_length) > LENGTH_TOL_MM:
            errors.append('Fabric length metric does not match actual placed geometry')
        if abs(float(layout['fabric_width']) - fabric_width) > GEOM_TOL_MM:
            errors.append('Fabric width metric differs from requested usable width')
        expected_area = sum(p.polygon.area for p in pieces)
        if abs(float(layout['pattern_area']) - expected_area) > max(0.05, expected_area * .00005):
            errors.append('Pattern area metric does not match all input instances')
        expected_fabric_area = fabric_width * actual_length
        if abs(float(layout['fabric_area']) - expected_fabric_area) > max(0.05, expected_fabric_area * .00005):
            errors.append('Fabric area metric does not match actual dimensions')
        expected_utilization = 100.0 * expected_area / expected_fabric_area if expected_fabric_area else 0.0
        if abs(float(layout['utilization_percent']) - expected_utilization) > 0.03:
            errors.append('Utilization metric does not match input polygon areas')
        if abs(float(layout['waste_percent']) - (100.0 - expected_utilization)) > 0.03:
            errors.append('Waste metric does not match input polygon areas')
    except (KeyError, ValueError, TypeError, OverflowError):
        errors.append('Invalid or missing fabric measurement fields')

    try:
        fingerprint = _digest(_canonical_layout(layout))
    except (KeyError, ValueError, TypeError, OverflowError):
        fingerprint = None
        errors.append('Unable to compute layout fingerprint')
    return {
        'passed': not errors,
        'production_approved': False,
        'instances_checked': len(outlines),
        'minimum_clearance_mm': round(min_clearance, 4) if min_clearance is not None else None,
        'layout_fingerprint': fingerprint,
        'issues': errors[:30],
        'note': 'Geometry-only preview audit: NOT independent source CAD, grainline, notches, graded sizes or physical cutting certification.',
    }


def replay_benchmark(patterns: list[dict[str, Any]], fabric_width: float,
                     gap: float = 2.0, enforce_constraints: bool = False,
                     fabric_mode: str = 'flat', fabric_fold_side: str = 'left',
                     effort: str = 'quick', seed: int = 42) -> dict[str, Any]:
    """Two same-seed replays, and a third run with an alternate seed.

    Compare stable geometry hashes (never wall-clock times). Return only the
    best audited layout across runs. A failure is reported, not hidden.
    """
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 or seed > 999998:
        raise ValueError('benchmark seed must be between 0 and 999998')
    input_hash = _digest({
        'patterns': patterns, 'width': fabric_width, 'gap': gap,
        'enforce': enforce_constraints, 'mode': fabric_mode, 'side': fabric_fold_side,
        'effort': effort,
    })
    start = perf_counter()
    runs = []
    best = None
    for run_index, current_seed in enumerate((seed, seed, seed + 1), 1):
        elapsed_start = perf_counter()
        experiment = optimize_geometry(patterns, fabric_width, gap, enforce_constraints,
                                       fabric_mode, fabric_fold_side, effort, current_seed)
        winning = experiment['trials'][experiment['best_trial_id']]['layout']
        audit = audit_layout(patterns, winning, fabric_width, gap, enforce_constraints,
                             fabric_mode, fabric_fold_side)
        runs.append({
            'run': run_index, 'seed': current_seed,
            'best_length_mm': winning['fabric_length'],
            'utilization_percent': winning['utilization_percent'],
            'runtime_ms': round((perf_counter() - elapsed_start) * 1000, 2),
            'geometry_hash': audit['layout_fingerprint'],
            'audit_passed': audit['passed'],
            'issues': audit['issues'],
        })
        if audit['passed'] and (best is None or winning['fabric_length'] < best[0]):
            best = (winning['fabric_length'], run_index, winning, audit)
    identical = runs[0]['geometry_hash'] is not None and runs[0]['geometry_hash'] == runs[1]['geometry_hash']
    all_valid = all(r['audit_passed'] for r in runs)
    return {
        'version': '0.7.0', 'input_sha256': input_hash,
        'same_seed_reproducible': identical,
        'all_geometry_audits_passed': all_valid,
        'production_approved': False,
        'runs': runs,
        'best_run': best[1] if best else None,
        'best_layout': best[2] if best else None,
        'best_audit': best[3] if best else None,
        'total_runtime_ms': round((perf_counter() - start) * 1000, 2),
        'note': 'Two identical-seed replays and one alternate-seed run. Timing varies. Geometry audit is not CAD/cutting certification.',
    }
