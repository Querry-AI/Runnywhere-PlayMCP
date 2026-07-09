"""Animal-shaped GPS-art courses (PRD §5.4).

Each shape is a normalized closed polyline template. Generation: scale the
template to the target distance, try several rotations, snap vertices to the
pedestrian graph, route between consecutive snapped nodes, then score shape
fidelity (mean deviation of the routed path from the template outline).
Results under the similarity gate are never returned — alternatives are
suggested instead.
"""

import math
import time
from dataclasses import dataclass

import networkx as nx

from . import graph as graphmod
from .course import Course, CourseError, _path_metrics
from .facilities import facility_requirement_score
from .geo import to_latlon, to_xy
from .models import CourseParams
from .rfs import route_rfs_summary, routing_weight

SIMILARITY_GATE = 0.7  # below this we refuse to ship the course (PRD §5.4)
ROTATIONS = (0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330)
SCALES = (1.0, 0.85, 1.15)  # real road grids often fit a slightly resized shape
# Anytime cutoffs (PRD §7.1). Worst case = full search + alternatives probe;
# the pair must stay well under the 3s p99 spec including queueing headroom.
TIME_BUDGET_S = 0.6
PROBE_BUDGET_S = 0.4
GOOD_ENOUGH = 0.82  # early exit — no need to keep searching past this


@dataclass(frozen=True)
class ShapeSpec:
    key: str
    name_ko: str
    emoji: str
    min_km: float
    # Closed polyline, roughly centered on origin, arbitrary scale.
    outline: tuple[tuple[float, float], ...]


def _closed(points: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    if points[0] != points[-1]:
        points = points + [points[0]]
    return tuple(points)


SHAPES: dict[str, ShapeSpec] = {
    s.key: s
    for s in (
        ShapeSpec(
            "rabbit", "토끼", "🐰", 2.0,
            _closed([(0, 0), (2, 0), (2.6, 1.2), (2.2, 2.2), (2.8, 4.2), (2.2, 4.4),
                     (1.8, 2.6), (1.2, 2.6), (0.8, 4.4), (0.2, 4.2), (0.8, 2.2), (-0.4, 1.4)]),
        ),
        ShapeSpec(
            "cat", "고양이", "🐱", 3.0,
            _closed([(0, 0), (3, 0), (3, 2), (2.6, 3.2), (3.2, 4.4), (2.2, 3.8),
                     (1.5, 4.1), (0.8, 3.8), (-0.2, 4.4), (0.4, 3.2), (0, 2)]),
        ),
        ShapeSpec(
            "dog", "강아지", "🐶", 3.0,
            _closed([(0, 0), (1.2, 0), (1.4, 1.2), (2.8, 1.2), (3.0, 0), (4.2, 0),
                     (4.4, 2.0), (3.6, 3.2), (4.4, 3.0), (4.6, 2.2), (5.0, 2.6),
                     (4.4, 3.8), (3.0, 3.6), (1.0, 3.4), (0.4, 2.2), (-0.6, 2.6), (-0.2, 1.4)]),
        ),
        ShapeSpec(
            "whale", "고래", "🐳", 3.0,
            _closed([(0, 0), (2.5, -0.6), (5, 0), (6.2, 1.0), (7.4, 0.4), (7.2, 1.4),
                     (7.4, 2.4), (6.2, 1.8), (5, 2.4), (2.5, 2.8), (0.8, 2.2), (-0.4, 1.1)]),
        ),
        ShapeSpec(
            "giraffe", "기린", "🦒", 6.0,
            _closed([(0, 0), (1.4, 0), (1.5, 1.6), (2.6, 1.6), (2.7, 0), (4.1, 0),
                     (4.0, 2.4), (4.8, 5.6), (5.6, 6.0), (5.6, 6.8), (4.4, 6.6),
                     (3.6, 5.8), (3.2, 3.0), (0.4, 2.8)]),
        ),
    )
}


def list_shapes() -> list[dict]:
    return [
        {"shape": s.key, "name_ko": s.name_ko, "emoji": s.emoji, "min_km": s.min_km}
        for s in SHAPES.values()
    ]


def _outline_length(outline) -> float:
    return sum(math.dist(outline[i], outline[i + 1]) for i in range(len(outline) - 1))


def _resample(outline, n: int) -> list[tuple[float, float]]:
    """n points evenly spaced along the closed outline."""
    total = _outline_length(outline)
    step = total / n
    points, acc, target = [], 0.0, 0.0
    i = 0
    ax, ay = outline[0]
    while len(points) < n and i < len(outline) - 1:
        bx, by = outline[i + 1]
        seg = math.dist((ax, ay), (bx, by))
        while acc + seg >= target and len(points) < n:
            t = 0.0 if seg == 0 else (target - acc) / seg
            points.append((ax + t * (bx - ax), ay + t * (by - ay)))
            target += step
        acc += seg
        ax, ay = bx, by
        i += 1
    return points


def _point_to_polyline_dist(p, poly) -> float:
    px, py = p
    best = math.inf
    for (ax, ay), (bx, by) in zip(poly, poly[1:]):
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        t = 0.0 if denom == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        best = min(best, math.dist((px, py), (ax + t * dx, ay + t * dy)))
    return best


def similarity(routed_xy: list[tuple[float, float]], template_xy: list[tuple[float, float]],
               scale_m: float) -> float:
    """1 - normalized symmetric mean deviation between routed path and template."""
    closed_t = template_xy + [template_xy[0]]
    closed_r = routed_xy + [routed_xy[0]]
    dev_r = sum(_point_to_polyline_dist(p, closed_t) for p in routed_xy) / len(routed_xy)
    dev_t = sum(_point_to_polyline_dist(p, closed_r) for p in template_xy) / len(template_xy)
    dev = (dev_r + dev_t) / 2
    return max(0.0, 1.0 - dev / (0.10 * scale_m))


def _search_shape(spec: ShapeSpec, params: CourseParams, deadline: float,
                  rotations=ROTATIONS, scales=SCALES) -> tuple[Course | None, float]:
    """Snap the shape template to the road network; best candidate + score."""
    target_m = params.distance_km * 1000.0
    n_anchor = max(16, min(40, int(target_m / 250)))
    template = _resample(spec.outline, n_anchor)
    base_scale = target_m / _outline_length(list(spec.outline) + [spec.outline[0]])
    xs = [p[0] for p in template]
    ys = [p[1] for p in template]
    diameter_units = max(max(xs) - min(xs), max(ys) - min(ys))

    lat0, lon0 = params.lat, params.lon
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    # Local subgraph bounded by the largest candidate footprint (PRD §7.1).
    g = graphmod.subgraph_around(
        lat0, lon0, diameter_units * base_scale * max(scales) * 0.7 + 500)

    best: Course | None = None
    best_sim = -1.0
    best_key: tuple | None = None
    for scale_f in scales:
        scale = base_scale * scale_f
        diameter_m = diameter_units * scale
        for rot in rotations:
            if time.perf_counter() > deadline or best_sim >= GOOD_ENOUGH:
                break
            cos_r, sin_r = math.cos(math.radians(rot)), math.sin(math.radians(rot))
            anchors_xy = [
                (((x - cx) * cos_r - (y - cy) * sin_r) * scale,
                 ((x - cx) * sin_r + (y - cy) * cos_r) * scale)
                for x, y in template
            ]
            nodes = []
            for x, y in anchors_xy:
                lat, lon = to_latlon(x, y, lat0, lon0)
                node, snap = graphmod.nearest_node(lat, lon)
                if snap > 800 or node not in g:
                    nodes = []
                    break
                if not nodes or node != nodes[-1]:
                    nodes.append(node)
            if len(nodes) < 6:
                continue
            try:
                path = []
                for a, b in zip(nodes + [nodes[0]], nodes[1:] + [nodes[0]]):
                    seg = nx.bidirectional_dijkstra(g, a, b, weight="length")[1]
                    path.extend(seg if not path else seg[1:])
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue
            routed_xy = [to_xy(g.nodes[n]["lat"], g.nodes[n]["lon"], lat0, lon0)
                         for n in path]
            sim = similarity(routed_xy, anchors_xy, diameter_m)
            length, ascent = _path_metrics(g, path)
            if abs(length - target_m) / target_m > 0.25:
                sim *= 0.8  # penalize big distance misses when ranking
            points = [(g.nodes[n]["lat"], g.nodes[n]["lon"]) for n in path]
            fac_hits, fac_total = facility_requirement_score(points, params.need_facilities)
            clears_gate = sim >= SIMILARITY_GATE
            key = (
                not clears_gate,
                fac_total - fac_hits if clears_gate else 999,
                -sim,
            )
            if best_key is None or key < best_key:
                best_key = key
                best_sim = sim
                best = Course(
                    params=params,
                    path=path,
                    points=points,
                    length_m=length,
                    ascent_m=ascent,
                    rfs=route_rfs_summary(g, path, params.night_mode,
                                          params.include_hills),
                    shape_similarity=round(sim, 3),
                )
        if time.perf_counter() > deadline or best_sim >= GOOD_ENOUGH:
            break
    return best, best_sim


def generate_shape_course(params: CourseParams) -> Course:
    spec = SHAPES.get(params.shape or "")
    if spec is None:
        names = ", ".join(f"{s.emoji}{s.key}" for s in SHAPES.values())
        raise CourseError(f"지원하지 않는 모양이에요. 가능한 모양: {names}")
    if params.distance_km < spec.min_km:
        alts = [s for s in SHAPES.values() if s.min_km <= params.distance_km]
        alt_msg = (
            "이 거리에서는 " + ", ".join(f"{a.emoji}{a.name_ko}" for a in alts) + " 모양이 가능해요."
            if alts else "거리를 늘려서 다시 요청해 주세요."
        )
        raise CourseError(
            f"{spec.name_ko} 모양은 {spec.min_km:g}km 이상에서 예쁘게 나와요. {alt_msg}"
        )

    best, best_sim = _search_shape(spec, params,
                                   deadline=time.perf_counter() + TIME_BUDGET_S)
    if best is None or best_sim < SIMILARITY_GATE:
        # Honest alternatives: quick-probe the other shapes and suggest only
        # those that actually clear the gate here (편의성 — 실패도 대화의 일부).
        alts = suggest_alternatives(params)
        alt_msg = (
            " 대신 이 근처에서 검증된 모양: " + ", ".join(alts) + "."
            if alts else
            " 강남·잠실처럼 길이 바둑판인 동네나 큰 공원 근처에서 성공률이 높아요."
        )
        raise CourseError(
            f"이 지역 도로망에서는 {spec.name_ko} 모양(완성도 {max(best_sim, 0):.0%})이 "
            f"충분히 예쁘게 나오지 않아 추천하지 않을게요.{alt_msg}"
        )
    return best


def suggest_alternatives(params: CourseParams, limit: int = 2) -> list[str]:
    """Quick-probe other shapes at this location; return only verified fits."""
    deadline = time.perf_counter() + PROBE_BUDGET_S
    out = []
    for spec in SHAPES.values():
        if spec.key == params.shape or spec.min_km > params.distance_km:
            continue
        if time.perf_counter() > deadline:
            break
        probe = params.model_copy(update={"shape": spec.key})
        course, sim = _search_shape(
            spec, probe, deadline=min(deadline, time.perf_counter() + 0.35),
            rotations=(0, 60, 120, 180, 240, 300), scales=(1.0,))
        if course is not None and sim >= SIMILARITY_GATE:
            out.append(f"{spec.emoji}{spec.name_ko}(완성도 {sim:.0%})")
            if len(out) >= limit:
                break
    return out
