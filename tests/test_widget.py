"""Kakao Tools widget contract and MCP-boundary fallback tests."""

import json

import pytest

from runart.course import Course
from runart.models import CourseParams, decode_course_id, encode_course_id
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


def _components(value, kind: str) -> list[dict]:
    found = []
    if isinstance(value, dict):
        if value.get("type") == kind:
            found.append(value)
        for child in value.values():
            found.extend(_components(child, kind))
    elif isinstance(value, list):
        for child in value:
            found.extend(_components(child, kind))
    return found


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

    card = payload["widget"]
    assert card["size"] == "md" and card["padding"] == "sm"
    row = card["children"][0]
    assert row["type"] == "Row" and row["align"] == "center"
    image, overview = row["children"]
    assert image == {
        "type": "Image",
        "src": f"https://runnywhere.example/c/{course_id}/thumb.svg",
        "alt": "강남역 🐶 댕댕런 코스",
        "width": 132,
        "height": 132,
        "fit": "contain",
        "radius": "lg",
        "frame": True,
    }
    assert overview["type"] == "Col"
    assert [child["type"] for child in overview["children"]] == [
        "Title", "Caption", "Text", "Button",
    ]
    assert overview["children"][0]["value"] == "🐶 댕댕런"
    assert overview["children"][1]["value"] == "강남역 출발·도착"
    assert overview["children"][2]["value"] == "9.0km · 오르막 31m"
    buttons = _components(card, "Button")
    assert [button["label"] for button in buttons] == ["코스 보기"]
    target = buttons[0]["onClickAction"]["payload"]["target"]
    assert target == {
        "url": f"https://runnywhere.example/c/{course_id}",
        "pcUrl": f"https://runnywhere.example/c/{course_id}",
    }

    copy_text = payload["copy_text"]
    assert "러닝 친화도" not in first
    assert "오르막 31m" in first
    assert "GPX" not in first
    assert "# " not in copy_text
    assert "|" not in copy_text
    assert "[" not in copy_text and "](" not in copy_text
    assert "```" not in copy_text


def test_kakao_tools_origin_is_the_deploy_default_without_console_env():
    from pathlib import Path

    expected = "https://runnywhere-kakaotools.playmcp-endpoint.kakaocloud.io"
    assert server.DEFAULT_BASE_URL == expected
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert f"ARG RUNART_BASE_URL={expected}" in dockerfile
    assert "ENV RUNART_BASE_URL=${RUNART_BASE_URL}" in dockerfile


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
    assert result.structuredContent["assistant_text"].startswith("거리를 말씀하지 않아")
    assert result.content[1].text == result.structuredContent["assistant_text"]
    assert "기본 5km" not in result.content[0].text


@pytest.mark.parametrize("shape", ["dog", "cat", "rabbit", "whale"])
def test_each_confirmed_animal_course_type_is_widget_eligible(shape):
    course = _course(shape=shape)
    course_id = encode_course_id(course.params)
    server._cache_put(course_id, course)
    markdown = f"## 동물 코스\n- 지도: {server.BASE_URL}/c/{course_id}"

    result = server._course_tool_result(markdown, course_type=shape)
    payload = json.loads(result.content[0].text)

    assert payload["widget"]["type"] == "Card"
    assert result.structuredContent["result_code"] == "course_ready"


def test_new_course_is_cached_and_widgeted_in_its_first_tool_response(monkeypatch):
    """Cache-only handoff must not mean old-course-only widget support."""
    course = _course(shape=None)
    # The generator preserves the resolved request params on the Course.
    course.params = course.params.model_copy(update={
        "lat": 37.4986144, "lon": 127.0280696,
    })
    course_id = encode_course_id(course.params)
    with server._CACHE_LOCK:
        server._course_cache.pop(course_id, None)

    calls = []

    def generate_once(fn, params, timeout_s=None):
        calls.append((fn, params, timeout_s))
        return course

    monkeypatch.setattr(server, "_offload", generate_once)
    result = server.create_seoul_running_course(
        course_type="standard", location="강남역", distance_km=9.0
    )
    payload = json.loads(result.content[0].text)

    assert len(calls) == 1
    assert payload["widget"]["type"] == "Card"
    primary = _components(payload["widget"], "Button")[0]
    assert primary["onClickAction"]["payload"]["target"]["url"].endswith(
        f"/c/{course_id}"
    )
    with server._CACHE_LOCK:
        assert server._course_cache.get(course_id) is course


@pytest.mark.parametrize(
    ("legacy_name", "generator_name", "kwargs", "shape"),
    [
        (
            "_legacy_generate_running_course",
            "generate_running_course",
            {"location": "강남역", "distance_km": 5.0},
            None,
        ),
        (
            "_legacy_generate_animal_course",
            "generate_animal_course",
            {"shape": "dog", "location": "강남역"},
            "dog",
        ),
    ],
)
def test_legacy_preview_calls_return_latest_widget_contract(
    monkeypatch, legacy_name, generator_name, kwargs, shape
):
    """Cached old tool names must still emit the new-domain Kakao Card."""
    course = _course(shape=shape)
    course_id = encode_course_id(course.params)
    server._cache_put(course_id, course)
    markdown = f"## 호환 코스\n- 지도: {server.BASE_URL}/c/{course_id}"
    monkeypatch.setattr(server, generator_name, lambda **_kwargs: markdown)

    result = getattr(server, legacy_name)(**kwargs)
    payload = json.loads(result.content[0].text)
    buttons = {child["label"]: child
               for child in _components(payload["widget"], "Button")}

    assert result.structuredContent["result_code"] == "course_ready"
    assert payload["widget"]["type"] == "Card"
    # An animal call may now carry alternative choices, so the primary course
    # is identified by its own button rather than by position.
    target = buttons["코스 보기"]["onClickAction"]["payload"]["target"]["url"]
    assert target == f"{server.BASE_URL}/c/{course_id}"


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
    best_payload = json.loads(best_animal.content[0].text)
    assert best_payload["widget"]["type"] == "Card"

    second = _course(location_name="잠실역", shape=None)
    second_id = encode_course_id(second.params)
    server._cache_put(second_id, second)
    ambiguous = (
        f"{markdown}\n- 다른 지도: {server.BASE_URL}/c/{second_id}"
    )
    multiple_ids = server._course_tool_result(
        ambiguous, course_type="standard"
    )
    assert multiple_ids.content[0].text == ambiguous

    # best_animal intentionally lists alternatives; its first visible course
    # is the featured recommendation and must survive as the primary card.
    best_with_choices = server._course_tool_result(
        ambiguous, course_type="best_animal"
    )
    best_choices_payload = json.loads(best_with_choices.content[0].text)
    primary_button = next(
        child for child in _components(best_choices_payload["widget"], "Button")
        if child.get("label") == "코스 보기"
    )
    assert primary_button["onClickAction"]["payload"]["target"]["url"] == (
        f"{server.BASE_URL}/c/{course_id}"
    )

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


def test_course_widget_offers_the_alternative_choices_as_extra_buttons():
    """Case 2/3 answers are only useful if the other two choices are clickable."""
    from runart.courseplan import CourseChoice

    primary = _course(location_name="낙성대(강감찬)역")
    primary_id = encode_course_id(primary.params)
    other = _course(location_name="봉천역", shape="cat")
    other.params = other.params.model_copy(update={"distance_km": 6.0})
    other.length_m = 6_000.0
    other_id = encode_course_id(other.params)
    plain = _course(location_name="서울대입구역", shape=None)
    plain.params = plain.params.model_copy(update={"distance_km": 5.0})
    plain.length_m = 5_000.0
    plain_id = encode_course_id(plain.params)

    serialized = build_course_widget(
        primary, primary_id, "https://runnywhere.example",
        alternatives=(
            CourseChoice(other, other_id, "other_animal", 900.0),
            CourseChoice(plain, plain_id, "standard", 0.0),
        ),
    )
    payload = json.loads(serialized)
    children = payload["widget"]["children"]
    buttons = _components(children, "Button")
    labels = [button["label"] for button in buttons]

    assert payload["widget"]["type"] == "Card"
    assert not _contains_key(payload, "status")
    assert labels[0] == "코스 보기"
    assert len(labels) == 3
    assert "야옹런" in labels[1] and "6.0km" in labels[1] and "봉천역" in labels[1]
    assert "5.0km" in labels[2]
    assert all("떨어진" not in label for label in labels)
    targets = [button["onClickAction"]["payload"]["target"]["url"]
               for button in buttons]
    assert targets[1] == f"https://runnywhere.example/c/{other_id}"
    assert targets[2] == f"https://runnywhere.example/c/{plain_id}"
    assert len(serialized.encode("utf-8")) < WIDGET_MAX_BYTES
    # KakaoTalk share copy must carry the same choices, without link markup.
    copy_text = payload["copy_text"]
    assert "야옹런" in copy_text
    assert "[" not in copy_text and "](" not in copy_text


def test_widget_primary_link_decodes_to_the_same_detail_start_and_course():
    """A card must never open a detail page for a different course/start."""
    course = _course(location_name="경복궁역", shape="rabbit")
    course_id = encode_course_id(course.params)
    payload = json.loads(build_course_widget(
        course, course_id, "https://runnywhere.example"
    ))
    primary = next(
        child for child in _components(payload["widget"], "Button")
        if child.get("label") == "코스 보기"
    )
    linked_id = primary["onClickAction"]["payload"]["target"]["url"].rsplit(
        "/", 1
    )[1]

    assert linked_id == course_id
    assert decode_course_id(linked_id).canonical() == course.params.canonical()
    assert any(child.get("value") == "경복궁역 출발·도착"
               for child in _components(payload["widget"], "Caption"))


def test_course_widget_without_alternatives_keeps_one_primary_action():
    course = _course()
    course_id = encode_course_id(course.params)
    payload = json.loads(
        build_course_widget(course, course_id, "https://runnywhere.example"))
    buttons = _components(payload["widget"], "Button")
    assert [button["label"] for button in buttons] == ["코스 보기"]
