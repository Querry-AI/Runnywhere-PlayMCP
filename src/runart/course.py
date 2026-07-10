"""Loop course generation (PRD §5.3).

Strategy: place k waypoints on a circle whose circumference approximates the
target distance, route between consecutive waypoints with RFS-weighted
Dijkstra, then iteratively rescale the circle until the realized length is
within tolerance. Several bearings are tried and the best-scoring loop wins.
Bounded iterations keep worst-case latency predictable (anytime behavior,
PRD §7.1).
"""

import math
import time
from dataclasses import dataclass, field

import networkx as nx

from . import graph as graphmod
from .facilities import facility_requirement_score
from .geo import to_latlon, to_xy
from .models import CourseParams
from .rfs import route_rfs_summary, routing_weight

DISTANCE_TOLERANCE = 0.05  # ±5% (PRD §7.2)
N_WAYPOINTS = 4
BEARINGS = (0, 45, 90, 135, 180, 225, 270, 315)
MAX_RESCALES = 5
PACE_MIN_PER_KM = 6.5
FOLLOW_EDGE_PENALTY_M = 12.0
HIGHWAY_COST_FACTOR = {
    "primary": 0.84,
    "primary_link": 0.90,
    "secondary": 0.88,
    "secondary_link": 0.92,
    "tertiary": 0.94,
    "tertiary_link": 0.98,
    "unclassified": 1.00,
    "residential": 1.15,
    "living_street": 1.20,
    "service": 1.30,
    "footway": 1.24,
    "path": 1.28,
    "pedestrian": 1.16,
    "steps": 1.55,
}
# "평지" 판정: 누적 상승 < 8m/km. SRTM 30m 고도의 현실적 잡음 수준을 반영한
# 기준 (러닝 앱 통념상 10m/km 이하면 평지 취급).
FLAT_CUM_GAIN_PER_KM = 8.0


class CourseError(Exception):
    """User-facing generation failure; message must say what to do next."""


@dataclass
class Course:
    params: CourseParams
    path: list  # graph nodes
    points: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon)
    length_m: float = 0.0
    ascent_m: float = 0.0
    rfs: dict = field(default_factory=dict)
    shape_similarity: float | None = None

    @property
    def length_km(self) -> float:
        return self.length_m / 1000.0

    @property
    def duration_range_min(self) -> tuple[int, int]:
        base = self.length_km * PACE_MIN_PER_KM
        return (round(base * 0.95), round(base * 1.2))

    @property
    def is_flat(self) -> bool:
        return self.ascent_m < FLAT_CUM_GAIN_PER_KM * self.length_km

    @property
    def grade_label(self) -> str:
        per_km = self.ascent_m / self.length_km if self.length_km else 0.0
        if per_km < FLAT_CUM_GAIN_PER_KM:
            return "평지 위주"
        if per_km < 15.0:
            return "완만한 경사"
        return "오르막 포함"


def smooth_series(vals: list[float], window: int = 5) -> list[float]:
    """Moving average — SRTM in dense city reads rooftops, so raw per-node
    differences are ±2-3m noise that would inflate cumulative ascent."""
    if len(vals) < window:
        return vals
    half = window // 2
    return [
        sum(vals[max(0, i - half):i + half + 1]) / len(vals[max(0, i - half):i + half + 1])
        for i in range(len(vals))
    ]


def _path_metrics(g, path) -> tuple[float, float]:
    """(length_m, cumulative_ascent_m). True ascent from smoothed node
    elevations when the ETL provided them; slope-based estimate otherwise."""
    length = sum(g.edges[u, v]["length"] for u, v in zip(path, path[1:]))
    elevs = [g.nodes[n].get("elev") for n in path]
    if all(e is not None for e in elevs) and len(elevs) >= 3:
        sm = smooth_series(elevs)
        ascent = sum(max(0.0, b - a) for a, b in zip(sm, sm[1:]))
    else:
        ascent = sum(
            g.edges[u, v]["length"] * max(0.0, g.edges[u, v].get("slope_pct", 0.0)) / 100.0 * 0.5
            for u, v in zip(path, path[1:])
        )
    return length, ascent


def _route(g, weight, a, b) -> list:
    # Bidirectional search: same shortest path, a fraction of the visits.
    return nx.bidirectional_dijkstra(g, a, b, weight=weight)[1]


def easy_route_weight(base_weight: str):
    """Prefer roads that are easier to follow without exposing a new score.

    A tiny fixed cost per edge discourages routes made of many short alley
    fragments while keeping the existing RFS/length weighting dominant.
    """
    def _weight(_u, _v, attrs):
        highway = attrs.get("highway")
        if isinstance(highway, (list, tuple)):
            highway = highway[0] if highway else None
        factor = HIGHWAY_COST_FACTOR.get(str(highway), 1.06)
        sidewalk = float(attrs.get("sidewalk_score", 0.5))
        if sidewalk >= 0.85:
            factor *= 0.90
        elif sidewalk < 0.55:
            factor *= 1.12
        return attrs.get(base_weight, attrs["length"]) * factor + FOLLOW_EDGE_PENALTY_M
    return _weight


def followability_penalty(points: list[tuple[float, float]], length_m: float) -> float:
    """Internal ranking penalty for confusing routes: many turns, sharp turns,
    and U-turn-like bends. This is deliberately not shown as a user-facing
    score; it just nudges candidate selection toward runnable courses."""
    if len(points) < 3 or length_m <= 0:
        return 0.0
    turns = sharp = uturns = 0
    for a, b, c in zip(points, points[1:], points[2:]):
        ax, ay = to_xy(a[0], a[1], b[0], b[1])
        cx, cy = to_xy(c[0], c[1], b[0], b[1])
        v1 = (-ax, -ay)
        v2 = (cx, cy)
        n1 = math.hypot(*v1)
        n2 = math.hypot(*v2)
        if n1 < 8 or n2 < 8:
            continue
        cosv = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        angle = math.degrees(math.acos(cosv))
        if angle >= 35:
            turns += 1
        if angle >= 70:
            sharp += 1
        if angle >= 125:
            uturns += 1
    km = max(length_m / 1000.0, 0.1)
    return 0.6 * turns / km + 1.6 * sharp / km + 4.0 * uturns / km


def _loop_via_circle(g, weight, start_node, target_m: float, bearing_deg: float) -> list | None:
    start = g.nodes[start_node]
    lat0, lon0 = start["lat"], start["lon"]
    radius = target_m / (2 * math.pi)
    for _ in range(MAX_RESCALES):
        theta0 = math.radians(bearing_deg)
        cx = radius * math.cos(theta0)
        cy = radius * math.sin(theta0)
        waypoints = []
        for k in range(1, N_WAYPOINTS):
            ang = theta0 + math.pi + 2 * math.pi * k / N_WAYPOINTS
            x = cx + radius * math.cos(ang)
            y = cy + radius * math.sin(ang)
            lat, lon = to_latlon(x, y, lat0, lon0)
            node, snap = graphmod.nearest_node(lat, lon)
            # Reject waypoints that snapped far away (e.g. across a river) or
            # outside the local subgraph — that bearing doesn't fit the land.
            if snap > 600 or node not in g:
                return None
            waypoints.append(node)
        stops = [start_node, *waypoints, start_node]
        try:
            path = []
            for a, b in zip(stops, stops[1:]):
                seg = _route(g, weight, a, b)
                path.extend(seg if not path else seg[1:])
        except nx.NetworkXNoPath:
            return None
        length, _ = _path_metrics(g, path)
        if abs(length - target_m) / target_m <= DISTANCE_TOLERANCE:
            return path
        if length <= 0:
            return None
        radius *= target_m / length  # circumference scales ~linearly with r
    return path  # best effort after rescales; caller checks tolerance


def generate_course(params: CourseParams) -> Course:
    start_node, snap_dist = graphmod.nearest_node(params.lat, params.lon)
    if snap_dist > 1500:
        raise CourseError(
            "출발점이 현재 지원 지역(서울 보행 네트워크) 밖이에요. "
            "서울 시내 지명이나 좌표로 다시 요청해 주세요."
        )
    target_m = params.distance_km * 1000.0
    # Local subgraph keeps every Dijkstra bounded regardless of city size.
    g = graphmod.subgraph_around(params.lat, params.lon,
                                 target_m / math.pi * 1.4 + 400)
    weight = easy_route_weight(routing_weight(params.night_mode, params.include_hills))

    best: Course | None = None
    best_key: tuple | None = None
    best_err = math.inf
    n_in_tol = 0
    deadline = time.perf_counter() + 0.8  # anytime cutoff (PRD §7.1)
    for bearing in BEARINGS:
        if time.perf_counter() > deadline:
            break
        # Early exit: a good in-tolerance loop is enough; exhaustive bearing
        # search buys little quality for a lot of latency.
        if n_in_tol >= 2 or (best is not None and best_err <= DISTANCE_TOLERANCE
                             and best.rfs["score"] >= 55):
            break
        path = _loop_via_circle(g, weight, start_node, target_m, bearing)
        if not path or len(path) < 3:
            continue
        length, ascent = _path_metrics(g, path)
        err = abs(length - target_m) / target_m
        if err <= DISTANCE_TOLERANCE:
            n_in_tol += 1
        summary = route_rfs_summary(g, path, params.night_mode, params.include_hills)
        points = [(g.nodes[n]["lat"], g.nodes[n]["lon"]) for n in path]
        fac_hits, fac_total = facility_requirement_score(points, params.need_facilities)
        # Prefer in-tolerance loops with the highest RFS; flat mode also
        # rewards low cumulative ascent. A hidden followability penalty keeps
        # the selected course from becoming a maze of tiny bends.
        ascent_per_km = ascent / (length / 1000.0) if length else 0.0
        quality = (
            -summary["score"]
            + (0.0 if params.include_hills else 2.0 * ascent_per_km)
            + followability_penalty(points, length)
        )
        missing_facilities = fac_total - fac_hits
        key = (
            err > DISTANCE_TOLERANCE,
            missing_facilities,
            err if err > DISTANCE_TOLERANCE else quality,
        )
        if best_key is None or key < best_key:
            best = Course(
                params=params,
                path=path,
                points=points,
                length_m=length,
                ascent_m=ascent,
                rfs=summary,
            )
            best_key = key
            best_err = err
    if best is None:
        raise CourseError(
            "이 위치에서는 순환 코스를 만들지 못했어요. "
            "출발점을 큰길이나 공원 근처로 조금 옮겨서 다시 시도해 주세요."
        )
    # Near-misses are still useful: we always display the *real* distance, so
    # a 4.4km loop for a 4km ask beats a refusal (river/terrain constraints).
    if best_err > DISTANCE_TOLERANCE * 2.5:
        raise CourseError(
            f"목표 {params.distance_km:g}km에 맞는 코스를 찾지 못했어요 "
            f"(가장 근접: {best.length_km:.1f}km). 거리를 조금 조정해 다시 요청해 주세요."
        )
    return best
