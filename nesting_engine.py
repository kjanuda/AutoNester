"""AutoNester v0.2: deterministic polygon nesting heuristic (units: mm).

This is a prototype heuristic, NOT an optimal industrial nesting solver.
Permitted rotations must be supplied by the caller; no mirroring occurs.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from shapely.affinity import rotate, translate
from shapely.geometry import Polygon

EPS = 1e-7
MAX_PIECES = 60
MAX_VERTICES = 80


@dataclass(frozen=True)
class Piece:
    name: str
    instance: int
    polygon: Polygon
    rotations: tuple[int, ...]
    original_index: int
    fold_required: bool = False
    fold_edge: str | None = None
    fold_verified: bool = False
    grainline_verified: bool = False
    fold_status_verified: bool = False


@dataclass
class Placement:
    piece: Piece
    polygon: Polygon
    rotation: int


def _normalise(poly: Polygon) -> Polygon:
    minx, miny, _, _ = poly.bounds
    return translate(poly, xoff=-minx, yoff=-miny)


def _width_height(poly: Polygon) -> tuple[float, float]:
    minx, miny, maxx, maxy = poly.bounds
    return maxx - minx, maxy - miny


def _vertices(poly: Polygon) -> list[tuple[float, float]]:
    """Sample vertices to bound the prototype's search cost; exact geometry kept for checks."""
    coordinates = list(poly.exterior.coords)[:-1]
    if len(coordinates) <= 14:
        return coordinates
    sampled = [coordinates[(i * len(coordinates)) // 14] for i in range(14)]
    return list(dict.fromkeys(sampled))


def _validate_patterns(patterns: list[dict[str, Any]], fabric_width: float, gap: float) -> list[Piece]:
    if not isfinite(fabric_width) or fabric_width <= 0:
        raise ValueError("fabric_width must be a positive finite number (mm)")
    if not isfinite(gap) or gap < 0:
        raise ValueError("gap must be a finite non-negative number (mm)")
    if not patterns:
        raise ValueError("At least one pattern is required")

    pieces: list[Piece] = []
    for index, pattern in enumerate(patterns):
        name = str(pattern.get("name", "")).strip()
        if not name:
            raise ValueError(f"Pattern #{index + 1} requires a name")
        coords = pattern.get("points")
        if not isinstance(coords, list) or not 3 <= len(coords) <= MAX_VERTICES:
            raise ValueError(f"{name}: expected 3–{MAX_VERTICES} polygon vertices")
        if any(len(p) != 2 or not all(isfinite(float(c)) for c in p) for p in coords):
            raise ValueError(f"{name}: coordinates must be finite XY pairs")
        poly = Polygon(coords)
        if not poly.is_valid or poly.area <= EPS:
            raise ValueError(f"{name}: invalid or self-intersecting polygon")
        poly = _normalise(poly)

        quantity = pattern.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_PIECES:
            raise ValueError(f"{name}: quantity must be an integer from 1 to {MAX_PIECES}")
        angles = pattern.get("allowed_rotations", [0])
        if not isinstance(angles, list) or not angles:
            raise ValueError(f"{name}: allowed_rotations must be a non-empty list")
        if any(isinstance(a, bool) or not isinstance(a, int) for a in angles):
            raise ValueError(f"{name}: allowed_rotations must contain integer degrees")
        angles = tuple(dict.fromkeys(a % 360 for a in angles))
        if len(angles) > 12:
            raise ValueError(f"{name}: no more than 12 rotation angles per pattern")
        if not any(_width_height(_normalise(rotate(poly, a, origin=(0, 0))))[0] <= fabric_width + EPS for a in angles):
            raise ValueError(f"{name}: no allowed rotation fits the fabric width")
        fold_required = pattern.get("fold_required", False)
        fold_edge = pattern.get("fold_edge")
        fold_verified = pattern.get("fold_verified", False)
        grainline_verified = pattern.get("grainline_verified", False)
        fold_status_verified = pattern.get("fold_status_verified", False)
        if not all(isinstance(x, bool) for x in (fold_required, fold_verified, grainline_verified, fold_status_verified)):
            raise ValueError(f"{name}: fold/grainline verification fields must be boolean")
        if fold_edge not in (None, "left", "right"):
            raise ValueError(f"{name}: fold_edge must be left, right or null")
        for i in range(1, quantity + 1):
            pieces.append(Piece(name, i, poly, angles, index,
                                fold_required, fold_edge, fold_verified, grainline_verified, fold_status_verified))
            if len(pieces) > MAX_PIECES:
                raise ValueError(f"Prototype limit: maximum {MAX_PIECES} total pieces per request")
    return pieces


def _position_events(
    moving: Polygon, placed: list[Placement], fabric_width: float,
    used_length: float, gap: float,
) -> tuple[list[float], list[float]]:
    """Potential origin positions inferred from current polygon vertices/edges."""
    width, height = _width_height(moving)
    xs: set[float] = {0.0, fabric_width - width}
    ys: set[float] = {0.0, used_length + gap}
    own_vertices = _vertices(moving)
    for item in placed:
        polygon = item.polygon
        minx, miny, maxx, maxy = polygon.bounds
        xs.update((minx - width - gap, minx, maxx, maxx + gap, maxx - width))
        ys.update((miny - height - gap, miny, maxy, maxy + gap, maxy - height))
        for px, py in _vertices(polygon):
            for vx, vy in own_vertices:
                xs.update((px - vx - gap, px - vx, px - vx + gap))
                ys.update((py - vy - gap, py - vy, py - vy + gap))

    # Rounding merges floating-point near duplicates, not placement geometry.
    valid_x = sorted({round(x, 5) for x in xs if -EPS <= x <= fabric_width - width + EPS})
    valid_y = sorted({round(y, 5) for y in ys if -EPS <= y <= used_length + gap + EPS})
    return [max(x, 0.0) for x in valid_x], [max(y, 0.0) for y in valid_y]


def _can_place(candidate: Polygon, others: list[Placement], gap: float) -> bool:
    x0, y0, x1, y1 = candidate.bounds
    for other in others:
        a0, b0, a1, b1 = other.polygon.bounds
        if x0 >= a1 + gap - EPS or a0 >= x1 + gap - EPS:
            continue
        if y0 >= b1 + gap - EPS or b0 >= y1 + gap - EPS:
            continue
        if gap > 0:
            if candidate.distance(other.polygon) < gap - EPS:
                return False
        elif candidate.intersection(other.polygon).area > EPS:
            return False
    return True


def _place_order(pieces: list[Piece], fabric_width: float, gap: float) -> list[Placement]:
    result: list[Placement] = []
    used_length = 0.0
    for piece in pieces:
        best: tuple[tuple[float, float, float, int], Polygon, int] | None = None
        for angle in piece.rotations:
            shape = _normalise(rotate(piece.polygon, angle, origin=(0, 0)))
            width, height = _width_height(shape)
            if width > fabric_width + EPS:
                continue
            xs, ys = _position_events(shape, result, fabric_width, used_length, gap)
            for y in ys:
                # Once current best would be exceeded, higher y cannot beat it.
                if best and max(used_length, y + height) > best[0][0] + EPS:
                    break
                for x in xs:
                    score = (round(max(used_length, y + height), 5), y, x, angle)
                    if best and score >= best[0]:
                        continue
                    candidate = translate(shape, xoff=x, yoff=y)
                    if _can_place(candidate, result, gap):
                        best = score, candidate, angle
        if best is None:
            raise ValueError(f"Cannot place {piece.name} within the specified fabric width")
        _, polygon, angle = best
        result.append(Placement(piece, polygon, angle))
        used_length = max(used_length, polygon.bounds[3])
    return result


def generate_nesting(
    patterns: list[dict[str, Any]], fabric_width: float, gap: float = 2.0,
    strategy: str = "balanced",
    enforce_constraints: bool = False, fabric_mode: str = "flat",
    fabric_fold_side: str = "left",
) -> dict[str, Any]:
    """Try several deterministic greedy orders. Lower used length is preferable."""
    if strategy not in ("quick", "balanced", "fast"):
        raise ValueError("strategy must be 'quick', 'balanced' or 'fast'")
    pieces = _validate_patterns(patterns, fabric_width, gap)
    if fabric_mode not in ("flat", "folded"):
        raise ValueError("fabric_mode must be flat or folded")
    if fabric_fold_side not in ("left", "right"):
        raise ValueError("fabric_fold_side must be left or right")
    if enforce_constraints:
        _validate_cutting_constraints(pieces, fabric_mode, fabric_fold_side)
    if strategy == "fast" or len(pieces) > 3 or enforce_constraints:
        return _generate_fast(pieces, fabric_width, gap, strategy,
                              enforce_constraints, fabric_mode, fabric_fold_side)
    orders = [pieces]
    if strategy == "balanced":
        orders.extend([
            sorted(pieces, key=lambda p: -p.polygon.area),
            sorted(pieces, key=lambda p: -_width_height(p.polygon)[1]),
            sorted(pieces, key=lambda p: -max(_width_height(p.polygon))),
        ])

    winning: list[Placement] | None = None
    best_length = float("inf")
    for order in orders:
        layout = _place_order(order, fabric_width, gap)
        length = max(p.polygon.bounds[3] for p in layout)
        if length < best_length - EPS:
            best_length, winning = length, layout

    assert winning is not None
    used_area = sum(p.piece.polygon.area for p in winning)
    total_area = fabric_width * best_length
    utilization = 100 * used_area / total_area if total_area else 0.0
    return {
        "algorithm": "polygon-vertex-greedy-v0.2",
        "strategy": strategy,
        "constraints_enforced": False,
        "production_ready": False,
        "warnings": ["Preview only: fold, grainline, units, notches and cutting geometry require CAD verification."],
        "fabric_width": fabric_width,
        "fabric_length": round(best_length, 2),
        "fabric_area": round(total_area, 2),
        "pattern_area": round(used_area, 2),
        "utilization_percent": round(utilization, 2),
        "waste_percent": round(100 - utilization, 2),
        "placed_patterns": [
            {
                "name": p.piece.name,
                "instance": p.piece.instance,
                "rotation": p.rotation,
                "points": [[round(x, 4), round(y, 4)] for x, y in list(p.polygon.exterior.coords)[:-1]],
                "area": round(p.piece.polygon.area, 2),
            }
            for p in winning
        ],
    }


def _continuous_fold_edge(poly: Polygon, side: str) -> bool:
    """Conservative straight fold edge check on the piece's extreme vertical border."""
    minx, miny, maxx, maxy = poly.bounds
    target = minx if side == 'left' else maxx
    height = maxy - miny
    if height <= EPS:
        return False
    pts = list(poly.exterior.coords)
    intervals = []
    for a, b in zip(pts, pts[1:]):
        if abs(a[0]-target) <= 0.01 and abs(b[0]-target) <= 0.01:
            intervals.append((min(a[1], b[1]), max(a[1], b[1])))
    if not intervals:
        return False
    intervals.sort()
    lo, hi = intervals[0]
    spans = []
    for a, b in intervals[1:]:
        if a <= hi + 0.02:
            hi = max(hi, b)
        else:
            spans.append((lo, hi))
            lo, hi = a, b
    spans.append((lo, hi))
    return any(a <= miny + 0.02 and b >= maxy - 0.02 for a, b in spans)


def _validate_cutting_constraints(pieces: list[Piece], fabric_mode: str, fold_side: str):
    """Require explicit operator confirmation; imported metadata alone is insufficient."""
    for piece in pieces:
        if not piece.fold_status_verified:
            raise ValueError(f'{piece.name}: verify Fold Y/N requirement in source CAD first')
        if not piece.grainline_verified:
            raise ValueError(f'{piece.name}: verify grainline and allowed rotations in source CAD first')
        if piece.fold_required:
            if fabric_mode != 'folded':
                raise ValueError(f'{piece.name}: fold required; use a folded fabric marker')
            if not piece.fold_verified or piece.fold_edge != fold_side:
                raise ValueError(f'{piece.name}: verify fold edge side ({fold_side}) against source CAD')
            for angle in piece.rotations:
                poly = _normalise(rotate(piece.polygon, angle, origin=(0, 0)))
                if not _continuous_fold_edge(poly, fold_side):
                    raise ValueError(f'{piece.name}: no continuous straight {fold_side} fold edge at {angle} degrees; manually establish fold line in CAD')


def _fast_candidates(shape: Polygon, placed: list[Placement], width: float,
                     used: float, gap: float, fold_side: str | None):
    """O(n) bounded contact candidates per new polygon; avoid x*y Cartesian search."""
    w, h = _width_height(shape)
    current_x = 0.0 if fold_side != 'right' else width - w
    raw = [(current_x, 0.0), (current_x, used + gap)]
    if fold_side is None:
        raw += [(width-w, 0.0), (width-w, used+gap)]
    # A compact list of rectangle contacts and limited polygon point contacts.
    for item in placed[-24:]:
        a,b,c,d = item.polygon.bounds
        if fold_side:
            raw += [(current_x, d+gap), (current_x, b)]
        else:
            raw.extend([
                (c+gap, b), (c+gap, 0.0), (a, d+gap),
                (a-w-gap, b), (c-w, b), (c-w, d+gap),
                (0.0, d+gap), (a, 0.0), (width-w, d+gap),
            ])
            # A few vertex-to-vertex contacts are useful for irregular profiles.
            mine = _vertices(shape)
            theirs = _vertices(item.polygon)
            for vx,vy in theirs[::max(1,len(theirs)//3)][:3]:
                for mx,my in mine[::max(1,len(mine)//2)][:2]:
                    raw.append((vx-mx+gap, vy-my))
    candidates = set()
    for x,y in raw:
        if x < -EPS or x > width-w+EPS or y < -EPS:
            continue
        candidates.add((round(max(0.0,x),4), round(max(0.0,y),4)))
    return sorted(candidates, key=lambda xy: (round(max(used,xy[1]+h),4),xy[1],xy[0]))


def _fast_place(pieces: list[Piece], width: float, gap: float,
                enforce: bool, fold_side: str) -> list[Placement]:
    placed: list[Placement] = []
    used = 0.0
    for piece in pieces:
        best = None
        for angle in piece.rotations:
            shape = _normalise(rotate(piece.polygon, angle, origin=(0, 0)))
            w,h = _width_height(shape)
            if w > width + EPS:
                continue
            side = fold_side if (enforce and piece.fold_required) else None
            for x,y in _fast_candidates(shape, placed, width, used, gap, side):
                score = (round(max(used,y+h),4),round(y,4),round(x,4),angle)
                if best is not None and score >= best[0]:
                    break
                candidate = translate(shape,xoff=x,yoff=y)
                if _can_place(candidate, placed, gap):
                    best = (score, candidate, angle)
                    break
        if best is None:
            # Safe new-row fallback, even for concave pattern geometries.
            for angle in piece.rotations:
                shape = _normalise(rotate(piece.polygon,angle,origin=(0,0)))
                w,h = _width_height(shape)
                if w > width+EPS:
                    continue
                x = width-w if enforce and piece.fold_required and fold_side=='right' else 0.0
                candidate = translate(shape,xoff=x,yoff=used+gap)
                if _can_place(candidate,placed,gap):
                    best = ((used+gap+h,used+gap,x,angle),candidate,angle)
                    break
        if best is None:
            raise ValueError(f'Cannot place {piece.name} in fabric width {width} mm')
        _,poly,angle = best
        placed.append(Placement(piece,poly,angle))
        used = max(used, poly.bounds[3])
    return placed


def _generate_fast(pieces: list[Piece], width: float, gap: float, strategy: str,
                   enforce: bool, fabric_mode: str, fold_side: str):
    orders = [pieces]
    if strategy == 'balanced':
        orders += [sorted(pieces, key=lambda p: -p.polygon.area)]
        if len(pieces) <= 24:
            orders += [sorted(pieces, key=lambda p: -_width_height(p.polygon)[1])]
    best_layout = None
    length = float('inf')
    for order in orders:
        result = _fast_place(order, width, gap, enforce, fold_side)
        end = max(p.polygon.bounds[3] for p in result)
        if end < length - EPS:
            best_layout, length = result, end
    assert best_layout is not None
    area = sum(p.piece.polygon.area for p in best_layout)
    total = width * length
    utilization = (100 * area / total) if total else 0
    warnings = ['Marker preview only. Confirm DXF source units, seam allowance, notches, print/cut scale and material constraints.']
    if not enforce:
        warnings.append('Fold and grainline constraints are NOT enforced. Do not cut from this layout.')
    else:
        warnings.append('Fold/grainline confirmations passed, but the output is NOT certified for production cutting.')
    return {
        'algorithm': 'bounded-polygon-greedy-v0.4',
        'strategy': strategy,
        'constraints_enforced': enforce,
        'production_ready': False,
        'warnings': warnings,
        'fabric_mode': fabric_mode,
        'fabric_width': width,
        'fabric_length': round(length, 2),
        'fabric_area': round(total, 2),
        'pattern_area': round(area, 2),
        'utilization_percent': round(utilization, 2),
        'waste_percent': round(100-utilization, 2),
        'placed_patterns': [
            {
                'name': p.piece.name, 'instance': p.piece.instance,
                'rotation': p.rotation,
                'fold_required': p.piece.fold_required,
                'points': [[round(x, 4), round(y, 4)] for x,y in list(p.polygon.exterior.coords)[:-1]],
                'area': round(p.piece.polygon.area, 2),
            } for p in best_layout
        ],
    }
