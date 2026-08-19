"""Kakao Tools widget contract and MCP-boundary fallback tests."""

import json

import pytest

from runart.course import Course
from runart.models import CourseParams, encode_course_id
from runart import server
from runart.widget import (
    WIDGET_MAX_BYTES,
    WidgetBuildError,
    build_course_widget,
)


def _course(*, location_name: str = "강남역", shape: str | None = "dog") -> Course:
    params = CourseParams(
        lat=37.4986,
        lon=127.0281,
        location_name=location_name,
        distance_km=9.0,
        shape=shape,
    )
    return Course(
        params=params,
        path=[],
        points=[],
        length_m=9_040.0,
        ascent_m=31.0,
        rfs={"score": 86, "highlights": []},
        shape_similarity=0.91 if shape else None,
    )


def _contains_key(value, forbidden: str) -> bool:
    if isinstance(value, dict):
        return forbidden in value or any(
            _contains_key(child, forbidden) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(child, forbidden) for child in value)
    return False


def test_course_widget_matches_kakao_card_contract_and_is_deterministic():
    course = _course()
    course_id = encode_course_id(course.params)

    first = build_course_widget(course, course_id, "https://runnywhere.example")
    second = build_course_widget(course, course_id, "https://runnywhere.example")
    payload = json.loads(first)

    assert first == second
    assert set(payload) == {"widget", "copy_text", "name"}
    assert payload["name"] == "runnywhere_course"
    assert payload["widget"]["type"] == "Card"
    assert not _contains_key(payload, "status")
    assert "댕댕런" in first
    assert "\\uac15" not in first
    assert len(first.encode("utf-8")) < WIDGET_MAX_BYTES

    children = payload["widget"]["children"]
    assert all(
        (child["type"] == "Text" and set(child) == {"type", "value"})
        or (child["type"] == "Button" and "label" in child)
        for child in children
    )
    buttons = [child for child in children if child["type"] == "Button"]
    assert [button["label"] for button in buttons] == ["코스 지도 보기", "GPX 다운로드"]
    targets = [button["onClickAction"]["payload"]["target"] for button in buttons]
    assert targets[0] == {
        "url": f"https://runnywhere.example/c/{course_id}",
        "pcUrl": f"https://runnywhere.example/c/{course_id}",
    }
    assert targets[1] == {
        "url": f"https://runnywhere.example/c/{course_id}.gpx",
        "pcUrl": f"https://runnywhere.example/c/{course_id}.gpx",
    }

    copy_text = payload["copy_text"]
    assert "# " not in copy_text
    assert "|" not in copy_text
    assert "[" not in copy_text and "](" not in copy_text
    assert "```" not in copy_text


def test_course_widget_bounds_untrusted_place_copy_and_rejects_bad_urls():
    location = "  강남역\n<script>" + "아" * 100
    course = _course(location_name=location, shape=None)
    course_id = encode_course_id(course.params)
    serialized = build_course_widget(course, course_id, "https://runnywhere.example/")
    payload = json.loads(serialized)

    rendered = json.dumps(payload, ensure_ascii=False)
    assert "\n" not in rendered
    assert "<script>" not in payload["copy_text"]
    assert len(serialized.encode("utf-8")) < WIDGET_MAX_BYTES

    with pytest.raises(WidgetBuildError):
        build_course_widget(course, "not/a/course", "https://runnywhere.example")
    with pytest.raises(WidgetBuildError):
        build_course_widget(course, course_id, "javascript:alert(1)")


def test_mcp_success_uses_cached_course_without_regeneration(monkeypatch):
    course = _course(shape=None)
    course_id = encode_course_id(course.params)
    markdown = (
        "거리를 말씀하지 않아 기본 5km로 만들었어요.\n"
        f"## 임시 제목\n- 지도: {server.BASE_URL}/c/{course_id}\n"
        f"- GPX: {server.BASE_URL}/c/{course_id}.gpx"
    )
    server._cache_put(course_id, course)

    def must_not_generate(*args, **kwargs):
        raise AssertionError("widget handoff must not regenerate a course")

    monkeypatch.setattr(server, "_get_course", must_not_generate)
    result = server._course_tool_result(markdown, course_type="standard")
    payload = json.loads(result.content[0].text)

    assert result.structuredContent["result_code"] == "course_ready"
    assert result.isError is False
    assert payload["widget"]["type"] == "Card"
    assert "기본 5km" in result.content[0].text


def test_mcp_widget_falls_back_to_original_markdown(monkeypatch):
    course = _course(shape=None)
    course_id = encode_course_id(course.params)
    markdown = f"## 코스\n- 지도: {server.BASE_URL}/c/{course_id}"

    with server._CACHE_LOCK:
        server._course_cache.pop(course_id, None)
    cache_miss = server._course_tool_result(markdown, course_type="standard")
    assert cache_miss.content[0].text == markdown

    server._cache_put(course_id, course)
    monkeypatch.setattr(server, "KAKAO_WIDGETS_ENABLED", False)
    disabled = server._course_tool_result(markdown, course_type="standard")
    assert disabled.content[0].text == markdown

    monkeypatch.setattr(server, "KAKAO_WIDGETS_ENABLED", True)
    best_animal = server._course_tool_result(markdown, course_type="best_animal")
    assert best_animal.content[0].text == markdown

    error_text = "⚠️ 출발 위치가 필요해요."
    error = server._course_tool_result(error_text, course_type="standard")
    assert error.content[0].text == error_text
    assert error.isError is True


def test_mcp_widget_build_error_preserves_markdown_and_result_code(monkeypatch):
    course = _course(shape=None)
    course_id = encode_course_id(course.params)
    markdown = f"## 코스\n- 지도: {server.BASE_URL}/c/{course_id}"
    server._cache_put(course_id, course)

    def broken_builder(*args, **kwargs):
        raise WidgetBuildError("test failure")

    monkeypatch.setattr(server, "build_course_widget", broken_builder)
    result = server._course_tool_result(markdown, course_type="standard")
    assert result.content[0].text == markdown
    assert result.structuredContent["result_code"] == "course_ready"
    assert result.isError is False
