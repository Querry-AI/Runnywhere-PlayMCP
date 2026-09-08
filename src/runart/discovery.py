"""Discover existing road circuits without imposing a default target distance."""
from __future__ import annotations

import time

import networkx as nx

from . import graph as graphmod
from .course import Course, CourseError, course_from_path, easy_route_weight, edge_is_runnable
from .courseplan import EFFORT_TOLERANCE, NEARBY_RADIUS_M, RECOMMENDATION_COUNT, route_signature
from .facilities import facility_requirement_score
from .geo import haversine_m
from .models import CourseParams
from .rfs import has_sufficient_night_lighting, route_rfs_summary, routing_weight


def discover_courses(params: CourseParams, *, nearby_start: bool = False,
                     target_km: float | None = None, timeout_s: float = 1.0) -> list[Course]:
    """Find real local circuits; only an explicit distance creates an effort gate.

    Nearby requests can start on a local road. Exact-start requests include a
    real pedestrian connector to/from that circuit. Stored manual paths make
    the chosen distance and start reproducible without repeating this search.
    """
    if timeout_s <= 0:
        return []
    deadline = time.monotonic() + timeout_s
    source = graphmod.get_graph()
    local = graphmod.subgraph_around(params.lat, params.lon, NEARBY_RADIUS_M)
    graph = nx.Graph()
    graph.add_edges_from((a, b, data) for a, b, data in local.edges(data=True)
                         if edge_is_runnable(data) and not data.get("military")
                         and not (params.night_mode and data.get("gated")))
    distances = {n: haversine_m(params.lat, params.lon, source.nodes[n]["lat"],
                               source.nodes[n]["lon"]) for n in graph}
    start, snap = graphmod.nearest_node(params.lat, params.lon)
    if not nearby_start and (start not in graph or snap > 150):
        return []
    paths = None
    if not nearby_start:
        weight = easy_route_weight(routing_weight(params.night_mode, params.include_hills))
        paths = nx.single_source_dijkstra_path(graph, start, weight=weight)
    # Cycle basis enumerates actual connected roads, not circle sizes like 5km.
    # Locality and deadline bound the work; they are not user distance goals.
    candidates = []
    seen = set()
    for cycle in nx.cycle_basis(graph, root=start if start in graph else None):
        if time.monotonic() >= deadline:
            break
        anchor = min(cycle, key=lambda n: (distances[n], n))
        if distances[anchor] > NEARBY_RADIUS_M:
            continue
        index = cycle.index(anchor)
        ring = cycle[index:] + cycle[:index] + [anchor]
        if paths is not None:
            connector = paths.get(anchor)
            if connector is None:
                continue
            path = connector[:-1] + ring + list(reversed(connector[:-1]))
        else:
            path = ring
        if len(path) > 1200:
            continue
        length = sum(graph.edges[a, b]["length"] for a, b in zip(path, path[1:])) / 1000
        if not 1 <= length <= 42.195:
            continue
        if target_km is not None and abs(length - target_km) / target_km > EFFORT_TOLERANCE:
            continue
        signature = frozenset((min(a, b), max(a, b)) for a, b in zip(path, path[1:]))
        if signature in seen:
            continue
        seen.add(signature)
        summary = route_rfs_summary(graph, path, params.night_mode, params.include_hills)
        if params.night_mode and not has_sufficient_night_lighting(summary):
            continue
        # Nearby means start proximity, not fidelity to an unrequested length.
        candidates.append((distances[anchor] if nearby_start else 0,
                           -summary.get("components", {}).get("lighting", 0) if params.night_mode else 0,
                           path))
    candidates.sort(key=lambda c: c[:2])
    result = []
    for _, _, path in candidates:
        if time.monotonic() >= deadline:
            break
        updates = {}
        if nearby_start:
            node = source.nodes[path[0]]
            road = source.edges[path[0], path[1]].get("name")
            if isinstance(road, (tuple, list)):
                road = road[0] if road else None
            updates = {"lat": node["lat"], "lon": node["lon"],
                       "location_name": str(road) if road else f"{params.location_name} 인근 보행로"}
        try:
            course = course_from_path(params.model_copy(update=updates), path)
        except CourseError:
            continue
        if params.include_hills is not None and course.is_flat == params.include_hills:
            continue
        hits, wanted = facility_requirement_score(course.points, params.need_facilities)
        if hits < wanted:
            continue
        if route_signature(course) not in {route_signature(c) for c in result}:
            result.append(course)
        if len(result) == RECOMMENDATION_COUNT:
            break
    return result
