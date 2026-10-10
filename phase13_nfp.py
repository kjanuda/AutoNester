"""AutoNester Phase 13: bounded *partial* no-fit-polygon search.

Exact Minkowski-derived NFP boundaries are built for small simple polygons
(convex directly; selected concave polygons by ear-clipping). For large or
numerically awkward shapes, bounded vertex/contact heuristics are used instead.
Every accepted layout passes the independent Phase 7 geometry audit.
This is PREVIEW research, not a complete industrial NFP implementation.
"""
from __future__ import annotations

from math import hypot, isfinite
from random import Random
from time import perf_counter
from typing import Any

from shapely.affinity import rotate, translate
from shapely.geometry import MultiPoint, Point, Polygon
from shapely.ops import unary_union

from nesting_engine import (
    EPS, _can_place, _normalise, _validate_cutting_constraints,
    _validate_patterns, _width_height, Placement, Piece,
)
from geometric_optimizer import _contact_candidates
from layout_audit import audit_layout
from optimization_compare import _layout_response
from nesting_engine import generate_nesting


def _cross(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0]-a[0]) * (c[1]-a[1]) - (b[1]-a[1]) * (c[0]-a[0])


def _triangles(polygon: Polygon, max_vertices: int = 22) -> list[list[tuple[float, float]]] | None:
    """Convex decomposition by ear clipping of a hole-free simple polygon.

    Limit complexity for user-uploaded DXFs; abort instead of guessing when
    collinear/numerical geometry is not robust enough for this method.
    """
    if polygon.interiors:
        return None
    pts = [(float(x), float(y)) for x, y in list(polygon.exterior.coords)[:-1]]
    if len(pts) > max_vertices or len(pts) < 3:
        return None
    # Collapse adjacent duplicates and nearly collinear redundant vertices.
    cleaned: list[tuple[float, float]] = []
    for p in pts:
        if not cleaned or hypot(p[0]-cleaned[-1][0], p[1]-cleaned[-1][1]) > 1e-8:
            cleaned.append(p)
    pts = cleaned
    if len(pts) < 3:
        return None
    if polygon.equals(polygon.convex_hull):
        return [pts]
    area2 = sum(pts[i][0]*pts[(i+1)%len(pts)][1] - pts[(i+1)%len(pts)][0]*pts[i][1]
                for i in range(len(pts)))
    if area2 < 0:
        pts.reverse()
    result = []
    active = list(range(len(pts)))
    iterations = 0
    while len(active) > 3:
        iterations += 1
        if iterations > len(pts)*len(pts):
            return None
        found = False
        for k in range(len(active)):
            a, b, c = active[k-1], active[k], active[(k+1)%len(active)]
            if _cross(pts[a], pts[b], pts[c]) <= 1e-9:
                continue
            tri = Polygon([pts[a],pts[b],pts[c]])
            if tri.area <= 1e-9:
                continue
            if any(idx not in (a,b,c) and (tri.contains(Point(pts[idx])) or tri.touches(Point(pts[idx])))
                   for idx in active):
                continue
            result.append([pts[a], pts[b], pts[c]])
            active.pop(k)
            found = True
            break
        if not found:
            return None
    result.append([pts[idx] for idx in active])
    # Ear clipping must recover the polygon exactly enough for candidate generation.
    union = unary_union([Polygon(t) for t in result])
    if abs(union.area-polygon.area) > max(1e-5, polygon.area*1e-8):
        return None
    return result


def _nfp(stationary: Polygon, moving: Polygon) -> Polygon | None:
    """Collision-translation set A + (-B), i.e. a no-fit polygon region.

    Computes convex Minkowski sums over convex pieces. NFP is used ONLY as
    candidate geometry; clearance and validity are independently checked.
    """
    a = _triangles(stationary)
    b = _triangles(moving)
    if a is None or b is None or len(a)*len(b) > 110:
        return None
    hulls = []
    for left in a:
        for right in b:
            hull = MultiPoint([(ax-bx, ay-by) for ax, ay in left for bx, by in right]).convex_hull
            if not isinstance(hull, Polygon) or hull.area <= 0:
                return None
            hulls.append(hull)
    region = unary_union(hulls)
    if not isinstance(region, Polygon) or region.is_empty:
        return None
    return region


def _nfp_offsets(stationary: Polygon, moving: Polygon, gap: float,
                 max_sites: int = 35) -> list[tuple[float, float]] | None:
    region = _nfp(stationary, moving)
    if region is None:
        return None
    # Safe outward margin: four-decimal DXF layout coords must not eliminate gap.
    boundary = region.buffer(gap + 0.015, quad_segs=6).exterior
    coords = list(boundary.coords)[:-1]
    if not coords:
        return []
    # Capture extrema, vertex landmarks, and uniformly sampled arc-length sites.
    ids = {0, len(coords)-1}
    ids.update(round(i*(len(coords)-1)/(min(max_sites, len(coords))-1))
               for i in range(min(max_sites, len(coords))) if min(max_sites, len(coords)) > 1)
    return [tuple(coords[i]) for i in sorted(ids)]


def _placement_candidates(moving: Polygon, placed: list[Placement],
                          width: float, used: float, gap: float,
                          fold_side: str | None, budget: int,
                          stats: dict[str, int]) -> list[tuple[float, float]]:
    w, h = _width_height(moving)
    base = _contact_candidates(moving, placed, width, used, gap, fold_side, max(35, budget//2))
    positions = set(base)
    for other in placed[-4:]:
        offsets = _nfp_offsets(other.polygon, moving, gap, max_sites=27)
        if offsets is None:
            stats['nfp_fallback_pairs'] += 1
            continue
        stats['nfp_exact_pairs'] += 1
        for x, y in offsets:
            if not (isfinite(x) and isfinite(y)):
                continue
            if x < -EPS or x > width-w+EPS or y < -EPS or y > used+gap+EPS:
                continue
            if fold_side == 'left' and abs(x) > 1e-4:
                continue
            if fold_side == 'right' and abs(x-(width-w)) > 1e-4:
                continue
            positions.add((round(max(0., x), 4), round(max(0.,y), 4)))
    ranked = sorted(positions, key=lambda p: (round(max(used, p[1]+h), 4), p[1], p[0]))
    if len(ranked) > budget:
        preferred = ranked[:max(1, int(budget*0.7))]
        tail = ranked[len(preferred):]
        sample = tail[::max(1, len(tail)//max(1, budget-len(preferred)))][:budget-len(preferred)]
        ranked = sorted(set(preferred+sample), key=lambda p: (max(used, p[1]+h), p[1], p[0]))
    stats['candidate_origins'] += len(ranked)
    return ranked


def _place_order(order: list[Piece], width: float, gap: float,
                 enforce: bool, side: str, budget: int,
                 stats: dict[str, int]) -> list[Placement]:
    placed = []
    used = 0.0
    for piece in order:
        best = None
        for angle in piece.rotations:
            moving = _normalise(rotate(piece.polygon, angle, origin=(0,0)))
            w, h = _width_height(moving)
            if w > width + EPS:
                continue
            constrained_side = side if enforce and piece.fold_required else None
            origins = _placement_candidates(moving, placed, width, used, gap,
                                           constrained_side, budget, stats)
            for x, y in origins:
                score = (round(max(used, y+h), 6), y, x, angle)
                if best is not None and score >= best[0]:
                    continue
                candidate = translate(moving, xoff=x, yoff=y)
                stats['candidate_checks'] += 1
                if _can_place(candidate, placed, gap):
                    best = score, candidate, angle
        if best is None:
            for angle in piece.rotations:
                moving = _normalise(rotate(piece.polygon, angle, origin=(0,0)))
                w, _ = _width_height(moving)
                if w > width + EPS:
                    continue
                x = width-w if enforce and piece.fold_required and side == 'right' else 0.0
                poly = translate(moving, xoff=x, yoff=used+gap)
                if _can_place(poly, placed, gap):
                    best = ((), poly, angle)
                    break
        if best is None:
            raise ValueError(f'Cannot place {piece.name} inside usable fabric width')
        _, poly, angle = best
        placed.append(Placement(piece, poly, angle))
        used = max(used, poly.bounds[3])
    return placed


def optimize_nfp_hybrid(patterns: list[dict[str, Any]], fabric_width: float,
                        gap: float = 2.0, enforce_constraints: bool = False,
                        fabric_mode: str = 'flat', fabric_fold_side: str = 'left',
                        effort: str = 'quick', seed: int = 42) -> dict[str, Any]:
    if effort not in ('quick', 'standard', 'deep'):
        raise ValueError('effort must be quick, standard or deep')
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 999999:
        raise ValueError('seed must be an integer from 0 to 999999')
    if fabric_mode not in ('flat','folded') or fabric_fold_side not in ('left','right'):
        raise ValueError('Invalid fabric mode or fold side')
    started = perf_counter()
    pieces = _validate_patterns(patterns, fabric_width, gap)
    if enforce_constraints:
        _validate_cutting_constraints(pieces, fabric_mode, fabric_fold_side)

    baseline = generate_nesting(patterns, fabric_width, gap, strategy='fast',
                                enforce_constraints=enforce_constraints,
                                fabric_mode=fabric_mode, fabric_fold_side=fabric_fold_side)
    base_audit = audit_layout(patterns, baseline, fabric_width, gap, enforce_constraints,
                              fabric_mode, fabric_fold_side)
    if not base_audit['passed']:
        raise ValueError('Reference layout did not pass independent geometry audit')
    winner = baseline
    winner_audit = base_audit
    trials = [{'method': 'Existing Fast baseline', 'length_mm': baseline['fabric_length'], 'audit_passed': True}]
    stats = {'candidate_origins': 0, 'candidate_checks': 0,
             'nfp_exact_pairs': 0, 'nfp_fallback_pairs': 0}
    rng = Random(seed)
    orders = [sorted(pieces, key=lambda p: (-p.polygon.area,p.original_index,p.instance)),
              sorted(pieces, key=lambda p: (-_width_height(p.polygon)[1],p.original_index,p.instance)),
              list(pieces)]
    if effort in ('standard','deep'):
        copy = list(pieces); rng.shuffle(copy); orders.append(copy)
    if effort == 'deep':
        copy = list(pieces); rng.shuffle(copy); orders.append(copy)
    if effort == 'quick':
        orders = orders[:2]
    if len(pieces)>24:
        orders=orders[:2]
    budget = {'quick': 75, 'standard': 125, 'deep': 190}[effort]
    if len(pieces)>18:
        budget = min(budget, 75)
    seen = set()
    for idx, order in enumerate(orders):
        identity = tuple((p.original_index,p.instance) for p in order)
        if identity in seen:
            continue
        seen.add(identity)
        candidate = _place_order(order, fabric_width, gap, enforce_constraints,
                                 fabric_fold_side, budget, stats)
        candidate_layout = _layout_response(candidate, fabric_width, gap,
                                            enforce_constraints, fabric_mode)
        candidate_layout['algorithm'] = 'bounded-hybrid-partial-nfp-v0.13'
        candidate_layout['warnings'].append('Exact NFP constructed only for limited geometry; fallback contacts used otherwise.')
        audit = audit_layout(patterns, candidate_layout, fabric_width, gap, enforce_constraints,
                             fabric_mode, fabric_fold_side)
        trials.append({'method': f'Partial NFP / contact ordering {idx+1}',
                       'length_mm': candidate_layout['fabric_length'], 'audit_passed': audit['passed']})
        if audit['passed'] and candidate_layout['fabric_length'] < winner['fabric_length'] - 0.011:
            winner, winner_audit = candidate_layout, audit

    baseline_mm = baseline['fabric_length']
    best_mm = winner['fabric_length']
    return {
        'version':'0.13.0',
        'engine':'Bounded hybrid: limited exact NFP + fallback contour contacts (not industrial exact NFP)',
        'production_ready':False, 'effort':effort, 'seed':seed,
        'pattern_instances':len(pieces),
        'baseline_length_mm':baseline_mm, 'best_length_mm':best_mm,
        'improvement_percent':round(100*(baseline_mm-best_mm)/baseline_mm,2) if baseline_mm else 0.,
        'baseline_layout':baseline, 'best_layout':winner,
        'baseline_audit':base_audit, 'best_audit':winner_audit,
        'trials':trials, 'search_stats':stats,
        'total_runtime_ms':round((perf_counter()-started)*1000,2),
        'limitations':[
            'Prototype preview only; not for cutting.',
            'NFP region is exact for a bounded subset of simple polygons only; fallback heuristics cover larger CAD pieces.',
            'No guaranteed global optimum or improvement.',
            'CAD source units, grainline, fold, notches, seam allowance and material semantics are unverified.',
        ],
    }
