import pytest

from runart.course import CourseError, generate_course
from runart.models import CourseParams, decode_course_id, encode_course_id

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
