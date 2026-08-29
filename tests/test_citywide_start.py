"""Start-scope policy, district isolation and hard recommendation gates."""
import asyncio
import json
from copy import deepcopy

import pytest

from runart import geocode, server
from runart.animal_presets import PresetMatch
from runart.course import Course, CourseError
from runart.models import CourseParams, decode_course_id, encode_course_id


@pytest.fixture(scope="module", autouse=True)
def warm():
    server._warm()


@pytest.mark.parametrize("location", [None, "", "   ", "서울", "서울 시내", "서울 전역",
    "서울시 아무데나", "서울 어디서든", "서울 안의 모든 코스 중에서", "아무데나",
    "아무 곳이나", "어디든", "어디든지", "어디나", "랜덤", "랜덤으로", "상관없음", "아무거나"])
@pytest.mark.parametrize("extra", [{}, {"distance_km": 7}, {"night_mode": True}])
def test_missing_scope_never_geocodes_or_invents_a_start(monkeypatch, location, extra):
    def unexpected(*args, **kwargs):
        pytest.fail("missing starts must not geocode or generate")
    monkeypatch.setattr(server, "resolve_location", unexpected)
    monkeypatch.setattr(server, "generate_running_course", unexpected)
    monkeypatch.setattr(server, "_animal_text_for_cards", unexpected)
    for call in (lambda **kw: server.create_seoul_running_course(course_type="whale", **kw),
                 server._legacy_generate_running_course, server._legacy_generate_animal_course):
        result = call(location=location, **extra)
        assert result.isError and result.structuredContent["result_code"] == "missing_start"
        assert result.structuredContent["start_scope"] == "missing"
        assert result.content[0].text.index("구 이름(강남구)") < result.content[0].text.index("역 이름")
        assert "widget" not in result.content[0].text
        assert server.COURSE_EDIT_NOTICE not in result.content[0].text


@pytest.mark.parametrize("district", geocode._SEOUL_DISTRICTS)
def test_every_district_has_real_stations_and_classifies_without_geocoding(district):
    assert geocode.DISTRICT_STATIONS[district]
    for location in (district, f"서울 {district}", f"{district} 근처"):
        assert server._classify_start_scope({"location": location}) == "district"
        assert not geocode.is_citywide_scope(location)
    assert server._classify_start_scope({"location": f"서울 {district} 아차산로 100"}) == "specific"


@pytest.mark.parametrize("location", ["서울역", "서울시청", "서울숲", "서울대입구역", "강남역",
    "서울시 마포구 상암동 1601", "서울 성동구 아차산로 100", "서울 시내 서점", "강남구청역"])
def test_specific_places_are_not_scopes(location):
    assert server._classify_start_scope({"location": location}) == "specific"


@pytest.mark.parametrize("location", ["서울 시내", "서울, 전역.", "아무데나", "어디든"])
def test_geocoder_blocks_scopes_even_outside_the_dispatcher(monkeypatch, location):
    monkeypatch.setattr(geocode, "_kakao_get", lambda *a, **kw: pytest.fail("scope reached Kakao"))
    with pytest.raises(CourseError, match="구 이름"):
        geocode.resolve_location(location, None, None)


@pytest.mark.parametrize("extra", [{"lat": 37.5}, {"lon": 127.0}, {"lat": 90, "lon": 127.0},
    {"lat": float("nan"), "lon": 127.0}])
@pytest.mark.parametrize("park", [False, True])
def test_invalid_coordinates_never_become_random_recommendations(extra, park):
    result = server.create_seoul_running_course(course_type="standard", location="강남구",
        need_facilities=["park"] if park else None, **extra)
    assert result.isError
    assert result.structuredContent["result_code"] == "invalid_coordinates"


def _courses(result):
    selection = result.structuredContent["course_selection"]
    return [server._cached_course(f["course_id"]) for f in [selection["primary"], *selection["alternatives"]]]


@pytest.mark.parametrize("district", geocode._SEOUL_DISTRICTS)
def test_district_animals_stay_inside_district_and_keep_restorable_ids(monkeypatch, district):
    monkeypatch.setattr(server, "resolve_location", lambda *a, **kw: pytest.fail("district geocoded"))
    result = server.create_seoul_running_course(course_type="best_animal", location=district)
    assert not result.isError, result.content[0].text
    courses = _courses(result)
    assert 1 <= len(courses) <= 3
    assert len({c.params.location_name for c in courses}) == len(courses)
    for course in courses:
        assert server._course_district(course) == district
        cid = encode_course_id(course.params)
        with server._CACHE_LOCK:
            server._course_cache.pop(cid, None)
        restored = server._get_course(decode_course_id(cid))
        assert restored.path == course.path
    assert result.structuredContent["conditions_satisfied"] is True


def test_district_standard_uses_one_station_without_geocoding(monkeypatch):
    monkeypatch.setattr(server, "resolve_location", lambda *a, **kw: pytest.fail("district geocoded"))
    result = server.create_seoul_running_course(course_type="standard", location="강남구", distance_km=5)
    assert not result.isError
    assert result.structuredContent["course_selection"]["primary_matches_requested_shape"]
    assert all(server._course_district(c) == "강남구" for c in _courses(result))
    assert all(abs(c.length_km - 5) <= .5 for c in _courses(result))


def test_night_uncovered_district_gives_evidence_based_alternatives():
    covered = {server._course_district(c) for c in server.all_verified_animal_presets()
               if server.has_sufficient_night_lighting(c.rfs)}
    uncovered = set(geocode._SEOUL_DISTRICTS) - covered
    assert "강북구" in uncovered
    for district in uncovered:
        result = server.create_seoul_running_course(course_type="standard", location=district, night_mode=True)
        assert result.isError and result.structuredContent["result_code"] == "insufficient_courses"
        assert not result.structuredContent["repeat_tool_call"]
        assert any(d in result.content[0].text for d in covered if d)


def test_hard_filters_count_all_rejections_and_do_not_rewrite_presets(monkeypatch):
    course = deepcopy(server.all_verified_animal_presets()[0])
    course.length_m = 7000
    course.ascent_m = 100
    course.rfs = {"components": {"lighting": .39}}
    monkeypatch.setattr(server, "facilities_along", lambda *a, **kw: [{"type": "water"}])
    request = {"distance_km": 5, "include_hills": False, "night_mode": True,
               "need_facilities": ["restroom", "water"]}
    before = course.params.canonical()
    assert not server._eligible_matches([PresetMatch(course, 2001)], request, "standard")
    assert set(request["_stats"]["rejection_counts"]) == {
        "start_distance", "distance", "terrain", "lighting", "night_animal", "shape", "restroom"}
    assert course.params.canonical() == before


def test_legacy_false_terrain_is_unspecified_but_primary_false_is_explicit(monkeypatch):
    captured = []
    monkeypatch.setattr(server, "_dispatch_course_request", lambda kind, req: captured.append((kind, req)))
    server.create_seoul_running_course(course_type="standard", location="강남구", include_hills=False)
    server._legacy_generate_running_course("강남구", include_hills=False)
    server._legacy_generate_animal_course("whale", "강남구", include_hills=False)
    assert [req["include_hills"] for _, req in captured] == [False, None, None]
    assert [req["location"] for _, req in captured] == ["강남구"] * 3


def test_no_base_course_does_not_try_variants_or_preset_substitutes(monkeypatch):
    monkeypatch.setattr(server, "generate_running_course", lambda **kw: "⚠️ 순환 코스를 만들지 못했어요.")
    monkeypatch.setattr(server, "_standard_alternatives", lambda *a: pytest.fail("variant attempted"))
    result = server.create_seoul_running_course(course_type="standard", location="시청")
    assert result.structuredContent["result_code"] == "no_candidate_evidence"
    assert result.structuredContent["retryable"] is False
    assert result.structuredContent["repeat_tool_call"] is False


def test_parallel_variants_use_existing_pool_and_have_inline_fallback(monkeypatch):
    course = server._get_course(CourseParams(lat=37.4979, lon=127.0276, location_name="강남역"))
    calls = []
    monkeypatch.setattr(server, "_course_cache", {})
    def parallel(fn, probes, timeout_s):
        calls.append((fn, probes, timeout_s))
        return {k: None for k in probes}
    with monkeypatch.context() as patch:
        patch.setattr(server, "_offload_map", parallel)
        assert server._standard_alternatives(course.params, course, 1.5) == []
    assert list(calls[0][1]) == [2, 4, 6]
    assert calls[0][0] is server.generate_course
    monkeypatch.setattr(server, "_get_pool", lambda: None)
    matches = server._standard_alternatives(course.params, course, 2)
    assert matches and all(isinstance(m.course, Course) for m in matches)


def test_schema_contracts_fit_budget_and_terrain_is_tristate():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("create_seoul_running_course",):
        description = tools[name].description
        assert all(term in description for term in ("missing", "district", "specific", "omit location"))
        assert len(description) <= 900
    assert all(len(t.description) <= 1024 for t in tools.values())
    schema = tools["create_seoul_running_course"].inputSchema["properties"]
    assert schema["include_hills"]["default"] is None
    assert {item["type"] for item in schema["include_hills"]["anyOf"]} == {"boolean", "null"}
    assert set(schema["need_facilities"]["anyOf"][0]["items"]["enum"]) == set(server.FACILITY_TYPES)


@pytest.mark.parametrize("kind,location,facilities", [("standard", "시청", None),
    ("whale", "강남구", None), ("standard", None, ["park"])])
def test_visible_edit_notice_matches_all_output_channels(kind, location, facilities):
    result = server.create_seoul_running_course(course_type=kind, location=location, need_facilities=facilities)
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["widget"]["children"][-1] == {"type": "Text", "value": server.COURSE_EDIT_NOTICE, "size": "sm"}
    assert payload["copy_text"].endswith(server.COURSE_EDIT_NOTICE)
    assert result.structuredContent["assistant_final_text"].endswith(server.COURSE_EDIT_NOTICE)
    assert len(result.content[0].text.encode()) <= 12000
    assert server.course_markdown(_courses(result)[0], server.BASE_URL, []).endswith(server.COURSE_EDIT_NOTICE)


def test_scope_logs_include_counts_but_not_raw_location(monkeypatch, caplog):
    raw = "비공개 상호명 주소"
    monkeypatch.setattr(server, "generate_running_course", lambda **kw: "⚠️ 위치를 찾지 못했어요.")
    with caplog.at_level("INFO", logger="runart"):
        server.create_seoul_running_course(course_type="standard", location=raw)
    assert raw not in caplog.text
    assert all(field in caplog.text for field in ("release_sha=", "start_scope=", "district=",
        "candidate_count=", "eligible_count=", "rejection_counts=", "result_code=", "duration_ms="))


@pytest.mark.parametrize("text,expected", [
    ("⚠️ 이 출발지와 거리에서 가로등이 충분한 코스를 확인하지 못했어요.", "insufficient_courses"),
    ("⚠️ 이 위치에서는 순환 코스를 만들지 못했어요.", "insufficient_courses"),
    ("⚠️ 목표 거리 근처 코스를 찾지 못했어요.", "insufficient_courses"),
    ("⚠️ 거리는 1km에서 42.195km 사이로 알려주세요.", "invalid_request"),
    ("⚠️ 이 위치를 찾지 못했어요.", "location_not_found"),
    ("⚠️ 추천 거리: 7km", "exact_shape_unavailable"),
])
def test_failure_codes_distinguish_bad_input_from_unavailable_courses(text, expected):
    result = server._course_tool_result(text, course_type="standard")
    assert result.structuredContent["result_code"] == expected
    if expected == "insufficient_courses":
        assert result.isError and result.structuredContent["repeat_tool_call"] is False
        assert result.structuredContent["retryable"] is False


def test_refine_rejects_citywide_location_before_kakao(monkeypatch):
    monkeypatch.setattr(geocode, "_kakao_get", lambda *a, **kw: pytest.fail("scope reached Kakao"))
    cid = encode_course_id(CourseParams(lat=37.5665, lon=126.978))
    assert "구 이름" in server.refine_course(cid, location="서울 시내")


@pytest.mark.parametrize("value", [-1, 1000, float("nan"), float("inf"), "bad", True])
@pytest.mark.parametrize("field", ["distance_km", "duration_min"])
def test_invalid_effort_does_not_generate_or_break_metadata(monkeypatch, value, field):
    monkeypatch.setattr(server, "_specific_course_result", lambda *a: pytest.fail("invalid request generated"))
    result = server.create_seoul_running_course(course_type="standard", location="강남구", **{field: value})
    assert result.structuredContent["result_code"] == "invalid_request"
    json.loads(result.model_dump_json())


@pytest.mark.parametrize("km,moved,accepted", [(5.5, 2000, True), (5.5001, 0, False),
    (5, 2000.1, False), (4.5, 0, True), (4.499, 0, False)])
def test_preset_slots_never_relax_explicit_distance_or_radius(km, moved, accepted):
    course = Course(params=CourseParams(lat=37.5, lon=127, shape="whale"), path=[], length_m=km * 1000)
    matches = server._eligible_matches([PresetMatch(course, moved)], {"distance_km": 5},
                                       "standard", allow_animal_alternatives=True)
    assert bool(matches) is accepted


def test_variant_cache_is_reused_instead_of_restarting_workers(monkeypatch):
    course = server._get_course(CourseParams(lat=37.4979, lon=127.0276, location_name="강남역"))
    for variant in (2, 4, 6):
        params = course.params.model_copy(update={"route_variant": variant})
        server._get_course(params)
    monkeypatch.setattr(server, "_offload_map", lambda *a, **kw: pytest.fail("warm variants regenerated"))
    assert server._standard_alternatives(course.params, course, 2)


def test_real_streamable_http_contract_and_course_links():
    import httpx

    async def check():
        app = server.mcp.streamable_http_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url="http://localhost:8000",
                    headers={"Accept": "application/json, text/event-stream"}) as client:
                async def rpc(method, params):
                    response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                        "method": method, "params": params})
                    assert response.status_code == 200, response.text
                    data = response.json()
                    assert "error" not in data, data
                    return data["result"]
                initialized = await rpc("initialize", {"protocolVersion": "2025-03-26",
                    "capabilities": {}, "clientInfo": {"name": "local-policy-check", "version": "1"}})
                assert initialized["serverInfo"]["name"] == "Runnywhere"
                listed = await rpc("tools/list", {})
                assert len(listed["tools"]) == 7
                for name, arguments, code in (
                    ("create_seoul_running_course", {"course_type": "whale", "distance_km": 7}, "missing_start"),
                    ("create_seoul_running_course", {"course_type": "standard", "location": "서울 시내"}, "missing_start"),
                    ("create_seoul_running_course", {"course_type": "whale", "location": "강남구"}, "course_ready"),
                    ("create_seoul_running_course", {"course_type": "standard", "location": "시청", "distance_km": 100}, "invalid_request"),
                    ("create_seoul_running_course", {"course_type": "standard", "need_facilities": ["park"]}, "course_ready"),
                ):
                    result = await rpc("tools/call", {"name": name, "arguments": arguments})
                    assert result["structuredContent"]["result_code"] == code, result
                    if code != "course_ready":
                        continue
                    selection = result["structuredContent"]["course_selection"]
                    for facts in [selection["primary"], *selection["alternatives"]]:
                        # Evict the performance cache to prove stateless reconstruction.
                        cid = facts["course_id"]
                        with server._CACHE_LOCK:
                            server._course_cache.pop(cid, None)
                        for suffix in ("", "/editor", ".gpx"):
                            response = await client.get(f"/c/{cid}{suffix}")
                            assert response.status_code == 200
                health = (await client.get("/healthz")).json()
                assert health["ready"] and health["animal_presets"].startswith("ok")
    asyncio.run(check())
