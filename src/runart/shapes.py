"""Animal-shaped GPS-art courses (PRD §5.4).

Each shape is a normalized closed polyline template. Generation: scale the
template to the target distance, try several rotations, snap vertices to the
pedestrian graph, route between consecutive snapped nodes, then score shape
fidelity (mean deviation of the routed path from the template outline).
The search first tries strict, high-clarity silhouettes. If a local street
grid cannot satisfy that threshold, it falls back to the best closed,
non-intersecting animal-like route instead of giving up.
"""

import math
import time
from dataclasses import dataclass

import networkx as nx

from . import graph as graphmod
from .course import Course, CourseError, _path_metrics, followability_penalty
from .facilities import facility_requirement_score
from .geo import to_latlon, to_xy
from .models import CourseParams
from .rfs import route_rfs_summary, routing_weight

SIMILARITY_GATE = 0.55  # below this we refuse to ship the course (PRD §5.4)
UPRIGHT_ROTATIONS = (0, 15, 345, 30, 330)
GENTLE_ROTATIONS = (0, 15, 345, 30, 330, 45, 315, 60, 300)
# The references are tilted, but still read left-to-right as side-profile
# animals. Try shallow diagonals first; steep diagonals tended to produce
# tall, abstract loops in Gangnam-style grids.
DIAGONAL_ROTATIONS = (30, 330, 45, 315, 15, 345, 60, 300, 0)
# Reference GPS-art animals stand upright (0 degrees) and Seoul street grids
# are mostly axis-aligned, so the upright pose snaps best AND its strokes
# follow whole streets instead of staircasing across the grid. The strict
# pass therefore only tries shallow poses; steeper tilts remain available in
# the relaxed fallback for neighborhoods whose grid is genuinely rotated.
SIDE_PROFILE_ROTATIONS = (0, 15, 345)
FALLBACK_ROTATIONS = (0, 15, 345, 30, 330, 45, 315, 60, 300)
ROTATIONS = UPRIGHT_ROTATIONS
SCALES = (1.0, 0.85, 1.15)  # real road grids often fit a slightly resized shape
FIT_SCALES = (0.8, 0.9, 0.85, 0.7, 1.0, 0.6, 0.5, 1.1, 1.15)
FALLBACK_SCALES = (0.75, 0.85, 0.65, 0.95, 0.55, 1.05, 1.15, 0.45, 1.25)

# Numeric "cute & simple" spec (user-provided targets):
MAX_SHAPE_DISTANCE_ERROR = 0.10   # 목표 거리 오차 ±10%
MIN_ANCHORS = 8                   # authored feature corners; routing may add smooth outline guides
MAX_ANCHORS = 32                  # fewer anchors = fewer forced corners = straighter strokes
BACKTRACK_MAX_FRAC = 0.20         # 왕복(같은 도로 되짚기) 구간 20% 이하
OUTLINE_MIN_FRAC = 0.70           # 전체 루트의 70% 이상은 큰 외곽선(완만한 구간)
SHARP_TURN_DEG = 100.0            # 이보다 큰 방향 전환은 "꺾는 곳"(귀/꼬리/다리 코너)으로 취급
SIMILARITY_TOLERANCE_FRAC = 0.085  # 평균 점수로 사라진 귀/꼬리를 숨기지 않도록 엄격히 비교

# Anytime cutoffs (PRD §7.1). PlayMCP requires p99 <= 3s per tool call, so the
# budget is speed-first: a single animal must finish well under ~1.5s even on a
# cold cache. Quality is deliberately traded for latency here — fewer rotations,
# scales and placements, and a short corridor-refinement window.
TIME_BUDGET_S = 1.6  # nearby-center screening + corridor refinement
FALLBACK_BUDGET_S = 0.8
SCREEN_BUDGET_FRAC = 0.5
PROBE_BUDGET_S = 0.3
GOOD_ENOUGH = 0.70  # early exit — no need to keep searching past this


@dataclass(frozen=True)
class ShapeSpec:
    key: str
    name_ko: str
    emoji: str
    min_km: float
    # Closed polyline, roughly centered on origin, arbitrary scale.
    outline: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class ShapeStyle:
    rotations: tuple[int, ...]
    scales: tuple[float, ...]
    similarity_gate: float
    outline_min_frac: float
    max_sharp_turns: int
    corridor_frac: float
    aspect_min: float
    aspect_max: float
    simplicity_weight: float = 0.015
    zigzag_weight: float = 0.035


def _closed(points: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    if points[0] != points[-1]:
        points = points + [points[0]]
    return tuple(points)


# Cute, minimal full-body side-profile silhouettes. The reference routes work
# because the whole animal reads first, then one or two exaggerated features
# identify the species. Authored points describe only the outer contour and
# its identifying features; hidden routing guides may be added later.
SHAPES: dict[str, ShapeSpec] = {
    s.key: s
    for s in (
        # Feature depth matters more than realism: on a city map a leg gap or
        # ear notch shallower than the coverage tolerance (8% of the shape
        # diameter) can be shortcut without any metric noticing, which is how
        # blobs used to win. Every species feature below is at least ~15% of
        # the shape diameter deep, like the reference GPS-art routes.
        ShapeSpec(
            "rabbit", "토끼", "🐰", 3.0,
            # Blocky reference style: one rectangular body/head block with two
            # tall rectangular ears. Every segment is axis-aligned so the
            # route runs straight along whole streets instead of staircasing.
            _closed([(0.0, 0.0), (3.4, 0.0), (3.4, 4.2), (2.9, 4.2),
                     (2.9, 7.0), (2.1, 7.0), (2.1, 4.2), (1.3, 4.2),
                     (1.3, 7.0), (0.5, 7.0), (0.5, 4.2), (0.0, 4.2)]),
        ),
        ShapeSpec(
            "cat", "고양이", "🐱", 3.0,
            # Blocky reference style: big square head with two rectangular
            # ears, one rectangular body, two short legs and an L-shaped
            # raised tail. Fully axis-aligned — diagonal ear/tail strokes
            # staircase across Seoul's grid and destroy the silhouette.
            _closed([(0.0, 2.2), (0.0, 6.8), (0.9, 6.8), (0.9, 5.4),
                     (2.1, 5.4), (2.1, 6.8), (3.0, 6.8), (3.0, 4.0),
                     (6.8, 4.0), (6.8, 5.9), (7.8, 5.9), (7.8, 0.0),
                     (6.8, 0.0), (6.8, 1.8), (4.2, 1.8), (4.2, 0.0),
                     (3.2, 0.0), (3.2, 2.2)]),
        ),
        ShapeSpec(
            "dog", "강아지", "🐶", 5.0,
            # Reference GPS-art dog: big blocky head on the left, one tall
            # ear, a broad rectangular body, two short legs and a raised tail.
            # Keep it as a simple outer contour; small anatomy makes map
            # routes jagged and less like the supplied reference.
            _closed([(0.2, 3.1), (0.2, 4.6), (1.4, 4.8), (1.7, 6.2),
                     (2.3, 4.9), (3.2, 4.8), (3.0, 3.6), (5.9, 3.7),
                     (6.4, 4.6), (7.0, 4.0), (6.1, 3.2), (6.2, 1.6),
                     (5.2, 1.6), (5.2, 0.5), (4.4, 0.5), (4.3, 1.7),
                     (2.7, 1.7), (2.7, 0.5), (1.9, 0.5), (1.8, 1.8),
                     (0.8, 2.0), (0.8, 3.0)]),
        ),
        ShapeSpec(
            "whale", "고래", "🐳", 3.0,
            # Blocky reference style: one long body block, narrow tail stock
            # and an oversized V fluke — 10 points, mostly straight strokes.
            _closed([(0.0, 0.4), (0.4, 3.0), (5.8, 3.0), (6.4, 1.9),
                     (8.2, 3.6), (7.8, 1.7), (9.0, 0.2), (6.8, 1.0),
                     (5.4, 0.0), (0.6, 0.0)]),
        ),
    )
}


SHAPE_STYLES: dict[str, ShapeStyle] = {
    # Icon-like land animals. The reference images read because the
    # silhouette is simple before it is clever: one body, one head, short legs,
    # and exaggerated ears/tail. Rotation is allowed when it makes the animal
    # clearer on the local road grid. Diagonal side-profile candidates are
    # tried first because the supplied references mostly lie about 45 degrees.
    "cat": ShapeStyle(
        rotations=SIDE_PROFILE_ROTATIONS,
        scales=(0.9, 1.0, 0.8),
        # Lower than dog: the L-shaped tail and rear leg share street
        # columns, which costs a few similarity points even on clean grids
        # (Gangnam peaks around 0.72-0.78 depending on the time budget).
        similarity_gate=0.70,
        outline_min_frac=0.66,
        max_sharp_turns=6,
        corridor_frac=0.075,
        aspect_min=1.05,
        aspect_max=2.00,
        simplicity_weight=0.040,
        zigzag_weight=0.070,
    ),
    "dog": ShapeStyle(
        rotations=SIDE_PROFILE_ROTATIONS,
        scales=(0.9, 0.85, 1.0),
        # Speed-first gate: the reduced candidate search rarely reaches the old
        # 0.78, so accept a slightly looser dog silhouette to keep latency low.
        similarity_gate=0.68,
        outline_min_frac=0.70,
        max_sharp_turns=6,
        corridor_frac=0.065,
        aspect_min=1.05,
        aspect_max=1.60,
        simplicity_weight=0.045,
        zigzag_weight=0.070,
    ),
    "rabbit": ShapeStyle(
        rotations=SIDE_PROFILE_ROTATIONS,
        scales=(0.9, 0.85, 1.0),
        similarity_gate=0.70,
        outline_min_frac=0.66,
        max_sharp_turns=5,
        corridor_frac=0.075,
        aspect_min=0.35,
        aspect_max=0.75,
        simplicity_weight=0.040,
        zigzag_weight=0.070,
    ),
    # Whale keeps a little tilt freedom: a simple body + V fluke still reads
    # rotated, and it fits rivers/large roads better with shallow diagonals.
    "whale": ShapeStyle(
        rotations=(0, 15, 345),
        scales=(0.9, 1.0, 0.85),
        similarity_gate=0.72,
        outline_min_frac=0.75,
        max_sharp_turns=4,
        corridor_frac=0.09,
        aspect_min=1.70,
        aspect_max=3.20,
        simplicity_weight=0.040,
        zigzag_weight=0.065,
    ),
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


def _corner_prominence(outline) -> list[tuple[float, tuple[float, float]]]:
    """(prominence, vertex) for every template vertex, most prominent first.

    Prominence = turn sharpness × adjacent segment length, so genuine
    features (ear tips, tail tip, snout — sharp AND reached by a real
    stretch of outline) rank above tiny zigzag noise (sharp but with almost
    no adjacent length)."""
    pts = list(outline[:-1]) if outline[0] == outline[-1] else list(outline)
    n = len(pts)
    scored = []
    for i in range(n):
        ax, ay = pts[(i - 1) % n]
        bx, by = pts[i]
        cx, cy = pts[(i + 1) % n]
        v1 = (bx - ax, by - ay)
        v2 = (cx - bx, cy - by)
        m1, m2 = math.hypot(*v1), math.hypot(*v2)
        if m1 < 1e-9 or m2 < 1e-9:
            continue
        cos_a = (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)
        sharpness = 1.0 - max(-1.0, min(1.0, cos_a))  # 0 straight .. 2 full reversal
        prominence = sharpness * min(m1, m2)
        scored.append((prominence, (bx, by)))
    scored.sort(key=lambda t: -t[0])
    return scored


def _arc_position_fn(outline):
    """Returns a function mapping any point to its arc-length position along
    the closed outline (nearest-projection), for stable ordering."""
    ring = list(outline)
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    cum = [0.0]
    for a, b in zip(ring, ring[1:]):
        cum.append(cum[-1] + math.dist(a, b))

    def arc_pos(p) -> float:
        best_s, best_d = 0.0, math.inf
        for k in range(len(ring) - 1):
            ax, ay = ring[k]
            bx, by = ring[k + 1]
            dx, dy = bx - ax, by - ay
            denom = dx * dx + dy * dy
            t = 0.0 if denom == 0 else max(0.0, min(1.0, ((p[0]-ax)*dx + (p[1]-ay)*dy) / denom))
            proj = (ax + t * dx, ay + t * dy)
            d = math.dist(p, proj)
            if d < best_d:
                best_d, best_s = d, cum[k] + t * math.dist((ax, ay), (bx, by))
        return best_s

    return arc_pos


def _resample_keep_corners(outline, n: int) -> list[tuple[float, float]]:
    """Keep every authored species corner and add evenly spaced outline guides.

    The guides are collinear/interpolated, so they do not create extra visual
    detail. They stop real-road routing from cutting across the long back,
    belly, ears or tail between sparse feature corners.
    """
    pts = list(outline[:-1]) if outline[0] == outline[-1] else list(outline)
    budget = max(MIN_ANCHORS, min(MAX_ANCHORS, n))
    if len(pts) > budget:
        ranked = _corner_prominence(outline)
        chosen = [p for _, p in ranked[:budget]]
    else:
        chosen = list(pts)
        for p in _resample(outline, budget * 3):
            if len(chosen) >= budget:
                break
            if all(math.dist(p, q) > 1e-6 for q in chosen):
                chosen.append(p)
    arc_pos = _arc_position_fn(outline)
    chosen.sort(key=arc_pos)
    out = []
    for p in chosen:
        if not out or math.dist(out[-1], p) > 1e-6:
            out.append(p)
    return out[:MAX_ANCHORS]


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
    return max(0.0, 1.0 - dev / (SIMILARITY_TOLERANCE_FRAC * scale_m))


def ordered_similarity(routed_xy: list[tuple[float, float]],
                       template_xy: list[tuple[float, float]],
                       scale_m: float) -> float:
    """Order-preserving outline similarity.

    Symmetric point-to-line distance can confuse a tail with a nearby back or
    collapse paired ears onto one head edge. Equal-distance samples compare the
    silhouette in traversal order, preserving the species feature sequence.
    """
    if len(routed_xy) < 3 or len(template_xy) < 3:
        return 0.0
    route_ring = list(routed_xy)
    template_ring = list(template_xy)
    if route_ring[0] != route_ring[-1]:
        route_ring.append(route_ring[0])
    if template_ring[0] != template_ring[-1]:
        template_ring.append(template_ring[0])
    n = 64
    route_samples = _resample(route_ring, n)
    template_samples = _resample(template_ring, n)
    best = math.inf
    # The concatenated road loop may start at any surviving snapped guide, so
    # compare every cyclic start while preserving clockwise feature order.
    for samples in (route_samples, list(reversed(route_samples))):
        for shift in range(n):
            dev = sum(
                math.dist(samples[(i + shift) % n], template_samples[i])
                for i in range(n)
            ) / n
            best = min(best, dev)
    # Road grids advance unevenly around corners; allow local phase stretch
    # while still rejecting gross feature-order changes.
    return max(0.0, 1.0 - best / (0.38 * max(scale_m, 1.0)))


# ---------- simplicity metrics (numeric spec) ----------
#
# These score the ANCHOR-level polygon (≤12 points) — cheap, and this is
# where "simple silhouette" actually lives; the routed road path inherits
# whatever shape the anchors describe.

def _turn_angles_deg(ring_xy: list[tuple[float, float]]) -> list[float]:
    """Interior turn angle at each vertex of a closed ring, in degrees
    (0 = straight through, 180 = full reversal)."""
    n = len(ring_xy)
    angles = []
    for i in range(n):
        ax, ay = ring_xy[(i - 1) % n]
        bx, by = ring_xy[i]
        cx, cy = ring_xy[(i + 1) % n]
        v1, v2 = (bx - ax, by - ay), (cx - bx, cy - by)
        m1, m2 = math.hypot(*v1), math.hypot(*v2)
        if m1 < 1e-9 or m2 < 1e-9:
            angles.append(0.0)
            continue
        cos_a = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)))
        angles.append(math.degrees(math.acos(cos_a)))
    return angles


def outline_fraction(anchors_xy: list[tuple[float, float]]) -> float:
    """Share of the anchor polygon's perimeter that is NOT adjacent to a
    sharp (> SHARP_TURN_DEG) corner — i.e. the "big outline" (back/belly)
    versus feature corners (ears/tail/snout/legs). Target ≥ OUTLINE_MIN_FRAC."""
    n = len(anchors_xy)
    if n < 3:
        return 1.0
    angles = _turn_angles_deg(anchors_xy)
    seg_len = [math.dist(anchors_xy[i], anchors_xy[(i + 1) % n]) for i in range(n)]
    total = sum(seg_len) or 1.0
    # A sharp feature should not erase both adjacent outline segments from the
    # "big outline" share. Count each sharp endpoint as half of its adjoining
    # segment instead; ears/tails still register, but the surrounding silhouette
    # remains credited as outer contour.
    sharp_len = sum(
        seg_len[i] * 0.5 * (
            (1 if angles[i] > SHARP_TURN_DEG else 0)
            + (1 if angles[(i + 1) % n] > SHARP_TURN_DEG else 0)
        )
        for i in range(n)
    )
    return 1.0 - sharp_len / total


def sharp_turn_count(anchors_xy: list[tuple[float, float]]) -> int:
    """Number of anchor vertices that are genuine feature corners (minimize
    "각도 변화 많은 구간")."""
    return sum(1 for a in _turn_angles_deg(anchors_xy) if a > SHARP_TURN_DEG)


def small_zigzag_penalty(routed_xy: list[tuple[float, float]],
                         length_m: float) -> float:
    """Density of short left-right staircase bends in the routed outline.

    Species features create a few intentional concave turns. Street snapping
    creates a different signature: direction changes alternate repeatedly
    within one or two blocks. Keep a generous feature allowance, then penalize
    only the excess plus shallow visual noise along otherwise broad contours.
    """
    if len(routed_xy) < 4 or length_m <= 0:
        return 0.0

    cumulative = [0.0]
    for a, b in zip(routed_xy, routed_xy[1:]):
        cumulative.append(cumulative[-1] + math.dist(a, b))

    events: list[tuple[float, float]] = []
    shallow = 0
    for i in range(1, len(routed_xy) - 1):
        a, b, c = routed_xy[i - 1], routed_xy[i], routed_xy[i + 1]
        incoming = (b[0] - a[0], b[1] - a[1])
        outgoing = (c[0] - b[0], c[1] - b[1])
        if math.hypot(*incoming) < 8.0 or math.hypot(*outgoing) < 8.0:
            continue
        cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
        dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
        angle = math.degrees(math.atan2(cross, dot))
        if 25.0 <= abs(angle) < 65.0:
            shallow += 1
        if abs(angle) >= 30.0:
            events.append((cumulative[i], angle))

    alternating = sum(
        1
        for (distance_a, angle_a), (distance_b, angle_b) in zip(events, events[1:])
        if angle_a * angle_b < 0 and distance_b - distance_a <= 180.0
    )
    km = max(length_m / 1000.0, 0.1)
    # Up to four alternating bends are reserved for ears, legs, tail/fluke
    # and other authored silhouette corners.
    excess_alternating = max(0, alternating - 4)
    return 1.4 * excess_alternating / km + 0.25 * shallow / km


def diagonal_rotation_error(rotation_deg: int | float) -> float:
    """0 near a readable side-profile pose (upright or shallow diagonal)."""
    rot = rotation_deg % 360
    err = min(abs((rot - t + 180) % 360 - 180)
              for t in (0, 15, 345, 30, 330, 45, 315))
    return min(err, 90.0) / 90.0


def aspect_penalty(routed_xy: list[tuple[float, float]], style: ShapeStyle) -> float:
    """How far the visible bbox is from the reference-like side-profile frame.

    The reference animals are cute GPS art because the full outer silhouette
    reads before the small details. If the route bbox becomes tall and skinny,
    ears/tails collapse into map noise even when point-to-line similarity is
    numerically high.
    """
    if not routed_xy:
        return 1.0
    xs = [x for x, _ in routed_xy]
    ys = [y for _, y in routed_xy]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if width <= 1e-6 or height <= 1e-6:
        return 1.0
    aspect = width / height
    if style.aspect_min <= aspect <= style.aspect_max:
        return 0.0
    if aspect < style.aspect_min:
        return min(1.0, (style.aspect_min - aspect) / style.aspect_min)
    return min(1.0, (aspect - style.aspect_max) / style.aspect_max)


def side_profile_pose_penalty(routed_xy: list[tuple[float, float]]) -> float:
    """Penalty when the actual routed silhouette loses its diagonal pose.

    Rotation of the source template is not enough: street routing can turn a
    30-degree animal into a near-vertical loop. PCA measures the final route's
    long axis. Reference-like poses lie around 30-45 degrees (or their mirrored
    135-150 degree equivalents).
    """
    if len(routed_xy) < 3:
        return 1.0
    ring = list(routed_xy)
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    sampled = _resample(ring, min(48, max(12, len(routed_xy) // 3)))
    cx = sum(x for x, _ in sampled) / len(sampled)
    cy = sum(y for _, y in sampled) / len(sampled)
    xx = sum((x - cx) ** 2 for x, _ in sampled) / len(sampled)
    yy = sum((y - cy) ** 2 for _, y in sampled) / len(sampled)
    xy = sum((x - cx) * (y - cy) for x, y in sampled) / len(sampled)
    angle = (0.5 * math.degrees(math.atan2(2.0 * xy, xx - yy))) % 180.0
    err = min(abs(angle - target)
              for target in (0.0, 30.0, 45.0, 135.0, 150.0, 180.0))
    return min(1.0, err / 35.0)


def _segments_cross(p1, p2, p3, p4) -> bool:
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])
    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and ccw(p1, p2, p3) != ccw(p1, p2, p4)


def count_self_intersections(ring_xy: list[tuple[float, float]]) -> int:
    """True crossings in a closed ring (shared-vertex touches at road
    intersections are NOT counted — only segments that actually cross).
    "self-intersection 금지" is enforced using this.

    Bounding-box sweep along x: routed rings have thousands of short
    segments, so only the handful whose x-ranges overlap are ever compared —
    same count as the all-pairs check at a fraction of the cost."""
    n = len(ring_xy)
    if n < 4:
        return 0
    segs = [(ring_xy[i], ring_xy[(i + 1) % n]) for i in range(n)]
    order = sorted(range(n), key=lambda i: min(segs[i][0][0], segs[i][1][0]))
    # active: (index, max_x, min_y, max_y) of segments whose x-range may
    # still overlap upcoming segments.
    active: list[tuple[int, float, float, float]] = []
    count = 0
    for idx in order:
        a, b = segs[idx]
        min_x, max_x = min(a[0], b[0]), max(a[0], b[0])
        min_y, max_y = min(a[1], b[1]), max(a[1], b[1])
        remaining = []
        for entry in active:
            j, j_max_x, j_min_y, j_max_y = entry
            if j_max_x < min_x:
                continue  # ends before this segment starts — drop forever
            remaining.append(entry)
            if j_max_y < min_y or j_min_y > max_y:
                continue
            if (idx - j) % n in (1, n - 1):
                continue  # consecutive segments share a vertex
            if _segments_cross(a, b, *segs[j]):
                count += 1
        active = remaining
        active.append((idx, max_x, min_y, max_y))
    return count


def backtrack_fraction(g, path: list) -> float:
    """Share of route length spent on an edge that's traveled more than once
    (there-and-back or re-looped) — "왕복 구간". Target ≤ BACKTRACK_MAX_FRAC."""
    edge_len: dict[frozenset, float] = {}
    counts: dict[frozenset, int] = {}
    total = 0.0
    for u, v in zip(path, path[1:]):
        key = frozenset((u, v))
        length = g.edges[u, v]["length"]
        edge_len[key] = length
        counts[key] = counts.get(key, 0) + 1
        total += length
    if total == 0:
        return 0.0
    dup = sum(edge_len[k] * (c - 1) for k, c in counts.items() if c > 1)
    return dup / total


# Corridor routing (윤곽 추종): route each template segment inside a ribbon
# around the ideal line. Edges outside the ribbon are hidden; inside it,
# lateral deviation is penalized quadratically — the routed path hugs the
# silhouette instead of taking alley shortcuts. This is what turns "blob
# that scores 0.8" into a course that visually reads as the animal
# (concave features like leg arches survive because the shortcut across
# the arch lies outside the ribbon).
CORRIDOR_FRAC = 0.09       # ribbon half-width as fraction of shape diameter
CORRIDOR_MIN_M = 70.0
CORRIDOR_MAX_M = 180.0
REFINE_TOP_K = 4           # speed: only the very best screening candidates
SNAP_CANDIDATES = 4
SNAP_BEAM_WIDTH = 10
MAIN_ROAD_EDGE_PENALTY_M = 14.0
# Big-road preference is intentionally strong (user: "큰길 위주"). Broad named
# arterials are cheap; alleys, service roads and tiny footpaths are expensive,
# so the silhouette strokes run straight along whole main streets.
HIGHWAY_COST_FACTOR = {
    "primary": 0.70,
    "primary_link": 0.78,
    "secondary": 0.74,
    "secondary_link": 0.82,
    "tertiary": 0.86,
    "tertiary_link": 0.92,
    "trunk": 0.74,
    "trunk_link": 0.82,
    "unclassified": 1.05,
    "residential": 1.30,
    "living_street": 1.40,
    "service": 1.60,
    "footway": 1.45,
    "path": 1.55,
    "pedestrian": 1.20,
    "steps": 1.90,
}


def main_road_cost(attrs: dict) -> float:
    """Lower cost for broad, named streets; higher for alleys and tiny paths."""
    highway = attrs.get("highway")
    if isinstance(highway, (list, tuple)):
        highway = highway[0] if highway else None
    factor = HIGHWAY_COST_FACTOR.get(str(highway), 1.08)
    sidewalk = float(attrs.get("sidewalk_score", 0.5))
    if sidewalk >= 0.85:
        factor *= 0.88
    elif sidewalk < 0.55:
        factor *= 1.16
    if not attrs.get("name") and str(highway) in {"service", "footway", "path"}:
        factor *= 1.10
    return factor


def _road_weight(u, v, attrs) -> float:
    """Dijkstra weight that prefers broad main streets over alleys, so the
    silhouette strokes run straight along big walkable roads."""
    return attrs["length"] * main_road_cost(attrs)


def main_road_badness(g, path: list) -> float:
    """Length-weighted penalty for alley-like streets in the final trace."""
    total = 0.0
    bad = 0.0
    for u, v in zip(path, path[1:]):
        attrs = g.edges[u, v]
        length = attrs["length"]
        total += length
        bad += max(0.0, main_road_cost(attrs) - 1.0) * length
    return bad / max(total, 1.0)


def _strip_backtracks(path: list) -> list:
    """Remove immediate out-and-back spurs (…A,B,A…) left at anchor joints."""
    changed = True
    while changed and len(path) > 2:
        changed = False
        out = [path[0]]
        for n in path[1:]:
            if len(out) >= 2 and out[-2] == n:
                out.pop()
                changed = True
            else:
                out.append(n)
        path = out
    return path


def _snip_small_loops(g, path: list, node_xy: dict,
                      max_frac: float = 0.10, max_pass: int = 3) -> list:
    """Cut tiny self-crossing loops out of a routed ring.

    Corridor routing around dense guides occasionally doubles back on itself
    for a block or two. Such a crossing is fatal to the similarity gate but
    visually meaningless, so reroute across the small loop instead of
    discarding the whole candidate.
    """
    for _ in range(max_pass):
        xy = [node_xy[n] for n in path]
        seg_len = [math.dist(xy[k], xy[k + 1]) for k in range(len(xy) - 1)]
        total = sum(seg_len) or 1.0
        cum = [0.0]
        for s in seg_len:
            cum.append(cum[-1] + s)
        snipped = False
        n_seg = len(seg_len)
        for i in range(n_seg):
            if snipped:
                break
            for j in range(i + 2, n_seg):
                if _segments_cross(xy[i], xy[i + 1], xy[j], xy[j + 1]):
                    arc = cum[j] - cum[i + 1]
                    if arc > max_frac * total:
                        continue
                    try:
                        seg = nx.bidirectional_dijkstra(
                            g, path[i], path[j + 1], weight="length")[1]
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        continue
                    path = _strip_backtracks(path[:i] + seg + path[j + 2:])
                    snipped = True
                    break
        if not snipped:
            break
    return path


def _angle_error(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Normalized direction difference between two vectors (0..1)."""
    ma, mb = math.hypot(*a), math.hypot(*b)
    if ma < 1e-9 or mb < 1e-9:
        return 1.0
    cos_a = max(-1.0, min(1.0, (a[0] * b[0] + a[1] * b[1]) / (ma * mb)))
    return math.acos(cos_a) / math.pi


def _snap_shape_anchors(g, anchors_xy: list[tuple[float, float]],
                        lat0: float, lon0: float, diameter_m: float,
                        node_xy: dict,
                        ) -> tuple[list, list[tuple[float, float]], list[float]]:
    """Snap the full silhouette jointly instead of vertex-by-vertex.

    A small beam search keeps nearby road choices that preserve each template
    segment's direction and length. This is especially important for paired
    ears, short legs, a dog snout and a whale fluke: independent nearest-node
    snapping frequently merges those characteristic points.
    """
    choices: list[list[tuple[object, float]]] = []
    # Wide search radii let anchors get dragged hundreds of meters and warp
    # the silhouette; placements on roadless blocks are already pruned before
    # snapping, so a tighter radius is safe.
    search_m = min(340.0, max(150.0, diameter_m * 0.13))
    for x, y in anchors_xy:
        lat, lon = to_latlon(x, y, lat0, lon0)
        nearby = [item for item in graphmod.nearby_nodes(
            lat, lon, limit=SNAP_CANDIDATES, max_distance_m=search_m
        ) if item[0] in g]
        if not nearby:
            node, snap = graphmod.nearest_node(lat, lon)
            if node not in g or snap > 800:
                return [], [], []
            nearby = [(node, snap)]
        choices.append(nearby)

    # cost, selected nodes, snap distances. Geometry costs are normalized so
    # a feature-preserving second-nearest node can beat a collapsed nearest one.
    beams: list[tuple[float, list, list[float]]] = [(0.0, [], [])]
    for i, candidates in enumerate(choices):
        expanded: list[tuple[float, list, list[float]]] = []
        ideal = None if i == 0 else (
            anchors_xy[i][0] - anchors_xy[i - 1][0],
            anchors_xy[i][1] - anchors_xy[i - 1][1],
        )
        ideal_len = math.hypot(*ideal) if ideal is not None else 0.0
        for cost, nodes, snaps in beams:
            for node, snap in candidates:
                step_cost = 0.45 * snap / max(diameter_m, 1.0)
                if nodes:
                    if node == nodes[-1]:
                        step_cost += 4.0
                    actual = (
                        node_xy[node][0] - node_xy[nodes[-1]][0],
                        node_xy[node][1] - node_xy[nodes[-1]][1],
                    )
                    actual_len = math.hypot(*actual)
                    step_cost += 0.70 * _angle_error(ideal, actual)
                    step_cost += 0.35 * abs(actual_len - ideal_len) / max(ideal_len, 1.0)
                    if node in nodes[:-1]:
                        step_cost += 1.5
                expanded.append((cost + step_cost, nodes + [node], snaps + [snap]))
        expanded.sort(key=lambda item: item[0])
        beams = expanded[:SNAP_BEAM_WIDTH]
        if not beams:
            return [], [], []

    # Include the closing edge in the choice. It is part of the animal's main
    # outline and otherwise tends to become an arbitrary map shortcut.
    scored = []
    closing_ideal = (
        anchors_xy[0][0] - anchors_xy[-1][0],
        anchors_xy[0][1] - anchors_xy[-1][1],
    )
    closing_len = math.hypot(*closing_ideal)
    for cost, nodes, snaps in beams:
        actual = (
            node_xy[nodes[0]][0] - node_xy[nodes[-1]][0],
            node_xy[nodes[0]][1] - node_xy[nodes[-1]][1],
        )
        cost += 0.70 * _angle_error(closing_ideal, actual)
        cost += 0.35 * abs(math.hypot(*actual) - closing_len) / max(closing_len, 1.0)
        scored.append((cost, nodes, snaps))
    _, nodes, snaps = min(scored, key=lambda item: item[0])

    kept_nodes, kept_anchors, kept_snaps = [], [], []
    for node, anchor, snap in zip(nodes, anchors_xy, snaps):
        if not kept_nodes or node != kept_nodes[-1]:
            kept_nodes.append(node)
            kept_anchors.append(anchor)
            kept_snaps.append(snap)
    return kept_nodes, kept_anchors, kept_snaps


def _densify_route_guides(g, major_nodes: list,
                          major_anchors: list[tuple[float, float]],
                          major_snaps: list[float], lat0: float, lon0: float,
                          step_m: float = 95.0
                          ) -> tuple[list, list[tuple[float, float]], list[float]]:
    """Add hidden road-following guides along long silhouette edges.

    The authored animal still has only 8-12 semantic control points. These
    guides merely prevent a 500m back or belly edge from taking a shortest-path
    detour between its endpoints and disappearing from the final GPS trace.
    """
    dense_nodes: list = []
    dense_anchors: list[tuple[float, float]] = []
    dense_snaps: list[float] = []
    count = len(major_nodes)
    for i in range(count):
        a = major_anchors[i]
        b = major_anchors[(i + 1) % count]
        seg_len = math.dist(a, b)
        pieces = max(1, int(math.ceil(seg_len / step_m)))
        for part in range(pieces):
            if part == 0:
                node, snap = major_nodes[i], major_snaps[i]
                guide = a
            else:
                t = part / pieces
                guide = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                lat, lon = to_latlon(guide[0], guide[1], lat0, lon0)
                node, snap = graphmod.nearest_node(lat, lon)
                if node not in g or snap > 500:
                    return [], [], []
            if not dense_nodes or node != dense_nodes[-1]:
                dense_nodes.append(node)
                dense_anchors.append(guide)
                dense_snaps.append(snap)
    return dense_nodes, dense_anchors, dense_snaps


def _corridor_route(g, node_xy, a, b, seg_a, seg_b, corridor_m: float) -> list:
    ax, ay = seg_a
    bx, by = seg_b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy

    def lateral(p) -> float:
        px, py = p
        t = 0.0 if denom == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        return math.dist((px, py), (ax + t * dx, ay + t * dy))

    # The snapped endpoints may sit outside the nominal ribbon (snapping
    # tolerates a few hundred meters). Widen just enough to contain them,
    # otherwise the segment is unroutable by construction.
    ribbon = max(corridor_m, lateral(node_xy[a]) + 30.0, lateral(node_xy[b]) + 30.0)

    def weight(u, v, attrs):
        d = (lateral(node_xy[u]) + lateral(node_xy[v])) * 0.5
        if d > ribbon:
            return None  # hidden edge — stay inside the ribbon
        # Main-road preference inside the ribbon: given several streets that
        # trace the same silhouette stroke, take the broad straight one.
        return attrs["length"] * main_road_cost(attrs) * (1.0 + 16.0 * (d / ribbon) ** 2)

    return nx.bidirectional_dijkstra(g, a, b, weight=weight)[1]


def _route_loop(g, nodes: list, anchors_xy: list, node_xy: dict | None,
                corridor_m: float | None) -> list:
    """Concatenate per-segment routes into a closed loop. With node_xy and
    corridor_m set, segments follow the template; otherwise plain shortest."""
    # Map each snapped node to its anchor position for corridor endpoints.
    path: list = []
    n = len(nodes)
    for i in range(n):
        a, b = nodes[i], nodes[(i + 1) % n]
        seg = None
        if corridor_m is not None and node_xy is not None:
            for width in (corridor_m, corridor_m * 1.45, corridor_m * 2.1):
                try:
                    seg = _corridor_route(g, node_xy, a, b,
                                          anchors_xy[i], anchors_xy[(i + 1) % len(anchors_xy)],
                                          min(CORRIDOR_MAX_M, width))
                    break
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    seg = None
            # A roadless block (palace, park, river) on one silhouette edge
            # must not kill the whole candidate: route just that segment
            # freely and keep the rest of the outline corridor-following.
        if seg is None:
            seg = nx.bidirectional_dijkstra(g, a, b, weight=_road_weight)[1]
        path.extend(seg if not path else seg[1:])
    return _strip_backtracks(path)


def _evaluate(g, params: CourseParams, path: list, anchors_xy: list,
              diameter_m: float, target_m: float,
              style: ShapeStyle,
              rotation_error: float = 0.0,
              snap_error: float = 0.0,
              refined: bool = False,
              check_self_intersection: bool = False) -> tuple[Course, float, float, tuple]:
    routed_xy = [to_xy(g.nodes[n]["lat"], g.nodes[n]["lon"], params.lat, params.lon)
                 for n in path]
    sim = min(
        similarity(routed_xy, anchors_xy, diameter_m),
        ordered_similarity(routed_xy, anchors_xy, diameter_m),
    )
    # Mean distance can hide exactly the failure users notice first: one
    # missing ear, tail, snout or fluke. Track the worst template feature and
    # require every major anchor to remain visible on the routed outline.
    closed_r = routed_xy + [routed_xy[0]]
    feature_devs = [_point_to_polyline_dist(p, closed_r) for p in anchors_xy]
    worst_feature_error = max(feature_devs, default=diameter_m) / max(diameter_m, 1.0)
    feature_coverage = (
        sum(d <= diameter_m * 0.080 for d in feature_devs) / len(feature_devs)
        if feature_devs else 0.0
    )
    length, ascent = _path_metrics(g, path)
    length_err = abs(length - target_m) / target_m
    if length_err > MAX_SHAPE_DISTANCE_ERROR:
        sim *= 0.7  # strong penalty: cute shape is not useful at the wrong distance

    # Simplicity metrics (numeric spec). Anchor-level checks are cheap and
    # always run; the O(n²) self-intersection check on the full routed path
    # only runs for corridor-refined finalists (REFINE_TOP_K) — too slow to
    # run on every screening candidate within the time budget.
    outline_frac = outline_fraction(anchors_xy)
    n_sharp = sharp_turn_count(anchors_xy)
    backtrack_frac = backtrack_fraction(g, path)
    aspect_badness = aspect_penalty(routed_xy, style)
    pose_badness = side_profile_pose_penalty(routed_xy)
    zigzag_badness = small_zigzag_penalty(routed_xy, length)
    road_badness = main_road_badness(g, path)
    self_intersects = check_self_intersection and count_self_intersections(routed_xy) > 0
    if self_intersects:
        # Hard forbid ("self-intersection 금지"): never let this clear the
        # similarity gate, whatever else looks good about it.
        sim = min(sim, style.similarity_gate - 0.01)

    points = [(g.nodes[n]["lat"], g.nodes[n]["lon"]) for n in path]
    fac_hits, fac_total = facility_requirement_score(points, params.need_facilities)
    clears_gate = sim >= style.similarity_gate
    follow = followability_penalty(points, length)
    # Hard constraints first (crossings, distance gate, no missing species
    # feature), then readability dominates: template fit minus stroke
    # complexity. A tilted candidate that traces slightly closer but turns a
    # street grid into a staircase of jogs loses to the upright candidate the
    # reference routes look like; every softer signal only breaks near-ties.
    readability = (
        sim
        - style.simplicity_weight * follow
        - style.zigzag_weight * zigzag_badness
        - 0.12 * road_badness
    )
    key = (
        self_intersects,
        length_err > MAX_SHAPE_DISTANCE_ERROR,
        feature_coverage < 1.0,
        -round(readability / 0.02),
        round(worst_feature_error, 2),
        round(zigzag_badness, 2),
        round(road_badness, 2),
        round(pose_badness, 2) if pose_badness > 0.20 else 0.0,
        aspect_badness > 0.0,
        rotation_error,
        fac_total - fac_hits if clears_gate else 999,
        backtrack_frac > BACKTRACK_MAX_FRAC,
        outline_frac < style.outline_min_frac,
        n_sharp > style.max_sharp_turns,
        length_err,
        snap_error,
        follow,
        -sim,
    )
    course = Course(
        params=params,
        path=path,
        points=points,
        length_m=length,
        ascent_m=ascent,
        rfs=route_rfs_summary(g, path, params.night_mode, params.include_hills),
        shape_similarity=round(sim, 3),
    )
    return course, sim, length_err, key


# Search-area cache: the subgraph, projected node coordinates and the road
# occupancy grid depend only on the center and footprint radius — not on the
# shape, distance or rotation. A distance sweep re-enters _search_shape many
# times for the same start point; rebuilding these each time used to dominate
# the per-try cost. Radii are bucketed upward so nearby distances share one
# entry (a slightly larger subgraph never removes routing options).
_AREA_CACHE: dict[tuple, tuple] = {}
_AREA_CACHE_MAX = 16
_AREA_RADIUS_BUCKET_M = 500.0
_OCCUPANCY_CELL_M = 130.0


def _search_area(lat0: float, lon0: float, radius_m: float):
    bucket = math.ceil(radius_m / _AREA_RADIUS_BUCKET_M) * _AREA_RADIUS_BUCKET_M
    key = (round(lat0, 5), round(lon0, 5), bucket)
    cached = _AREA_CACHE.get(key)
    if cached is None:
        g = graphmod.subgraph_around(lat0, lon0, bucket)
        node_xy = {n: to_xy(g.nodes[n]["lat"], g.nodes[n]["lon"], lat0, lon0)
                   for n in g.nodes}
        occupied = {(int(x // _OCCUPANCY_CELL_M), int(y // _OCCUPANCY_CELL_M))
                    for x, y in node_xy.values()}
        if len(_AREA_CACHE) >= _AREA_CACHE_MAX:
            _AREA_CACHE.clear()
        cached = _AREA_CACHE[key] = (g, node_xy, occupied)
    return cached


def _search_shape(spec: ShapeSpec, params: CourseParams, deadline: float,
                  outline: tuple[tuple[float, float], ...] | None = None,
                  rotations=ROTATIONS, scales=SCALES,
                  refine: bool = True,
                  style: ShapeStyle | None = None) -> tuple[Course | None, float]:
    """Two-phase snap: screen rotation/scale candidates with plain shortest-
    path routing, then corridor-refine the top candidates so the final loop
    actually traces the silhouette."""
    target_m = params.distance_km * 1000.0
    style = style or SHAPE_STYLES.get(spec.key) or ShapeStyle(
        rotations=rotations,
        scales=scales,
        similarity_gate=SIMILARITY_GATE,
        outline_min_frac=OUTLINE_MIN_FRAC,
        max_sharp_turns=5,
        corridor_frac=CORRIDOR_FRAC,
        aspect_min=0.75,
        aspect_max=2.50,
    )
    # Sparse outline samples keep long back/belly strokes as single straight
    # road segments; corridor routing (not extra anchors) protects the shape.
    n_anchor = max(14, min(MAX_ANCHORS, int(target_m / 260)))
    source = outline or spec.outline
    template = _resample_keep_corners(source, n_anchor)
    base_scale = target_m / _outline_length(list(source) + [source[0]])
    xs = [p[0] for p in template]
    ys = [p[1] for p in template]
    diameter_units = max(max(xs) - min(xs), max(ys) - min(ys))

    lat0, lon0 = params.lat, params.lon
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    # Local subgraph bounded by the largest candidate footprint (PRD §7.1),
    # plus projected coordinates and the coarse road-occupancy grid used to
    # prune placements on roadless blocks (palace grounds, rivers, parks).
    # All three are location-cached and shared across the distance sweep.
    g, node_xy, occupied = _search_area(
        lat0, lon0, diameter_units * base_scale * max(scales) * 0.7 + 1000)
    cell = _OCCUPANCY_CELL_M

    def _placement_coverage(anchors) -> float:
        ok = 0
        for x, y in anchors:
            cx_, cy_ = int(x // cell), int(y // cell)
            if any((cx_ + dx_, cy_ + dy_) in occupied
                   for dx_ in (-1, 0, 1) for dy_ in (-1, 0, 1)):
                ok += 1
        return ok / max(len(anchors), 1)

    # The reference routes are drawn where the shape fits, not exactly on the
    # runner's doorstep. Search a few nearby placements. Speed-first: a small
    # cardinal set instead of the full 13-offset grid.
    OFFSETS = ((0.0, 0.0), (250.0, 0.0), (-250.0, 0.0),
               (0.0, 250.0), (0.0, -250.0))

    best: Course | None = None
    best_sim = -1.0
    best_len_err = math.inf
    best_key: tuple | None = None
    candidates: list[tuple[tuple, list, list, list, float, list[float], float]] = []
    # Screening must not starve corridor refinement — the refined loops are
    # the ones that actually look like the animal on the map.
    now = time.perf_counter()
    screen_deadline = now + (deadline - now) * SCREEN_BUDGET_FRAC
    for scale_f in scales:
        scale = base_scale * scale_f
        diameter_m = diameter_units * scale
        for rot in rotations:
            if time.perf_counter() > screen_deadline or (best_sim >= GOOD_ENOUGH and best_len_err <= 0.08):
                break
            cos_r, sin_r = math.cos(math.radians(rot)), math.sin(math.radians(rot))
            base_anchors = [
                (((x - cx) * cos_r - (y - cy) * sin_r) * scale,
                 ((x - cx) * sin_r + (y - cy) * cos_r) * scale)
                for x, y in template
            ]
            rotation_error = diagonal_rotation_error(rot)
            placements = sorted(
                ([(x + ox, y + oy) for x, y in base_anchors]
                 for ox, oy in OFFSETS),
                key=_placement_coverage, reverse=True)
            for anchors_xy in placements[:2]:
                if time.perf_counter() > screen_deadline:
                    break
                if _placement_coverage(anchors_xy) < 0.9:
                    continue
                nodes, kept_anchors, snap_dists = _snap_shape_anchors(
                    g, anchors_xy, lat0, lon0, diameter_m, node_xy)
                if len(nodes) < 6:
                    continue
                try:
                    path = _route_loop(g, nodes, kept_anchors, None, None)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                course, sim, length_err, key = _evaluate(
                    g, params, path, anchors_xy, diameter_m, target_m,
                    style,
                    rotation_error=rotation_error,
                    snap_error=(sum(snap_dists) / len(snap_dists)) / max(diameter_m, 1.0),
                    refined=False,
                    check_self_intersection=True)
                candidates.append((key, nodes, kept_anchors, anchors_xy,
                                   diameter_m, snap_dists, rotation_error))
                if best_key is None or key < best_key:
                    best_key, best_sim, best_len_err, best = key, sim, length_err, course
        if time.perf_counter() > screen_deadline or (best_sim >= GOOD_ENOUGH and best_len_err <= 0.08):
            break

    # Phase 2 — corridor refinement of the most promising configurations.
    if refine and candidates and time.perf_counter() < deadline:
        candidates.sort(key=lambda c: c[0])

        def _refine_once(major_nodes, major_anchors, major_snaps,
                         feature_anchors, diameter_m, rotation_error):
            nodes, route_guides, snap_dists = _densify_route_guides(
                g, major_nodes, major_anchors, major_snaps, lat0, lon0)
            if len(nodes) < len(major_nodes):
                return None
            corridor_m = min(CORRIDOR_MAX_M,
                             max(CORRIDOR_MIN_M, diameter_m * style.corridor_frac))
            try:
                path = _route_loop(g, nodes, route_guides, node_xy, corridor_m)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                return None
            path = _snip_small_loops(g, path, node_xy)
            return _evaluate(
                g, params, path, feature_anchors, diameter_m, target_m,
                style,
                rotation_error=rotation_error,
                snap_error=(sum(snap_dists) / len(snap_dists)) / max(diameter_m, 1.0),
                refined=True,
                check_self_intersection=True)

        for _, major_nodes, major_anchors, major_snaps_c, feature_anchors, diameter_m, rotation_error in [
            (c[0], c[1], c[2], c[5], c[3], c[4], c[6]) for c in candidates[:REFINE_TOP_K]
        ]:
            if time.perf_counter() > deadline:
                break
            result = _refine_once(major_nodes, major_anchors, major_snaps_c,
                                  feature_anchors, diameter_m, rotation_error)
            if result is None:
                continue
            course, sim, length_err, key = result
            # Faithfully tracing the outline is longer than the screening
            # shortcuts the scale was tuned on. Rescale the template by the
            # observed winding factor and re-snap, so corridor-refined shapes
            # land inside the distance gate instead of losing to blobs.
            if length_err > MAX_SHAPE_DISTANCE_ERROR and time.perf_counter() < deadline:
                k = max(0.55, min(1.45, target_m / max(course.length_m, 1.0)))
                anchors2 = [(x * k, y * k) for x, y in feature_anchors]
                nodes2, kept2, snaps2 = _snap_shape_anchors(
                    g, anchors2, lat0, lon0, diameter_m * k, node_xy)
                if len(nodes2) >= 6:
                    retry = _refine_once(nodes2, kept2, snaps2,
                                         anchors2, diameter_m * k, rotation_error)
                    if retry is not None and retry[3] < key:
                        course, sim, length_err, key = retry
            if best_key is None or key < best_key:
                best_key, best_sim, best_len_err, best = key, sim, length_err, course
    return best, best_sim


def _relaxed_style(style: ShapeStyle) -> ShapeStyle:
    """Best-effort mode for neighborhoods whose roads do not fit the strict
    icon template. It still favors the same diagonal side-profile, but allows
    more scale variation and a wider routing ribbon so we return a usable
    animal route instead of failing outright."""
    return ShapeStyle(
        rotations=FALLBACK_ROTATIONS,
        scales=FALLBACK_SCALES,
        similarity_gate=max(0.44, style.similarity_gate - 0.18),
        outline_min_frac=max(0.58, style.outline_min_frac - 0.10),
        max_sharp_turns=style.max_sharp_turns + 2,
        corridor_frac=min(0.14, style.corridor_frac + 0.04),
        aspect_min=max(0.70, style.aspect_min - 0.20),
        aspect_max=style.aspect_max + 0.50,
        simplicity_weight=style.simplicity_weight,
        zigzag_weight=style.zigzag_weight,
    )


def _course_is_usable(params: CourseParams, course: Course | None) -> bool:
    if course is None:
        return False
    if not course.path or course.path[0] != course.path[-1]:
        return False
    target_m = params.distance_km * 1000.0
    if abs(course.length_m - target_m) / target_m > MAX_SHAPE_DISTANCE_ERROR:
        return False
    g = graphmod.get_graph()
    if backtrack_fraction(g, course.path) > BACKTRACK_MAX_FRAC:
        return False
    xy = [to_xy(lat, lon, params.lat, params.lon) for lat, lon in course.points]
    return count_self_intersections(xy) == 0


def generate_shape_course(params: CourseParams) -> Course:
    spec = SHAPES.get(params.shape or "")
    if spec is None:
        raise CourseError(
            "현재는 강아지, 고양이, 고래, 토끼 코스만 가능하며 "
            "다른 동물 코스는 추후 업데이트 예정입니다. 죄송합니다."
        )
    if params.distance_km < spec.min_km:
        alts = [s for s in SHAPES.values() if s.min_km <= params.distance_km]
        alt_msg = (
            "이 거리에서는 " + ", ".join(f"{a.emoji}{a.name_ko}" for a in alts) + " 모양이 가능해요."
            if alts else "거리를 늘려서 다시 요청해 주세요."
        )
        raise CourseError(
            f"{spec.name_ko} 모양은 {spec.min_km:g}km 이상에서 예쁘게 나와요. {alt_msg}"
        )

    deadline = time.perf_counter() + TIME_BUDGET_S
    # Shape clarity beats compass orientation. Land animals still avoid
    # upside-down poses, but tilted candidates can win when they read better
    # on the local road grid.
    style = SHAPE_STYLES.get(spec.key) or ShapeStyle(
        rotations=GENTLE_ROTATIONS if spec.key == "whale" else UPRIGHT_ROTATIONS,
        scales=FIT_SCALES,
        similarity_gate=SIMILARITY_GATE,
        outline_min_frac=OUTLINE_MIN_FRAC,
        max_sharp_turns=5,
        corridor_frac=CORRIDOR_FRAC,
        aspect_min=0.75,
        aspect_max=2.50,
    )
    required_gate = style.similarity_gate
    best, best_sim = _search_shape(
        spec, params, deadline=deadline, rotations=style.rotations, scales=style.scales,
        style=style)
    if best is None or best_sim < style.similarity_gate or not _course_is_usable(params, best):
        relaxed = _relaxed_style(style)
        relaxed_best, relaxed_sim = _search_shape(
            spec, params, deadline=time.perf_counter() + FALLBACK_BUDGET_S,
            rotations=relaxed.rotations, scales=relaxed.scales, style=relaxed)
        # The relaxed pass may tilt the animal and staircase across the grid,
        # so it only replaces a usable strict result when clearly better.
        if _course_is_usable(params, relaxed_best) and (
            best is None
            or not _course_is_usable(params, best)
            or relaxed_sim > best_sim + 0.05
        ):
            best, best_sim, style = relaxed_best, relaxed_sim, relaxed

    if (best is not None and best_sim >= required_gate
            and _course_is_usable(params, best)):
        return best

    if best is None or best_sim < required_gate:
        # Honest alternatives: quick-probe the other shapes and suggest only
        # those that actually clear the gate here (편의성 — 실패도 대화의 일부).
        alts = suggest_alternatives(params)
        alt_msg = (
            " 대신 이 근처에서 검증된 모양: " + ", ".join(alts) + "."
            if alts else
            " 강남·잠실처럼 길이 바둑판인 동네나 큰 공원 근처에서 성공률이 높아요."
        )
        raise CourseError(
            f"이 지역 도로망에서는 {spec.name_ko} 옆모습 실루엣이 "
            f"충분히 또렷하지 않아 추천하지 않을게요.{alt_msg}"
        )
    raise CourseError(
        f"{spec.name_ko} 모양은 이 위치에서 목표 거리 ±10% 안의 폐루프로 만들기 어려워요. "
        "거리를 조금 늘리거나 근처 큰 도로·공원 쪽 출발점을 잡아 주세요."
    )


# Quality-first mode: distance is an OUTPUT. Forcing an arbitrary requested
# length onto a street grid that cannot draw the animal at that size is what
# produced blob courses. Sweep up to the user-facing animal-art cap and keep
# the strongest silhouette; over the cap, say there is no good animal course.
MAX_ANIMAL_ART_KM = 11.0
# Speed-first sweep: coarse 1km steps and a hard wall-clock cap so the whole
# search fits the PlayMCP p99 <= 3s budget. Fewer probe distances means a lower
# chance of finding the theoretically best size, which is the quality we trade.
MIN_CLEAN_DISTANCE_STEP_KM = 1.0
MIN_CLEAN_TRY_BUDGET_S = 0.7
MIN_CLEAN_TOTAL_BUDGET_S = 2.2  # hard cap across all probed distances
_MIN_CLEAN_CACHE: dict[tuple, Course | None] = {}
_MIN_CLEAN_SUCCESS_CACHE: dict[tuple, Course] = {}
_DISTANCE_SUCCESS_CACHE: dict[tuple, tuple[Course, float]] = {}

# Each silhouette has a scale at which its characteristic features usually
# survive snapping to Seoul's road grid. Start near that scale instead of
# spending the whole response budget walking upward from the minimum. The
# full 3..11km range remains available, and every returned course still has
# to clear the exact same shape, loop, distance and intersection gates.
PREFERRED_ANIMAL_DISTANCE_KM = {
    "dog": 9.0,
    "cat": 8.0,
    "whale": 5.0,
    "rabbit": 8.0,
}


def _distance_probe_order(spec: ShapeSpec, start_km: float) -> list[float]:
    """Probe likely clean scales first, then cover the remaining range.

    This is an anytime ordering only; it does not relax quality or remove any
    legal distance from a complete search.
    """
    steps = int(round((MAX_ANIMAL_ART_KM - start_km) /
                      MIN_CLEAN_DISTANCE_STEP_KM))
    distances = [
        round(start_km + idx * MIN_CLEAN_DISTANCE_STEP_KM, 1)
        for idx in range(steps + 1)
    ]
    preferred = max(start_km, PREFERRED_ANIMAL_DISTANCE_KM.get(spec.key, start_km))
    return sorted(distances, key=lambda distance: (abs(distance - preferred), distance))


def _search_success_key(params: CourseParams, spec: ShapeSpec,
                        start_km: float) -> tuple:
    return (
        round(params.lat, 4), round(params.lon, 4), spec.key,
        params.include_hills, params.night_mode,
        tuple(sorted(params.need_facilities)), round(start_km, 1),
    )


def find_min_clean_course(params: CourseParams,
                          per_try_s: float = MIN_CLEAN_TRY_BUDGET_S,
                          total_budget_s: float = MIN_CLEAN_TOTAL_BUDGET_S,
                          ) -> Course | None:
    """Best animal-art course under the 11km cap whose silhouette clears gate.

    Returns None when no distance up to 11km produces a clean, closed,
    non-self-intersecting silhouette here — the caller should say so honestly
    instead of shipping a bad course.
    """
    spec = SHAPES.get(params.shape or "")
    if spec is None:
        return None
    style = SHAPE_STYLES.get(spec.key)
    if style is None:
        return None
    start_km = max(spec.min_km, params.distance_km or 0.0)
    success_key = _search_success_key(params, spec, start_km)
    success = _MIN_CLEAN_SUCCESS_CACHE.get(success_key)
    if success is not None:
        return success
    # Keep budget-specific memoization for completed searches. A timed-out
    # search is deliberately not cached as None: it proves only that this
    # attempt ran out of time, not that the road network cannot draw the animal.
    cache_key = (
        *success_key,
        round(per_try_s, 1), round(total_budget_s, 1),
    )
    if cache_key in _MIN_CLEAN_CACHE:
        return _MIN_CLEAN_CACHE[cache_key]
    if start_km > MAX_ANIMAL_ART_KM:
        _MIN_CLEAN_CACHE[cache_key] = None
        return None
    hard_deadline = time.perf_counter() + total_budget_s
    best: Course | None = None
    best_sim = -1.0
    completed = True
    for dist in _distance_probe_order(spec, start_km):
        if time.perf_counter() > hard_deadline:
            completed = False
            break
        probe = params.model_copy(update={"distance_km": dist})
        distance_key = (*success_key[:-1], round(dist, 1))
        cached_distance = _DISTANCE_SUCCESS_CACHE.get(distance_key)
        if cached_distance is None:
            course, sim = _search_shape(
                spec, probe,
                deadline=min(time.perf_counter() + per_try_s, hard_deadline),
                rotations=style.rotations, scales=style.scales, style=style)
        else:
            course, sim = cached_distance
        if (course is not None and sim >= style.similarity_gate
                and course.length_km <= MAX_ANIMAL_ART_KM
                and _course_is_usable(probe, course)):
            _DISTANCE_SUCCESS_CACHE[distance_key] = (course, sim)
            # Shape clarity is primary. If two candidates are visually close,
            # prefer the shorter one so the recommendation still feels runnable.
            if (sim > best_sim + 0.015
                    or (abs(sim - best_sim) <= 0.015
                        and (best is None or course.length_km < best.length_km))):
                best = course
                best_sim = sim
            # A silhouette this clear will not get meaningfully better at a
            # longer distance — stop sweeping instead of burning the budget.
            if best_sim >= GOOD_ENOUGH:
                break
    if best is not None:
        _MIN_CLEAN_SUCCESS_CACHE[success_key] = best
        _MIN_CLEAN_CACHE[cache_key] = best
    elif completed:
        _MIN_CLEAN_CACHE[cache_key] = None
    return best


def find_best_reference_course(params: CourseParams,
                               per_distance_budget_s: float = 5.0,
                               ) -> Course | None:
    """Offline quality-first search for station presets.

    Unlike the request-time anytime search, this evaluates every legal 1km
    distance through 11km and never stops merely because a good-enough route
    was found. Reference-shape similarity is the primary objective; distance
    only breaks visually equivalent results.
    """
    spec = SHAPES.get(params.shape or "")
    if spec is None:
        return None
    style = SHAPE_STYLES.get(spec.key)
    if style is None:
        return None
    start_km = max(spec.min_km, params.distance_km or 0.0)
    if start_km > MAX_ANIMAL_ART_KM:
        return None
    best = None
    best_sim = -1.0
    for distance in _distance_probe_order(spec, start_km):
        probe = params.model_copy(update={"distance_km": distance})
        course, sim = _search_shape(
            spec, probe,
            deadline=time.perf_counter() + per_distance_budget_s,
            rotations=style.rotations, scales=style.scales, style=style)
        if (course is None or sim < style.similarity_gate
                or course.length_km > MAX_ANIMAL_ART_KM
                or not _course_is_usable(probe, course)):
            continue
        if (sim > best_sim + 0.015
                or (abs(sim - best_sim) <= 0.015
                    and (best is None or course.length_km < best.length_km))):
            best, best_sim = course, sim
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
            rotations=SHAPE_STYLES.get(spec.key, SHAPE_STYLES["dog"]).rotations,
            scales=SHAPE_STYLES.get(spec.key, SHAPE_STYLES["dog"]).scales[:2],
            refine=False,
            style=SHAPE_STYLES.get(spec.key))
        if course is not None and sim >= SHAPE_STYLES.get(spec.key, SHAPE_STYLES["dog"]).similarity_gate:
            out.append(f"{spec.emoji}{spec.name_ko}")
            if len(out) >= limit:
                break
    return out
