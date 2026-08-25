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
from dataclasses import dataclass, field, replace

import networkx as nx

from . import graph as graphmod
from .facilities import facility_requirement_score
from .geo import haversine_m, to_latlon, to_xy
from .models import CourseParams, CourseWaypoint
from .rfs import route_rfs_summary, routing_weight

DISTANCE_TOLERANCE = 0.05  # ±5% (PRD §7.2)
N_WAYPOINTS = 4
BEARINGS = (0, 45, 90, 135, 180, 225, 270, 315)
MAX_RESCALES = 5
PACE_MIN_PER_KM = 6.5
FOLLOW_EDGE_PENALTY_M = 12.0
MAX_COURSE_START_OFFSET_M = 150.0
MAX_DEFAULT_ASCENT_PER_KM = 30.0
# The bundled OSM graph came from a walk-network query, but its ETL retained
# only the final highway class, not foot/access/sidewalk tags. These classes
# therefore cannot be proven runnable and must never appear in a preset or a
# generated course. In particular, *_link edges are often vehicle ramps.
NON_RUNNABLE_HIGHWAYS = frozenset({
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary_link", "secondary_link", "tertiary_link",
    "busway", "track", "bridleway", "steps", "corridor", "road",
})
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


def rebase_closed_course_start(course: Course) -> Course:
    """Rotate a closed loop so its first/last point is nearest the requested start.

    GPS-art generation places a silhouette around the requested location. The
    route is cyclic, so its serialized first node is otherwise just whichever
    template anchor happened to be authored first and can be hundreds of
    metres away. Rotating a closed cycle changes neither its streets nor its
    metrics, but makes the map pin and GPX start honest and useful.
    """
    if (
        len(course.path) < 3
        or course.path[0] != course.path[-1]
        or len(course.points) != len(course.path)
        or course.points[0] != course.points[-1]
    ):
        return course
    core_points = course.points[:-1]
    start_index = min(
        range(len(core_points)),
        key=lambda index: haversine_m(
            course.params.lat, course.params.lon, *core_points[index]
        ),
    )
    if start_index == 0:
        return course
    core_path = course.path[:-1]
    path = core_path[start_index:] + core_path[:start_index]
    points = core_points[start_index:] + core_points[:start_index]
    return replace(
        course,
        path=path + [path[0]],
        points=points + [points[0]],
    )


def highway_class(attrs: dict) -> str:
    value = attrs.get("highway")
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "")


def edge_is_runnable(attrs: dict) -> bool:
    """Conservative runtime gate for edges whose pedestrian status is clear."""
    highway = highway_class(attrs)
    if highway in NON_RUNNABLE_HIGHWAYS:
        return False
    # A steep generic OSM path is the mountain-trail failure mode seen in the
    # station presets. Paved urban roads remain available for explicit hill
    # training, but an unqualified path above 10% is not a default run route.
    if highway == "path" and abs(float(attrs.get("slope_pct", 0.0) or 0.0)) > 10.0:
        return False
    return True


def course_route_issues(course: Course, graph) -> list[str]:
    """Return deterministic reasons a course is not safe as a station preset."""
    issues: list[str] = []
    if not course.path or course.path[0] != course.path[-1]:
        issues.append("open_route")
    if course.points:
        nearest = min(
            haversine_m(course.params.lat, course.params.lon, *point)
            for point in course.points
        )
        if nearest > MAX_COURSE_START_OFFSET_M:
            issues.append("start_too_far")
    blocked = {
        highway_class(graph.edges[u, v])
        for u, v in zip(course.path, course.path[1:])
        if not edge_is_runnable(graph.edges[u, v])
    }
    issues.extend(f"blocked_highway:{value or 'unknown'}" for value in sorted(blocked))
    if course.length_km and not course.params.include_hills:
        if course.ascent_m / course.length_km > MAX_DEFAULT_ASCENT_PER_KM:
            issues.append("excessive_ascent")
    return issues


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


def _manual_waypoint_course(params: CourseParams) -> Course:
    """Route a user-edited loop through bounded, ordered street waypoints."""
    start_node, start_snap = graphmod.nearest_node(params.lat, params.lon)
    if start_snap > 1500:
        raise CourseError("출발점이 현재 지원 지역 밖이에요.")
    max_radius_m = min(8000.0, params.distance_km * 1000.0)
    requested_points = [(p.lat, p.lon) for p in params.manual_waypoints]
    if len(requested_points) < 2:
        raise CourseError("경유점은 두 곳 이상 추가해 주세요.")
    perimeter = sum(
        haversine_m(a[0], a[1], b[0], b[1])
        for a, b in zip(
            [(params.lat, params.lon), *requested_points],
            [*requested_points, (params.lat, params.lon)],
        )
    )
    if perimeter > 42195.0:
        raise CourseError("수정한 코스가 42.195km를 넘어요. 경유점을 가까이 옮겨 주세요.")
    farthest = max(
        haversine_m(params.lat, params.lon, lat, lon)
        for lat, lon in requested_points
    )
    if farthest > max_radius_m:
        raise CourseError(
            f"경유점은 출발점에서 {max_radius_m / 1000:g}km 안에 둬야 해요."
        )

    snapped: list[tuple[object, tuple[float, float]]] = []
    for lat, lon in requested_points:
        node, snap_m = graphmod.nearest_node(lat, lon)
        if node is None or snap_m > 300:
            raise CourseError(
                "경유점이 달릴 수 있는 도로에서 너무 멀어요. 경유점을 도로 가까이 옮겨 주세요."
            )
        if node == start_node or (snapped and node == snapped[-1][0]):
            raise CourseError("경유점이 출발점 또는 다른 경유점과 너무 가까워요.")
        node_data = graphmod.get_graph().nodes[node]
        snapped.append((node, (node_data["lat"], node_data["lon"])))

    g = graphmod.subgraph_around(
        params.lat, params.lon, min(6000.0, max(2000.0, farthest * 1.35 + 600.0))
    )
    stops = [start_node, *(node for node, _ in snapped), start_node]
    if any(node not in g for node in stops):
        raise CourseError("경유점이 현재 도로망 범위를 벗어났어요. 경유점을 가까이 옮겨 주세요.")
    weight = easy_route_weight(routing_weight(params.night_mode, params.include_hills))
    deadline = time.perf_counter() + 0.8
    path: list = []
    for a, b in zip(stops, stops[1:]):
        if time.perf_counter() > deadline:
            raise CourseError("경로 다시 계산에 시간이 걸렸어요. 경유점을 줄여 다시 시도해 주세요.")
        try:
            segment = _route(g, weight, a, b)
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            raise CourseError(
                "경유점을 잇는 보행 경로를 찾지 못했어요. 경유점을 가까운 도로로 옮겨 주세요."
            ) from exc
        path.extend(segment if not path else segment[1:])

    length, ascent = _path_metrics(g, path)
    if not 1000.0 <= length <= 42195.0:
        raise CourseError("수정한 코스는 1km 이상 42.195km 이하가 되도록 경유점을 조정해 주세요.")
    summary = route_rfs_summary(g, path, params.night_mode, params.include_hills)
    points = [(g.nodes[node]["lat"], g.nodes[node]["lon"]) for node in path]
    normalized = params.model_copy(
        update={
            "manual_waypoints": [
                CourseWaypoint(lat=lat, lon=lon) for _, (lat, lon) in snapped
            ]
        }
    )
    return Course(
        params=normalized,
        path=path,
        points=points,
        length_m=length,
        ascent_m=ascent,
        rfs=summary,
    )


def course_from_path(params: CourseParams, path: list[int]) -> Course:
    """Build metrics for an exact, already road-snapped path."""
    g = graphmod.get_graph()
    if len(path) < 3 or len(path) > 1200 or path[0] != path[-1]:
        raise CourseError("코스 선이 출발점으로 이어지지 않았어요. 끊어진 구간을 다시 연결해 주세요.")
    if any(node not in g for node in path):
        raise CourseError("현재 보행 지도에서 찾을 수 없는 코스 구간이 있어요.")
    if any(not g.has_edge(a, b) for a, b in zip(path, path[1:])):
        raise CourseError("보행로로 연결되지 않은 구간이 있어요. 해당 구간을 다시 선택해 주세요.")
    length, ascent = _path_metrics(g, path)
    if not 1000.0 <= length <= 42195.0:
        raise CourseError("수정한 코스는 1km 이상 42.195km 이하로 만들어 주세요.")
    summary = route_rfs_summary(g, path, params.night_mode, params.include_hills)
    normalized = params.model_copy(update={
        "shape": None,
        "manual_waypoints": [],
        "manual_path": path,
        "distance_km": round(length / 1000.0, 2),
    })
    return Course(
        params=normalized,
        path=path,
        points=[(g.nodes[node]["lat"], g.nodes[node]["lon"]) for node in path],
        length_m=length,
        ascent_m=ascent,
        rfs=summary,
    )


def reroute_segment(params: CourseParams, path: list[int], from_index: int,
                    to_index: int) -> Course:
    """Detach one route span and reconnect its endpoints on another walkable path.

    The selected edges are hidden from Dijkstra, so this operation behaves like
    lifting one piece of the route off the map and snapping a different street
    segment between the same two endpoints.  It is intentionally endpoint-only:
    the browser never has to turn an imprecise finger stroke into coordinates.
    """
    course_from_path(params, path)
    if not (0 <= from_index < to_index < len(path) - 1):
        raise CourseError("바꿀 구간을 코스 선 위에서 다시 선택해 주세요.")
    if to_index - from_index > max(80, (len(path) - 1) // 2):
        raise CourseError("한 번에 바꿀 구간이 너무 길어요. 더 짧은 구간을 선택해 주세요.")

    g = graphmod.get_graph()
    blocked_edges = {
        frozenset((a, b))
        for a, b in zip(path[from_index:to_index], path[from_index + 1:to_index + 1])
    }
    base_weight = easy_route_weight(
        routing_weight(params.night_mode, params.include_hills)
    )

    def alternative_weight(u, v, attrs):
        if frozenset((u, v)) in blocked_edges:
            return None
        return base_weight(u, v, attrs)

    try:
        replacement = _route(
            g, alternative_weight, path[from_index], path[to_index]
        )
    except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
        raise CourseError(
            "이 구간을 대신할 보행로가 없어요. 더 짧거나 다른 구간을 선택해 주세요."
        ) from exc
    if replacement == path[from_index:to_index + 1]:
        raise CourseError(
            "이 구간을 대신할 다른 보행로가 없어요. 다른 구간을 선택해 주세요."
        )
    edited_path = path[:from_index] + replacement + path[to_index + 1:]
    return course_from_path(params, edited_path)


def snap_drawn_segment(params: CourseParams, path: list[int], from_index: int,
                       to_index: int, stroke: list[CourseWaypoint]) -> Course:
    """Replace only one route section with the pedestrian path under a finger stroke."""
    course_from_path(params, path)
    if not (0 <= from_index < to_index < len(path) - 1):
        raise CourseError(
            "지울 구간의 양 끝이 코스 선 위에 있어야 해요. "
            "지우개로 다시 구간을 고른 뒤 그려 주세요.")
    # Sweeping with the eraser routinely marks more than 80 nodes; the old cap
    # rejected ordinary edits. The real limit is how much walking the server
    # has to re-route, which the stroke-length check below already bounds.
    if to_index - from_index > max(400, (len(path) - 1) * 3 // 4):
        raise CourseError(
            f"한 번에 바꾸려는 구간이 {to_index - from_index}개로 너무 길어요. "
            "절반쯤씩 나눠서 수정해 주세요.")
    if len(stroke) < 2:
        raise CourseError("연필로 지운 구간의 한쪽 끝에서 다른 쪽 끝까지 이어 그려 주세요.")

    walked = sum(
        haversine_m(a.lat, a.lon, b.lat, b.lon)
        for a, b in zip(stroke, stroke[1:])
    )
    if walked < 8:
        raise CourseError(
            f"그린 선이 {walked:.0f}m로 너무 짧아요. "
            "지운 구간의 한쪽 끝에서 다른 쪽 끝까지 이어 그려 주세요.")
    if walked > 6000:
        raise CourseError(
            f"한 번에 그린 선이 {walked / 1000:.1f}km로 너무 길어요. "
            "6km보다 짧게 나눠 그려 주세요.")

    g = graphmod.get_graph()
    drawn_nodes = []
    far_from_road = 0
    for point in stroke_waypoints(stroke):
        node, snap_m = graphmod.nearest_node(point[0], point[1])
        # A waypoint that lands nowhere near a walkable way is dropped rather
        # than fatal: the drawn line says roughly where to go, and one stray
        # sample should not refuse the whole edit.
        if node is None or snap_m > STROKE_SNAP_MAX_M:
            far_from_road += 1
            continue
        if not drawn_nodes or drawn_nodes[-1] != node:
            drawn_nodes.append(node)
    if far_from_road and not drawn_nodes:
        raise CourseError(
            "그린 선이 걸을 수 있는 길에서 너무 멀어요. "
            "지도에 보이는 도로나 보도를 따라 그려 주세요.")

    stops = [path[from_index], *drawn_nodes, path[to_index]]
    weight = easy_route_weight(routing_weight(params.night_mode, params.include_hills))
    replacement: list[int] = []
    for a, b in zip(stops, stops[1:]):
        try:
            segment = _route(g, weight, a, b)
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            raise CourseError("그린 선을 잇는 보행로를 찾지 못했어요. 가까운 길을 따라 다시 그려 주세요.") from exc
        replacement.extend(segment if not replacement else segment[1:])
    if not stroke_is_doubled(stroke):
        replacement = drop_backtracking(replacement)
    edited_path = path[:from_index] + replacement + path[to_index + 1:]
    return course_from_path(params, edited_path)


# A drawn line says where to go, not which junction to touch. Waypoints closer
# together than this add no shape a runner can see, but each one is a hard
# constraint the router must satisfy -- and two neighbouring samples that snap
# to different ways force a detour out to one and back.
# Walking the same path out and back is a valid loop and a joyless run. The
# penalty is scaled to the RFS range so a fully retraced loop loses to almost
# any real circuit, while a short shared spur stays acceptable.
RETRACE_PENALTY = 120.0
RETRACE_ACCEPTABLE = 0.25
STROKE_WAYPOINT_MIN_M = 140.0
# Perpendicular deviation below which a drawn line counts as straight.
STROKE_SIMPLIFY_TOLERANCE_M = 45.0
STROKE_WAYPOINT_MAX = 8
# How far a drawn waypoint may sit from a walkable way before it is ignored.
# A finger is not a surveyor and a phone's GPS-free tap has real slop.
STROKE_SNAP_MAX_M = 220.0
# How close two non-adjacent parts of one stroke must come before the line
# counts as deliberately drawn over itself.
DOUBLED_STROKE_M = 35.0
# ...and how much drawn line must separate them, so a slowly drawn straight
# line full of near-identical samples does not read as a double pass.
DOUBLED_STROKE_MIN_GAP_M = 250.0
# Headings this opposed (about 120 degrees apart) mean the line turned back on
# itself. A wobble never reverses; only a deliberate return does.
DOUBLED_STROKE_MAX_FACING = -0.5
DOUBLED_STROKE_MIN_PAIRS = 4


def _perpendicular_m(point, start, end) -> float:
    """Distance from ``point`` to the segment ``start``-``end``, in metres."""
    px, py = to_xy(point[0], point[1], start[0], start[1])
    ax, ay = 0.0, 0.0
    bx, by = to_xy(end[0], end[1], start[0], start[1])
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span == 0.0:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * dx + py * dy) / span))
    return math.hypot(px - dx * t, py - dy * t)


def _simplify(points: list[tuple[float, float]], tolerance_m: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker over lat/lon points, keeping both ends."""
    if len(points) < 3:
        return list(points)
    worst, index = 0.0, 0
    for i in range(1, len(points) - 1):
        d = _perpendicular_m(points[i], points[0], points[-1])
        if d > worst:
            worst, index = d, i
    if worst <= tolerance_m:
        return [points[0], points[-1]]
    left = _simplify(points[: index + 1], tolerance_m)
    right = _simplify(points[index:], tolerance_m)
    return left[:-1] + right


def stroke_waypoints(stroke) -> list[tuple[float, float]]:
    """The few points a drawn line actually asks the route to pass through.

    Sampling the raw stroke at a fixed stride treated every wobble as an
    instruction: a straight 489m line came back as a 1,515m path that doubled
    back nine times. Simplifying first means a straight line contributes no
    intermediate waypoints at all, and the router is free to follow the road.
    """
    points = [(point.lat, point.lon) for point in stroke]
    simplified = _simplify(points, STROKE_SIMPLIFY_TOLERANCE_M)[1:-1]
    spaced: list[tuple[float, float]] = []
    for point in simplified:
        if spaced and haversine_m(spaced[-1][0], spaced[-1][1],
                                  point[0], point[1]) < STROKE_WAYPOINT_MIN_M:
            continue
        spaced.append(point)
    if len(spaced) <= STROKE_WAYPOINT_MAX:
        return spaced
    step = math.ceil(len(spaced) / STROKE_WAYPOINT_MAX)
    return spaced[::step]


def _local_direction(points: list[tuple[float, float]], i: int) -> tuple[float, float]:
    """Unit heading of the stroke at sample ``i``, in local metres."""
    a = points[max(0, i - 1)]
    b = points[min(len(points) - 1, i + 1)]
    dx, dy = to_xy(b[0], b[1], a[0], a[1])
    span = math.hypot(dx, dy)
    return (dx / span, dy / span) if span else (0.0, 0.0)


def stroke_is_doubled(stroke) -> bool:
    """Did the drawn line deliberately go back over its own ground?

    Proximity alone is not enough: a finger drawing a straight line wobbles,
    and a wobble of a few tens of metres brings the line back within reach of
    ground it covered a moment earlier. Read as doubling, that skipped spur
    removal and returned a route 2.4x the length of the line with ten repeated
    nodes -- the overlapping course a runner saw after drawing straight.

    A real second pass *reverses*: the heading at the two nearby samples is
    roughly opposite. Jitter never is, so the heading test is what separates
    an intentional out-and-back from an unsteady hand.
    """
    points = [(point.lat, point.lon) for point in stroke]
    if len(points) < 6:
        return False
    step = max(1, len(points) // 80)
    sampled = points[::step]
    walked = [0.0]
    for a, b in zip(sampled, sampled[1:]):
        walked.append(walked[-1] + haversine_m(a[0], a[1], b[0], b[1]))
    if walked[-1] < DOUBLED_STROKE_MIN_GAP_M:
        return False
    headings = [_local_direction(sampled, i) for i in range(len(sampled))]
    # A real return leg shares a whole stretch, so it produces many reversed
    # near-matches. Violent jitter can fake one or two by coincidence; it
    # cannot fake a run of them.
    matches = 0
    for i, a in enumerate(sampled):
        for j in range(i + 1, len(sampled)):
            if walked[j] - walked[i] < DOUBLED_STROKE_MIN_GAP_M:
                continue
            if haversine_m(a[0], a[1], sampled[j][0], sampled[j][1]) > DOUBLED_STROKE_M:
                continue
            facing = headings[i][0] * headings[j][0] + headings[i][1] * headings[j][1]
            if facing <= DOUBLED_STROKE_MAX_FACING:
                matches += 1
                if matches >= DOUBLED_STROKE_MIN_PAIRS:
                    return True
    return False


def drop_backtracking(nodes: list[int]) -> list[int]:
    """Cut every excursion that returns to a node the route already visited.

    A repeated node *is* an out-and-back spur: whatever happens between the
    two visits leaves and comes back to the same place, so removing it leaves
    a shorter walk over the same graph edges.
    """
    trimmed: list[int] = []
    seen: dict[int, int] = {}
    for node in nodes:
        if node in seen:
            del trimmed[seen[node] + 1:]
            for gone in list(seen):
                if seen[gone] > seen[node]:
                    del seen[gone]
            continue
        seen[node] = len(trimmed)
        trimmed.append(node)
    return trimmed


def retrace_share(graph, path: list[int]) -> float:
    """Fraction of the loop's length walked more than once.

    A park's footpath network is often a tree rather than a mesh, so the
    cheapest way to reach a target distance is to go out and come back along
    the same path. That is a valid loop and a joyless run, and the ranking had
    no way to tell it apart from a real circuit.
    """
    seen: set[frozenset] = set()
    total = repeated = 0.0
    for u, v in zip(path, path[1:]):
        length = float(graph.edges[u, v].get("length", 0.0))
        total += length
        key = frozenset((u, v))
        if key in seen:
            repeated += length
        else:
            seen.add(key)
    return repeated / total if total else 0.0


def easy_route_weight(base_weight: str):
    """Prefer roads that are easier to follow without exposing a new score.

    A tiny fixed cost per edge discourages routes made of many short alley
    fragments while keeping the existing RFS/length weighting dominant.
    """
    def _weight(_u, _v, attrs):
        if not edge_is_runnable(attrs):
            return None
        highway = highway_class(attrs)
        factor = HIGHWAY_COST_FACTOR.get(highway, 1.06)
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
    if params.manual_path:
        return course_from_path(params, params.manual_path)
    if params.manual_waypoints:
        return _manual_waypoint_course(params)
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
    best_retrace = 1.0
    n_in_tol = 0
    deadline = time.perf_counter() + 0.8  # anytime cutoff (PRD §7.1)
    for bearing in BEARINGS:
        if time.perf_counter() > deadline:
            break
        # Early exit: a good in-tolerance loop is enough; exhaustive bearing
        # search buys little quality for a lot of latency.
        # Stopping early on a loop that doubles back over itself is how a
        # 92%-retraced park course got shipped: keep looking for a real circuit.
        if (n_in_tol >= 2 or (best is not None and best_err <= DISTANCE_TOLERANCE
                              and best.rfs["score"] >= 55)) \
                and best_retrace <= RETRACE_ACCEPTABLE:
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
        retraced = retrace_share(g, path)
        quality = (
            -summary["score"]
            + (0.0 if params.include_hills else 2.0 * ascent_per_km)
            + followability_penalty(points, length)
            + RETRACE_PENALTY * retraced
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
            best_retrace = retraced
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
