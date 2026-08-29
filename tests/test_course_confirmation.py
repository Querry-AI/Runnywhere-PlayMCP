"""No relocated routes are delivered before a user's explicit choice."""
from dataclasses import replace

import pytest

from runart import server
from test_course_start_disclosure import _plan


def _result(monkeypatch, request=None, *, exact=False, course_type="dog"):
    monkeypatch.setattr(server, "_animal_course_plan", lambda *a: _plan(exact=exact))
    return server._planned_course_result("", course_type=course_type,
        request=request or {"location": "왕십리역"}, timeout_s=10)


@pytest.mark.parametrize("widgets", [True, False])
def test_unconfirmed_nearby_is_question_only(monkeypatch, widgets):
    monkeypatch.setattr(server, "KAKAO_WIDGETS_ENABLED", widgets)
    result = _result(monkeypatch)
    assert result.structuredContent["result_code"] == "start_change_confirmation_required"
    assert not result.isError
    assert len(result.content) == 1
    assert "왕십리역" in result.content[0].text
    assert "강아지" in result.content[0].text
    assert "일반 러닝 코스" in result.content[0].text
    assert "widget" not in result.content[0].text
    assert "/c/" not in result.content[0].text
    assert "course_selection" not in result.structuredContent


def test_followup_options_preserve_explicit_conditions(monkeypatch):
    request = dict(location="왕십리역", distance_km=9, duration_min=60,
                   include_hills=False, night_mode=True, need_facilities=["restroom"],
                   _resolved=(37.5, 127, "왕십리역"))
    result = _result(monkeypatch, request)
    options = result.structuredContent["confirmation_options"]
    for option in options:
        args = option["arguments"]
        assert option["tool"] == "create_seoul_running_course"
        for key in request.keys() - {"_resolved"}:
            assert args[key] == request[key]
        assert "_resolved" not in args
    assert options[0]["arguments"]["course_type"] == "dog"
    assert options[0]["arguments"]["allow_nearby_start"] is True
    assert options[1]["arguments"]["course_type"] == "standard"
    assert options[1]["arguments"]["allow_nearby_start"] is False


def test_confirmed_nearby_delivers_disclosed_widget(monkeypatch):
    result = _result(monkeypatch, {"location": "왕십리역", "allow_nearby_start": True})
    assert result.structuredContent["result_code"] == "nearby_course_ready"
    assert "widget" in result.content[0].text
    assert "출발 대안" in result.structuredContent["assistant_final_text"]


def test_exact_results_do_not_include_unapproved_nearby_cards(monkeypatch):
    result = _result(monkeypatch, exact=True)
    selection = result.structuredContent["course_selection"]
    assert selection["returned_count"] == 1
    assert selection["primary"]["is_start_alternative"] is False
    assert not selection["alternatives"]


def test_catalogue_plan_without_point_origin_is_unchanged(monkeypatch):
    monkeypatch.setattr(server, "_animal_course_plan", lambda *a: replace(_plan(), requested_start=None))
    result = server._planned_course_result("", course_type="dog", request={}, timeout_s=10)
    assert result.structuredContent["result_code"] != "start_change_confirmation_required"


def test_standard_question_does_not_offer_itself_again(monkeypatch):
    result = _result(monkeypatch, course_type="standard")
    assert len(result.structuredContent["confirmation_options"]) == 1
    assert "2." not in result.content[0].text


def test_coordinate_origin_is_preserved_in_followup(monkeypatch):
    result = _result(monkeypatch, {"lat": 37.56, "lon": 127.03})
    for option in result.structuredContent["confirmation_options"]:
        assert option["arguments"]["lat"] == 37.56
        assert option["arguments"]["lon"] == 127.03
        assert "location" not in option["arguments"]


def test_nearby_permission_does_not_invent_a_missing_start():
    result = server.create_seoul_running_course(course_type="dog", allow_nearby_start=True)
    assert result.structuredContent["result_code"] == "missing_start"


def test_public_schema_defaults_to_no_consent():
    import asyncio
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "create_seoul_running_course")
    consent = tool.inputSchema["properties"]["allow_nearby_start"]
    assert consent["default"] is False
    assert consent["type"] == "boolean"
    assert "명시적으로 선택" in consent["description"]
    assert "NEXT user" in tool.description
    assert "ambiguous yes" in tool.description


def test_confirmation_does_not_relax_hard_constraints(monkeypatch):
    captured = []
    def no_candidates(request, *args):
        captured.append(request)
        return None
    monkeypatch.setattr(server, "_animal_course_plan", no_candidates)
    request = dict(location="왕십리역", allow_nearby_start=True, distance_km=2,
                   night_mode=True, need_facilities=["water"])
    result = server._course_tool_result("", course_type="dog", request=request, timeout_s=10)
    assert captured == [request]
    assert result.isError
    assert result.structuredContent["result_code"] == "no_candidate_evidence"
