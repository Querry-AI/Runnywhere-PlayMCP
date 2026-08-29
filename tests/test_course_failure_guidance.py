"""Evidence-backed failure, timeout, and selectable-relaxation contracts."""

import asyncio

from runart import server
from runart.animal_presets import PresetMatch
from runart.course import Course
from runart.models import CourseParams


COMPLEX_WANGSIMNI = {
    "course_type": "dog",
    "location": "왕십리역",
    "distance_km": 2,
    "strict_distance": True,
    "include_hills": False,
    "night_mode": True,
    "need_facilities": ["restroom"],
}


def test_complex_failure_uses_real_candidates_and_replayable_options():
    result = server.create_seoul_running_course(**COMPLEX_WANGSIMNI)

    assert result.isError
    assert result.structuredContent["result_code"] == "constraint_mismatch"
    assert result.structuredContent["conditions_satisfied"] is False
    assert len(result.content) == 1
    assert '"widget"' not in result.content[0].text
    assert "지도 보기를 열어 경로를 직접 편집" in result.content[0].text
    assert "정확히 2km(허용 오차 ±50m)" in result.content[0].text
    assert "야간 조명" in result.content[0].text

    options = result.structuredContent["relaxation_options"]
    assert options
    for option in options:
        arguments = option["arguments"]
        assert arguments["course_type"] == "dog"
        assert arguments["location"] == "왕십리역"
        assert arguments["strict_distance"] is True
        assert arguments["need_facilities"] == ["restroom"]
        replay = server.create_seoul_running_course(**arguments)
        assert not replay.isError, replay.content[0].text
        assert replay.structuredContent["conditions_satisfied"] is True
        assert replay.structuredContent["course_selection"]["actual_start_names"]


def test_no_candidate_evidence_never_invents_a_failed_condition():
    request = {
        "location": "왕십리역", "distance_km": 2, "night_mode": True,
        "need_facilities": ["restroom"], "_resolved": (
            37.5613, 127.0374, "왕십리역"),
    }
    result = server._failure_result(request, "dog")

    assert result.structuredContent["result_code"] == "no_candidate_evidence"
    assert result.structuredContent["relaxation_options"] == []
    assert "야간 조명이 부족" not in result.content[0].text
    assert "근거가 없어 임의로 제안하지 않을게요" in result.content[0].text
    assert "코스가 생성되면 지도 보기를 열어" in result.content[0].text


def test_timeout_does_not_claim_a_constraint_failed_or_emit_a_widget():
    result = server._timeout_result({"location": "왕십리역"}, "dog")

    assert result.isError and result.structuredContent["retryable"] is True
    assert result.structuredContent["result_code"] == "generation_timeout"
    assert result.structuredContent["relaxation_options"] == []
    assert "조건 충족 여부를 확인하지 못했어요" in result.content[0].text
    assert "조명이 부족" not in result.content[0].text
    assert '"widget"' not in result.content[0].text


def _course_at_length(length_km: float) -> Course:
    params = CourseParams(
        lat=37.5613, lon=127.0374, location_name="왕십리역",
        distance_km=length_km, shape="dog")
    return Course(
        params=params, path=[1, 2, 1],
        points=[(37.5613, 127.0374), (37.562, 127.038), (37.5613, 127.0374)],
        length_m=length_km * 1000, ascent_m=0, rfs={})


def test_strict_and_ordinary_distance_tolerances_are_real_hard_gates():
    base = {"distance_km": 2, "_resolved": (37.5613, 127.0374, "왕십리역")}

    assert server._eligible_matches(
        [PresetMatch(_course_at_length(2.05), 0)],
        {**base, "strict_distance": True}, "dog")
    assert not server._eligible_matches(
        [PresetMatch(_course_at_length(2.051), 0)],
        {**base, "strict_distance": True}, "dog")
    assert server._eligible_matches(
        [PresetMatch(_course_at_length(2.20), 0)],
        {**base, "strict_distance": False}, "dog")
    assert not server._eligible_matches(
        [PresetMatch(_course_at_length(2.201), 0)],
        {**base, "strict_distance": False}, "dog")


def test_unified_schema_exposes_strict_distance_only_on_the_creation_tool():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    field = tools["create_seoul_running_course"].inputSchema["properties"]["strict_distance"]
    assert field["default"] is False
    assert "정확히" in field["description"] and "2.5%" in field["description"]
    assert len(tools["create_seoul_running_course"].description) <= 900
