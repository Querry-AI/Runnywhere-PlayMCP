"""Kakao-basemap alignment profiles for OSM route generation."""

from runart.course import _routing_weight_for
from runart.models import CourseParams
from runart.rfs import (UNNAMED_WAY_PENALTY, map_alignment_factor,
                        prefers_park_paths, weight_value)
from runart.shapes import main_road_cost


BASE_EDGE = {
    "length": 100.0,
    "highway": "footway",
    "sidewalk_score": 0.5,
    "slope_pct": 0.0,
    "lighting_score": 0.5,
    "cctv_score": 0.5,
    "park_score": 0.0,
    "crossing_score": 0.5,
}


def test_unnamed_footway_gets_a_strong_default_map_alignment_penalty():
    named = {**BASE_EDGE, "name": "보이는 산책로"}
    unnamed = dict(BASE_EDGE)

    assert UNNAMED_WAY_PENALTY >= 2.0
    assert map_alignment_factor(unnamed) == UNNAMED_WAY_PENALTY
    assert weight_value(unnamed, False, False) > (
        weight_value(named, False, False) * 2)
    assert main_road_cost(unnamed) > main_road_cost(named) * 2


def test_explicit_park_mode_restores_unnamed_park_paths():
    named = {**BASE_EDGE, "name": "보이는 산책로"}
    unnamed = dict(BASE_EDGE)

    assert prefers_park_paths(["park"])
    assert not prefers_park_paths([])
    assert map_alignment_factor(unnamed, prefer_parks=True) == 1.0
    assert weight_value(unnamed, False, False, prefer_parks=True) == (
        weight_value(named, False, False))
    assert main_road_cost(unnamed, prefer_parks=True) == main_road_cost(named)


def test_park_request_selects_the_precomputed_park_routing_profile():
    base = CourseParams(lat=37.5665, lon=126.9780, location_name="시청")
    park = base.model_copy(update={"need_facilities": ["park"]})

    assert _routing_weight_for(base) == "w_df"
    assert _routing_weight_for(park) == "w_dfp"


def _unnamed_share(course, graph):
    from runart.course import highway_class

    total = unnamed = 0.0
    for u, v in zip(course.path, course.path[1:]):
        attrs = graph.edges[u, v]
        length = float(attrs.get("length", 0.0))
        total += length
        if highway_class(attrs) in ("footway", "path") and not attrs.get("name"):
            unnamed += length
    return unnamed / total if total else 0.0


def test_a_generated_course_prefers_ways_the_basemap_actually_draws():
    """Kakao draws named streets and almost no unnamed footpaths, so a route
    over them reads as a line floating across a green polygon."""
    from runart import graph as graphmod
    from runart.course import generate_course
    from runart.models import CourseParams

    g = graphmod.get_graph()
    starts = [("시청", 37.5665, 126.9780), ("여의도", 37.5215, 126.9243),
              ("강남", 37.4979, 127.0276)]

    for name, lat, lon in starts:
        course = generate_course(CourseParams(
            lat=lat, lon=lon, location_name=name, distance_km=5.0))
        assert _unnamed_share(course, g) <= 0.15, name


def test_asking_for_a_park_run_puts_the_park_paths_back():
    """Park and riverside paths are exactly the ways Kakao does not draw, so a
    runner who asks for one has to be able to get it."""
    from runart import graph as graphmod
    from runart.course import generate_course
    from runart.models import CourseParams

    g = graphmod.get_graph()
    where = dict(lat=37.5215, lon=126.9243, location_name="여의도")

    plain = generate_course(CourseParams(**where, distance_km=5.0))
    park = generate_course(CourseParams(**where, distance_km=5.0,
                                        need_facilities=["park"]))

    assert _unnamed_share(park, g) > _unnamed_share(plain, g)
