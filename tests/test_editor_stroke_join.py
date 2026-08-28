"""Real edit requests: finger-sized misses and connected pen lifts."""
import asyncio
from collections import Counter
import json

import pytest

from runart import graph as graphmod, server
from runart.course import generate_course
from runart.models import CourseParams, encode_course_id
from test_course_edit import _json_request


@pytest.fixture(scope="module")
def source():
    return generate_course(CourseParams(lat=37.5665, lon=126.9780,
        location_name="서울시청", distance_km=5))


def _stroke(source, lo=10, hi=40, offset=0):
    g = graphmod.get_graph()
    return [dict(lat=g.nodes[node]["lat"] + offset / 111320,
                 lon=g.nodes[node]["lon"]) for node in source.path[lo:hi+1]]


def _snap(source, strokes):
    response = asyncio.run(server.edit_course_route(_json_request(encode_course_id(source.params),
        dict(action="snap", path=source.path, from_index=10, to_index=40, strokes=strokes))))
    return response, json.loads(response.body)


@pytest.mark.parametrize("offset", [.5, 5])
def test_finger_miss_at_erased_endpoints_can_be_previewed(source, offset):
    response, data = _snap(source, [_stroke(source, offset=offset)])
    assert response.status_code == 200, data
    path = [point[0] for point in data["path"]]
    assert path[:11] == source.path[:11]
    assert path[-len(source.path[40:]):] == source.path[40:]


@pytest.mark.parametrize("reverse", [False, True])
def test_connected_pen_lifts_replace_gap_without_deleting_green_edges(source, reverse):
    strokes = [_stroke(source, 10, 20), _stroke(source, 20, 30), _stroke(source, 30, 40)]
    if reverse:
        strokes = [list(reversed(stroke)) for stroke in reversed(strokes)]
    response, data = _snap(source, strokes)
    assert response.status_code == 200, data
    path = [point[0] for point in data["path"]]
    protected = Counter(zip(source.path[:10], source.path[1:11]))
    protected.update(zip(source.path[40:], source.path[41:]))
    assert not (protected - Counter(zip(path, path[1:])))


def test_nearby_pen_lifts_snap_only_to_the_same_junction(source):
    strokes = [_stroke(source, 10, 25), _stroke(source, 25, 40, offset=.5)]
    response, data = _snap(source, strokes)
    assert response.status_code == 200, data


def test_untouched_gap_with_disconnected_strokes_stays_an_error(source):
    response, data = _snap(source, [_stroke(source, 10, 15), _stroke(source, 35, 40)])
    assert response.status_code == 422, data
    assert "error" in data


def test_connected_multistroke_preview_can_be_saved(source):
    response, data = _snap(source, [_stroke(source, 10, 25), _stroke(source, 25, 40)])
    assert response.status_code == 200, data
    saved = asyncio.run(server.edit_course_route(_json_request(encode_course_id(source.params),
        dict(action="save", path=[row[0] for row in data["path"]], name="이어 그린 코스"))))
    assert saved.status_code == 200, json.loads(saved.body)
    assert "preview_url" in json.loads(saved.body)


@pytest.fixture
def small_graph(monkeypatch):
    """Real small graph, with nearest-node lookup bound to this test graph."""
    import networkx as nx
    from runart.geo import haversine_m
    g = nx.Graph()
    for node, north in [(1, 0), (2, 100), (3, 200), (4, 5)]:
        g.add_node(node, lat=37.56 + north / 111320, lon=127.0)
    def nearest(lat, lon):
        return min(((n, haversine_m(lat, lon, data["lat"], data["lon"]))
                    for n, data in g.nodes(data=True)), key=lambda row: row[1])
    monkeypatch.setattr(graphmod, "nearest_node", nearest)
    return g


def test_nearby_but_grade_separated_endpoint_is_not_attached(small_graph):
    from runart.course import _snap_gap_endpoints
    from runart.models import CourseWaypoint
    stroke = [CourseWaypoint(lat=small_graph.nodes[n]["lat"], lon=127) for n in (4, 3)]
    result = _snap_gap_endpoints(small_graph, [1, 2, 3], 0, 2, [stroke])
    assert result[0][0] == stroke[0]


@pytest.mark.parametrize("highway,attached", [("footway", True), ("steps", False)])
def test_only_short_runnable_edges_can_attach_neighboring_endpoint(small_graph, highway, attached):
    from runart.course import _snap_gap_endpoints
    from runart.models import CourseWaypoint
    small_graph.add_edge(1, 4, highway=highway, length=5)
    stroke = [CourseWaypoint(lat=small_graph.nodes[n]["lat"], lon=127) for n in (4, 3)]
    result = _snap_gap_endpoints(small_graph, [1, 2, 3], 0, 2, [stroke])
    assert (result[0][0].lat == small_graph.nodes[1]["lat"]) is attached


def test_separate_streets_do_not_merge_just_because_they_are_close(small_graph):
    from runart.course import _join_connected_strokes
    from runart.models import CourseWaypoint
    point = lambda n: CourseWaypoint(lat=small_graph.nodes[n]["lat"], lon=127)
    strokes = [[point(2), point(1)], [point(4), point(3)]]
    assert _join_connected_strokes(small_graph, strokes) == strokes


def test_endpoint_outside_interaction_radius_is_not_moved(small_graph):
    from runart.course import _snap_gap_endpoints
    from runart.models import CourseWaypoint
    stroke = [CourseWaypoint(lat=37.56 - 13 / 111320, lon=127),
              CourseWaypoint(lat=small_graph.nodes[3]["lat"], lon=127)]
    assert _snap_gap_endpoints(small_graph, [1, 2, 3], 0, 2, [stroke])[0][0] == stroke[0]


def test_long_detour_edge_cannot_bridge_a_nearby_gap(small_graph):
    from runart.course import _snap_gap_endpoints
    from runart.models import CourseWaypoint
    small_graph.add_edge(1, 4, highway="footway", length=200)
    stroke = [CourseWaypoint(lat=small_graph.nodes[n]["lat"], lon=127) for n in (4, 3)]
    assert _snap_gap_endpoints(small_graph, [1, 2, 3], 0, 2, [stroke])[0][0] == stroke[0]


def test_ambiguous_three_way_contact_is_not_arbitrarily_merged(small_graph):
    from runart.course import _join_connected_strokes
    from runart.models import CourseWaypoint
    point = lambda n: CourseWaypoint(lat=small_graph.nodes[n]["lat"], lon=127)
    strokes = [[point(n), point(1)] for n in (2, 3, 4)]
    assert _join_connected_strokes(small_graph, strokes) == strokes


def test_mixed_direction_unordered_strokes_join(source):
    response, data = _snap(source, [_stroke(source, 20, 30),
        list(reversed(_stroke(source, 30, 40))), _stroke(source, 10, 20)])
    assert response.status_code == 200, data


def test_normalization_does_not_mutate_the_callers_draft(source):
    from copy import deepcopy
    strokes = [_stroke(source, 10, 25, offset=.5), _stroke(source, 25, 40, offset=.5)]
    original = deepcopy(strokes)
    response, data = _snap(source, strokes)
    assert response.status_code == 200, data
    assert strokes == original


def test_pen_lift_near_one_junction_becomes_one_continuous_stroke(small_graph):
    from runart.course import _join_connected_strokes
    from runart.models import CourseWaypoint
    point = lambda n: CourseWaypoint(lat=small_graph.nodes[n]["lat"], lon=127)
    strokes = [[point(2), CourseWaypoint(lat=37.56 - .5 / 111320, lon=127)],
               [CourseWaypoint(lat=37.56 + .5 / 111320, lon=127), point(3)]]
    joined = _join_connected_strokes(small_graph, strokes)
    assert joined == [[point(2), point(1), point(3)]]


def test_exact_pen_lift_in_middle_of_edge_does_not_require_a_junction(small_graph):
    from runart.course import _join_connected_strokes
    from runart.models import CourseWaypoint
    point = lambda n: CourseWaypoint(lat=small_graph.nodes[n]["lat"], lon=127)
    middle = CourseWaypoint(lat=37.56 + 50 / 111320, lon=127)
    assert _join_connected_strokes(small_graph, [[point(1), middle], [middle, point(3)]]) == [
        [point(1), middle, point(3)]]


def test_identical_coordinates_on_different_levels_do_not_join(small_graph):
    from runart.course import _join_connected_strokes
    from runart.models import CourseWaypoint
    small_graph.add_node(5, lat=small_graph.nodes[1]["lat"], lon=127, layer=1)
    point = lambda n: CourseWaypoint(lat=small_graph.nodes[n]["lat"], lon=127)
    strokes = [[point(2), point(1)], [point(5), point(3)]]
    assert _join_connected_strokes(small_graph, strokes) == strokes


def test_closed_chain_keeps_all_strokes_and_terminates(small_graph):
    from runart.course import _join_connected_strokes
    from runart.models import CourseWaypoint
    point = lambda n: CourseWaypoint(lat=small_graph.nodes[n]["lat"], lon=127)
    joined = _join_connected_strokes(small_graph,
        [[point(1), point(2)], [point(3), point(2)], [point(1), point(3)]])
    assert joined == [[point(1), point(2), point(3), point(1)]]
