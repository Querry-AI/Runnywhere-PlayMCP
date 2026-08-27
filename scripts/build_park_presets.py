#!/usr/bin/env python3
"""Build five actual park-path courses; never alter measured lighting data."""
import hashlib
import json

import networkx as nx

from runart import graph as graphmod
from runart.animal_presets import graph_fingerprint
from runart.course import (BEARINGS, CourseError, _loop_via_circle,
                           course_from_path, edge_is_runnable, retrace_share)
from runart.geo import haversine_m
from runart.models import CourseParams
from runart.park_presets import PARK_SPOTS, PRESET_PATH


def build(spot):
    graph = graphmod.get_graph()
    local = graphmod.subgraph_around(spot.lat, spot.lon, 2800)
    green = nx.Graph()
    for a, b, attrs in local.edges(data=True):
        if attrs.get("park_score", 0) >= .8 and edge_is_runnable(attrs) and not attrs.get("military"):
            green.add_edge(a, b, **attrs)
    for node in green:
        green.nodes[node].update(graph.nodes[node])
    distance = lambda n: haversine_m(spot.lat, spot.lon, graph.nodes[n]["lat"], graph.nodes[n]["lon"])
    components = [nodes for nodes in nx.connected_components(green)
                  if len(nodes) >= 30 and min(map(distance, nodes)) < 400]
    if not components:
        raise RuntimeError(f"No connected park paths at {spot.name}")
    green = green.subgraph(max(components, key=len)).copy()
    start = min(green, key=distance)
    params = CourseParams(lat=green.nodes[start]["lat"], lon=green.nodes[start]["lon"],
                          location_name=spot.name, need_facilities=["park"])
    paths = []
    for km in (5, 4, 3, 6):
        for bearing in BEARINGS:
            path = _loop_via_circle(green, lambda a, b, d: d["length"], start, km * 1000, bearing)
            if path:
                paths.append(path)
    # A waterside out-and-back is valid, too. Keep its actual geometry and
    # retracing facts; never label it a separate loop or invent a bridge.
    lengths, routes = nx.single_source_dijkstra(green, start, weight="length")
    end = min(lengths, key=lambda n: abs(lengths[n] - 2500))
    outward = routes[end]
    paths.append(outward + outward[-2::-1])
    courses = []
    for path in paths:
        try:
            courses.append(course_from_path(params, path))
        except CourseError:
            continue
    if not courses:
        raise RuntimeError(f"No valid course at {spot.name}")
    course = min(courses, key=lambda c: (
        abs(c.length_km - 5) > .75,
        retrace_share(graph, c.path), abs(c.length_km - 5)))
    print(spot.name, f"{course.length_km:.2f}km", len(course.path),
          f"lighting={course.rfs['components']['lighting']}", flush=True)
    return course.params.canonical()


if __name__ == "__main__":
    entries = {spot.id: build(spot) for spot in PARK_SPOTS}
    payload = {"format_version": 1, "graph_fingerprint": graph_fingerprint(), "entries": entries}
    PRESET_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Register checksum:", hashlib.sha256(PRESET_PATH.read_bytes()).hexdigest())
