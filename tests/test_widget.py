"""Kakao Tools widget contract and MCP-boundary fallback tests."""

import json
from copy import deepcopy
from dataclasses import replace

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


def _three_course_plan(course):
    """Serializer fixture; actual route distinctness is tested at generation."""
    from runart.courseplan import CourseChoice, CoursePlan

    choices = []
    for variant in (0, 2, 4):
        c = deepcopy(course)
        c.params = c.params.model_copy(update={"route_variant": variant})
        choices.append(CourseChoice(c, encode_course_id(c.params), "standard"))
    return CoursePlan("exact", "추천 코스 3개를 준비했어요.", choices[0], tuple(choices[1:]))


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
    assert card["size"] == "lg" and card["padding"] == {"x": "12px", "top": "12px"}
    assert len(_components(card, "Card")) == 1
    assert card["border"] == {"size": 0, "color": "transparent"}
    assert not _components(card, "Basic")
    assert not _components(card, "Divider")
    # The card leads with a one-line heading so the runner knows what it is.
    header = card["children"][0]
    assert header["type"] == "Title" and header["value"] == "추천 코스"
    assert header["weight"] == "bold" and header["color"] == "#000000"
    row = card["children"][1]
    assert row["type"] == "Row"
    assert row["padding"] == {"top": "8px", "bottom": "8px"}
    image = _components(row, "Image")[0]
    overview = row["children"][1]
    action = _components(row, "Button")[0]
    assert action["type"] == "Button" and "block" not in action
    assert image == {
        "type": "Image",
        "src": f"https://runnywhere.example/c/{course_id}/thumb.svg",
        "alt": "🐶 강남역 댕댕런 실제 지도 코스",
        "width": 88,
        "height": 88,
        "fit": "contain",
        "radius": "md",
        "frame": False,
    }
    assert overview["type"] == "Col"
    assert [child["type"] for child in overview["children"]] == [
        "Row", "Text", "Row",
    ]
    assert "평지 위주" in _components(overview["children"][0], "Badge")[0]["label"]
    assert overview["children"][1]["value"] == "🐶 강남역 댕댕런"
    footer = overview["children"][2]
    assert footer["gap"] == "12px"
    assert _components(footer, "Text")[0]["value"] == "9.0km · 약 63분"
    assert _components(footer, "Text")[0]["weight"] == "bold"
    assert _components(footer, "Caption")[0]["value"] == f"오르막 {course.ascent_m:.0f}m"
    assert footer["children"][-1] is action
    assert action["pill"] is True and action["variant"] == "solid"
    # Distance pairs with time, not ascent: time is what a runner plans around.
    buttons = _components(card, "Button")
    assert [button["label"] for button in buttons] == ["지도 보기"]
    target = buttons[0]["onClickAction"]["payload"]["target"]
    assert target == {
        "url": f"https://runnywhere.example/c/{course_id}",
        "pcUrl": f"https://runnywhere.example/c/{course_id}",
    }

    copy_text = payload["copy_text"]
    assert "러닝 친화도" not in first
    assert "약 63분" in first
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
    assert result.structuredContent["assistant_text"] == "강남역에서 출발하는 코스예요."
    assert result.structuredContent["assistant_text_position"] == "before_widget"
    assert result.structuredContent["assistant_text_verbatim"] is True
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
    # A stored loop runs past 강남역, and this test is about what generation
    # then caches, so keep it on the generation path like the preset stubs do.
    monkeypatch.setattr(server, "on_route_preset", lambda _: None)
    monkeypatch.setattr(server, "_animal_course_plan", lambda *args: _three_course_plan(course))
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
    """Cached old tool names must still emit the new-domain Kakao listing."""
    course = _course(shape=shape)
    course_id = encode_course_id(course.params)
    server._cache_put(course_id, course)
    markdown = f"## 호환 코스\n- 지도: {server.BASE_URL}/c/{course_id}"
    monkeypatch.setattr(server, generator_name, lambda **_kwargs: markdown)
    monkeypatch.setattr(server, "_animal_course_plan", lambda *args: _three_course_plan(course))

    result = getattr(server, legacy_name)(**kwargs)
    payload = json.loads(result.content[0].text)
    buttons = _components(payload["widget"], "Button")

    assert result.structuredContent["result_code"] == "course_ready"
    assert payload["widget"]["type"] == "Card"
    # An animal call may now carry alternative choices, so the primary course
    # is identified by its own button rather than by position.
    target = buttons[0]["onClickAction"]["payload"]["target"]["url"]
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
        if child.get("label") == "지도 보기"
    )
    assert primary_button["onClickAction"]["payload"]["target"]["url"] == (
        f"{server.BASE_URL}/c/{course_id}"
    )

    error_text = "⚠️ 출발 위치가 필요해요."
    error = server._course_tool_result(error_text, course_type="standard")
    assert error.content[0].text == error_text
    assert error.isError is True


def test_mcp_widget_build_error_preserves_actual_course_and_result_code(monkeypatch):
    course = _course(shape=None)
    course_id = encode_course_id(course.params)
    markdown = f"## 코스\n- 지도: {server.BASE_URL}/c/{course_id}"
    server._cache_put(course_id, course)

    def broken_builder(*args, **kwargs):
        raise WidgetBuildError("test failure")

    monkeypatch.setattr(server, "build_course_widget", broken_builder)
    result = server._course_tool_result(markdown, course_type="standard")
    assert result.content[0].text == result.structuredContent["assistant_final_text"]
    assert f"{server.BASE_URL}/c/{course_id}" in result.content[0].text
    assert result.content[0].text.endswith(server.COURSE_EDIT_NOTICE)
    assert result.structuredContent["result_code"] == "course_ready"
    assert result.isError is False


def test_course_widget_offers_every_alternative_as_a_full_matching_card():
    """All three recommendations need the same image, facts, and action."""
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
    rows = _components(children, "Row")
    images = _components(children, "Image")
    titles = [row["children"][1]["children"][1]["value"]
              for row in rows if row["children"][0]["type"] == "Image"]

    assert payload["widget"]["type"] == "Card"
    assert not _contains_key(payload, "status")
    assert labels == ["지도 보기"] * 3
    assert len(images) == 3
    # Alternatives are introduced, not silently appended under the winner.
    assert any(child.get("value") == "다른 코스도 있어요"
               for child in _components(children, "Text"))
    for child in children:
        if child.get("value") in {"추천 코스", "다른 코스도 있어요"}:
            assert child["weight"] == "bold" and child["color"] == "#000000"
    assert "야옹런" in titles[1]
    assert "낙성대역" in titles[0]
    assert "봉천역" in titles[1]
    assert "서울대입구역" in titles[2]
    assert all(image["width"] == image["height"] == 88 for image in images)
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
        if child.get("label") == "지도 보기"
    )
    linked_id = primary["onClickAction"]["payload"]["target"]["url"].rsplit(
        "/", 1
    )[1]

    assert linked_id == course_id
    assert decode_course_id(linked_id).canonical() == course.params.canonical()
    assert any("경복궁역" in child.get("value", "")
               for child in _components(payload["widget"], "Text"))


def test_course_widget_without_alternatives_keeps_one_primary_action():
    course = _course()
    course_id = encode_course_id(course.params)
    payload = json.loads(
        build_course_widget(course, course_id, "https://runnywhere.example"))
    buttons = _components(payload["widget"], "Button")
    assert [button["label"] for button in buttons] == ["지도 보기"]


def test_widget_title_removes_parenthetical_station_aliases():
    course = _course(location_name="서울대입구(abx)역", shape="cat")
    payload = json.loads(build_course_widget(
        course, encode_course_id(course.params), "https://runnywhere.example"))
    titles = [item["value"] for item in _components(payload["widget"], "Text")]
    assert "🐱 서울대입구역 야옹런" in titles
    assert "abx" not in json.dumps(payload, ensure_ascii=False)


def test_course_card_reads_like_a_recommendation_not_a_data_dump():
    """The card has to answer 'is this my run?' before 'what are its numbers?'."""
    course = _course(location_name="성수역")
    course_id = encode_course_id(course.params)
    payload = json.loads(build_course_widget(
        course, course_id, "https://runnywhere.example"))
    values = [child["value"] for child in
              _components(payload["widget"], "Caption")
              + _components(payload["widget"], "Text")
              + _components(payload["widget"], "Title")]
    joined = " ".join(values + [
        child["label"] for child in _components(payload["widget"], "Badge")
    ])

    # Heading, then identity, start, effort, character -- in that order.
    assert "추천 코스" in joined
    assert "성수역" in joined
    assert "약 63분" in joined
    # Character chips carry the words, not bare emoji.
    assert "평지 위주" in joined or "오르막 포함" in joined or "완만한 경사" in joined


def test_widget_effort_matches_the_detail_page_default_pace():
    """A card promising 63분 must not open a page that says something else."""
    from runart.pace import DEFAULT_PACE_S, effort

    course = _course()
    course_id = encode_course_id(course.params)
    payload = json.loads(build_course_widget(
        course, course_id, "https://runnywhere.example"))
    expected = effort(course.length_km, DEFAULT_PACE_S)["duration_min"]

    assert f"약 {expected}분" in json.dumps(payload, ensure_ascii=False)


def test_card_chips_prefer_what_separates_one_course_from_another():
    """Three courses around one station are all 도심 위주; saying so on each
    card costs a slot and settles nothing."""
    from runart.insights import CourseFacts
    from runart.widget import _card_traits

    facts = CourseFacts(
        traits=(
            {"emoji": "🛣️", "label": "평지 위주"},
            {"emoji": "🏙️", "label": "도심 위주"},
            {"emoji": "🚦", "label": "신호 적음"},
        ),
        highlights=(), cautions=(), signals=4,
        facility_counts={"convenience_store": 0, "restroom": 0},
    )
    labels = [trait["label"] for trait in _card_traits(facts)]
    assert labels == ["평지 위주", "신호 적음"]

    # With no specific opinion to show, terrain keeps the slot.
    plain = CourseFacts(
        traits=({"emoji": "🛣️", "label": "평지 위주"},
                {"emoji": "🌳", "label": "공원·강변 위주"}),
        highlights=(), cautions=(), signals=0,
        facility_counts={"convenience_store": 0, "restroom": 0},
    )
    assert [t["label"] for t in _card_traits(plain)] == ["평지 위주", "공원·강변 위주"]


def test_card_rows_read_as_a_listing_not_a_stack_of_paragraphs():
    """Keep the reference's two columns and the action beside the metrics."""
    from runart.courseplan import CourseChoice

    primary = _course(location_name="성수역")
    primary_id = encode_course_id(primary.params)
    other = _course(location_name="뚝섬역", shape=None)
    other.params = other.params.model_copy(update={"distance_km": 6.0})
    other.length_m = 6_000.0
    other_id = encode_course_id(other.params)

    payload = json.loads(build_course_widget(
        primary, primary_id, "https://runnywhere.example",
        alternatives=(CourseChoice(other, other_id, "standard", 0.0),)))
    rows = [row for row in _components(payload["widget"], "Row")
            if row["children"][0]["type"] == "Image"]

    for row in rows:
        kinds = [child["type"] for child in row["children"]]
        assert kinds == ["Image", "Col"]
        column = row["children"][1]
        # Reference: badges, title, bold effort, ascent beside the action.
        assert [child["type"] for child in column["children"]] == [
            "Row", "Text", "Row",
        ]
        tags = column["children"][0]
        assert tags["wrap"] == "wrap"
        assert all(tag["variant"] == "soft" for tag in tags["children"])
        assert column["children"][1]["maxLines"] == 1
        assert column["children"][-1]["children"][-1]["type"] == "Button"

    # Headings are one line each; the explanatory category captions are gone.
    assert "동물 코스" not in json.dumps(payload, ensure_ascii=False)
    assert "일반 코스" not in json.dumps(payload, ensure_ascii=False)
    assert "요청 조건에 가장 잘 맞는" not in json.dumps(payload, ensure_ascii=False)


def test_bongcheon_reference_copy_uses_live_facts_and_keeps_map_action(monkeypatch):
    """The requested example is data, not text hardcoded onto every course."""
    from runart.insights import CourseFacts

    course = _course(location_name="봉천역", shape="cat")
    course.params = course.params.model_copy(update={"distance_km": 7.5})
    course.length_m = 7_540.0  # Displays 7.5km, 53 minutes at the detail pace.
    course.ascent_m = 106.0
    facts = CourseFacts(
        traits=(
            {"emoji": "🌤️", "label": "완만한 경사"},
            {"emoji": "🏙️", "label": "도심 위주"},
            {"emoji": "🌒", "label": "조명 어두움"},
        ),
        highlights=(), cautions=(), signals=0,
        # Deliberately reversed: the UI always puts shops before toilets.
        facility_counts={"restroom": 2, "convenience_store": 7},
    )
    monkeypatch.setattr("runart.widget.course_facts", lambda value: facts)
    course_id = encode_course_id(course.params)
    payload = json.loads(build_course_widget(
        course, course_id, "https://runnywhere.example"))
    overview = payload["widget"]["children"][1]["children"][1]
    tags, title, footer = overview["children"]

    assert " · ".join(tag["label"] for tag in tags["children"]) == (
        "🌤️ 완만한 경사 · 🌒 조명 어두움 · 🏪 7 · 🚻 2"
    )
    assert title["value"] == "🐱 봉천역 야옹런"
    metrics, action = footer["children"]
    assert [item["value"] for item in metrics["children"]] == [
        "7.5km · 약 53분", "오르막 106m",
    ]
    assert action["label"] == "지도 보기"
    assert action["onClickAction"]["payload"]["target"]["url"] == (
        f"https://runnywhere.example/c/{course_id}"
    )


def test_unavailable_animal_explanation_renders_before_recommendations(monkeypatch):
    from runart.courseplan import build_course_plan

    standard = _course(location_name="서울역", shape=None)
    plan = build_course_plan(
        requested_name="서울역", shape="dog", exact=None,
        shape_matches=[], animal_matches=[], standard=standard,
    )
    monkeypatch.setattr(server, "_animal_course_plan", lambda *args: plan)
    result = server._planned_course_result(
        "", course_type="dog", request={"location": "서울역"}, timeout_s=5)
    payload = json.loads(result.content[0].text)
    heading, row, notice = payload["widget"]["children"]
    assert notice["value"] == server.COURSE_EDIT_NOTICE
    lead = result.structuredContent["assistant_text"]
    assert not _components(payload["widget"], "Markdown")
    assert "서울역에서 출발하는 강아지 모양 코스를 찾지 못해" in lead
    assert "다른 추천 코스를 준비했어요" in lead
    assert heading["value"] == "추천 코스"
    assert _components(row, "Button")[0]["label"] == "지도 보기"
    assert result.structuredContent["assistant_text_in_widget"] is False
    assert result.structuredContent["assistant_text_position"] == "before_widget"
    assert plan.lead not in result.content[0].text
    assert payload["copy_text"].endswith(server.COURSE_EDIT_NOTICE)


@pytest.mark.parametrize("actual_shape", ["rabbit", None, "dog"])
@pytest.mark.parametrize("widget_fails", [False, True])
def test_requested_dog_and_actual_course_stay_consistent_even_without_widget(
    monkeypatch, actual_shape, widget_fails,
):
    from runart.animal_presets import PresetMatch
    from runart.courseplan import build_course_plan

    actual = _course(location_name="서울역", shape=actual_shape)
    # Deliberately feed stale dog copy: neither the normal nor fallback answer
    # may reuse it when the plan contains a different course.
    stale = "강아지 코스가 완성됐어요! 위 카드 버튼을 눌러보세요."
    plan = build_course_plan(
        requested_name="서울역", shape="dog", exact=actual if actual_shape == "dog" else None,
        shape_matches=[],
        animal_matches=[PresetMatch(actual, 0)] if actual_shape else [],
        standard=actual if actual_shape is None else None,
    )
    plan = replace(plan, alternatives=_three_course_plan(actual).alternatives)
    monkeypatch.setattr(server, "_animal_course_plan", lambda *args: plan)
    if widget_fails:
        def broken_builder(*args, **kwargs):
            raise WidgetBuildError("renderer contract unavailable")
        monkeypatch.setattr(server, "build_course_widget", broken_builder)
    result = server._course_tool_result(
        stale, course_type="dog", request={"location": "서울역"}, timeout_s=5)
    selection = result.structuredContent["course_selection"]
    primary = selection["primary"]
    final_text = result.structuredContent["assistant_final_text"]
    cid = encode_course_id(actual.params)

    assert selection["requested_course_type"] == "dog"
    assert selection["primary_matches_requested_shape"] is (actual_shape == "dog")
    assert selection["requested_shape_offered"] is (actual_shape == "dog")
    assert primary["course_type"] == (actual_shape or "standard")
    assert primary["course_id"] == cid
    assert primary["title"] in final_text
    assert primary["shape_label"] in final_text
    assert "9.0km · 약 63분" in final_text
    assert f"]({server.BASE_URL}/c/{cid})" in final_text
    assert result.content[-1].text == final_text
    assert final_text.endswith(server.COURSE_EDIT_NOTICE)
    assert result.structuredContent["assistant_final_text_verbatim"] is True
    assert stale not in "\n".join(c.text for c in result.content)
    assert "완성" not in final_text and "위 카드" not in final_text
    if actual_shape != "dog":
        assert f"강아지 모양 대신 {primary['shape_label']}" in final_text
    if widget_fails:
        assert primary["title"] in result.content[0].text
        assert primary["map_url"] in result.content[0].text
        assert result.content[0].text.startswith(plan.lead)
        assert result.content[0].text.endswith(server.COURSE_EDIT_NOTICE)
        assert result.structuredContent["assistant_text_in_widget"] is False
    else:
        payload = json.loads(result.content[0].text)
        assert payload["widget"]["type"] == "Card"
        assert not _components(payload["widget"], "Markdown")
        assert primary["title"] in [c["value"] for c in _components(payload, "Text")]
        target = _components(payload, "Button")[0]["onClickAction"]["payload"]["target"]
        assert target["url"] == primary["map_url"]


def test_fallback_keeps_all_selected_courses_and_does_not_confuse_primary_with_alternative(monkeypatch):
    from runart.animal_presets import PresetMatch
    from runart.courseplan import build_course_plan

    rabbit = _course(shape="rabbit")
    dog = _course(location_name="역삼역", shape="dog")
    plan = build_course_plan(
        requested_name="강남역", shape="dog", exact=None, shape_matches=[],
        animal_matches=[PresetMatch(rabbit, 0), PresetMatch(dog, 900)], standard=None,
    )
    monkeypatch.setattr(server, "_animal_course_plan", lambda *args: plan)
    monkeypatch.setattr(server, "_plan_widget", lambda *args, **kwargs: None)
    result = server._planned_course_result(
        "강아지를 그렸어요", course_type="dog",
        request={"location": "강남역", "allow_nearby_start": True}, timeout_s=5)
    selection = result.structuredContent["course_selection"]

    assert selection["primary_matches_requested_shape"] is False
    assert selection["requested_shape_offered"] is True
    assert selection["primary"]["course_type"] == "rabbit"
    assert selection["alternatives"][0]["course_type"] == "dog"
    for course in [selection["primary"], *selection["alternatives"]]:
        assert course["map_url"] in result.content[0].text
    assert "강아지 모양 대신 토끼 모양" in result.structuredContent["assistant_final_text"]


def test_cached_unplanned_result_also_names_the_actual_shape():
    rabbit = _course(shape="rabbit")
    course_id = encode_course_id(rabbit.params)
    server._cache_put(course_id, rabbit)
    stale = f"강아지 코스를 그렸어요.\n## 코스\n- 지도: {server.BASE_URL}/c/{course_id}"
    result = server._course_tool_result(stale, course_type="dog")

    assert result.structuredContent["course_selection"]["primary"]["course_type"] == "rabbit"
    assert "강아지 모양 대신 토끼 모양" in result.structuredContent["assistant_final_text"]
    assert "강아지 코스를 그렸어요" not in "\n".join(c.text for c in result.content)


@pytest.mark.parametrize("widget_fails", [False, True])
def test_cached_same_shape_cannot_reuse_a_stale_requested_region_claim(monkeypatch, widget_fails):
    course = _course(location_name="서울숲", shape=None)
    cid = encode_course_id(course.params)
    server._cache_put(cid, course)
    stale = f"강남구에서 공원을 포함하는 코스로 잡아봤어요.\n## 코스\n{server.BASE_URL}/c/{cid}"
    if widget_fails:
        def broken(*args, **kwargs):
            raise WidgetBuildError("unavailable")
        monkeypatch.setattr(server, "build_course_widget", broken)
    result = server._course_tool_result(stale, course_type="standard")
    assert "강남구" not in "\n".join(c.text for c in result.content)
    assert result.structuredContent["course_selection"]["actual_start_names"] == ["서울숲"]
    assert "서울숲" in result.structuredContent["assistant_final_text"]
    assert result.content[-1].text.endswith(server.COURSE_EDIT_NOTICE)
