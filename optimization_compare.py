"""AutoNester v0.5 deterministic *heuristic* marker comparison.

Compare several bounded-search piece orders under the SAME fabric and cutting
constraints. No claims of global optimality, CAD validation, or production safety.
Internal placement primitives belong to the verified v0.4 core nesting engine.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter
from typing import Any

from nesting_engine import (
    EPS,
    MAX_PIECES,
    _fast_place,
    _validate_cutting_constraints,
    _validate_patterns,
    _width_height,
    Piece,
    Placement,
)


def _layout_response(layout: list[Placement], width: float, gap: float,
                     enforced: bool, fabric_mode: str) -> dict[str, Any]:
    # Use full-precision lengths for layout ranking; only round to display in API.
    length = max(item.polygon.bounds[3] for item in layout)
    used_area = sum(item.piece.polygon.area for item in layout)
    fabric_area = width * length
    utilization = 100.0 * used_area / fabric_area if fabric_area else 0.0
    return {
        'algorithm': 'multi-order-bounded-greedy-v0.5',
        'strategy': 'comparison',
        'constraints_enforced': enforced,
        'production_ready': False,
        'fabric_mode': fabric_mode,
        'fabric_width': width,
        'fabric_length': round(length, 2),
        'fabric_area': round(fabric_area, 2),
        'pattern_area': round(used_area, 2),
        'utilization_percent': round(utilization, 2),
        'waste_percent': round(100.0 - utilization, 2),
        'warnings': [
            'Preview only: imported DXF units, source geometry, fold/grainline, notches and cutting allowance need independent CAD verification.',
            'Optimization compares a limited set of heuristics and does not guarantee minimum fabric waste.',
            ('Operator-reviewed limited fold/grainline checks enabled; NOT production-certified.'
             if enforced else 'Fold/grainline rules NOT enforced. Do not cut from this layout.'),
        ],
        'placed_patterns': [
            {
                'name': p.piece.name,
                'instance': p.piece.instance,
                'rotation': p.rotation,
                'fold_required': p.piece.fold_required,
                'points': [[round(x, 4), round(y, 4)] for x, y in list(p.polygon.exterior.coords)[:-1]],
                'area': round(p.piece.polygon.area, 2),
            }
            for p in layout
        ],
    }


def compare_layouts(patterns: list[dict[str, Any]], fabric_width: float,
                    gap: float = 2.0, enforce_constraints: bool = False,
                    fabric_mode: str = 'flat', fabric_fold_side: str = 'left',
                    max_trials: int = 7) -> dict[str, Any]:
    """Produce a baseline plus deterministic alternatives and choose shortest marker.

    Returns full placements for each trial so a user can inspect alternatives.
    MAX_PIECES and fold verification constraints match /api/nesting/generate.
    """
    if isinstance(max_trials, bool) or not isinstance(max_trials, int) or not 1 <= max_trials <= 9:
        raise ValueError('max_trials must be an integer between 1 and 9')
    if fabric_mode not in ('flat', 'folded'):
        raise ValueError('fabric_mode must be flat or folded')
    if fabric_fold_side not in ('left', 'right'):
        raise ValueError('fabric_fold_side must be left or right')
    pieces = _validate_patterns(patterns, fabric_width, gap)
    if len(pieces) > MAX_PIECES:
        raise ValueError(f'At most {MAX_PIECES} pattern instances are supported')
    if enforce_constraints:
        _validate_cutting_constraints(pieces, fabric_mode, fabric_fold_side)

    # Trial 1 is an unconditional input-order baseline; other trials can never
    # make 'best' worse than this reference layout.
    definitions: list[tuple[str, list[Piece]]] = [
        ('Input order (baseline)', pieces),
        ('Largest area first', sorted(pieces, key=lambda p: (-p.polygon.area, p.original_index, p.instance))),
        ('Tallest first', sorted(pieces, key=lambda p: (-_width_height(p.polygon)[1], p.original_index, p.instance))),
        ('Widest first', sorted(pieces, key=lambda p: (-_width_height(p.polygon)[0], p.original_index, p.instance))),
        ('Smallest area first', sorted(pieces, key=lambda p: (p.polygon.area, p.original_index, p.instance))),
        ('Narrowest first', sorted(pieces, key=lambda p: (_width_height(p.polygon)[0], p.original_index, p.instance))),
        ('Fold-required first', sorted(pieces, key=lambda p: (not p.fold_required, -p.polygon.area, p.original_index, p.instance))),
        ('Reverse input order', list(reversed(pieces))),
        ('Longest edge first', sorted(pieces, key=lambda p: (-max(_width_height(p.polygon)), p.original_index, p.instance))),
    ]
    # Avoid evaluating exact duplicate orderings, but always preserve first baseline.
    observed: set[tuple[tuple[int, int], ...]] = set()
    trials: list[dict[str, Any]] = []
    winning_length = float('inf')
    winning_index = 0
    total_start = perf_counter()
    for name, order in definitions:
        key = tuple((p.original_index, p.instance) for p in order)
        if key in observed:
            continue
        observed.add(key)
        started = perf_counter()
        layout = _fast_place(order, fabric_width, gap, enforce_constraints, fabric_fold_side)
        runtime_ms = (perf_counter() - started) * 1000
        true_length = max(p.polygon.bounds[3] for p in layout)
        result = _layout_response(layout, fabric_width, gap, enforce_constraints, fabric_mode)
        trials.append({
            'id': len(trials), 'method': name,
            'runtime_ms': round(runtime_ms, 2),
            'layout': result,
        })
        if true_length < winning_length - EPS:
            winning_length = true_length
            winning_index = len(trials) - 1
        if len(trials) >= max_trials:
            break
    baseline = trials[0]['layout']
    winner = trials[winning_index]['layout']
    improvement = max(0.0, (baseline['fabric_length'] - winner['fabric_length']) /
                      baseline['fabric_length'] * 100) if baseline['fabric_length'] > 0 else 0.0
    return {
        'version': '0.5.0',
        'trials_run': len(trials),
        'total_runtime_ms': round((perf_counter() - total_start) * 1000, 2),
        'baseline_id': 0,
        'best_trial_id': winning_index,
        'improvement_vs_input_order_percent': round(improvement, 2),
        'baseline_length_mm': baseline['fabric_length'],
        'best_length_mm': winner['fabric_length'],
        'trials': trials,
    }
