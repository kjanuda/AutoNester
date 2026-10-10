"""Phase 6 bounded contour-contact multi-start heuristic for AutoNester.

Search includes polygon vertex/edge-contact candidates plus order diversification and
an optional reposition pass. It is NOT an exact no-fit-polygon (NFP) algorithm.
No rotations not explicitly supplied are introduced, and no mirroring is done.
"""
from __future__ import annotations

from math import isfinite
from random import Random
from time import perf_counter
from typing import Any

from shapely.affinity import rotate, translate
from shapely.geometry import Polygon

from nesting_engine import (
    EPS, _can_place, _fast_place, _normalise, _validate_cutting_constraints,
    _validate_patterns, _width_height, Piece, Placement,
)
from optimization_compare import _layout_response, compare_layouts


def _anchors(poly: Polygon, limit: int = 11) -> list[tuple[float, float]]:
    """Limit sampled vertices; retain all bounding-box extremes as anchors."""
    pts = list(poly.exterior.coords)[:-1]
    if not pts:
        return []
    if len(pts) <= limit:
        return [(float(x), float(y)) for x, y in pts]
    ids = {0, len(pts) - 1}
    ids.update(round(i * (len(pts) - 1) / (limit - 1)) for i in range(limit))
    for dimension in (0, 1):
        ids.add(min(range(len(pts)), key=lambda i: pts[i][dimension]))
        ids.add(max(range(len(pts)), key=lambda i: pts[i][dimension]))
    return [tuple(map(float, pts[i])) for i in sorted(ids)]


def _contact_candidates(shape: Polygon, others: list[Placement],
                        fabric_width: float, used: float, gap: float,
                        fixed_side: str | None,
                        candidate_limit: int) -> list[tuple[float, float]]:
    """Bounded placement origins from polygon contacts, edges and fabric rails.

    True polygon intersection + Euclidean distance is tested separately for
    every candidate; anchors propose possibilities only, never relax geometry.
    """
    w, h = _width_height(shape)
    left_x = 0.0 if fixed_side != 'right' else fabric_width - w
    raw: set[tuple[float, float]] = set()

    def add(x: float, y: float) -> None:
        if not (isfinite(x) and isfinite(y)):
            return
        if x < -EPS or x > fabric_width - w + EPS or y < -EPS:
            return
        if fixed_side is not None and abs(x - left_x) > 1e-4:
            return
        if y > used + gap + EPS:
            return
        raw.add((round(max(0.0, x), 4), round(max(0.0, y), 4)))

    xrails = (left_x,) if fixed_side else (0.0, fabric_width - w)
    for x in xrails:
        add(x, 0.0)
        add(x, used + gap)
    own_pts = _anchors(shape, 9)
    # Limit geometry search for large material batches.
    recent = others[-(10 if len(others) <= 16 else 6):]
    for other in recent:
        a, b, c, d = other.polygon.bounds
        ys = (0.0, b, d + gap, b - h - gap, d - h)
        if fixed_side:
            for y in ys:
                add(left_x, y)
        else:
            for x in (0.0, fabric_width - w, a, c - w, c + gap, a - w - gap):
                for y in ys:
                    add(x, y)
            if len(others) <= 24:
                their_pts = _anchors(other.polygon, 9)
                for px, py in their_pts:
                    for qx, qy in own_pts:
                        # Vertices/edges introduce contact candidate origins.
                        # Actual 2 mm separation is verified geometrically.
                        add(px - qx, py - qy)
                        add(px - qx + gap, py - qy)
                        add(px - qx - gap, py - qy)
                        add(px - qx, py - qy + gap)
                        add(px - qx, py - qy - gap)
    # Prefer minimal new material length; keep a small extra exploration
    # budget from the right edge to avoid monopolization by invalid left sites.
    ordered = sorted(raw, key=lambda p: (round(max(used, p[1] + h), 4), p[1], p[0]))
    if len(ordered) <= candidate_limit:
        return ordered
    # Deterministic partition ensures candidate budget includes diverse x bands.
    initial = ordered[:int(candidate_limit * .7)]
    remaining = ordered[int(candidate_limit * .7):]
    spaced = remaining[::max(1, len(remaining) // max(1, candidate_limit - len(initial)))][:candidate_limit-len(initial)]
    return sorted(set(initial + spaced), key=lambda p: (max(used, p[1]+h), p[1], p[0]))


def _place_contour_order(order: list[Piece], width: float, gap: float,
                         enforce: bool, fold_side: str,
                         candidate_limit: int = 240) -> list[Placement]:
    placed: list[Placement] = []
    used = 0.0
    for piece in order:
        best: tuple[tuple[float, float, float, int], Polygon, int] | None = None
        for angle in piece.rotations:
            shape = _normalise(rotate(piece.polygon, angle, origin=(0, 0)))
            w, h = _width_height(shape)
            if w > width + EPS:
                continue
            side = fold_side if enforce and piece.fold_required else None
            for x, y in _contact_candidates(shape, placed, width, used, gap, side, candidate_limit):
                score = (round(max(used, y + h), 5), y, x, angle)
                if best is not None and score >= best[0]:
                    continue
                candidate = translate(shape, xoff=x, yoff=y)
                if _can_place(candidate, placed, gap):
                    best = score, candidate, angle
        if best is None:
            # New horizontal strip is always a geometrically safe fallback.
            for angle in piece.rotations:
                shape = _normalise(rotate(piece.polygon, angle, origin=(0, 0)))
                if _width_height(shape)[0] > width + EPS:
                    continue
                x = width - _width_height(shape)[0] if enforce and piece.fold_required and fold_side == 'right' else 0.0
                p = translate(shape, xoff=x, yoff=used + gap)
                if _can_place(p, placed, gap):
                    best = ((), p, angle)
                    break
        if best is None:
            raise ValueError(f'Cannot place {piece.name} inside usable fabric width')
        _, poly, angle = best
        placed.append(Placement(piece, poly, angle))
        used = max(used, poly.bounds[3])
    return placed


def _refine(layout: list[Placement], width: float, gap: float,
            enforce: bool, fold_side: str, candidate_limit: int) -> list[Placement]:
    """One bounded reposition pass; never alter an unapproved rotation."""
    result = list(layout)
    if len(result) > 18:
        return result
    for idx in range(len(result) - 1, -1, -1):
        current = result[idx]
        others = [p for j, p in enumerate(result) if j != idx]
        used = max((p.polygon.bounds[3] for p in others), default=0.0)
        shape = _normalise(rotate(current.piece.polygon, current.rotation, origin=(0, 0)))
        side = fold_side if enforce and current.piece.fold_required else None
        # Preserve current position in candidate set so reposition cannot worsen.
        candidates = _contact_candidates(shape, others, width, used, gap, side, candidate_limit)
        oldx, oldy, _, _ = current.polygon.bounds
        candidates.append((oldx, oldy))
        best_poly = current.polygon
        best_score = (max(used, current.polygon.bounds[3]), oldy, oldx)
        for x, y in candidates:
            score = (max(used, y + _width_height(shape)[1]), y, x)
            if score >= best_score:
                continue
            possible = translate(shape, xoff=x, yoff=y)
            if _can_place(possible, others, gap):
                best_poly, best_score = possible, score
        result[idx] = Placement(current.piece, best_poly, current.rotation)
    return result


def optimize_geometry(patterns: list[dict[str, Any]], fabric_width: float,
                      gap: float = 2.0, enforce_constraints: bool = False,
                      fabric_mode: str = 'flat', fabric_fold_side: str = 'left',
                      effort: str = 'standard', seed: int = 42) -> dict[str, Any]:
    """Multistart + contour contact with Phase5 baseline as a safe reference.

    Returns comparison-compatible trial data. Effort adjusts a bounded finite
    set of starts, not an unbounded expensive search. No production guarantee.
    """
    if effort not in ('quick', 'standard', 'deep'):
        raise ValueError('effort must be quick, standard or deep')
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 999999:
        raise ValueError('seed must be an integer from 0 to 999999')
    if fabric_mode not in ('flat', 'folded') or fabric_fold_side not in ('left', 'right'):
        raise ValueError('Invalid fabric/fold mode')
    pieces = _validate_patterns(patterns, fabric_width, gap)
    if enforce_constraints:
        _validate_cutting_constraints(pieces, fabric_mode, fabric_fold_side)

    t0 = perf_counter()
    baseline = compare_layouts(patterns, fabric_width, gap, enforce_constraints,
                               fabric_mode, fabric_fold_side, max_trials=7)
    reference_trial = baseline['trials'][0]
    best_base = baseline['trials'][baseline['best_trial_id']]
    candidate_limit = {'quick': 80, 'standard': 150, 'deep': 300}[effort]
    starts = {'quick': 3, 'standard': 8, 'deep': 16}[effort]
    if len(pieces) > 24:
        starts = min(starts, 6)
        candidate_limit = min(candidate_limit, 100)

    # Seeded permutations and deterministic geometry-aware sorts. Reverse/sort
    # seeds are distinct by piece identity (original_index and instance).
    orders = [pieces,
              sorted(pieces, key=lambda p: (-p.polygon.area, p.original_index, p.instance)),
              sorted(pieces, key=lambda p: (-_width_height(p.polygon)[1], p.original_index, p.instance)),
              sorted(pieces, key=lambda p: (-_width_height(p.polygon)[0], p.original_index, p.instance))]
    rng = Random(seed)
    for _ in range(starts):
        shuffled = list(pieces)
        rng.shuffle(shuffled)
        orders.append(shuffled)
    seen = set()
    trials = [{**reference_trial, 'id': 0}]
    # Show best Phase 5 ordering separately if it improves original baseline.
    if best_base['layout']['fabric_length'] < reference_trial['layout']['fabric_length']:
        trials.append({**best_base, 'id': len(trials), 'method': 'Phase 5 best ordering'})
    best_index = min(range(len(trials)), key=lambda i: trials[i]['layout']['fabric_length'])
    best_length = min(trial['layout']['fabric_length'] for trial in trials)
    attempts = 0
    for number, order in enumerate(orders):
        identity = tuple((p.original_index, p.instance) for p in order)
        if identity in seen:
            continue
        seen.add(identity)
        run_start = perf_counter()
        proposed = _place_contour_order(order, fabric_width, gap, enforce_constraints,
                                        fabric_fold_side, candidate_limit)
        if len(pieces) <= 18:
            proposed = _refine(proposed, fabric_width, gap, enforce_constraints,
                              fabric_fold_side, candidate_limit // 2)
        elapsed = (perf_counter() - run_start) * 1000
        attempts += 1
        length = max(p.polygon.bounds[3] for p in proposed)
        # Compare full precision where possible; baseline only exposes display
        # precision; do not claim a gain smaller than 0.01 mm.
        if length < best_length - 0.011:
            result = _layout_response(proposed, fabric_width, gap, enforce_constraints, fabric_mode)
            result['algorithm'] = 'contour-contact-multistart-v0.6'
            result['warnings'].append('Contour-contact candidates are an approximation, NOT an exact no-fit polygon solver.')
            trials.append({'id': len(trials), 'method': f'Geometric search #{attempts}',
                           'runtime_ms': round(elapsed, 2), 'layout': result})
            best_index = len(trials) - 1
            best_length = length
    reference_length = reference_trial['layout']['fabric_length']
    best = trials[best_index]['layout']
    gain = max(0.0, (reference_length - best['fabric_length']) / reference_length * 100) if reference_length > 0 else 0
    return {
        'version': '0.6.0', 'engine': 'bounded contour-contact multistart (not exact NFP)',
        'effort': effort, 'seed': seed, 'attempts_run': attempts,
        'trials_run': len(trials), 'total_runtime_ms': round((perf_counter()-t0)*1000, 2),
        'baseline_id': 0, 'best_trial_id': best_index,
        'baseline_length_mm': reference_length, 'best_length_mm': best['fabric_length'],
        'improvement_vs_input_order_percent': round(gain, 2),
        'trials': trials,
        'note': 'Only improved milestones are retained; all tested placements use exact polygon overlap and distance checks. Preview only, not production-ready.',
    }
