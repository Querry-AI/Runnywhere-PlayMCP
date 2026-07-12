"""Tool-level tests: call the underlying functions the MCP tools wrap."""

import time

from runart import geocode, server
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


def test_external_location_search_shares_one_wall_clock_deadline(monkeypatch):
    monkeypatch.setenv("KAKAO_REST_API_KEY", "test-key")
    seen = []

    def slow_miss(location, deadline):
        seen.append(deadline)
        time.sleep(0.025)
        return None

    monkeypatch.setattr(geocode, "_run_keyword_search", slow_miss)
    monkeypatch.setattr(geocode, "_run_address_search", slow_miss)
    started = time.monotonic()
    try:
        geocode.resolve_location("새로운 임의 장소", None, None, timeout_s=0.015)
    except Exception:
        pass
    assert time.monotonic() - started < 0.08
    assert len(seen) <= 1


def test_offloaded_tool_has_outer_three_second_response_cap(monkeypatch):
    import asyncio

    monkeypatch.setattr(server, "MCP_OUTER_RESPONSE_BUDGET_S", 0.02)

    @server.offloaded
    def slow_tool():
        time.sleep(0.08)
        return "too late"

    started = time.monotonic()
    out = asyncio.run(slow_tool())
    assert time.monotonic() - started < 0.07
    assert out.startswith("⏱️") and "3초" in out


def test_animal_request_surveys_verified_minimum_distances_first():
    out = server.generate_animal_course(**CITY_HALL)
    # First try already renders the best-fitting shape as a full course.
    assert "요청 조건에 가장 잘 맞는" in out
    assert "## " in out and "/c/" in out
    # The choice list after the featured course keeps the survey order.
    survey = out[out.index("지금 선택할 수 있는"):]
    assert (survey.index("강아지") < survey.index("고양이")
            < survey.index("고래") < survey.index("토끼"))
    assert "11km 이내" in out
    assert "추천" in out or "3초 안에" in out
    assert "다른 동물" in out or "다시 요청" in out
    assert "모양 완성도" not in out
    # No fixed promotional footer (PlayMCP ad-steering policy).
    assert "서울 동물 지도 보기" not in out


def test_chosen_animal_without_distance_uses_verified_minimum():
    out = server.generate_animal_course(shape="whale", **CITY_HALL)
    assert "고래" in out
    assert "11km 이내 최상 코스" in out or "3초 안에" in out
    if "최상 코스" in out:
        assert "/c/" in out


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
    assert out.startswith(("⏱️", "✅", "🔎"))
    if out.startswith("⏱️"):
        assert "3초 안에" in out and "한 번 더 시도" in out
    elif out.startswith("✅"):
        assert "11km 이내 최상 코스" in out and "/c/" in out
    else:
        assert "이동해도 될까요?" in out and "/c/" in out


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


def test_address_start_tries_exact_point_before_station_preset(monkeypatch):
    """At an arbitrary address the animal is judged from that exact start
    first; the nearest station preset is only substituted after the
    generation budget fails."""
    server._animal_recommendation_cache.clear()

    calls = 0

    def timeout(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise server._GenerationTimeout

    monkeypatch.setattr(server, "_offload", timeout)
    # Non-station coordinates near Gangnam: generation times out, so the
    # nearest verified station preset must be served with a moved-start note.
    out = server.generate_animal_course(shape="dog", lat=37.5041, lon=127.0293)
    assert "검증 코스" in out
    assert "/c/" in out
    repeated = server.generate_animal_course(shape="dog", lat=37.5041, lon=127.0293)
    assert "검증 코스" in repeated and "이동" in repeated
    assert calls == 1


def test_no_animal_course_offers_relocation_not_general_course(monkeypatch):
    server._animal_recommendation_cache.clear()
    monkeypatch.setattr(server, "get_animal_preset", lambda p: None)
    monkeypatch.setattr(server, "find_nearest_animal_preset", lambda p: None)
    out = server.generate_animal_course(shape="whale", **CITY_HALL)
    assert "일반 코스만 가능해요" in out
    assert "이동해도 될까요?" in out and "/c/" in out
    assert "일반 코스를 추천해요" not in out


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
        "record_animal_completion", "extend_shape_relay",
    }
    assert len(names) == len(set(names))
    assert 3 <= len(names) <= 10
    open_world_tools = {
        "generate_running_course", "generate_animal_course", "refine_course",
    }
    for tool in tools:
        assert 1 <= len(tool.name) <= 128
        assert all(c.isascii() and (c.isalnum() or c in "_-") for c in tool.name)
        assert tool.inputSchema
        assert tool.annotations.title
        assert tool.annotations.openWorldHint is (tool.name in open_world_tools)
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


def test_rate_limit_rejects_burst_but_health_remains_available():
    import asyncio

    calls = 0

    async def inner(scope, receive, send):
        nonlocal calls
        calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = server._TokenBucketMiddleware(inner, rps=1)

    async def request(path):
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        await middleware({"type": "http", "path": path,
                          "client": ("203.0.113.1", 1), "headers": []},
                         receive, send)
        return messages[0]["status"]

    async def scenario():
        assert await request("/") == 200
        assert await request("/") == 200
        assert await request("/") == 429
        # Readiness is deliberately outside the public request bucket.
        assert await request("/healthz") == 200

    asyncio.run(scenario())
    assert calls == 3


def test_mcp_request_body_limit_rejects_declared_and_chunked_oversize():
    import asyncio

    calls = 0

    async def inner(scope, receive, send):
        nonlocal calls
        calls += 1
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = server._TokenBucketMiddleware(inner, max_body_bytes=8)

    async def request(headers, chunks):
        messages = []
        pending = list(chunks)
        async def receive():
            return pending.pop(0)
        async def send(message):
            messages.append(message)
        await middleware({"type": "http", "path": "/mcp",
                          "client": ("203.0.113.2", 1), "headers": headers},
                         receive, send)
        return messages[0]["status"]

    async def scenario():
        assert await request([(b"content-length", b"9")], []) == 413
        assert await request([], [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"6789", "more_body": False},
        ]) == 413

    asyncio.run(scenario())
    assert calls == 1  # declared oversize is rejected before reaching the app


def test_mcp_concurrency_limit_fails_fast_and_recovers():
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()

    async def inner(scope, receive, send):
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = server._TokenBucketMiddleware(
        inner, rps=100, max_concurrent_mcp=1)

    async def request(client):
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        await middleware({"type": "http", "path": "/mcp",
                          "client": (client, 1), "headers": []},
                         receive, send)
        return messages[0]["status"]

    async def scenario():
        first = asyncio.create_task(request("203.0.113.3"))
        await entered.wait()
        assert await request("203.0.113.4") == 429
        release.set()
        assert await first == 200
        assert await request("203.0.113.4") == 200

    asyncio.run(scenario())


def test_representative_tool_responses_stay_below_playmcp_24k_limit():
    """PlayMCP rejects tool text above 24k; keep headroom for framing."""
    from runart.models import CourseParams, encode_course_id

    params = CourseParams(lat=37.5665, lon=126.9780,
                          location_name="시청", distance_km=5.0)
    cid = encode_course_id(params)
    animal_params = params.model_copy(update={"shape": "whale"})
    animal_cid = encode_course_id(animal_params)
    outputs = [
        server.generate_running_course(location="시청", distance_km=5),
        server.generate_animal_course(location="시청"),
        server.list_available_shapes(),
        server.find_facilities_near_course(cid),
        server.refine_course(cid, night_mode=True),
        server.get_course_status(cid),
        server.record_animal_completion(animal_cid),
        server.extend_shape_relay(animal_cid),
    ]
    for output in outputs:
        assert len(output.encode("utf-8")) < 24_000


def test_container_runs_as_non_root_user():
    from pathlib import Path

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
