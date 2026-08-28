"""A nearby recommendation must not claim to start at the requested station."""

import asyncio
import json

import httpx
import pytest

from runart import server
from runart.animal_presets import PresetMatch
from runart.course import Course
from runart.courseplan import build_course_plan
from runart.models import CourseParams, encode_course_id
from runart.widget import WIDGET_MAX_BYTES, WidgetTooLargeError


def _course(name, variant=0):
    return Course(
        params=CourseParams(lat=37.56, lon=127.03, location_name=name,
                            shape="dog", distance_km=9, route_variant=variant),
        path=[], points=[], length_m=9000, ascent_m=30,
        rfs={"score": 86, "highlights": []}, shape_similarity=.91,
    )


def _plan(*, offsets=(1500, 1900), exact=False, requested="왕십리역"):
    return build_course_plan(
        requested_name=requested, shape="dog",
        exact=_course(requested) if exact else None,
        shape_matches=[PresetMatch(_course(name, i), distance)
                       for i, (name, distance) in enumerate(zip(("신당역", "청구역"), offsets))],
        animal_matches=[], standard=None,
    )


def _visible_texts(component):
    if isinstance(component, dict):
        if component.get("type") in {"Text", "Caption", "Title"}:
            yield component["value"]
        for child in component.get("children", []):
            yield from _visible_texts(child)


def _assert_disclosed(result, requested="왕십리역"):
    metadata = result.structuredContent
    selection = metadata["course_selection"]
    notice = selection["start_change_notice"]
    assert selection["requested_start"] == requested
    assert selection["requested_start_offered"] is False
    assert requested + " 출발 코스가 아닌" in notice
    assert "신당역" in notice and "청구역" in notice and "출발 대안" in notice
    assert metadata["assistant_final_text"].startswith(notice)
    assert result.content[-1].text == metadata["assistant_final_text"]
    assert metadata["assistant_final_text"].endswith(server.COURSE_EDIT_NOTICE)
    assert "왕십리역 출발 기준으로" not in metadata["assistant_final_text"]
    return notice


def test_nearby_notice_is_visible_in_widget_final_text_and_share_copy():
    plan = _plan()
    result = server._plan_result(plan, "dog")
    notice = _assert_disclosed(result)
    payload = json.loads(result.content[0].text)
    assert notice in list(_visible_texts(payload["widget"]))
    assert notice in payload["copy_text"]
    assert payload["widget"]["children"][-1]["value"] == server.COURSE_EDIT_NOTICE
    assert len(result.content[0].text.encode()) < WIDGET_MAX_BYTES
    facts = result.structuredContent["course_selection"]
    assert [c["course_id"] for c in [facts["primary"], *facts["alternatives"]]] == [
        encode_course_id(c.course.params) for c in (plan.primary, *plan.alternatives)]
    assert facts["primary"]["start_offset_m"] == 1500
    assert facts["primary"]["is_start_alternative"] is True


@pytest.mark.parametrize("fallback", ["disabled", "too_large"])
def test_markdown_fallback_keeps_the_same_start_disclosure(monkeypatch, fallback):
    if fallback == "disabled":
        monkeypatch.setattr(server, "KAKAO_WIDGETS_ENABLED", False)
    else:
        def too_large(*args, **kwargs):
            raise WidgetTooLargeError("test")
        monkeypatch.setattr(server, "build_course_widget", too_large)
    result = server._plan_result(_plan(), "dog")
    notice = _assert_disclosed(result)
    assert notice in result.content[0].text


def test_exact_primary_with_nearby_alternatives_does_not_deny_the_exact_start():
    result = server._plan_result(_plan(exact=True), "dog")
    selection = result.structuredContent["course_selection"]
    assert selection["requested_start_offered"] is True
    assert selection["primary"]["is_start_alternative"] is False
    notice = selection["start_change_notice"]
    assert "왕십리역 출발 코스와" in notice
    assert "신당역" in notice and "청구역" in notice and "출발 대안" in notice
    assert "출발 코스가 아닌" not in notice
    assert result.structuredContent["assistant_final_text"].startswith(notice)


@pytest.mark.parametrize("offset,relocated", [(0, False), (149.9, False), (150, True)])
def test_start_disclosure_uses_station_exit_tolerance(offset, relocated):
    result = server._plan_result(_plan(offsets=(offset,)), "dog")
    selection = result.structuredContent["course_selection"]
    assert selection["primary"]["is_start_alternative"] is relocated
    assert bool(selection["start_change_notice"]) is relocated
    if not relocated:
        assert "출발 대안" not in result.structuredContent["assistant_final_text"]


def test_same_station_label_does_not_hide_a_measured_start_change():
    plan = build_course_plan(requested_name="왕십리역", shape="dog", exact=None,
        shape_matches=[PresetMatch(_course("왕십리역"), 1500)],
        animal_matches=[], standard=None)
    result = server._plan_result(plan, "dog")
    notice = result.structuredContent["course_selection"]["start_change_notice"]
    assert "출발 위치가 다른" in notice
    assert "출발 코스가 아닌, 왕십리역" not in notice


def test_start_disclosure_is_bounded_plain_text_without_line_clamping():
    requested = "<왕십리역> **요청** " + "가" * 90
    result = server._plan_result(_plan(requested=requested), "dog")
    notice = result.structuredContent["course_selection"]["start_change_notice"]
    assert "<" not in notice and "**" not in notice
    payload = json.loads(result.content[0].text)
    component = next(c for c in payload["widget"]["children"] if c.get("value") == notice)
    assert "maxLines" not in component
    assert len(result.content[0].text.encode()) < WIDGET_MAX_BYTES


@pytest.fixture(scope="module")
def warmed_server():
    server._warm()


@pytest.mark.parametrize("tool", ["create_seoul_running_course", "generate_animal_course"])
def test_wangsimni_dog_reproduction_discloses_actual_starts(warmed_server, tool):
    if tool == "create_seoul_running_course":
        result = server.create_seoul_running_course(location="왕십리역", course_type="dog")
    else:
        result = server._legacy_generate_animal_course(location="왕십리역", shape="dog")
    assert result.structuredContent["result_code"] == "nearby_course_ready"
    notice = _assert_disclosed(result)
    payload = json.loads(result.content[0].text)
    assert notice in list(_visible_texts(payload["widget"]))


def test_mcp_http_preserves_disclosure_in_both_text_and_widget(warmed_server, monkeypatch):
    # FastMCP's lifespan is one-shot; another module also exercises this server.
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    async def check():
        app = server.mcp.streamable_http_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url="http://localhost:8000",
                    headers={"Accept": "application/json, text/event-stream"}) as client:
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call", "params": {"name": "create_seoul_running_course",
                        "arguments": {"location": "왕십리역", "course_type": "dog"}}})
                assert response.status_code == 200
                result = response.json()["result"]
                metadata = result["structuredContent"]
                notice = metadata["course_selection"]["start_change_notice"]
                assert metadata["assistant_final_text"].startswith(notice)
                assert result["content"][-1]["text"].startswith(notice)
                payload = json.loads(result["content"][0]["text"])
                assert notice in list(_visible_texts(payload["widget"]))
    asyncio.run(check())
