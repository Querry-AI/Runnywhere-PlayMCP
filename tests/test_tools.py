"""Tool-level tests: call the underlying functions the MCP tools wrap."""

from runart import server
from runart.course import Course
from runart.geocode import (
    _STATION_LOOKUP,
    _address_query_variants,
    resolve_location,
)
from runart.stations import SEOUL_METRO_STATIONS
from runart.models import CourseParams, encode_course_id
from runart.shapes import SHAPES, SHAPE_STYLES, find_min_clean_course

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


def test_bare_neighborhood_address_gets_district_context():
    variants = _address_query_variants("신설동 76-5")
    assert "동대문구 신설동 76-5" in variants
    assert "서울특별시 동대문구 신설동 76-5" in variants


def test_seoul_address_prefix_is_normalized():
    variants = _address_query_variants("서울 동대문구 신설동 76-5")
    assert "서울특별시 동대문구 신설동 76-5" in variants


def test_sinseoldong_station_resolves_without_external_api():
    lat, lon, name = resolve_location("신설동역", None, None)
    assert name == "신설동역"
    assert (lat, lon) == (37.5753, 127.0251)


def test_all_289_seoul_metro_rows_are_bundled():
    assert len(SEOUL_METRO_STATIONS) == 289
    assert len(_STATION_LOOKUP) > 289


def test_station_name_and_line_alias_resolve_without_external_api(monkeypatch):
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    plain = resolve_location("구파발역", None, None)
    qualified = resolve_location("3호선 구파발역", None, None)
    assert plain == qualified
    assert plain[2] == "구파발역"


def test_station_road_and_lot_addresses_resolve_without_external_api(monkeypatch):
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    by_road = resolve_location("서울 동대문구 왕산로 지하1(신설동)", None, None)
    by_lot = resolve_location("서울특별시 동대문구 신설동 76-5 신설동역(1호선)", None, None)
    assert by_road == by_lot
    assert by_road[2] == "신설동역"


def test_recently_added_station_resolves_offline(monkeypatch):
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    lat, lon, name = resolve_location("암사역사공원역", None, None)
    assert name == "암사역사공원역"
    assert (lat, lon) == (37.556667, 127.135556)


def test_location_error_does_not_ask_for_coordinates():
    out = server.generate_running_course(location="아무데나요")
    assert "위치를 찾지 못했어요" in out
    assert "좌표" not in out


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


def test_survey_result_is_reused_when_user_selects_animal(monkeypatch):
    server._animal_recommendation_cache.clear()
    lat, lon, name = resolve_location("시청", None, None)
    course = find_min_clean_course(CourseParams(
        lat=lat, lon=lon, location_name=name,
        distance_km=SHAPES["whale"].min_km, shape="whale"),
        per_try_s=1.5, total_budget_s=8.0)
    assert course is not None
    server._cache_animal_recommendation(course)

    def should_not_run(*args, **kwargs):
        raise AssertionError("cached recommendation should avoid regeneration")

    monkeypatch.setattr(server, "_offload", should_not_run)
    out = server.generate_animal_course(shape="whale", **CITY_HALL)
    assert "11km 이내 최상 코스" in out


def test_repeated_animal_survey_only_submits_missing_shapes(monkeypatch):
    server._animal_recommendation_cache.clear()
    lat, lon, name = resolve_location("시청", None, None)
    for key in server.SURVEY_SHAPES:
        params = CourseParams(lat=lat, lon=lon, location_name=name,
                              distance_km=SHAPES[key].min_km, shape=key)
        server._cache_animal_recommendation(Course(
            params=params, path=[], points=[], length_m=params.distance_km * 1000,
            ascent_m=0.0, rfs={"score": 50, "highlights": []},
            shape_similarity=1.0))

    def should_not_submit(*args, **kwargs):
        raise AssertionError("a fully cached survey must not submit CPU work")

    monkeypatch.setattr(server, "_offload_map", should_not_submit)
    out = server._animal_survey(lat, lon, name, timeout_s=2.7)
    assert all(SHAPES[key].name_ko in out for key in server.SURVEY_SHAPES)


def test_animal_timeout_returns_actionable_guidance(monkeypatch):
    server._animal_recommendation_cache.clear()

    def timeout(*args, **kwargs):
        raise server._GenerationTimeout

    monkeypatch.setattr(server, "_offload", timeout)
    out = server.generate_animal_course(shape="whale", **CITY_HALL)
    # A verified nearby preset now wins before CPU generation, so a simulated
    # timeout is visible only when no preset fallback exists.
    assert out.startswith(("⏱️", "✅"))
    if out.startswith("⏱️"):
        assert "3초 안에" in out and "한 번 더 시도" in out
    else:
        assert "11km 이내 최상 코스" in out and "/c/" in out


def test_bounded_animal_generation_falls_back_when_pool_is_unavailable(monkeypatch):
    monkeypatch.setattr(server, "_get_pool", lambda: None)
    lat, lon, name = resolve_location("강남역", None, None)
    params = CourseParams(lat=lat, lon=lon, location_name=name,
                          distance_km=SHAPES["whale"].min_km, shape="whale")
    course = server._offload(
        find_min_clean_course, params, timeout_s=server.ANIMAL_RESPONSE_BUDGET_S)
    assert course is not None
    assert course.shape_similarity >= SHAPE_STYLES["whale"].similarity_gate


def test_forced_short_animal_returns_choice_survey_not_blob():
    out = server.generate_animal_course(shape="dog", **CITY_HALL, distance_km=3.0)
    assert out.startswith(("⚠️", "⏱️"))
    assert "강아지" in out
    if out.startswith("⚠️"):
        assert "추천 거리" in out
        assert "바로 사용하려면" in out


def test_shape_token_recreates_shape():
    out = server.generate_animal_course(shape_token="whale-5k", **CITY_HALL)
    assert "고래" in out or "최단" in out


def test_shape_share_page_rejects_html_injection_token():
    import asyncio
    from starlette.requests import Request
    request = Request({"type": "http", "method": "GET", "path": "/s/x",
                       "headers": [], "path_params": {
                           "token": "<img-onerror=alert(1)>-5k"}})
    response = asyncio.run(server.share_shape(request))
    assert response.status_code == 404
    assert b"<img" not in response.body


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
        assert "Runnywhere" in t.description


def test_mcp_tools_match_playmcp_required_annotations():
    import asyncio
    tools = asyncio.run(server.mcp.list_tools())
    names = [tool.name for tool in tools]
    assert set(names) == {
        "generate_running_course", "generate_animal_course",
        "list_available_shapes", "find_facilities_near_course",
        "refine_course", "get_course_status",
        "explore_animal_collection", "record_animal_completion",
        "extend_shape_relay",
    }
    assert len(names) == len(set(names))
    assert 3 <= len(names) <= 10
    for tool in tools:
        assert 1 <= len(tool.name) <= 128
        assert all(c.isascii() and (c.isalnum() or c in "_-") for c in tool.name)
        assert tool.inputSchema
        assert tool.annotations.title
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.idempotentHint is True


def test_http_middleware_adds_security_headers():
    import asyncio

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    middleware = server._TokenBucketMiddleware(inner)
    asyncio.run(middleware({"type": "http", "client": ("127.0.0.1", 1)},
                           receive, send))
    headers = dict(messages[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert b"frame-ancestors 'none'" in headers[b"content-security-policy"]
