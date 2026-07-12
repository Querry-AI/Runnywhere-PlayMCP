import time

import pytest

from runart import server
from runart.animal_presets import all_verified_animal_presets
from runart.exploration import (create_relay, decode_passport, decode_relay,
                                atlas_html, legal_html, passport_summary,
                                record_run, weekly_recommendation)
from runart.gpx import to_gpx
from runart.models import encode_course_id
from runart.shapes import SHAPES


def _courses_by_shape():
    out = {}
    for course in all_verified_animal_presets():
        out.setdefault(course.params.shape, course)
    assert set(out) == set(SHAPES)
    return out


def test_passport_is_stateless_and_idempotent():
    course = _courses_by_shape()["whale"]
    cid = encode_course_id(course.params)
    token1, summary1 = record_run(cid, None)
    token2, summary2 = record_run(cid, token1)
    assert token1 == token2
    assert summary1 == summary2
    assert decode_passport(token1)["runs"][0]["s"] == "whale"
    with pytest.raises(ValueError, match="signature"):
        decode_passport(token1[:-1] + ("0" if token1[-1] != "0" else "1"))


def test_four_shapes_unlock_seoul_badge():
    token = None
    for course in _courses_by_shape().values():
        token, _ = record_run(encode_course_id(course.params), token)
    summary = passport_summary(decode_passport(token))
    assert len(summary["shapes"]) == 4
    assert "서울 동물도감 완성" in summary["badges"]


def test_shape_relay_accepts_only_same_animal():
    courses = _courses_by_shape()
    whale_id = encode_course_id(courses["whale"].params)
    token, data = create_relay(whale_id, None)
    assert decode_relay(token)["legs"] == [whale_id]
    with pytest.raises(ValueError, match="same shape"):
        create_relay(encode_course_id(courses["dog"].params), token)


def test_tokens_require_production_secret_outside_tests(monkeypatch):
    from runart import exploration
    monkeypatch.delenv("RUNART_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="required"):
        exploration._encode({"v": 1})


def test_weekly_recommendation_is_fast_and_undiscovered():
    started = time.perf_counter()
    course = weekly_recommendation(37.5665, 126.9780, None)
    assert course is not None
    assert time.perf_counter() - started < 0.1


def test_new_mcp_tool_answers_are_concise_and_actionable():
    course = _courses_by_shape()["whale"]
    cid = encode_course_id(course.params)
    passport_out = server.record_animal_completion(cid)
    assert "passport_token" in passport_out and "/passport/" in passport_out
    relay_out = server.extend_shape_relay(cid)
    assert "relay_token" in relay_out and "/relay/" in relay_out
    survey_out = server.generate_animal_course(location="시청")
    assert "추천" in survey_out and "/c/" in survey_out


def test_atlas_is_not_exposed_as_competing_mcp_tool():
    import asyncio
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert "generate_animal_course" in names
    assert "explore_animal_collection" not in names


def test_atlas_uses_kakao_map_sdk():
    page = atlas_html("https://runnywhere.example", "javascript-key")
    assert "dapi.kakao.com/v2/maps/sdk.js?appkey=javascript-key&autoload=false" in page
    assert "kakao.maps.load(bootAtlasMap)" in page
    assert "new kakao.maps.Map" in page
    assert "baseUrl=\"https://runnywhere.example\"" in page
    assert "mapBuckets" in page
    assert "atlas-detail" in page and "sheetHandle" in page
    assert "mapNode.replaceChildren()" in page
    assert "prefers-reduced-motion" in page


def test_atlas_explains_missing_kakao_key():
    page = atlas_html("https://runnywhere.example", "")
    assert "dapi.kakao.com/v2/maps/sdk.js" not in page
    assert "운영 환경의 KAKAO_JAVASCRIPT_KEY" in page


def test_legal_pages_are_linked_without_expanding_mcp_responses():
    for kind, marker in (("privacy", "Kakao Local API"),
                         ("terms", "112"),
                         ("licenses", "OpenStreetMap")):
        page = legal_html(kind, "legal@example.com")
        assert marker in page
        assert "/terms" in page and "/privacy" in page and "/data-licenses" in page
        if kind != "licenses":
            assert "legal@example.com" in page


def test_gpx_carries_osm_licence_notice():
    gpx = to_gpx("안전 & 코스", [(37.5, 127.0), (37.51, 127.01)])
    assert "OpenStreetMap contributors" in gpx
    assert "https://www.openstreetmap.org/copyright" in gpx
    assert "안전 &amp; 코스" in gpx
