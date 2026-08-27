import pytest

from runart.course import generate_course
from runart.models import CourseParams
from runart.naming import (RUN_NAMES_KO, course_badges, course_title,
                           green_share, short_place)
from runart.render import script_json
from runart.shapes import SHAPES

CITY_HALL = dict(lat=37.5665, lon=126.9780)


@pytest.mark.parametrize("address, expected", [
    ("강남대로 401-2", "강남대로"),
    ("강남대로401-2", "강남대로"),
    ("서울특별시 강남구 테헤란로 123", "테헤란로"),
    ("반포대로 58번지", "반포대로"),
    ("잠실역 2번출구", "잠실역"),
    ("여의도 한강공원", "한강공원"),
    ("서울시청", "서울시청"),
    ("63빌딩", "63빌딩"),          # numeric-led but nothing else to fall back to
    ("", ""),
    ("   ", ""),
])
def test_short_place_drops_lot_numbers_not_place_names(address, expected):
    assert short_place(address) == expected


def test_short_place_is_bounded():
    assert len(short_place("가" * 40)) <= 12


def test_basic_course_is_named_after_its_place():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0,
                                          location_name="강남대로 401-2"))
    assert course_title(course) == f"{course.length_km:.1f}km 강남대로런"


def test_course_without_a_place_still_has_a_name():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    assert course_title(course) == f"{course.length_km:.1f}km 러닝 코스"


@pytest.mark.parametrize("shape, run_name", sorted(RUN_NAMES_KO.items()))
def test_animal_courses_are_named_after_the_run(shape, run_name):
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0,
                                          location_name="서울시청"))
    course.params = course.params.model_copy(update={"shape": shape})
    title = course_title(course)
    assert title == f"{course.length_km:.1f}km {run_name}"
    # the run name replaces the old "<species> 모양 <km>km 러닝 코스" phrasing
    assert "모양" not in title
    assert "러닝 코스" not in title


def test_every_shape_has_a_run_name():
    assert set(RUN_NAMES_KO) == set(SHAPES)


def test_badges_lead_with_shape_then_terrain():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0,
                                          location_name="서울시청"))
    badges = course_badges(course)
    assert 1 <= len(badges) <= 3
    assert badges[0]["emoji"] == "🏟️"          # basic course -> track
    assert badges[1]["emoji"] in ("🌳", "🏙️")
    assert all(b["label"] for b in badges)      # never emoji-only

    course.params = course.params.model_copy(update={"shape": "dog"})
    assert course_badges(course)[0]["emoji"] == SHAPES["dog"].emoji


def test_night_badge_distinguishes_ordinary_and_good_lighting():
    day = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    assert len(course_badges(day)) == 2
    day.params.night_mode = True
    day.rfs["components"]["lighting"] = .3
    assert len(course_badges(day)) == 2
    day.rfs["components"]["lighting"] = .4
    assert course_badges(day)[2]["label"] == "야간 조명 보통"
    day.rfs["components"]["lighting"] = .9
    night_badges = course_badges(day)
    assert len(night_badges) == 3
    assert night_badges[2]["emoji"] == "💡"
    assert night_badges[2]["label"] == "야간 조명 양호"


def test_green_share_separates_riverside_from_city_courses():
    city = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    river = generate_course(CourseParams(lat=37.5285, lon=126.9325, distance_km=5.0))
    assert green_share(city) < green_share(river)
    assert course_badges(city)[1]["emoji"] == "🏙️"
    assert course_badges(river)[1]["emoji"] == "🌳"


def test_script_json_cannot_close_the_script_element():
    """A facility or place name containing markup must not end the <script>."""
    payload = {"name": "</script><img src=x onerror=alert(1)>", "note": "<!--"}
    out = script_json(payload)
    assert "</script>" not in out
    assert "<" not in out
    assert "\\u003c" in out
    import json
    assert json.loads(out) == payload      # data survives the escaping intact


def test_script_json_keeps_korean_readable():
    assert "화장실" in script_json({"t": "화장실"})
