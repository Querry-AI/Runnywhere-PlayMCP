"""Military access rules apply to generation, presets and old course links."""

import asyncio
import gzip
import json

import networkx as nx
import pytest
from starlette.requests import Request

from runart import animal_presets, course as coursemod, graph as graphmod, server
from runart.course import Course, CourseError, course_from_path, course_route_issues
from runart.models import CourseParams, encode_course_id
from runart.shapes import _corridor_route, _road_weight


@pytest.fixture
def routes(monkeypatch):
    graph = nx.Graph()
    points = [(37.56, 126.97), (37.561, 126.97), (37.561, 126.971),
              (37.56, 126.971), (37.559, 126.971)]
    for node, (lat, lon) in enumerate(points):
        graph.add_node(node, lat=lat, lon=lon)
    for a, b in [(0, 1), (1, 2), (2, 0), (0, 3), (3, 4), (4, 0)]:
        graph.add_edge(a, b, length=400.0, highway="residential", slope_pct=0,
                       military={a, b} == {0, 3})
    monkeypatch.setattr(graphmod, "get_graph", lambda: graph)
    params = CourseParams(lat=37.56, lon=126.97, location_name="테스트역",
                          distance_km=1.2, shape="dog")
    def make(path, shape):
        return Course(params=params.model_copy(update={"shape": shape}), path=path,
                      points=[points[node] for node in path], length_m=1200,
                      rfs={"score": 80}, shape_similarity=.9)
    return graph, make([0, 1, 2, 0], "cat"), make([0, 3, 4, 0], "dog")


@pytest.mark.parametrize("highway", ["residential", "footway", "service", "path"])
def test_military_edges_are_not_runnable_even_on_pedestrian_roads(highway):
    assert not coursemod.edge_is_runnable({"highway": highway, "military": True})
    assert coursemod.edge_is_runnable({"highway": highway, "military": False})


def test_both_animal_routing_weights_avoid_the_military_shortcut(routes):
    graph, _, _ = routes
    assert _road_weight(0, 3, graph.edges[0, 3]) is None
    assert nx.shortest_path(graph, 0, 3, weight=_road_weight) == [0, 4, 3]
    xy = {0: (0, 0), 3: (100, 0), 4: (50, 10), 1: (0, 50), 2: (50, 50)}
    assert _corridor_route(graph, xy, 0, 3, xy[0], xy[3], 100) == [0, 4, 3]


def test_runtime_route_audit_names_military_restriction(routes):
    graph, safe, unsafe = routes
    assert course_route_issues(safe, graph) == []
    assert "blocked_military" in course_route_issues(unsafe, graph)


def test_exact_saved_paths_cannot_reintroduce_military_edges(routes):
    _, safe, unsafe = routes
    assert course_from_path(safe.params, safe.path).path == safe.path
    with pytest.raises(CourseError, match="군사|군부대|통행"):
        course_from_path(unsafe.params, unsafe.path)


@pytest.fixture
def preset_file(routes, tmp_path, monkeypatch):
    _, safe, unsafe = routes
    entries = {animal_presets.preset_key(c.params.lat, c.params.lon, c.params.shape):
               animal_presets.serialize_course(c) for c in (safe, unsafe)}
    path = tmp_path / "presets.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump({"format_version": animal_presets.FORMAT_VERSION,
                   "graph_fingerprint": "unchanged-graph", "entries": entries}, stream)
    monkeypatch.setattr(animal_presets, "PRESET_PATH", path)
    monkeypatch.setattr(animal_presets, "graph_fingerprint", lambda: "unchanged-graph")
    monkeypatch.setattr(animal_presets, "verify_data_file", lambda _: None)
    animal_presets._load.cache_clear()
    animal_presets.all_verified_animal_presets.cache_clear()
    yield safe, unsafe
    animal_presets._load.cache_clear()
    animal_presets.all_verified_animal_presets.cache_clear()


def test_matching_graph_fingerprint_does_not_bypass_current_runtime_rules(preset_file):
    safe, unsafe = preset_file
    assert animal_presets.get_animal_preset(safe.params).path == safe.path
    assert animal_presets.get_animal_preset(unsafe.params) is None
    assert animal_presets.find_nearby_animal_presets(unsafe.params) == []
    assert animal_presets.find_nearest_animal_preset(unsafe.params) is None
    assert [c.path for c in animal_presets.all_verified_animal_presets()] == [safe.path]
    assert "1 blocked" in animal_presets.preset_status()


def test_preset_safety_scan_runs_only_once_per_load(preset_file, monkeypatch):
    calls = []
    original = coursemod.course_route_issues
    def audit(course, graph):
        calls.append(course.path)
        return original(course, graph)
    monkeypatch.setattr(animal_presets, "course_route_issues", audit, raising=False)
    safe, unsafe = preset_file
    for _ in range(3):
        animal_presets.get_animal_preset(safe.params)
        animal_presets.get_animal_preset(unsafe.params)
        animal_presets.find_nearby_animal_presets(unsafe.params)
        animal_presets.all_verified_animal_presets()
    assert len(calls) == 2


def test_blocked_cold_preset_link_does_not_silently_regenerate(preset_file, monkeypatch):
    _, unsafe = preset_file
    calls = []
    monkeypatch.setattr(server, "_course_cache", {})
    monkeypatch.setattr(server, "_offload", lambda *a, **kw: calls.append(a) or unsafe)
    with pytest.raises(CourseError, match="군사|군부대|통행"):
        server._get_course(unsafe.params)
    assert calls == []


def test_unsafe_course_cannot_enter_server_cache(routes, monkeypatch):
    _, safe, unsafe = routes
    monkeypatch.setattr(server, "_course_cache", {})
    server._cache_put(encode_course_id(safe.params), safe)
    assert server._cached_course(encode_course_id(safe.params)) is safe
    with pytest.raises(CourseError, match="군사|군부대|통행"):
        server._cache_put(encode_course_id(unsafe.params), unsafe)


def test_warm_cache_cannot_bypass_updated_military_rules(routes, monkeypatch):
    _, _, unsafe = routes
    cid = encode_course_id(unsafe.params)
    monkeypatch.setattr(server, "_course_cache", {cid: unsafe})
    with pytest.raises(CourseError, match="군사|군부대|통행"):
        server._get_course(unsafe.params)
    assert server._cached_course(cid) is None


def test_route_json_stays_preset_only_for_a_safe_hot_generated_course(routes, monkeypatch):
    _, safe, _ = routes
    cid = encode_course_id(safe.params)
    monkeypatch.setattr(server, "_course_cache", {cid: safe})
    monkeypatch.setattr(server, "get_animal_preset", lambda _: animal_presets.MISSING)
    request = Request({"type": "http", "method": "GET", "path": f"/c/{cid}/route.json",
                       "headers": [], "path_params": {"course_id": cid}})
    response = asyncio.run(server.course_route_json(request))
    assert response.status_code == 404
    assert b"not a verified course" in response.body


@pytest.mark.parametrize(
    "view", ["info", "gpx", "run", "editor", "card", "thumb"]
)
def test_existing_unsafe_links_return_a_clear_block_instead_of_route(routes, monkeypatch, view):
    _, _, unsafe = routes
    cid = encode_course_id(unsafe.params)
    monkeypatch.setattr(server, "_course_cache", {cid: unsafe})
    raw = cid + (".gpx" if view == "gpx" else "")
    request = Request({"type": "http", "method": "GET", "path": f"/c/{raw}",
                       "headers": [], "path_params": {"course_id": raw}})
    handler = {"info": server.preview, "gpx": server.preview,
               "run": server.course_run_page, "editor": server.course_editor_page,
               "card": server.share_card, "thumb": server.course_thumbnail}[view]
    response = asyncio.run(handler(request))
    assert response.status_code == 403
    assert "통행" in response.body.decode()


def test_blocked_verified_preset_route_json_returns_clear_403(routes, monkeypatch):
    _, _, unsafe = routes
    cid = encode_course_id(unsafe.params)
    monkeypatch.setattr(server, "animal_preset_is_blocked", lambda _: True)
    request = Request({"type": "http", "method": "GET", "path": f"/c/{cid}/route.json",
                       "headers": [], "path_params": {"course_id": cid}})
    response = asyncio.run(server.course_route_json(request))
    assert response.status_code == 403
    assert "통행" in response.body.decode()


def test_bundled_samgakji_dog_is_not_available_after_runtime_validation():
    graph = graphmod.get_graph()
    with gzip.open(animal_presets.PRESET_PATH, "rt", encoding="utf-8") as stream:
        entries = json.load(stream)["entries"]
    originals = [animal_presets._deserialize_course(raw) for raw in entries.values()
                 if raw and "삼각지" in raw["params"]["location_name"]
                 and raw["params"]["shape"] == "dog"]
    assert originals
    for course in originals:
        assert any(graph.edges[a, b].get("military")
                   for a, b in zip(course.path, course.path[1:]))
        assert animal_presets.get_animal_preset(course.params) is None
