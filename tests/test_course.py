import pytest

from runart.course import (Course, CourseError, course_route_issues,
                           edge_is_runnable, generate_course,
                           rebase_closed_course_start)
from runart.models import CourseParams, decode_course_id, encode_course_id
from runart.render import course_thumbnail_svg, preview_html

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="시청")


def test_loop_returns_to_start_within_tolerance():
    params = CourseParams(**CITY_HALL, distance_km=5.0)
    course = generate_course(params)
    assert course.points[0] == course.points[-1]
    assert abs(course.length_km - 5.0) / 5.0 <= 0.10
    assert 0 <= course.rfs["score"] <= 100


def test_closed_course_start_is_rebased_to_nearest_requested_point():
    params = CourseParams(**CITY_HALL, distance_km=5.0, shape="rabbit")
    original = Course(
        params=params,
        path=[10, 20, 30, 10],
        points=[
            (37.5750, 126.9900),
            (37.5666, 126.9781),
            (37.5600, 126.9700),
            (37.5750, 126.9900),
        ],
        length_m=5000,
        ascent_m=20,
        rfs={"score": 80},
    )

    rebased = rebase_closed_course_start(original)

    assert rebased.path == [20, 30, 10, 20]
    assert rebased.points[0] == rebased.points[-1] == (37.5666, 126.9781)
    assert rebased.length_m == original.length_m
    assert original.path == [10, 20, 30, 10]


def test_course_thumbnail_places_route_over_the_real_osm_street_network():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))

    svg = course_thumbnail_svg(course)

    assert 'viewBox="0 0 320 320"' in svg
    assert "<polyline" in svg and "<circle" in svg
    assert '<clipPath id="map-clip">' in svg
    assert svg.count("<path") >= 4
    assert "M0 80H320" not in svg  # decorative grid was not a real map
    assert 'stroke="#fff" stroke-width="2.2"' in svg
    assert "<text" not in svg
    assert "시청" not in svg and "5.0km" not in svg


@pytest.mark.parametrize("highway", [
    "trunk", "trunk_link", "primary_link", "secondary_link", "steps",
    "track", "bridleway", "busway", "corridor",
])
def test_non_runnable_highway_classes_are_blocked(highway):
    assert not edge_is_runnable({"highway": highway, "slope_pct": 0})


def test_steep_mountain_path_and_far_station_start_are_rejected():
    import networkx as nx

    params = CourseParams(**CITY_HALL, distance_km=5.0, shape="rabbit")
    course = Course(
        params=params,
        path=[1, 2, 1],
        points=[
            (37.5700, 126.9820),
            (37.5710, 126.9830),
            (37.5700, 126.9820),
        ],
        length_m=5000,
        ascent_m=20,
        rfs={"score": 70},
    )
    graph = nx.Graph()
    graph.add_edge(1, 2, length=2500, highway="path", slope_pct=14)

    issues = course_route_issues(course, graph)

    assert "start_too_far" in issues
    assert "blocked_highway:path" in issues


def test_flat_course_avoids_hills():
    # 여의도한강공원 — genuinely flat riverside; downtown always rolls a bit.
    params = CourseParams(lat=37.5285, lon=126.9328, location_name="여의도한강공원",
                          distance_km=4.0, include_hills=False)
    course = generate_course(params)
    assert course.is_flat


def test_night_mode_scores_lighting():
    day = generate_course(CourseParams(**CITY_HALL, distance_km=4.0))
    night = generate_course(CourseParams(**CITY_HALL, distance_km=4.0, night_mode=True))
    assert night.rfs["score"] >= day.rfs["score"] - 15  # night routing shouldn't crater quality


def test_out_of_area_raises_guidance():
    with pytest.raises(CourseError):
        generate_course(CourseParams(lat=37.41, lon=127.10, distance_km=5.0))


def test_course_id_roundtrip():
    params = CourseParams(**CITY_HALL, distance_km=5.0, night_mode=True,
                          need_facilities=["restroom", "water"])
    cid = encode_course_id(params)
    restored = decode_course_id(cid)
    assert restored.canonical() == params.canonical()
    assert encode_course_id(restored) == cid  # deterministic / stateless


def test_course_id_rejects_oversized_input():
    with pytest.raises(ValueError, match="too large"):
        decode_course_id("A" * 4097)


def test_preview_uses_kakao_maps_without_leaflet():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example",
                        kakao_javascript_key="javascript-key", page="run")
    assert "dapi.kakao.com/v2/maps/sdk.js?appkey=javascript-key" in page
    assert "new kakao.maps.Map" in page
    assert "Leaflet" not in page and "leaflet" not in page
    assert "basemaps.cartocdn.com" not in page
    assert "© OpenStreetMap contributors" in page
    assert "PretendardVariable.woff2" in page
    assert 'class="run-locate"' in page
    assert 'aria-label="내 위치 추적 시작"' in page
    assert "prefers-reduced-motion" in page
    assert "동물 실루엣" not in page  # plain courses use the neutral label
    assert "코스 라인" in page


def test_preview_explains_missing_kakao_javascript_key():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert "dapi.kakao.com/v2/maps/sdk.js" not in page
    assert "지도를 불러오지 못했어요" in page
    assert "KAKAO_JAVASCRIPT_KEY" not in page
