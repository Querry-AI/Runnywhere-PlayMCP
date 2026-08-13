import json
import asyncio

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from runart.course import CourseError, generate_course
from runart.models import CourseParams, CourseWaypoint, decode_course_id, encode_course_id
from runart.render import preview_html
from runart import server


CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="서울시청")


def test_exact_manual_path_roundtrip():
    original = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    params = original.params.model_copy(update={"manual_path": original.path})
    restored = decode_course_id(encode_course_id(params))
    assert restored.manual_path == original.path


def test_manual_waypoints_roundtrip_in_order():
    params = CourseParams(
        **CITY_HALL,
        distance_km=5.0,
        manual_waypoints=[
            CourseWaypoint(lat=37.5700, lon=126.9820),
            CourseWaypoint(lat=37.5610, lon=126.9860),
        ],
    )
    restored = decode_course_id(encode_course_id(params))
    assert restored.manual_waypoints == params.manual_waypoints


def test_animal_course_can_be_used_as_an_edit_template():
    params = CourseParams(
        **CITY_HALL,
        shape="dog",
        manual_waypoints=[
            CourseWaypoint(lat=37.57, lon=126.98),
            CourseWaypoint(lat=37.56, lon=126.99),
        ],
    )
    assert params.shape == "dog"


def test_manual_waypoints_require_at_least_two_and_at_most_six():
    point = CourseWaypoint(lat=37.57, lon=126.98)
    with pytest.raises(ValidationError):
        CourseParams(**CITY_HALL, manual_waypoints=[point])
    with pytest.raises(ValidationError):
        CourseParams(
            **CITY_HALL,
            manual_waypoints=[
                CourseWaypoint(lat=37.55 + i * 0.001, lon=126.98)
                for i in range(7)
            ],
        )


def test_manual_course_rejects_waypoint_too_far_from_start():
    params = CourseParams(
        **CITY_HALL,
        distance_km=5,
        manual_waypoints=[
            CourseWaypoint(lat=37.65, lon=126.98),
            CourseWaypoint(lat=37.66, lon=126.98),
        ],
    )
    with pytest.raises(CourseError, match="출발점"):
        generate_course(params)


def test_manual_course_is_deterministic_and_closed():
    params = CourseParams(
        **CITY_HALL,
        distance_km=5,
        manual_waypoints=[
            CourseWaypoint(lat=37.5700, lon=126.9820),
            CourseWaypoint(lat=37.5610, lon=126.9860),
        ],
    )
    first = generate_course(params)
    second = generate_course(params)
    assert first.points[0] == first.points[-1]
    assert first.path == second.path
    assert first.length_m == second.length_m


def _json_request(course_id: str, payload: dict, content_type="application/json"):
    body = json.dumps(payload).encode()
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/c/{course_id}/edit",
            "path_params": {"course_id": course_id},
            "headers": [(b"content-type", content_type.encode())],
        },
        receive,
    )


def test_edit_endpoint_saves_exact_path_as_new_stateless_url():
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    request = _json_request(
        cid,
        {"action": "save", "path": source.path},
    )
    response = asyncio.run(server.edit_course_route(request))
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["preview_url"].endswith("/c/" + payload["course_id"])
    assert decode_course_id(payload["course_id"]).manual_path == source.path


def test_edit_endpoint_snaps_a_drawn_segment_to_walkable_edges():
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "snap",
        "path": source.path,
        "from_index": 10,
        "to_index": 40,
        "stroke": [
            {"lat": source.points[10][0], "lon": source.points[10][1]},
            {"lat": source.points[40][0], "lon": source.points[40][1]},
        ],
    })))
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["path"][0][0] == source.path[0]
    assert payload["path"][-1][0] == source.path[-1]
    assert all(len(point) == 3 for point in payload["path"])


def test_edit_endpoint_separates_bad_course_id_from_bad_payload():
    bad_id = _json_request("not-a-course", {"action": "save", "path": [1, 2, 1]})
    response = asyncio.run(server.edit_course_route(bad_id))
    assert response.status_code == 404
    good_id = encode_course_id(CourseParams(**CITY_HALL))
    bad_payload = _json_request(good_id, {"action": "save", "path": []})
    response = asyncio.run(server.edit_course_route(bad_payload))
    assert response.status_code == 400


def test_edit_endpoint_converts_animal_course_to_direct_edit():
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params.model_copy(update={"shape": "dog"}))
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "save", "path": source.path,
    })))
    payload = json.loads(response.body)
    edited = decode_course_id(payload["course_id"])
    assert response.status_code == 200
    assert edited.shape is None
    assert edited.manual_path == source.path
    assert decode_course_id(cid).shape == "dog"


def test_mobile_preview_uses_compact_summary_and_accessible_edit_controls():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert 'class="course-metrics"' in page
    # 2x2 on mobile: auto-fit squeezed four cells into ~96px each, and the UA
    # margin on dt/dd then clipped values like "32~40분" out of the cell.
    assert 'repeat(2,minmax(0,1fr))' in page
    assert '.course-metrics dt,.course-metrics dd{margin:0}' in page
    assert '내 위치 추적 시작' in page
    assert 'aria-label="수정한 코스를 새 코스로 저장"' in page
    assert 'aria-live="polite"' in page
    assert 'AbortController' in page and '3500' in page
    assert "action:'snap'" in page
    assert "action:'save'" in page
    assert 'id="drawTool"' in page
    assert 'id="eraseTool"' in page
    assert 'id="editUndo"' in page
    assert 'id="editRedo"' not in page
    assert 'class="edit-tool-circle"' in page
    assert '.edit-tools{position:absolute;z-index:950;left:10px;top:10px' in page
    assert 'width:40px;height:40px' in page
    assert 'edit-overlay' in page
    assert 'body.editing .map-hud' in page
    assert 'body.editing.tool-active .facility-marker' in page
    assert "map.setDraggable(!editMode)" in page
    assert 'id="editBar" class="sr-only"' in page
    assert 'class="edit-bar"' not in page
    assert 'body.editing .edit-bar' not in page


def test_animal_preview_explains_save_as_new_editing():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    course.params = course.params.model_copy(update={"shape": "dog"})
    page = preview_html(course, [], "https://runnywhere.example")
    assert 'id="editRoute"' in page
    assert "원본 동물 코스는 유지" in page
    assert "직접 편집한 코스" in page


def test_preview_keeps_a_local_course_editor_available_without_map_sdk():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert "initLocalCourseEditor" in page
    assert "로컬 코스 편집 체험" in page
    assert "SVGPoint" in page
    assert "펜이나 지우개를 선택하세요" in page
    assert 'id="localEditRoute"' in page
    assert 'id="localEditSave"' in page
