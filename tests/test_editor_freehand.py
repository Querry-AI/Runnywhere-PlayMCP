"""The drawn line is the instruction.

The editor's promise is narrow and literal: what the runner draws is where the
course goes, corrected onto the pedestrian network beside it. These tests pin
that promise with measured numbers, because the failure it replaces was silent
-- a deliberate 180m detour changed the course by 11m and reported success.
"""
import asyncio
import json
import math

import pytest

from runart import course as coursemod
from runart import graph as graphmod
from runart import server
from runart.course import (CourseError, course_from_path, edge_is_runnable,
                           generate_course, snap_drawn_strokes)
from runart.geo import haversine_m
from runart.models import CourseParams, CourseWaypoint, encode_course_id

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="서울시청")
M_PER_DEG_LAT = 111320.0


def _m_per_deg_lon(lat: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


@pytest.fixture(scope="module")
def source():
    return generate_course(CourseParams(**CITY_HALL, distance_km=5.0))


def _latlon(node: int) -> tuple[float, float]:
    data = graphmod.get_graph().nodes[node]
    return data["lat"], data["lon"]


def _walked_m(path: list[int]) -> float:
    g = graphmod.get_graph()
    return sum(g.edges[u, v]["length"] for u, v in zip(path, path[1:]))


def _bow(start: tuple[float, float], end: tuple[float, float], depth_m: float,
         samples: int = 60) -> list[tuple[float, float]]:
    """A smooth curve from start to end, bulging ``depth_m`` to one side.

    A quadratic Bezier reaches half its control offset, so the control point
    goes out twice as far as the depth being asked for.
    """
    dlat = (end[0] - start[0]) * M_PER_DEG_LAT
    dlon = (end[1] - start[1]) * _m_per_deg_lon(start[0])
    span = math.hypot(dlat, dlon)
    normal = (-dlon / span, dlat / span)
    mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    control = (mid[0] + normal[0] * 2 * depth_m / M_PER_DEG_LAT,
               mid[1] + normal[1] * 2 * depth_m / _m_per_deg_lon(start[0]))
    curve = []
    for step in range(samples + 1):
        t = step / samples
        curve.append((
            (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control[0] + t ** 2 * end[0],
            (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control[1] + t ** 2 * end[1],
        ))
    return curve


def _stroke(points: list[tuple[float, float]]) -> list[CourseWaypoint]:
    return [CourseWaypoint(lat=lat, lon=lon) for lat, lon in points]


def _deviation_m(node: int, drawn: list[tuple[float, float]]) -> float:
    """How far a node sits from the line the runner actually drew."""
    point = _latlon(node)
    return min(coursemod._perpendicular_m(point, a, b)
               for a, b in zip(drawn, drawn[1:]))


def _course_with_spur(source):
    """The honest 삐죽 튀어나온 왕복: an out-and-back onto a real graph leaf.

    The only way off a leaf is the way you came, so erasing it can only be
    answered by deleting it -- there is no alternative route to find.
    """
    g = graphmod.get_graph()
    for index in range(5, len(source.path) - 5):
        node = source.path[index]
        for neighbour in g[node]:
            if g.degree(neighbour) == 1 and edge_is_runnable(g.edges[node, neighbour]):
                spurred = source.path[:index + 1] + [neighbour, node] + source.path[index + 1:]
                return course_from_path(source.params, spurred), index, neighbour
    raise AssertionError("no dead-end spur available in the bundled graph")


# ---------------------------------------------------------------------------
# The drawing is followed, not averaged away
# ---------------------------------------------------------------------------

def test_a_drawn_detour_actually_appears_in_the_route(source):
    """Measured before this suite existed: a 180m bow moved the course 11m.

    RDP simplification ran against the whole stroke's chord, so a smooth curve
    collapsed onto that chord no matter how deep it was, and the router was
    handed one waypoint for a kilometre of drawing.
    """
    start, end = _latlon(source.path[10]), _latlon(source.path[40])
    drawn = _bow(start, end, 180.0)

    edited = snap_drawn_strokes(source.params, source.path, 10, 40, [_stroke(drawn)])

    reach = max(coursemod._perpendicular_m(_latlon(node), start, end)
                for node in edited.path
                if _deviation_m(node, drawn) < 400.0)
    assert reach >= 120.0, f"drew a 180m detour, route only reached {reach:.0f}m"

    # Not a length comparison: the green being replaced already curved, so a
    # detour can be deep and barely longer. What has to change is *where* the
    # route goes, so measure the distance from the line it used to take.
    original = [_latlon(node) for node in source.path]
    moved = max(min(coursemod._perpendicular_m(_latlon(node), a, b)
                    for a, b in zip(original, original[1:]))
                for node in edited.path)
    assert moved >= 100.0, f"route never left the original by more than {moved:.0f}m"


def test_the_route_stays_on_the_line_the_runner_drew(source):
    """Fidelity is the average, not just the extreme: the replaced span has to
    lie along the drawing, not merely touch its far point once."""
    start, end = _latlon(source.path[10]), _latlon(source.path[40])
    drawn = _bow(start, end, 180.0)

    edited = snap_drawn_strokes(source.params, source.path, 10, 40, [_stroke(drawn)])

    replaced = [node for node in edited.path if node not in set(source.path)]
    assert len(replaced) >= 5, f"a kilometre of drawing produced {len(replaced)} new nodes"
    mean = sum(_deviation_m(node, drawn) for node in replaced) / len(replaced)
    assert mean <= 40.0, f"replaced span sits {mean:.0f}m off the drawn line"


def test_the_smoothing_window_stays_in_its_measured_range():
    """The one calibration knob, and the band it was measured into.

    Below 140m tremor steers the route (at 100m the tremor battery came back
    5.87x direct, worse than the pipeline this replaced). Above 200m intent
    goes instead: at 300m a drawn 180m detour was reproduced as 135m.
    """
    assert 140.0 <= coursemod.STROKE_SMOOTH_WINDOW_M <= 200.0


# ---------------------------------------------------------------------------
# Erasing erases
# ---------------------------------------------------------------------------

def test_erasing_an_out_and_back_closes_on_its_own(source):
    """The commonest reason to reach for the eraser. Both ends of the erased
    span are the same node, so removing it leaves a closed loop and there is
    nothing to draw -- yet this was refused, telling the runner to draw a line
    that could not exist."""
    spurred, index, tip = _course_with_spur(source)

    edited = snap_drawn_strokes(spurred.params, spurred.path, index, index + 2, [])

    assert tip not in edited.path
    assert edited.path == source.path
    assert "왕복" in edited.note


def test_erasing_without_a_gap_is_still_refused(source):
    """Nothing erased and nothing drawn is not an edit."""
    with pytest.raises(CourseError):
        snap_drawn_strokes(source.params, source.path, None, None, [])


# ---------------------------------------------------------------------------
# Green + blue, and nothing else
# ---------------------------------------------------------------------------

def test_a_one_sided_drawing_joins_through_the_nearest_walkway(source):
    """A runner who erases a spur and draws back in from one side only has
    said everything needed. The far end is joined by the short walk to the
    nearest retained point instead of refusing the whole edit."""
    start = _latlon(source.path[10])            # the erased end, still green
    end = _latlon(source.path[40])
    drawn = _bow(start, end, 150.0)[:44]        # the far tip stops in open ground

    edited = snap_drawn_strokes(source.params, source.path, 10, 40, [_stroke(drawn)])

    assert edited.length_m >= source.length_m * 0.5, "the loop collapsed"
    assert edited.path[0] == edited.path[-1]


def test_drawing_without_erasing_replaces_the_green_between_contacts(source):
    """Drawing is not an annotation. A line that meets the course twice says
    'go this way instead', and the green between those two contacts goes."""
    start, end = _latlon(source.path[10]), _latlon(source.path[40])
    drawn = _bow(start, end, 180.0)

    edited = snap_drawn_strokes(source.params, source.path, None, None, [_stroke(drawn)])

    replaced_away = set(source.path[15:35]) - set(edited.path)
    assert replaced_away, "the green between the contacts survived untouched"
    assert edited.path[0] == edited.path[-1]


def test_a_join_may_not_eat_more_green_than_was_drawn(source):
    """The guard that keeps a one-sided line from reaching across a closed
    loop and returning a 1.25km course in place of a 5km one.

    A short line drawn from one erased end towards the far side of the loop
    tempts the join to splice straight across it. Refusing is a fine answer;
    silently shipping a quarter of the course is not.
    """
    far = len(source.path) * 3 // 4
    drawn = _bow(_latlon(source.path[10]), _latlon(source.path[far]), 20.0)[:12]

    try:
        edited = snap_drawn_strokes(source.params, source.path, 10, 40, [_stroke(drawn)])
    except CourseError:
        return
    assert edited.length_m >= source.length_m * 0.5, (
        f"the loop collapsed to {edited.length_m:.0f}m of {source.length_m:.0f}m")


# ---------------------------------------------------------------------------
# What the drawing must never do
# ---------------------------------------------------------------------------

def test_the_endpoint_closes_an_erased_out_and_back(source):
    """The whole journey through the real endpoint: sweep, erase, done."""
    from test_course_edit import _json_request

    spurred, index, tip = _course_with_spur(source)
    cid = encode_course_id(spurred.params)

    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "snap", "path": spurred.path,
        "from_index": index, "to_index": index + 2, "strokes": [],
    })))

    payload = json.loads(response.body)
    assert response.status_code == 200, payload
    assert not payload.get("gap_open")
    assert tip not in [point[0] for point in payload["path"]]
    assert "왕복" in payload["note"]


def test_a_gap_that_needs_a_line_is_reported_open_not_failed(source):
    """Erasing a span with a real alternative cannot close by itself. That is
    a request for the other half, not a failed edit: answering with an error
    threw the erase away and made rubbing something out look broken."""
    from test_course_edit import _json_request

    cid = encode_course_id(source.params)

    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "snap", "path": source.path,
        "from_index": 10, "to_index": 40, "strokes": [],
    })))

    payload = json.loads(response.body)
    assert response.status_code == 200, payload
    assert payload["gap_open"] is True
    assert [point[0] for point in payload["path"]] == source.path
    assert "그려" in payload["note"]


def test_a_drawing_off_the_pedestrian_network_is_named_not_silently_moved(source):
    """Samples with no way within reach used to be dropped without a word, so
    a line drawn across a river came back as a road route with no explanation.
    """
    drawn = [(37.5665 + i * 0.0004, 126.8100) for i in range(20)]
    with pytest.raises(CourseError):
        snap_drawn_strokes(source.params, source.path, 10, 40, [_stroke(drawn)])
