"""Tool-level tests: call the underlying functions the MCP tools wrap."""

from runart import server
from runart.geocode import _address_query_variants
from runart.models import CourseParams, encode_course_id

CITY_HALL = dict(location="시청")


def test_generate_running_course_defaults_to_5km_with_note():
    out = server.generate_running_course(**CITY_HALL)
    assert "기본 5km" in out
    assert "러닝 친화도" in out
    assert "/c/" in out and ".gpx" in out


def test_duration_conversion_is_explained():
    out = server.generate_running_course(**CITY_HALL, duration_min=30)
    assert "6:30/km" in out and "4.6km" in out


def test_requested_facility_is_reflected_in_course():
    out = server.generate_running_course(**CITY_HALL, distance_km=5.0,
                                         need_facilities=["restroom"])
    assert "경유:" in out
    assert "화장실" in out


def test_short_address_variants_expand_to_full_seoul_address():
    variants = _address_query_variants("테헤란로 8길 8")
    assert "테헤란로8길 8" in variants
    assert "강남구 테헤란로8길 8" in variants
    assert "서울특별시 강남구 테헤란로8길 8" in variants


def test_animal_request_surveys_verified_minimum_distances_first():
    out = server.generate_animal_course(**CITY_HALL)
    assert out.index("강아지") < out.index("고양이") < out.index("고래") < out.index("토끼")
    assert "11km 이내" in out
    assert "추천" in out or "3초 안에" in out
    assert "어떤 동물" in out or "다시 요청" in out
    assert "모양 완성도" not in out


def test_chosen_animal_without_distance_uses_verified_minimum():
    out = server.generate_animal_course(shape="whale", **CITY_HALL)
    assert "고래" in out
    assert "11km 이내 최상 코스" in out or "3초 안에" in out
    if "최상 코스" in out:
        assert "/s/whale-" in out


def test_animal_timeout_returns_actionable_guidance(monkeypatch):
    def timeout(*args, **kwargs):
        raise server._GenerationTimeout

    monkeypatch.setattr(server, "_offload", timeout)
    out = server.generate_animal_course(shape="whale", **CITY_HALL)
    assert out.startswith("⏱️")
    assert "3초 안에" in out
    assert "불가능" not in out
    assert "한 번 더 시도" in out
    assert "동물 코스 추천" in out


def test_forced_short_animal_returns_choice_survey_not_blob():
    out = server.generate_animal_course(shape="dog", **CITY_HALL, distance_km=3.0)
    assert out.startswith(("⚠️", "⏱️"))
    assert "강아지" in out
    if out.startswith("⚠️"):
        assert "11km 이내" in out
        assert "고양이" in out and "고래" in out and "토끼" in out


def test_shape_token_recreates_shape():
    out = server.generate_animal_course(shape_token="whale-5k", **CITY_HALL)
    assert "고래" in out or "최단" in out


def test_refine_and_status_roundtrip():
    cid = encode_course_id(CourseParams(lat=37.5665, lon=126.9780,
                                        location_name="시청", distance_km=5.0))
    refined = server.refine_course(course_id=cid, distance_km=3.0)
    import re
    got_km = float(re.search(r"([\d.]+)km 러닝 코스", refined).group(1))
    assert abs(got_km - 3.0) / 3.0 <= 0.10  # demo 80m grid: ±10%; real graph targets ±5%
    status = server.get_course_status(course_id=cid)
    assert "러닝 친화도" in status


def test_bad_course_id_gives_guidance_not_traceback():
    out = server.find_facilities_near_course(course_id="not-a-real-id")
    assert out.startswith("⚠️")


def test_no_kakao_in_tool_names():
    import asyncio
    tools = asyncio.run(server.mcp.list_tools())
    assert 3 <= len(tools) <= 10
    for t in tools:
        assert "kakao" not in t.name.lower()
        assert t.annotations.readOnlyHint is True
        assert t.annotations.destructiveHint is False
        assert len(t.description) <= 1024
        assert "RunArt" in t.description
