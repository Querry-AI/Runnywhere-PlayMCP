import pytest

from runart.course import CourseError, generate_course
from runart.models import CourseParams, decode_course_id, encode_course_id
from runart.render import preview_html

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="시청")


def test_loop_returns_to_start_within_tolerance():
    params = CourseParams(**CITY_HALL, distance_km=5.0)
    course = generate_course(params)
    assert course.points[0] == course.points[-1]
    assert abs(course.length_km - 5.0) / 5.0 <= 0.10
    assert 0 <= course.rfs["score"] <= 100


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
                        kakao_javascript_key="javascript-key")
    assert "dapi.kakao.com/v2/maps/sdk.js?appkey=javascript-key" in page
    assert "new kakao.maps.Map" in page
    assert "Leaflet" not in page and "leaflet" not in page
    assert "basemaps.cartocdn.com" not in page
    assert "© OpenStreetMap contributors" in page
    assert "PretendardVariable.woff2" in page
    assert "mobile-dock" in page
    assert "prefers-reduced-motion" in page
    assert "동물 실루엣" not in page  # plain courses use the neutral label
    assert "코스 라인" in page


def test_preview_explains_missing_kakao_javascript_key():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert "dapi.kakao.com/v2/maps/sdk.js" not in page
    assert "KAKAO_JAVASCRIPT_KEY와 등록 도메인을 확인" in page
