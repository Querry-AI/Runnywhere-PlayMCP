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
