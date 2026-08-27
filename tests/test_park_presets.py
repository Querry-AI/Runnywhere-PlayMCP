"""The fixed catalogue must carry real, restorable paths at five places."""
from itertools import combinations

import pytest

from runart import graph as graphmod, park_presets
from runart.course import CourseError, course_route_issues, generate_course
from runart.geo import haversine_m
from runart.models import decode_course_id, encode_course_id
from runart.naming import green_share
from runart.widget import build_course_widget
from runart.courseplan import CourseChoice


def test_all_five_registered_paths_are_park_routes_and_restore_without_cache():
    courses = park_presets.park_courses()
    assert len(courses) == 5
    assert len({spot.id for spot, _ in courses}) == 5
    for spot, course in courses:
        assert not course_route_issues(course, graphmod.get_graph())
        assert green_share(course) >= .9
        assert haversine_m(spot.lat, spot.lon, *course.points[0]) < 500
        restored = generate_course(decode_course_id(encode_course_id(course.params)))
        assert restored.path == course.path
        assert restored.length_m == course.length_m
        assert restored.params.location_name == spot.name


def test_every_three_place_combination_fits_the_widget_limit():
    for combo in combinations(park_presets.park_courses(), 3):
        courses = [course for _, course in combo]
        choices = [CourseChoice(c, encode_course_id(c.params), "standard") for c in courses]
        payload = build_course_widget(courses[0], choices[0].course_id,
            "https://runnywhere-kakaotools.playmcp-endpoint.kakaocloud.io",
            alternatives=choices[1:], intro_text="등록된 공원·강변 5곳 중 3곳을 추천해요.")
        assert '"widget"' in payload


def test_a_changed_graph_fingerprint_cannot_serve_stale_presets(monkeypatch):
    monkeypatch.setattr(park_presets, "graph_fingerprint", lambda: "changed")
    park_presets._courses_for_graph.cache_clear()
    try:
        with pytest.raises(CourseError, match="보행 지도"):
            park_presets.park_courses()
    finally:
        park_presets._courses_for_graph.cache_clear()
