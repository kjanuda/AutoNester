"""Phase 8 bounded post-search coordinate descent, with independent Phase 7 auditing.

Start from the Phase 6 validated heuristic result. Never claim NFP or cutting-ready.
Only an independently audited improvement may replace the reference layout.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter
from typing import Any

from shapely.affinity import rotate, translate
from shapely.geometry import Polygon
from nesting_engine import (
    _can_place, _normalise, _validate_patterns, _width_height, Placement, Piece,
)
from geometric_optimizer import optimize_geometry
from layout_audit import audit_layout
from optimization_compare import _layout_response


def _build_placements(request_pieces: list[Piece], base_layout: dict) -> list[Placement]:
    by_id = {(piece.name, piece.instance): piece for piece in request_pieces}
    placements: list[Placement] = []
    for entry in base_layout['placed_patterns']:
        piece = by_id[(entry['name'], entry['instance'])]
        rotation = int(entry['rotation'])
        target = Polygon(entry['points'])
        x, y, _, _ = target.bounds
        shape = _normalise(rotate(piece.polygon, rotation, origin=(0, 0)))
        placements.append(Placement(piece, translate(shape, xoff=x, yoff=y), rotation))
    return placements


def _length(layout: list[Placement]) -> float:
    return max((p.polygon.bounds[3] for p in layout), default=0.0)


def _anchors(shp: Polygon, n: int = 9) -> list[tuple[float,float]]:
    points = list(shp.exterior.coords)[:-1]
    if len(points) <= n:
        return points
    idx = sorted(set(round(i * (len(points)-1) / (n-1)) for i in range(n)))
    return [points[k] for k in idx]


def _positions(piece: Placement, others: list[Placement], fabric_width: float,
               gap: float, constraint: bool, side: str, budget: int) -> list[tuple[float,float]]:
    shape = _normalise(rotate(piece.piece.polygon, piece.rotation, origin=(0,0)))
    w, h = _width_height(shape)
    xold, yold = piece.polygon.bounds[:2]
    xset = {xold, 0.0, fabric_width - w}
    yset = {yold, 0.0}
    for other in others:
        a,b,c,d = other.polygon.bounds
        xset.update((a, c-w, c+gap, a-w-gap))
        yset.update((b, d-h, d+gap, b-h-gap))
        # Landmark contacts from actual polygon vertices, not just bounding boxes.
        # Limited to small jobs to bound running time.
        if len(others) <= 8:
            for ox, oy in _anchors(other.polygon, 7):
                for mx, my in _anchors(shape, 7):
                    xset.update((ox-mx, ox-mx+gap, ox-mx-gap))
                    yset.update((oy-my, oy-my+gap, oy-my-gap))
    bound = max(_length(others), piece.polygon.bounds[3])
    if constraint and piece.piece.fold_required:
        xset = {0.0 if side == 'left' else fabric_width - w}
    # Preserve current position. Scan y below the current strip end first.
    xs = sorted({round(x,4) for x in xset if isfinite(x) and -1e-7 <= x <= fabric_width-w+1e-7})
    ys = sorted({round(y,4) for y in yset if isfinite(y) and -1e-7 <= y <= bound+gap})
    if not xs or not ys:
        return [(xold,yold)]
    combos = [(max(0.0,x),max(0.0,y)) for y in ys for x in xs
              if max(bound, y+h) <= bound + 1e-7]
    combos.sort(key=lambda z:(z[1]+h,z[1],z[0]))
    if len(combos)>budget:
        front = combos[:int(budget*.75)]
        rem = combos[int(budget*.75):]
        tail_budget = budget-len(front)
        tail = rem[::max(1,len(rem)//max(1,tail_budget))][:tail_budget]
        combos = front+tail
    if (xold,yold) not in combos:
        combos.append((xold,yold))
    return combos


def _compact_once(placements: list[Placement], width: float, gap: float,
                  enforce: bool, fold_side: str, budget: int) -> tuple[list[Placement], int]:
    work = list(placements)
    evaluated = 0
    # Tackle the last/highest outlines first to have a chance to reduce strip length.
    indices = sorted(range(len(work)), key=lambda j: (-work[j].polygon.bounds[3], j))
    for idx in indices:
        moving = work[idx]
        others = [p for j,p in enumerate(work) if j!=idx]
        oldx, oldy, _, _ = moving.polygon.bounds
        before = _length(work)
        best = (before, oldy, oldx)
        selected = moving.polygon
        shape = _normalise(rotate(moving.piece.polygon, moving.rotation, origin=(0,0)))
        h = _width_height(shape)[1]
        for x,y in _positions(moving, others, width, gap, enforce, fold_side, budget):
            score=(max(_length(others), y+h), y, x)
            if score >= best: continue
            candidate=translate(shape, xoff=x, yoff=y)
            evaluated += 1
            if _can_place(candidate,others,gap):
                selected,best=candidate,score
        work[idx]=Placement(moving.piece, selected, moving.rotation)
    return work, evaluated


def refine_geometry(patterns: list[dict[str, Any]], fabric_width: float,
                    gap: float=2.0, enforce_constraints: bool=False,
                    fabric_mode: str='flat', fabric_fold_side: str='left',
                    effort: str='quick', seed: int=42) -> dict[str, Any]:
    """Audited, non-worsening bounded local relocation on a Phase 6 candidate."""
    if effort not in ('quick','standard','deep'):
        raise ValueError('effort must be quick, standard or deep')
    if isinstance(seed,bool) or not isinstance(seed,int) or not (0<=seed<=999999):
        raise ValueError('seed must be an integer from 0 to 999999')
    start=perf_counter()
    pieces=_validate_patterns(patterns,fabric_width,gap)
    phase6=optimize_geometry(patterns,fabric_width,gap,enforce_constraints,
                             fabric_mode,fabric_fold_side,effort,seed)
    baseline=phase6['trials'][phase6['best_trial_id']]['layout']
    baseline_audit=audit_layout(patterns,baseline,fabric_width,gap,enforce_constraints,fabric_mode,fabric_fold_side)
    if not baseline_audit['passed']:
        raise ValueError('Phase 6 reference layout failed independent geometry audit: '+str(baseline_audit['issues'][:2]))
    winner=baseline
    winner_audit=baseline_audit
    improvements=[]
    candidates_evaluated=0
    # Bound work, never silently imply all large layouts are locally optimized.
    skipped=len(pieces)>18
    if not skipped:
        current=_build_placements(pieces,baseline)
        passes={'quick':1,'standard':2,'deep':3}[effort]
        budget={'quick':160,'standard':280,'deep':400}[effort]
        for pass_no in range(passes):
            current, checks=_compact_once(current,fabric_width,gap,enforce_constraints,fabric_fold_side,budget)
            candidates_evaluated += checks
            if _length(current) < float(winner['fabric_length'])-0.011:
                proposal=_layout_response(current,fabric_width,gap,enforce_constraints,fabric_mode)
                audit=audit_layout(patterns,proposal,fabric_width,gap,enforce_constraints,fabric_mode,fabric_fold_side)
                if audit['passed']:
                    winner,winner_audit=proposal,audit
                    improvements.append({'pass':pass_no+1,'length_mm':proposal['fabric_length']})
                else:
                    # stop if geometric rounding exposes any constraint failure
                    break
    length_delta=max(0.0, float(baseline['fabric_length'])-float(winner['fabric_length']))
    return {'version':'0.8.0','engine':'Phase 6 multistart + bounded coordinate compaction (not exact NFP)',
            'seed':seed, 'effort':effort,'pieces':len(pieces),
            'baseline_length_mm':baseline['fabric_length'],'best_length_mm':winner['fabric_length'],
            'length_saved_mm':round(length_delta,2),
            'improvement_percent':round(100*length_delta/baseline['fabric_length'],2) if baseline['fabric_length'] else 0.0,
            'candidates_checked':candidates_evaluated,'passes_completed':len(improvements),
            'compaction_skipped':skipped,
            'skip_reason':'Local coordinate refinement currently limited to 18 instances; Phase 6 result returned unchanged.' if skipped else None,
            'baseline_audit':baseline_audit,'best_audit':winner_audit,
            'baseline_layout':baseline,'best_layout':winner,
            'total_runtime_ms':round((perf_counter()-start)*1000,2),
            'production_ready':False,
            'note':'Only audited improvements are accepted. Source CAD units, grainline, folds, notches and material specifications remain unverified.'}
