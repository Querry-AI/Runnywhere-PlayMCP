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


def test_edit_endpoint_caches_new_version_without_overwriting_original():
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    original_id = encode_course_id(source.params)
    server._cache_put(original_id, source)

    response = asyncio.run(server.edit_course_route(_json_request(original_id, {
        "action": "save", "path": source.path,
    })))
    payload = json.loads(response.body)
    edited_id = payload["course_id"]

    with server._CACHE_LOCK:
        cached_original = server._course_cache.get(original_id)
        cached_edited = server._course_cache.get(edited_id)
    assert response.status_code == 200
    assert cached_original is source
    assert cached_edited is not None
    assert cached_edited.params.manual_path == source.path
    assert cached_edited.params.shape is None
    assert edited_id != original_id


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
    assert "map.panBy(panCenter.x-next.x,panCenter.y-next.y)" in page
    assert 'class="edit-bar"' not in page
    assert 'body.editing .edit-bar' not in page


def _hidden_text(page: str) -> str:
    """Concatenated text of every element carrying the sr-only class.

    Substring checks alone cannot tell "the page says X" from "the page says X
    where nobody can read it" -- the sr-only edit bar passed such checks for two
    commits while showing users nothing.
    """
    from html.parser import HTMLParser

    class Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.depth = 0
            self.stack: list[bool] = []
            self.text: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in ("br", "img", "input", "meta", "link"):
                return
            classes = dict(attrs).get("class", "") or ""
            self.stack.append("sr-only" in classes.split())
            if any(self.stack):
                self.depth = 1

        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()

        def handle_data(self, data):
            if any(self.stack):
                self.text.append(data)

    parser = Collector()
    parser.feed(page)
    return " ".join(parser.text)


def test_edit_feedback_is_visible_not_only_screen_reader_text():
    """F-01 regression guard: edit status must not live in an sr-only node."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    # The toast is the single feedback channel: visible *and* announced.
    assert 'id="editToast"' in page
    assert 'role="status" aria-live="polite"' in page
    assert 'class="edit-toast"' in page
    assert 'id="editToastText"' in page
    # ...and the old invisible bar is gone for good.
    assert 'id="editBar"' not in page
    assert "editStatus" not in page
    assert "펜이나 지우개를 선택하세요" not in _hidden_text(page)


def test_edit_toast_distinguishes_blocking_errors_from_transient_hints():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    # Errors and blocked states must not auto-dismiss; hints must.
    assert "AUTO_DISMISS_MS" in page
    assert "busy:0" in page and "error:0" in page and "blocked:0" in page
    assert "info:3600" in page
    assert '.edit-toast[data-tone="error"]' in page
    assert '.edit-toast[data-tone="blocked"]' in page
    # Errors carry a dismiss control rather than lingering forever.
    assert "label:'닫기'" in page


def test_edit_shows_live_distance_from_the_snap_response():
    """F-03 regression guard: the server returns length_km; the UI must use it."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert 'id="editDistance"' in page
    assert "setEditDistance(payload.length_km)" in page
    assert "body.editing .edit-distance{display:flex}" in page
    # Undo has to restore the distance that came back with that path.
    assert "km:editLengthKm" in page


def test_edit_blocks_duplicate_requests_while_one_is_in_flight():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert "setEditBusy" in page
    assert "editBusy" in page
    # every entry point guards on the busy flag
    assert "if(editBusy)return" in page                        # setMode
    assert "if(editBusy){clearGesture();return;}" in page      # finishGesture
    assert "if(!editing||editBusy)return" in page              # save
    assert "!editing||!editMode||editBusy" in page             # pointerdown


def test_edit_toolbar_keeps_small_icon_only_buttons():
    """Product requirement: small top-left icons, no text labels."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert "width:40px;height:40px" in page
    assert '.edit-tools{position:absolute;z-index:950;left:10px;top:10px' in page
    # Icon-only: labels stay in aria-label/title, never as visible text nodes.
    for label in ("펜으로 코스 선 그리기", "직선 지우개로 코스 구간 지우기",
                  "마지막 선 수정 되돌리기", "원본 코스로 복구", "수정한 코스를 새 코스로 저장"):
        assert f'aria-label="{label}"' in page


def test_edit_tools_have_44px_tap_targets_without_growing():
    """F-07: the icons stay 40px by product requirement, so the touch area is
    widened invisibly instead of enlarging the buttons."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert "width:40px;height:40px" in page                      # visual size unchanged
    assert ".edit-tool-circle::before" in page
    assert "top:-2px;right:-2px;bottom:-2px;left:-2px" in page   # 40 + 2 + 2 = 44
    # 12px facility dots are unhittable by finger, but only widen them on touch:
    # a 44px box under a mouse would swallow map drags near a marker.
    assert "@media (pointer:coarse)" in page
    assert ".facility-marker::before" in page
    assert "top:-16px;right:-16px;\n      bottom:-16px;left:-16px" in page  # 12 + 32 = 44


def test_mobile_map_gestures_and_facility_taps_do_not_conflict():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    markers = [{"type": "restroom", "name": "화장실", "at_km": 0.1, "lat": 37.566, "lon": 126.978}]
    page = preview_html(course, markers, "https://runnywhere.example")

    # An active tool owns one finger; two touch pointers pan the map instead of
    # accidentally submitting a stroke.
    assert "event.pointerType==='touch'&&editPointers.size>=2" in page
    assert "twoFingerPan=true" in page
    assert "map.panBy(panCenter.x-next.x,panCenter.y-next.y)" in page
    # Facility markers distinguish a short tap from a drag and use Kakao's own
    # propagation guard rather than relying on DOM bubbling alone.
    assert "const FACILITY_TAP_SLOP = 8" in page
    assert "kakao.maps.event.preventMap" in page
    assert "if(!facilityPointer.moved)toggle()" in page
    assert "map.panBy(facilityPointer.lastX-ev.clientX" in page
    assert "clickable:true" in page
    assert "kakao.maps.event.addListener(map, 'click', closePop)" in page


def test_edit_projection_uses_container_coordinates():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert "projection.containerPointFromCoords" in page
    assert "projection.coordsFromContainerPoint" in page


def test_revert_is_undoable_and_separated_from_save():
    """F-04: reverting is the only irreversible action, so it is made
    reversible rather than guarded by a map-covering confirm sheet."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert "label:'실행 취소'" in page
    assert "원본 코스로 되돌렸어요." in page
    assert "const hadEdits=" in page          # no undo offer when nothing was discarded
    assert "setEditDistance(initialLengthKm)" in page
    # mis-tap guard: extra room between revert and the confirming action
    assert ".edit-tool-circle.save{margin-left:8px" in page
    # and no confirmation dialog was introduced
    assert "confirm(" not in page


def test_revert_icon_is_not_a_house():
    """A home glyph on a destructive control reads as 'go to homepage'."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert 'aria-label="원본 코스로 복구"' in page
    assert "M6.5 10v9h11v-9" not in page       # old roof/door house path
    assert "M12 7v5l4 2" in page               # history glyph: clock hands


def test_zoom_control_is_removed_while_editing():
    """F-10: setZoomable(false) only stops wheel/pinch; a zoom press mid-gesture
    would move the ground under the screen-space stroke being collected."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert "const zoomControl = new kakao.maps.ZoomControl()" in page
    assert "map.removeControl(zoomControl)" in page
    assert "showZoomControl(!value)" in page


def test_detail_panels_follow_an_edit():
    """The panels under the map describe the course; once the course changes
    they must follow, or the page shows one route and describes another."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    # every element the summary rewrites has to be addressable
    for element_id in ("courseTitle", "courseBadges", "mLength", "mDuration", "mAscent",
                       "mRfs", "mRfsGrade", "courseHighlights", "factSignals",
                       "factStores", "factRestrooms", "facilityTally", "facilityList"):
        assert f'id="{element_id}"' in page, element_id
    assert "applySummary(payload.summary)" in page
    assert "const initialSummary =" in page
    # undo and revert restore the panels too, not just the line
    assert "applySummary(state.summary)" in page
    assert "applySummary(initialSummary)" in page


def test_edit_summary_matches_what_a_full_page_load_shows():
    """course_edit_summary() feeds the live update; if it drifts from
    preview_html() the page would change on save for no reason."""
    from runart.facilities import facilities_along
    from runart.naming import course_title
    from runart.render import course_edit_summary, route_points

    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    summary = course_edit_summary(course)
    facilities = [f for f in facilities_along(route_points(course),
                                              ["convenience_store", "restroom"], limit=80)]
    page = preview_html(course, facilities, "https://runnywhere.example")

    assert summary["title"] == course_title(course)
    assert summary["length_km"] == round(course.length_km, 2)
    assert summary["ascent_m"] == round(course.ascent_m)
    assert summary["rfs"] == course.rfs["score"]
    assert summary["facility_tally"] in page
    assert f'{course.rfs["score"]}/100' in page


def test_editing_offers_an_explicit_map_pan_tool():
    """Two-finger panning exists but nothing on screen says so; one-finger drag
    is the gesture people reach for."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert 'id="panTool"' in page
    assert 'aria-label="지도 이동"' in page
    assert "setMode('pan')" in page
    # pan is the default on entering edit mode, and it leaves the map draggable
    assert 'id="panTool" class="edit-tool-circle" type="button" aria-label="지도 이동" title="지도 이동" aria-pressed="true"' in page
    assert "map.setDraggable(!editMode)" in page


def test_drawing_overlay_is_absent_unless_a_drawing_tool_is_active():
    """A full-bleed touch-action:none layer over the map is exactly what eats a
    drag on mobile -- keep it out of the tree unless it is being drawn on."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert "touch-action:none;pointer-events:none;display:none" in page
    assert "body.editing.tool-active .edit-overlay{display:block}" in page


def test_route_decorations_do_not_swallow_map_drags():
    """km bubbles, direction arrows and the start pin are labels, not controls."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert ".km-marker,.dir-marker,.start-marker{pointer-events:none}" in page


def test_map_entry_points_sit_in_the_top_corners():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert ">코스 편집</button>" in page
    assert "코스 선 수정" not in page
    assert "#editRoute{position:absolute;z-index:540;left:14px;top:14px" in page
    assert ".view-toggle{position:absolute;z-index:530;right:14px;top:14px" in page
    # the metric pills move below them rather than competing for the corners
    assert ".map-hud{position:absolute;z-index:500;left:14px;right:14px;top:64px" in page


def test_course_title_and_badges_share_a_row():
    course = generate_course(CourseParams(lat=CITY_HALL["lat"], lon=CITY_HALL["lon"],
                                          distance_km=5.0, location_name="강남대로 401-2"))
    page = preview_html(course, [], "https://runnywhere.example")

    assert "강남대로런" in page and "401-2런" not in page
    assert 'class="course-head"' in page
    assert 'class="course-badges"' in page
    # badges are labelled, never emoji-only
    assert 'role="img" aria-label="일반 러닝 코스"' in page


def test_map_container_is_positioned_for_its_absolute_controls():
    """The HUD, toolbar and toast are absolutely positioned inside #map; do not
    rely on the Kakao SDK setting position:relative at runtime."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert "#map{position:relative" in page


def test_animal_preview_explains_save_as_new_editing():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    course.params = course.params.model_copy(update={"shape": "dog"})
    page = preview_html(course, [], "https://runnywhere.example")
    assert 'id="editRoute"' in page
    assert "원본 동물 코스는 유지" in page
    assert "직접 편집한 코스" in page
    # F-02: the warning has to be shown, not buried in screen-reader-only text.
    assert "원본 동물 코스는 유지" not in _hidden_text(page)
    assert "const editNotice =" in page
    assert "setEditStatus(editNotice" in page


def test_plain_course_has_no_animal_edit_notice():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert 'const editNotice = ""' in page
    assert "원본 동물 코스는 유지" not in page


def test_preview_keeps_a_local_course_editor_available_without_map_sdk():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")

    assert "initLocalCourseEditor" in page
    assert "로컬 코스 편집 체험" in page
    assert "SVGPoint" in page
    assert "펜이나 지우개를 선택하세요" in page
    assert 'id="localEditRoute"' in page
    assert 'id="localEditSave"' in page
