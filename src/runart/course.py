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
from .models import CourseParams, CourseWaypoint, clean_course_name
from .rfs import (GATED_WEIGHT_FACTOR, has_sufficient_night_lighting, prefers_park_paths,
                  route_rfs_summary, routing_weight)

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
# Ways that are pedestrian by construction. Named ones are drawn on the
# basemap and are the closest thing the data has to "the sidewalk".
NAMED_WALKWAY_HIGHWAYS = frozenset({"footway", "path", "pedestrian"})
NAMED_WALKWAY_COST = 0.95


class CourseError(Exception):
    """User-facing generation failure; message must say what to do next."""


class CourseAccessError(CourseError):
    """An existing route cannot be served under the current access rules."""

    def __init__(self):
        super().__init__(
            "이 코스는 군사시설 등 통행이 제한되거나 보행을 확인할 수 없는 "
            "구간이 포함되어 차단했어요. 새 코스를 요청해 주세요."
        )


class DistanceMissError(CourseError):
    """No loop close enough to the target; the nearest one is this long.

    Carries the two numbers so a caller that knows the runner asked in
    minutes can say the same thing in minutes. The default message stays
    correct for a request that really was made in kilometres.
    """

    def __init__(self, target_km: float, nearest_km: float):
        self.target_km = target_km
        self.nearest_km = nearest_km
        super().__init__(
            f"목표 {target_km:g}km에 맞는 코스를 찾지 못했어요 "
            f"(가장 근접: {nearest_km:.1f}km). 거리를 조금 조정해 다시 요청해 주세요."
        )


def _routing_weight_for(params: CourseParams):
    """Select the map-aligned profile unless park running was explicit."""
    return routing_weight(
        params.night_mode,
        params.include_hills,
        prefers_park_paths(params.need_facilities),
    )


@dataclass
class Course:
    params: CourseParams
    path: list  # graph nodes
    points: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon)
    length_m: float = 0.0
    ascent_m: float = 0.0
    rfs: dict = field(default_factory=dict)
    shape_similarity: float | None = None
    # Why the produced line differs from what was asked for -- stairs, a grade
    # the router refuses, a cut edge with no alternative. Empty when the route
    # is exactly what the request implied. Presentation only; never persisted
    # into the course_id.
    note: str = ""

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
    if attrs.get("military"):
        return False
    highway = highway_class(attrs)
    if highway in NON_RUNNABLE_HIGHWAYS:
        return False
    # A steep generic OSM path is the mountain-trail failure mode seen in the
    # station presets. Paved urban roads remain available for explicit hill
    # training, but an unqualified path above 10% is not a default run route.
    if highway == "path" and abs(float(attrs.get("slope_pct", 0.0) or 0.0)) > 10.0:
        return False
    return True


def path_access_issues(path: list[int], graph) -> list[str]:
    """Audit current graph access, including old paths made by older code."""
    issues = set()
    for u, v in zip(path, path[1:]):
        if not graph.has_edge(u, v):
            issues.add("missing_edge")
            continue
        attrs = graph.edges[u, v]
        if attrs.get("military"):
            issues.add("blocked_military")
        elif not edge_is_runnable(attrs):
            issues.add(f"blocked_highway:{highway_class(attrs) or 'unknown'}")
    return sorted(issues)


def ensure_course_runnable(course: Course) -> None:
    """Cache/protocol boundaries must not trust old build-time validation."""
    if path_access_issues(course.path, graphmod.get_graph()):
        raise CourseAccessError()


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
    issues.extend(path_access_issues(course.path, graph))
    if course.length_km and course.params.include_hills is False:
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
    weight = easy_route_weight(_routing_weight_for(params), prefer_named_walkways=True)
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


def course_from_path(params: CourseParams, path: list[int],
                     name: str | None = None) -> Course:
    """Build metrics for an exact, already road-snapped path.

    ``name`` is what the runner typed in the save dialog. ``None`` keeps
    whatever the params already carry; "" clears it back to the generated
    title. Either way it rides in the course_id, so a renamed course is still
    reproducible from its link alone.
    """
    g = graphmod.get_graph()
    if len(path) < 3 or len(path) > 1200 or path[0] != path[-1]:
        raise CourseError("코스 선이 출발점으로 이어지지 않았어요. 끊어진 구간을 다시 연결해 주세요.")
    if any(node not in g for node in path):
        raise CourseError("현재 보행 지도에서 찾을 수 없는 코스 구간이 있어요.")
    if any(not g.has_edge(a, b) for a, b in zip(path, path[1:])):
        raise CourseError("보행로로 연결되지 않은 구간이 있어요. 해당 구간을 다시 선택해 주세요.")
    if path_access_issues(path, g):
        raise CourseAccessError()
    length, ascent = _path_metrics(g, path)
    if not 1000.0 <= length <= 42195.0:
        raise CourseError("수정한 코스는 1km 이상 42.195km 이하로 만들어 주세요.")
    summary = route_rfs_summary(g, path, params.night_mode, params.include_hills)
    update = {
        "shape": None,
        "manual_waypoints": [],
        "manual_path": path,
        "distance_km": round(length / 1000.0, 2),
    }
    if name is not None:
        update["custom_name"] = clean_course_name(name)
    normalized = params.model_copy(update=update)
    return Course(
        params=normalized,
        path=path,
        points=[(g.nodes[node]["lat"], g.nodes[node]["lon"]) for node in path],
        length_m=length,
        ascent_m=ascent,
        rfs=summary,
    )


# How far outside the erased span to look for the junction that closes an
# out-and-back. A spur long enough to need more than this is not a spur.
EXCURSION_SEARCH = 200


def collapse_excursion(path: list[int], from_index: int,
                       to_index: int) -> list[int] | None:
    """Remove the out-and-back the erased span sits inside, if there is one.

    Rubbing out a spur that pokes off the route is the commonest reason to
    reach for the eraser, and it was the one thing the eraser could not do:
    the only way from one end of a spur to the other is back along the spur,
    so asking for an alternative walkable path correctly answered "there is
    none" and the runner was told to draw. What they meant was "take this
    off", and taking it off means cutting back to the junction where the route
    left itself.

    Returns the shortened path, or ``None`` when the span is not part of an
    out-and-back and genuinely needs a replacement route.
    """
    lo = max(0, from_index - EXCURSION_SEARCH)
    hi = min(len(path) - 1, to_index + EXCURSION_SEARCH)
    best: tuple[int, int, int] | None = None
    for i in range(from_index, lo - 1, -1):
        for j in range(to_index, hi + 1):
            if path[i] != path[j]:
                continue
            cost = (from_index - i) + (j - to_index)
            if best is None or cost < best[0]:
                best = (cost, i, j)
            break                      # nearest j for this i is enough
    if best is None:
        return None
    _, i, j = best
    trimmed = path[:i] + path[j:]
    # A loop still has to be a loop, and a course still has to have a course.
    if len(trimmed) < 3 or trimmed[0] != trimmed[-1]:
        return None
    return trimmed


def reroute_segment(params: CourseParams, path: list[int], from_index: int,
                    to_index: int) -> Course:
    """Detach one route span and reconnect its endpoints on another walkable path.

    The selected edges are hidden from Dijkstra, so this operation behaves like
    lifting one piece of the route off the map and snapping a different street
    segment between the same two endpoints.  It is intentionally endpoint-only:
    the browser never has to turn an imprecise finger stroke into coordinates.

    When no alternative exists at all, the span is checked for being part of an
    out-and-back and simply cut away -- see collapse_excursion.
    """
    course_from_path(params, path)
    if not (0 <= from_index < to_index < len(path) - 1):
        raise CourseError("바꿀 구간을 코스 선 위에서 다시 선택해 주세요.")
    # Matched to the eraser: one sweep routinely marks well over 80 nodes, and
    # the work here is a single bounded Dijkstra either way.
    if to_index - from_index > max(400, (len(path) - 1) * 3 // 4):
        raise CourseError(
            f"한 번에 바꾸려는 구간이 {to_index - from_index}개로 너무 길어요. "
            "절반쯤씩 나눠서 수정해 주세요.")

    g = graphmod.get_graph()
    blocked_edges = {
        frozenset((a, b))
        for a, b in zip(path[from_index:to_index], path[from_index + 1:to_index + 1])
    }
    base_weight = easy_route_weight(_routing_weight_for(params), prefer_named_walkways=True)

    def alternative_weight(u, v, attrs):
        if frozenset((u, v)) in blocked_edges:
            return None
        return base_weight(u, v, attrs)

    replacement = None
    try:
        replacement = _route(
            g, alternative_weight, path[from_index], path[to_index]
        )
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        replacement = None
    if replacement is None or replacement == path[from_index:to_index + 1]:
        trimmed = collapse_excursion(path, from_index, to_index)
        if trimmed is not None:
            course = course_from_path(params, trimmed)
            course.note = ("대신할 다른 보행로가 없어서, 왕복으로 튀어나온 "
                           "구간을 통째로 잘라냈어요.")
            return course
        raise CourseError(
            "이 구간을 대신할 다른 보행로가 없어요. 이 길은 두 지역을 잇는 "
            "유일한 보행로예요. 더 넓은 범위를 지우거나 다른 구간을 골라 주세요."
        )
    edited_path = path[:from_index] + replacement + path[to_index + 1:]
    return course_from_path(params, edited_path)


# How far from a requested point the produced route may pass and still count
# as having gone there. Two sides of one city block is ~70m; beyond that the
# router plainly went somewhere else and owes the runner a reason.
VIA_REACHED_M = 70.0
# Only ways this close to the tap are candidates for explaining the detour.
BLOCKED_PROBE_M = 55.0
MAX_VIA_POINTS = 12


def _blocked_edge_reason(attrs: dict) -> str:
    """Why this way can never carry a course, in the runner's words."""
    highway = highway_class(attrs)
    if attrs.get("military"):
        return "군사 시설 안이라 코스로 반영할 수 없어요."
    if attrs.get("gated"):
        return "입장료를 받고 밤에 문을 닫는 곳이라 코스에 넣지 않았어요."
    if highway == "steps":
        return "계단 구간이라 코스로 반영할 수 없어요."
    if highway == "path" and abs(float(attrs.get("slope_pct", 0.0) or 0.0)) > 10.0:
        return (f"경사가 {abs(float(attrs['slope_pct'])):.0f}%로 높아 "
                "해당 구간은 코스로 반영할 수 없어요.")
    if highway in NON_RUNNABLE_HIGHWAYS:
        return "보행자가 다닐 수 없는 차도라 코스로 반영할 수 없어요."
    return ""


def blocked_reason_near(lat: float, lon: float,
                        radius_m: float = BLOCKED_PROBE_M) -> str:
    """Explain why a spot the runner pointed at holds no usable course line.

    The probe is the *nearest way*, not the nearest node: every staircase ends
    at a footway, so asking "is there a runnable edge anywhere around here"
    answers yes in the middle of a stairway and explains nothing. Returns ""
    when the closest way is perfectly runnable -- the route simply preferred
    another one, which is not something to apologise for.
    """
    g = graphmod.get_graph()
    best_blocked = (math.inf, "")
    best_open = math.inf
    seen: set[frozenset] = set()
    for node, _ in graphmod.nearby_nodes(
            lat, lon, limit=16, max_distance_m=radius_m):
        for neighbour in g[node]:
            key = frozenset((node, neighbour))
            if key in seen:
                continue
            seen.add(key)
            attrs = g.edges[node, neighbour]
            a, b = g.nodes[node], g.nodes[neighbour]
            distance = _perpendicular_m(
                (lat, lon), (a["lat"], a["lon"]), (b["lat"], b["lon"]))
            if edge_is_runnable(attrs) and not attrs.get("gated"):
                best_open = min(best_open, distance)
            elif distance < best_blocked[0]:
                best_blocked = (distance, _blocked_edge_reason(attrs))
    # A tie goes to silence: only claim a way is in the runner's path when it
    # is clearly the closest thing to where they pointed.
    if best_blocked[1] and best_blocked[0] + 8.0 < best_open:
        return best_blocked[1]
    return ""


def unreached_point_note(g, path: list[int],
                         points: list[tuple[float, float]]) -> str:
    """One sentence about the first requested point the route never reached.

    A drawn or tapped point that the replacement skipped is the single most
    confusing thing the editor does: the line the runner asked for simply is
    not there, and until now nothing said why.
    """
    if not points:
        return ""
    on_route = [(g.nodes[node]["lat"], g.nodes[node]["lon"]) for node in path]
    for lat, lon in points:
        if any(haversine_m(lat, lon, plat, plon) <= VIA_REACHED_M
               for plat, plon in on_route):
            continue
        reason = blocked_reason_near(lat, lon)
        if reason:
            return reason
        return ("짚은 곳을 지나는 보행로를 찾지 못해 가장 가까운 길로 이었어요. "
                "지도에 그려진 길 위를 짚으면 더 정확해요.")
    return ""


def route_via_points(params: CourseParams, path: list[int], from_index: int,
                     to_index: int, vias: list[CourseWaypoint]) -> Course:
    """Re-route one span so it passes through the points the runner tapped.

    The freehand pencil asked a finger to trace a pedestrian network it cannot
    see. This asks for the same thing the eraser asks for -- two ends and, at
    most, a handful of places to go through -- and lets the walking graph draw
    every metre in between. Nothing here is freehand, so nothing here can leave
    the road network.
    """
    course_from_path(params, path)
    if not (0 <= from_index < to_index < len(path) - 1):
        raise CourseError("바꿀 구간을 코스 선 위에서 다시 선택해 주세요.")
    if to_index - from_index > max(400, (len(path) - 1) * 3 // 4):
        raise CourseError(
            f"한 번에 바꾸려는 구간이 {to_index - from_index}개로 너무 길어요. "
            "절반쯤씩 나눠서 수정해 주세요.")
    if len(vias) > MAX_VIA_POINTS:
        raise CourseError(
            f"경유점은 {MAX_VIA_POINTS}개까지 찍을 수 있어요. "
            "구간을 나눠서 수정해 주세요.")

    g = graphmod.get_graph()
    requested = [(point.lat, point.lon) for point in vias]
    weight = easy_route_weight(_routing_weight_for(params), prefer_named_walkways=True)
    replacement: list[int] = []
    touched: list[int] = []
    cursor = path[from_index]

    def connect(target: int) -> list[int] | None:
        try:
            return _route(g, weight, cursor, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    for lat, lon in requested:
        # A tap names a place, not a junction. The absolutely nearest node is
        # routinely one the router cannot reach at all -- the foot of a
        # staircase, a service ramp, a courtyard inside a building complex --
        # so the next few candidates get a turn before the edit is refused.
        segment = None
        candidates = graphmod.nearby_nodes(
            lat, lon, limit=6, max_distance_m=STROKE_SNAP_MAX_M)
        for node, _ in candidates:
            if node == cursor:
                segment, chosen = [cursor], cursor
                break
            found = connect(node)
            if found is not None:
                segment, chosen = found, node
                break
        if segment is None:
            raise CourseError(
                blocked_reason_near(lat, lon)
                or "짚은 곳까지 이어지는 보행로를 찾지 못했어요. "
                   "지도에 그려진 길 위를 짚어 주세요.")
        replacement.extend(segment if not replacement else segment[1:])
        cursor = chosen
        touched.append(chosen)

    closing = connect(path[to_index])
    if closing is None:
        raise CourseError(
            "짚은 곳에서 기존 코스로 돌아오는 보행로를 찾지 못했어요. "
            "조금 더 코스 가까이를 짚어 주세요.")
    replacement.extend(closing if not replacement else closing[1:])
    # A tapped detour is deliberate by construction -- there is no unsteady
    # hand to second-guess -- so only the router's own hesitation (a there-and-
    # back to touch one stop) is trimmed, and every tap is protected.
    replacement = drop_backtracking(replacement, frozenset(touched))
    edited_path = path[:from_index] + replacement + path[to_index + 1:]
    course = course_from_path(params, edited_path)
    course.note = unreached_point_note(g, course.path, requested)
    return course


def nearest_route_index(g, path: list[int], lat: float, lon: float) -> tuple[int, float]:
    """Which node index of ``path`` the drawn line touched, and how far away.

    Measured against the *drawn* geometry, not the graph nodes: the green line
    on screen follows the OSM way shapes, so a finger that lands on it may be
    a long way from either end of that edge.
    """
    best_index, best_d = 0, math.inf
    for index, (u, v) in enumerate(zip(path, path[1:])):
        points = graphmod.edge_points(g, u, v)
        for (alat, alon), (blat, blon) in zip(points, points[1:]):
            d = _perpendicular_m((lat, lon), (alat, alon), (blat, blon))
            if d < best_d:
                best_index, best_d = index, d
    return best_index, best_d


def _connect_through(g, weight, start: int, waypoints: list[int],
                     finish: int) -> list[int] | None:
    """Walk start -> waypoints -> finish, skipping any stop that cannot be
    reached. ``None`` only when even start -> finish has no walkable path."""
    route: list[int] = []
    cursor = start
    for stop in [*waypoints, finish]:
        if stop == cursor:
            continue
        try:
            segment = _route(g, weight, cursor, stop)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue          # this stop is unreachable; carry on to the next
        route.extend(segment if not route else segment[1:])
        cursor = stop
    if cursor != finish:
        return None
    return route or [start]


# Geometry decides whether two visible lines really meet. UI hit slop and road
# snapping are deliberately absent here: even a 1m gap must remain a gap.
INTERSECTION_EPSILON_M = 1e-4


def _segment_intersection_point(a: tuple[float, float], b: tuple[float, float],
                                c: tuple[float, float], d: tuple[float, float],
                                epsilon_m: float = INTERSECTION_EPSILON_M
                                ) -> tuple[float, float] | None:
    """Return the exact planar intersection of two short geographic segments.

    Seoul-scale coordinates are projected to local metres.  The epsilon is
    arithmetic tolerance only (0.1mm by default), never interaction hit slop.
    Collinear contact returns a shared endpoint; separated parallel segments
    return ``None``.
    """
    origin_lat = (a[0] + b[0] + c[0] + d[0]) / 4.0
    scale_x = 111_320.0 * math.cos(math.radians(origin_lat))
    scale_y = 111_320.0

    def xy(point):
        return point[1] * scale_x, point[0] * scale_y

    p, p2, q, q2 = map(xy, (a, b, c, d))
    r = (p2[0] - p[0], p2[1] - p[1])
    s = (q2[0] - q[0], q2[1] - q[1])
    cross = lambda u, v: u[0] * v[1] - u[1] * v[0]
    rxs = cross(r, s)
    qp = (q[0] - p[0], q[1] - p[1])
    scale = max(math.hypot(*r), math.hypot(*s), 1.0)
    if abs(rxs) <= epsilon_m * scale:
        # Only actual collinear contact counts.  A nearby parallel line does
        # not become connected merely because it is within a screen radius.
        if abs(cross(qp, r)) > epsilon_m * max(math.hypot(*r), 1.0):
            return None
        rr = r[0] * r[0] + r[1] * r[1]
        if rr <= epsilon_m * epsilon_m:
            if math.hypot(p[0] - q[0], p[1] - q[1]) <= epsilon_m:
                return a
            return None
        values = sorted(((q[0] - p[0]) * r[0] + (q[1] - p[1]) * r[1]) / rr
                        for q in (q, q2))
        t = max(0.0, values[0])
        if t > min(1.0, values[1]) + epsilon_m / max(math.sqrt(rr), 1.0):
            return None
    else:
        t = cross(qp, s) / rxs
        u = cross(qp, r) / rxs
        tolerance = epsilon_m / scale
        if not (-tolerance <= t <= 1.0 + tolerance
                and -tolerance <= u <= 1.0 + tolerance):
            return None
        t = min(1.0, max(0.0, t))
    x, y = p[0] + t * r[0], p[1] + t * r[1]
    return y / scale_y, x / scale_x


def _point_on_raw_stroke(point: tuple[float, float],
                         raw: list[tuple[float, float]]) -> bool:
    return any(_perpendicular_m(point, a, b) <= INTERSECTION_EPSILON_M
               for a, b in zip(raw, raw[1:]))


def _topological_intersection_nodes(g, base_path: list[int], drawn_path: list[int],
                                    raw: list[tuple[float, float]]) -> list[int]:
    """Shared graph junctions that the raw pencil line truly passes through.

    Geometry alone would connect a bridge to the road below it. Shared graph
    nodes alone would let road snapping turn a near miss into a connection.
    Requiring both properties avoids both failures.
    """
    if len(raw) < 2:
        return []
    base_nodes = set(base_path)
    found: list[int] = []
    for node in drawn_path:
        if node not in base_nodes or node in found:
            continue
        data = g.nodes[node]
        if _point_on_raw_stroke((data["lat"], data["lon"]), raw):
            found.append(node)

    # A stroke can meet the middle of the very same graph edge. It is a real
    # topological connection even though the graph has no vertex at the pen's
    # exact crossing coordinate. Choose the closest endpoint as the insertion
    # anchor; no unrelated crossing edge is accepted.
    base_edges = {frozenset((u, v)): (u, v) for u, v in zip(base_path, base_path[1:])}
    for u, v in zip(drawn_path, drawn_path[1:]):
        edge = base_edges.get(frozenset((u, v)))
        if edge is None:
            continue
        shape = graphmod.edge_points(g, u, v)
        touched = any(
            _segment_intersection_point(a, b, c, d) is not None
            for a, b in zip(raw, raw[1:])
            for c, d in zip(shape, shape[1:])
        )
        if not touched:
            continue
        for node in (u, v):
            if node in base_nodes and node not in found:
                found.append(node)
    return found


def _raw_stroke_closed(points: list[tuple[float, float]]) -> bool:
    if len(points) < 3:
        return False
    if haversine_m(*points[0], *points[-1]) <= INTERSECTION_EPSILON_M:
        return True
    segments = list(zip(points, points[1:]))
    for i, first in enumerate(segments):
        for j in range(i + 2, len(segments)):
            if i == 0 and j == len(segments) - 1:
                continue
            if _segment_intersection_point(*first, *segments[j]) is not None:
                return True
    return False


def _route_one_stroke(g, weight, stroke: list[CourseWaypoint], *, close: bool
                      ) -> list[int]:
    raw = [(point.lat, point.lon) for point in stroke]
    samples = [raw[0], *stroke_waypoints(stroke), raw[-1]]
    drawn: list[int] = []
    for lat, lon in samples:
        node, snap_m = graphmod.nearest_node(lat, lon)
        if node is None or snap_m > STROKE_SNAP_MAX_M:
            continue
        if not drawn or drawn[-1] != node:
            drawn.append(node)
    if len(drawn) < 2:
        raise CourseError("그린 선이 기존 코스와 이어지지 않았어요. 실제 코스 선과 교차하도록 이어 그려 주세요.")
    finish = drawn[0] if close else drawn[-1]
    via = drawn[1:] if close else drawn[1:-1]
    routed = _connect_through(g, weight, drawn[0], via, finish)
    if routed is None or len(routed) < 2:
        raise CourseError("그린 선을 도보 가능한 길로 잇지 못했어요. 지도에 보이는 길 위로 다시 그려 주세요.")
    return routed


# Interaction tolerance is separate from exact geometric intersection. Only
# explicit gap endpoints / pen lifts may use it, never arbitrary crossings.
DRAW_JOIN_MAX_M = 12.0
# A finger on a city-zoom map is not accurate to 12m. When the strict endpoint
# match has already failed, a wider reach is what lets a visibly connected line
# join at all; the runnable-edge check below still refuses a different level.
DRAW_TIP_JOIN_MAX_M = 45.0


def _snap_gap_endpoints(g, path: list[int], lo: int, hi: int,
                        strokes: list[list[CourseWaypoint]]) -> list[list[CourseWaypoint]]:
    """Bind a near-miss to its intended erased endpoint, not another road.

    The closest graph node must be that endpoint or its immediate neighbour
    on a short runnable edge. Proximity alone cannot join separate levels.
    """
    result = [list(stroke) for stroke in strokes]
    anchors = {path[lo], path[hi]}
    for stroke in result:
        for end in (0, -1):
            point = stroke[end]
            node, _ = graphmod.nearest_node(point.lat, point.lon)
            choices = sorted((haversine_m(point.lat, point.lon, g.nodes[anchor]["lat"],
                                           g.nodes[anchor]["lon"]), anchor) for anchor in anchors)
            for distance, anchor in choices:
                if distance > DRAW_JOIN_MAX_M:
                    break
                edge = g.get_edge_data(node, anchor) if node is not None else None
                if node == anchor or (edge is not None and edge_is_runnable(edge)
                                      and edge.get("length", math.inf) <= DRAW_JOIN_MAX_M * 2):
                    stroke[end] = CourseWaypoint(lat=g.nodes[anchor]["lat"], lon=g.nodes[anchor]["lon"])
                    break
    return result



def _snap_tips_to_retained(g, path: list[int], lo: int, hi: int,
                           strokes: list[list[CourseWaypoint]],
                           max_distance_m: float) -> list[list[CourseWaypoint]]:
    """Bind each pen tip to the retained route it visibly reaches.

    _snap_gap_endpoints only considers the two erased ends, so a line drawn
    back onto the green further out keeps a tip that belongs to no node and
    never registers as a connection. Proximity alone is still not enough: the
    tip must land on that node or one short runnable edge away from it, which
    is what keeps a bridge from joining the road beneath it.
    """
    retained = [path[i] for i in range(len(path)) if i <= lo or i >= hi]
    result = [list(stroke) for stroke in strokes]
    for stroke in result:
        for end in (0, -1):
            point = stroke[end]
            node, _ = graphmod.nearest_node(point.lat, point.lon)
            choices = sorted(
                (haversine_m(point.lat, point.lon, g.nodes[anchor]["lat"],
                             g.nodes[anchor]["lon"]), anchor)
                for anchor in retained)
            for distance, anchor in choices:
                if distance > max_distance_m:
                    break
                edge = g.get_edge_data(node, anchor) if node is not None else None
                if node == anchor or (edge is not None and edge_is_runnable(edge)
                                      and edge.get("length", math.inf) <= max_distance_m * 2):
                    stroke[end] = CourseWaypoint(lat=g.nodes[anchor]["lat"],
                                                 lon=g.nodes[anchor]["lon"])
                    break
    return result


def _join_connected_strokes(g, strokes: list[list[CourseWaypoint]]) -> list[list[CourseWaypoint]]:
    """Join unambiguous end-to-end pen lifts, independent of order/direction.

    Never flatten separate components or invent a long road connector. Near
    misses must resolve to the same nearby junction; exact endpoint contact
    can also lie in the middle of an edge. Routing/topology validation follows.
    """
    ends = {(i, end): stroke[0 if end == 0 else -1]
            for i, stroke in enumerate(strokes) for end in (0, 1)}
    nearest = {key: graphmod.nearest_node(p.lat, p.lon) for key, p in ends.items()}
    candidates = {key: [] for key in ends}
    keys = list(ends)
    for i, left in enumerate(keys):
        a = ends[left]
        for right in keys[i + 1:]:
            if left[0] == right[0]:
                continue
            b = ends[right]
            distance = haversine_m(a.lat, a.lon, b.lat, b.lon)
            if distance > DRAW_JOIN_MAX_M:
                continue
            an, ad = nearest[left]
            bn, bd = nearest[right]
            if distance <= INTERSECTION_EPSILON_M:
                exact_nodes = graphmod.nearby_nodes(
                    a.lat, a.lon, limit=3, max_distance_m=INTERSECTION_EPSILON_M)
                if an is None or an != bn or len(exact_nodes) > 1:
                    continue
                joint = a
            elif an is not None and an == bn and max(ad, bd) <= DRAW_JOIN_MAX_M:
                joint = CourseWaypoint(lat=g.nodes[an]["lat"], lon=g.nodes[an]["lon"])
            else:
                continue
            candidates[left].append((right, joint))
            candidates[right].append((left, joint))
    # A branched/ambiguous contact is not permission to choose a new route.
    links = {key: matches[0] for key, matches in candidates.items()
             if len(matches) == 1 and len(candidates[matches[0][0]]) == 1}
    roots = [key for key in keys if key not in links]
    consumed = set()
    result = []
    for root in [*roots, *keys]:
        if root[0] in consumed:
            continue
        chain = []
        entry = root
        while entry[0] not in consumed:
            index, end = entry
            consumed.add(index)
            part = list(strokes[index] if end == 0 else reversed(strokes[index]))
            if entry in links:
                part[0] = links[entry][1]
            chain.extend(part if not chain else part[1:])
            link = links.get((index, 1 - end))
            if link is None:
                break
            entry, joint = link
            chain[-1] = joint
        result.append(chain)
    return result



# A drawn line and the route it visibly meets can still be a few roads apart in
# the graph. Walking that remainder keeps the runner's own two lines; a longer
# connector would be a route they never drew, so it is refused instead.
DRAW_BRIDGE_MAX_M = 250.0


def _bridge_drawing_to_retained(g, weight, path: list[int], lo: int, hi: int,
                                strokes: list[list[CourseWaypoint]]):
    """Retained green + the drawn line, joined by the shortest walk between.

    The pair that deletes the least green wins, not the pair whose connector is
    shortest: a closed course revisits places, so the nearest join can sit on
    the far side of the loop and collapse the route.

    Returns (replacement, stroke index, start index, end index) or four Nones.
    """
    for index, stroke in enumerate(strokes):
        try:
            routed = _route_one_stroke(g, weight, stroke, close=False)
        except CourseError:
            continue
        if len(routed) < 2:
            continue
        for first, last, ordered in ((routed[0], routed[-1], routed),
                                     (routed[-1], routed[0], routed[::-1])):
            heads = [i for i in range(lo + 1)
                     if haversine_m(g.nodes[path[i]]["lat"], g.nodes[path[i]]["lon"],
                                    g.nodes[first]["lat"], g.nodes[first]["lon"])
                     <= DRAW_BRIDGE_MAX_M]
            tails = [j for j in range(hi, len(path))
                     if haversine_m(g.nodes[path[j]]["lat"], g.nodes[path[j]]["lon"],
                                    g.nodes[last]["lat"], g.nodes[last]["lon"])
                     <= DRAW_BRIDGE_MAX_M]
            # The join may eat into the green beyond the erased span, but not
            # by more than the runner actually drew. Without this a one-sided
            # line reaches the far side of a closed loop and the 5km course
            # comes back as 1.25km.
            allowance = _path_length(g, ordered) + DRAW_BRIDGE_MAX_M
            pairs = sorted(((lo - i) + (j - hi), i, j)
                           for i in heads for j in tails)
            for _removed, i, j in pairs:
                extra = (_path_length(g, path[i:lo + 1])
                         + _path_length(g, path[hi:j + 1]))
                if extra > allowance:
                    continue
                try:
                    lead = _route(g, weight, path[i], first)
                    trail = _route(g, weight, last, path[j])
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                if (_path_length(g, lead) > DRAW_BRIDGE_MAX_M
                        or _path_length(g, trail) > DRAW_BRIDGE_MAX_M):
                    continue
                return lead[:-1] + ordered + trail[1:], index, i, j
    return None, None, None, None


def _path_length(g, nodes: list[int]) -> float:
    return sum(haversine_m(g.nodes[a]["lat"], g.nodes[a]["lon"],
                           g.nodes[b]["lat"], g.nodes[b]["lon"])
               for a, b in zip(nodes, nodes[1:]))


def snap_drawn_strokes(params: CourseParams, path: list[int],
                       from_index: int | None, to_index: int | None,
                       strokes: list[list[CourseWaypoint]]) -> Course:
    """Apply independent pencil strokes without deleting untouched route edges.

    An eraser gap is the only span drawing may replace. Without a gap, a
    connected drawing is inserted as an excursion and every original directed
    edge occurrence remains. Connections require exact geometry *and* shared
    pedestrian-graph topology.
    """
    course_from_path(params, path)
    clean = [stroke for stroke in strokes if len(stroke) >= 2]
    if not clean:
        raise CourseError("그린 선이 기존 코스와 이어지지 않았어요. 실제 코스 선과 교차하도록 이어 그려 주세요.")
    g = graphmod.get_graph()
    weight = easy_route_weight(_routing_weight_for(params), prefer_named_walkways=True)
    current = list(path)
    gap = None
    if from_index is not None or to_index is not None:
        if (from_index is None or to_index is None
                or not 0 <= from_index < to_index < len(path)):
            raise CourseError("지운 구간의 양 끝을 다시 확인해 주세요.")
        gap = (from_index, to_index)

    pending = list(clean)
    if gap:
        lo, hi = gap
        if hi - lo > max(400, (len(path) - 1) * 3 // 4):
            raise CourseError("한 번에 지운 구간이 너무 길어요. 더 짧게 나눠 수정해 주세요.")
        pending = _snap_gap_endpoints(g, path, lo, hi, pending)
        pending = _join_connected_strokes(g, pending)
        replacement = None
        used = None
        for index, stroke in enumerate(pending):
            raw = [(point.lat, point.lon) for point in stroke]
            routed = _route_one_stroke(g, weight, stroke, close=False)
            connections = _topological_intersection_nodes(g, path, routed, raw)
            if path[lo] not in connections or path[hi] not in connections:
                continue
            a = routed.index(path[lo])
            b = len(routed) - 1 - routed[::-1].index(path[hi])
            if a > b:
                routed.reverse()
                a = routed.index(path[lo])
                b = len(routed) - 1 - routed[::-1].index(path[hi])
            if a < b:
                replacement = routed[a:b + 1]
                if not stroke_is_doubled(stroke):
                    replacement = drop_backtracking(
                        replacement,
                        keep_span=lambda span: _within_drawn_corridor(g, span, raw))
                used = index
                break
        join_lo, join_hi = lo, hi
        if replacement is None:
            # What the runner sees is the retained green plus the line they
            # drew. Keep exactly that and walk the short remainder between
            # them, deleting as little green as the join allows. Matching
            # shared graph nodes instead used to pick the far side of a closed
            # loop and collapse the course.
            replacement, used, join_lo, join_hi = _bridge_drawing_to_retained(
                g, weight, path, lo, hi, pending)
        if replacement is None:
            raise CourseError(
                "그린 선이 지운 구간의 양 끝과 실제로 이어지지 않았어요. 붉은 선 양 끝을 교차하도록 다시 그려 주세요.")
        current = path[:join_lo] + replacement + path[join_hi + 1:]
        pending.pop(used)

    # Add every remaining stroke independently. Never flatten two strokes:
    # flattening invents a straight connector between separate finger lifts.
    for stroke in pending:
        raw = [(point.lat, point.lon) for point in stroke]
        base_points = {
            (g.nodes[node]["lat"], g.nodes[node]["lon"])
            for node in current
        }
        if all(point in base_points for point in raw):
            continue
        closed = _raw_stroke_closed(raw)
        routed = _route_one_stroke(g, weight, stroke, close=closed)
        connections = _topological_intersection_nodes(g, current, routed, raw)
        if closed:
            if not connections:
                raise CourseError(
                    "그린 선이 기존 코스와 이어지지 않았어요. 실제 코스 선과 교차하도록 이어 그려 주세요.")
            # Re-tracing the existing nodes is not a new loop. Treat it as a
            # no-op so confirming a line drawn over green does not triple the
            # distance with an invented duplicate traversal.
            anchor = connections[0]
            core = routed[:-1] if routed[0] == routed[-1] else routed
            anchor_at = core.index(anchor)
            loop = core[anchor_at:] + core[:anchor_at] + [anchor]
            # Drawing over an already-present loop is a no-op. Adding it again
            # would preserve the original technically while making the runner
            # traverse the same course twice.
            from collections import Counter
            existing_edges = Counter(zip(current, current[1:]))
            loop_edges = Counter(zip(loop, loop[1:]))
            if not (loop_edges - existing_edges):
                continue
            insert_at = current.index(anchor)
            current = current[:insert_at] + loop + current[insert_at + 1:]
            continue

        positions = [(i, node) for i, node in enumerate(routed)
                     if node in set(connections)]
        if len({node for _, node in positions}) < 2:
            raise CourseError(
                "그린 선이 기존 코스와 이어지지 않았어요. 선이 실제 코스와 두 곳에서 교차하도록 이어 그려 주세요.")
        first_i, anchor = positions[0]
        last_i, _ = next(item for item in reversed(positions)
                         if item[1] != anchor)
        if first_i > last_i:
            first_i, last_i = last_i, first_i
            anchor = routed[first_i]
        addition = routed[first_i:last_i + 1]
        excursion = addition + list(reversed(addition[:-1]))
        insert_at = current.index(anchor)
        current = current[:insert_at] + excursion + current[insert_at + 1:]

    return course_from_path(params, current)


def snap_drawn_segment(params: CourseParams, path: list[int],
                       from_index: int | None, to_index: int | None,
                       stroke: list[CourseWaypoint]) -> Course:
    """Backward-compatible one-stroke entry point."""
    return snap_drawn_strokes(params, path, from_index, to_index, [stroke])


# A drawn line says where to go, not which junction to touch. Waypoints closer
# together than this add no shape a runner can see, but each one is a hard
# constraint the router must satisfy -- and two neighbouring samples that snap
# to different ways force a detour out to one and back.
# Walking the same path out and back is a valid loop and a joyless run. The
# penalty is scaled to the RFS range so a fully retraced loop loses to almost
# any real circuit, while a short shared spur stays acceptable.
RETRACE_PENALTY = 120.0
RETRACE_ACCEPTABLE = 0.25
# How far apart the points handed to the router have to be. Closer than this
# and two neighbouring samples snap to different ways, forcing a detour out to
# one and back -- the zigzag that turned a straight 489m line into 1,515m.
# Dropping it to 90m to follow a drawing more closely brought that straight
# back: a +/-25m wobble backtracked over six of thirty-two nodes. Fidelity
# comes from lifting the *count* cap instead, which is what actually bound a
# long outline.
STROKE_WAYPOINT_MIN_M = 140.0
# Perpendicular deviation below which a drawn line counts as straight.
STROKE_SIMPLIFY_TOLERANCE_M = 45.0
# The cap scales with the drawing. A fixed eight points meant a long outline --
# a 고구마 round 여의도, say -- was reconstructed from eight anchors and the
# router filled the gaps with whatever it liked. Editing is the one place the
# runner is entitled to be followed exactly, so the budget follows the line.
STROKE_WAYPOINT_PER_M = 120.0
STROKE_WAYPOINT_MIN_COUNT = 8
STROKE_WAYPOINT_MAX = 240
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
# How far off the straight run between the two ends a drawn waypoint must sit
# before it counts as a detour the runner meant rather than a wobble.
DETOUR_WAYPOINT_MIN_M = 150.0


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
    walked = sum(haversine_m(a[0], a[1], b[0], b[1])
                 for a, b in zip(points, points[1:]))
    budget = max(STROKE_WAYPOINT_MIN_COUNT,
                 min(STROKE_WAYPOINT_MAX, math.ceil(walked / STROKE_WAYPOINT_PER_M)))
    if len(spaced) <= budget:
        return spaced
    step = math.ceil(len(spaced) / budget)
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


def _detour_nodes(graph, drawn: list[int], start: int, end: int) -> frozenset[int]:
    """Which drawn waypoints actually send the route somewhere else.

    An unsteady hand leaves waypoints scattered along the line it meant to
    draw; they sit within tens of metres of the straight run between the two
    ends and take the route nowhere. A deliberate detour -- north around
    경복궁, say -- sits hundreds of metres off it. Only the second kind is
    worth protecting from spur removal, or every wobble becomes a spur the
    route has to honour.
    """
    a = (graph.nodes[start]["lat"], graph.nodes[start]["lon"])
    b = (graph.nodes[end]["lat"], graph.nodes[end]["lon"])
    out = set()
    for node in drawn:
        point = (graph.nodes[node]["lat"], graph.nodes[node]["lon"])
        if _perpendicular_m(point, a, b) >= DETOUR_WAYPOINT_MIN_M:
            out.add(node)
    return frozenset(out)


# How far a route may stray from the drawn line and still count as part of it.
STROKE_CORRIDOR_M = 55.0
# Ends this close together mean the runner drew a closed shape, and a closed
# shape is a whole course rather than a patch on one.
# A spur the runner drew reaches somewhere. One a wobbling finger produced does
# not: a +/-25m tremor makes out-and-backs a few tens of metres deep, and those
# are the zigzag, not an instruction.
DRAWN_SPUR_MIN_M = 120.0


def _within_drawn_corridor(graph, span: list[int],
                           drawn: list[tuple[float, float]]) -> bool:
    """Did the runner's own line go where this excursion goes?

    A repeated node is an out-and-back. Some are the runner's -- they drew a
    spur on purpose -- and some the router invented on its way out to touch a
    waypoint. The drawn line tells them apart: an excursion the runner drew
    stays inside the corridor of their stroke, and one the router invented
    sticks out of it. Judging by distance from the chord between the two ends
    instead protected everything on any large outline, which is why a 고구마
    drawn round 여의도 came back with spikes along its top edge.
    """
    if len(drawn) < 2 or not span:
        return True
    root = graph.nodes[span[0]]
    reach = 0.0
    for node in span:
        data = graph.nodes[node]
        point = (data["lat"], data["lon"])
        if min(_perpendicular_m(point, a, b)
               for a, b in zip(drawn, drawn[1:])) > STROKE_CORRIDOR_M:
            return False                      # the runner's line never went there
        reach = max(reach, haversine_m(root["lat"], root["lon"], *point))
    return reach >= DRAWN_SPUR_MIN_M


def drop_backtracking(nodes: list[int],
                      protected: frozenset[int] = frozenset(),
                      keep_span=None) -> list[int]:
    """Cut excursions the router invented, keep the ones the runner drew.

    A repeated node is usually an out-and-back spur, and removing what lies
    between the two visits leaves a shorter walk over the same edges. But the
    same shape appears when a runner deliberately draws a detour -- out to a
    place and back to the line -- and cutting that threw the drawn detour
    away: a line drawn north around 경복궁 came back as the original route,
    its northernmost point *lower* than before.

    ``protected`` holds the nodes the drawn waypoints snapped to. They are
    the runner's explicit instruction, so an excursion containing one is
    never cut; a spur the router invented on its own contains none.
    """
    trimmed: list[int] = []
    seen: dict[int, int] = {}
    for node in nodes:
        start = seen.get(node)
        if start is not None:
            span = trimmed[start + 1:]
            keep = (keep_span(span) if keep_span is not None
                    else not protected.isdisjoint(span))
            if not keep:
                del trimmed[start + 1:]
                for gone in list(seen):
                    if seen[gone] > start:
                        del seen[gone]
                continue
            # The runner asked to go here; keep the excursion and let the
            # repeat stand rather than deleting what they drew.
            trimmed.append(node)
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


def easy_route_weight(base_weight, prefer_named_walkways: bool = False):
    """Prefer roads that are easier to follow without exposing a new score.

    A tiny fixed cost per edge discourages routes made of many short alley
    fragments while keeping the existing RFS/length weighting dominant.

    ``prefer_named_walkways`` is an *edit-only* preference. A course_id
    re-generates its course from parameters alone, so any change to the
    weights generation uses silently rewrites every link ever shared --
    measured: the same eight starts came back with different routes. Edits
    carry their node path in the id instead, so they are free to route on a
    better rule than the one that drew the original.
    """
    def _weight(_u, _v, attrs):
        if not edge_is_runnable(attrs):
            return None
        # A base is not priced, it is refused. Pricing alone could not keep
        # courses out: _loop_via_circle places its waypoints geometrically, and
        # a waypoint that lands inside the walls forces the route in whatever
        # the edge costs. Refusing the edge makes that waypoint unroutable, and
        # the bearing/rescale loop simply tries another.
        if attrs.get("military"):
            return None
        gated = GATED_WEIGHT_FACTOR if attrs.get("gated") else 1.0
        highway = highway_class(attrs)
        factor = HIGHWAY_COST_FACTOR.get(highway, 1.06)
        # A *named* footway or plaza is a real walkway -- a riverside
        # promenade, a park path with a name -- and Kakao draws it. The
        # generic footway cost exists to steer away from the unnamed alley
        # network, which the map does not draw and a runner cannot follow;
        # it should not also push an edit off 반포천길 and onto the road
        # beside it. Unnamed ones keep their cost and their 2.2x alignment
        # penalty (rfs.map_alignment_factor).
        if (prefer_named_walkways and attrs.get("name")
                and highway in NAMED_WALKWAY_HIGHWAYS):
            factor = min(factor, NAMED_WALKWAY_COST)
        sidewalk = float(attrs.get("sidewalk_score", 0.5))
        if sidewalk >= 0.85:
            factor *= 0.90
        elif sidewalk < 0.55:
            factor *= 1.12
        base = base_weight(_u, _v, attrs) if callable(base_weight) else attrs.get(base_weight, attrs["length"])
        return (base * factor * gated
                + FOLLOW_EDGE_PENALTY_M)
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


# How far a circle waypoint may be nudged before the bearing is abandoned.
WAYPOINT_SNAP_MAX_M = 600.0


def _node_is_routable(g, weight, node) -> bool:
    return any(weight(node, other, g.edges[node, other]) is not None
               for other in g[node])


def _usable_waypoint(g, weight, lat: float, lon: float):
    """The nearest graph node at (lat, lon) that the router can actually use.

    The nearest node is taken first and returned unchanged whenever it works,
    so every course_id minted before Yongsan Garrison was excluded still
    reproduces its exact route -- checked against eight representative starts,
    16/16 identical. Only when that node is one no course may pass through do
    the next few candidates get a turn: a single unusable waypoint used to fail
    the whole bearing, and with a 2.5km hole in the middle of 용산 that left
    seven of sixteen starts around the base unable to produce a course at all.
    """
    node, snap = graphmod.nearest_node(lat, lon)
    if snap > WAYPOINT_SNAP_MAX_M or node not in g:
        return None
    if _node_is_routable(g, weight, node):
        return node
    for other, distance in graphmod.nearby_nodes(
            lat, lon, limit=6, max_distance_m=WAYPOINT_SNAP_MAX_M):
        if other is not node and other in g and _node_is_routable(g, weight, other):
            return other
    return None


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
            node = _usable_waypoint(g, weight, lat, lon)
            # Reject waypoints that snapped far away (e.g. across a river) or
            # outside the local subgraph — that bearing doesn't fit the land.
            if node is None:
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
    weight = easy_route_weight(_routing_weight_for(params))

    best: Course | None = None
    best_key: tuple | None = None
    best_err = math.inf
    best_retrace = 1.0
    n_in_tol = 0
    deadline = time.perf_counter() + 0.8  # anytime cutoff (PRD §7.1)
    offset = params.route_variant
    for bearing in BEARINGS[offset:] + BEARINGS[:offset]:
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
        summary = route_rfs_summary(g, path, params.night_mode, params.include_hills)
        if params.night_mode and not has_sufficient_night_lighting(summary):
            continue
        if err <= DISTANCE_TOLERANCE:
            n_in_tol += 1
        points = [(g.nodes[n]["lat"], g.nodes[n]["lon"]) for n in path]
        fac_hits, fac_total = facility_requirement_score(points, params.need_facilities)
        # Prefer in-tolerance loops with the highest RFS; flat mode also
        # rewards low cumulative ascent. A hidden followability penalty keeps
        # the selected course from becoming a maze of tiny bends.
        ascent_per_km = ascent / (length / 1000.0) if length else 0.0
        retraced = retrace_share(g, path)
        quality = (
            -summary["score"]
            + (2.0 * ascent_per_km if params.include_hills is False else 0.0)
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
        if params.night_mode:
            raise CourseError("이 출발지와 거리에서 가로등이 충분한 코스를 확인하지 못했어요. "
                              "조명 정보가 부족한 길을 야간 코스로 추천하지 않아요.")
        raise CourseError(
            "이 위치에서는 순환 코스를 만들지 못했어요. "
            "출발점을 큰길이나 공원 근처로 조금 옮겨서 다시 시도해 주세요."
        )
    # Near-misses are still useful: we always display the *real* distance, so
    # a 4.4km loop for a 4km ask beats a refusal (river/terrain constraints).
    if best_err > DISTANCE_TOLERANCE * 2.5:
        raise DistanceMissError(params.distance_km, best.length_km)
    return best
