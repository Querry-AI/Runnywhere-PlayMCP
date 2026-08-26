import json
import asyncio

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from runart.course import CourseError, generate_course
from runart.geo import haversine_m
from runart.models import CourseParams, CourseWaypoint, decode_course_id, encode_course_id
from runart.naming import course_title
from runart.render import preview_html
from runart import graph as graphmod
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


def test_edit_endpoint_snaps_and_saves_a_connected_draft_only_on_save():
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "save_draft",
        "path": source.path,
        "from_index": 10,
        "to_index": 40,
        "stroke": [
            {"lat": source.points[10][0], "lon": source.points[10][1]},
            {"lat": source.points[40][0], "lon": source.points[40][1]},
        ],
        "name": "자유 드로잉런",
    })))
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["preview_url"].endswith("/c/" + payload["course_id"])
    saved = decode_course_id(payload["course_id"])
    assert saved.manual_path
    assert saved.custom_name == "자유 드로잉런"


def test_a_draft_only_has_to_touch_the_course_not_the_erased_ends():
    """Requiring the stroke to land within 60m of both ends of the erased gap
    made the commonest edit impossible: rubbing out a spur and redrawing past
    it, where the gap ends are exactly the place you are getting away from.
    What gets replaced is read off wherever the stroke meets the green line."""
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "save_draft",
        "path": source.path,
        "from_index": 10,
        "to_index": 40,
        # Both ends land on the course, nowhere near index 10 or 40, and the
        # middle bulges away from it.
        "stroke": [
            {"lat": source.points[20][0], "lon": source.points[20][1]},
            {"lat": source.points[22][0] + 0.0012, "lon": source.points[22][1] + 0.0012},
            {"lat": source.points[25][0], "lon": source.points[25][1]},
        ],
    })))
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert "preview_url" in payload
    saved = decode_course_id(payload["course_id"])
    assert saved.manual_path[0] == saved.manual_path[-1]
    # The erased gap is folded into the replaced span, so it cannot survive.
    assert saved.manual_path != source.path
    assert source.path[10:40] != saved.manual_path[10:40]


def test_a_draft_saves_even_with_no_erased_span_at_all():
    """A drawn line on its own is a complete instruction."""
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "save_draft",
        "path": source.path,
        "stroke": [
            {"lat": source.points[i][0], "lon": source.points[i][1]}
            for i in (12, 16, 20, 24)
        ],
    })))
    assert response.status_code == 200
    assert "preview_url" in json.loads(response.body)


def test_editing_never_refuses_the_runner():
    """Free editing is the point of the screen. Every shortfall degrades to
    the best course still buildable and explains itself in `note`; none of
    them comes back as a refusal."""
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    lat, lon = source.points[20]
    strokes = {
        "far from any road": [{"lat": lat + 0.004, "lon": lon + 0.004},
                              {"lat": lat + 0.005, "lon": lon + 0.005}],
        "a single repeated point": [{"lat": lat, "lon": lon},
                                    {"lat": lat, "lon": lon}],
        "drawn backwards": [{"lat": source.points[i][0], "lon": source.points[i][1]}
                            for i in (30, 26, 22, 18)],
        "wandering off and back": [{"lat": lat, "lon": lon},
                                   {"lat": lat + 0.003, "lon": lon},
                                   {"lat": source.points[28][0], "lon": source.points[28][1]}],
    }
    for label, stroke in strokes.items():
        response = asyncio.run(server.edit_course_route(_json_request(cid, {
            "action": "save_draft", "path": source.path,
            "from_index": 10, "to_index": 40, "stroke": stroke,
        })))
        payload = json.loads(response.body)
        assert response.status_code == 200, f"{label}: {payload.get('error')}"
        assert "preview_url" in payload, label
        # A no-op still has to say so rather than pretend it drew something.
        assert "note" in payload, label


def test_a_drawn_stroke_is_trimmed_rather_than_refused_for_being_long():
    from runart.course import STROKE_MAX_LENGTH_M, snap_drawn_segment
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    # A stroke that walks the whole loop several times over.
    stroke = [CourseWaypoint(lat=la, lon=lo)
              for _ in range(4) for la, lo in source.points[::3]]
    walked = sum(
        haversine_m(a.lat, a.lon, b.lat, b.lon) for a, b in zip(stroke, stroke[1:]))
    assert walked > STROKE_MAX_LENGTH_M
    course = snap_drawn_segment(source.params, source.path, None, None, stroke)
    assert course.path[0] == course.path[-1]
    assert "너무 길어" in course.note


def test_edit_endpoint_detaches_one_edge_and_reconnects_an_alternate_walkway():
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "reroute",
        "path": source.path,
        "from_index": 1,
        "to_index": 2,
    })))
    payload = json.loads(response.body)

    assert response.status_code == 200
    edited_nodes = [point[0] for point in payload["path"]]
    assert edited_nodes[0] == source.path[0]
    assert edited_nodes[-1] == source.path[-1]
    assert edited_nodes != source.path
    assert source.path[1:3] != edited_nodes[1:3]
    assert payload["length_km"] > 0
    assert "summary" in payload


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
    page = preview_html(course, [], "https://runnywhere.example", page="edit")
    assert "edit-steps" in page
    assert 'aria-label="수정한 코스를 새 코스로 저장"' in page
    assert 'aria-live="polite"' in page
    assert 'AbortController' in page and '3500' in page
    assert "action:'save'" in page
    assert 'id="eraserTool"' in page and 'id="drawTool"' in page
    # The ids the toolbar has actually been through, so a half-done rename
    # cannot leave a handler bound to a control that is no longer rendered.
    assert 'id="penTool"' not in page
    assert 'id="viaTool"' not in page
    assert 'id="eraseTool"' not in page
    assert 'id="editUndo"' in page
    assert 'id="editRedo"' in page      # stepping back is reversible too
    assert 'class="edit-tool mode"' in page
    # Bottom-anchored, not a card over the top half of the map.
    assert '.edit-tools{position:absolute;z-index:950;left:10px;right:10px' in page
    assert 'bottom:calc(10px + env(safe-area-inset-bottom))' in page
    assert 'id="editDistance"' not in page          # no readout row at all
    assert 'edit-tools-head' not in page
    assert 'min-height:44px' in page
    assert 'edit-overlay' in page
    assert 'body.editing .view-toggle' in page
    assert 'body.editing.tool-active .facility-marker' in page
    assert "const syncMapInteraction = () =>" in page
    assert "map.setDraggable(!selecting)" in page
    # panByPixels, not panBy: Kakao's panBy animates, so one call per
    # pointermove restarts the animation every frame and the map barely moves.
    assert "panByPixels(panCenter.x-next.x,panCenter.y-next.y)" in page
    assert "map.setCenter(projection.coordsFromContainerPoint(" in page
    # Kakao's MapProjection snapshots the map when it is fetched, so it has to
    # be read fresh or every conversion after the first pan is wrong.
    assert "containerPointFromCoords:latlng=>map.getProjection()" in page
    assert "coordsFromContainerPoint:point=>map.getProjection()" in page
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
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

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
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    # Errors and blocked states must not auto-dismiss; hints must.
    assert "AUTO_DISMISS_MS" in page
    assert "busy:0" in page and "error:0" in page and "blocked:0" in page
    assert "info:3600" in page
    assert '.edit-toast[data-tone="error"]' in page
    assert '.edit-toast[data-tone="blocked"]' in page
    # Errors carry a dismiss control rather than lingering forever.
    assert "label:'닫기'" in page


def test_the_editor_shows_no_distance_readout_of_its_own():
    """The header row -- 코스 편집, a status chip, 거리 · 미완성 -- was the top
    third of a card that already covered half the map, and the distance it
    showed is on the card below the map anyway."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert 'id="editDistance"' not in page
    assert 'id="editDraftState"' not in page
    assert "' · 미완성'" not in page
    assert "'연결 필요'" not in page and "'경로 확인 필요'" not in page
    assert "edit-tools-head" not in page
    assert '<strong>코스 편집</strong>' not in page
    # The distance still lives where a runner reads the course: under the map.
    assert 'id="mLength"' in page
    # Undo restores the route distance together with the local draft.
    assert "km:editLengthKm" in page


def test_edit_toolbar_names_its_modes_and_keeps_the_map(monkeypatch=None):
    """Three named modes in a segmented pill at the bottom, undo/redo/reset as
    icon-only buttons in the corner stack -- AllTrails' route editor, where the
    map keeps the screen and one primary button carries the decision."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert 'class="edit-mode-group" role="group" aria-label="편집 방식"' in page
    assert 'class="edit-action-group"' not in page
    for label in ("지도 이동", "지우기", "그리기"):
        assert f'<span>{label}</span>' in page
    # The three quiet actions carry no visible label, only an accessible one.
    assert 'class="edit-quick" role="group" aria-label="편집 되돌리기"' in page
    for aria in ("마지막 수정 실행 취소", "되돌린 수정 다시 실행", "모든 수정 초기화"):
        assert f'aria-label="{aria}"' in page
    assert "<span>실행 취소</span>" not in page
    assert ".edit-primary{" in page


def test_edit_tools_have_visible_44px_tap_targets():
    """F-07: every labelled control is visibly finger-sized."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert ".edit-tool{" in page
    assert "min-height:44px" in page
    # 12px facility dots are unhittable by finger, but only widen them on touch:
    # a 44px box under a mouse would swallow map drags near a marker.
    assert "@media (pointer:coarse)" in page
    assert ".facility-marker::before" in page
    # 26px marker + 9px on each side = 44. It used to be 16px a side (58px),
    # which with 80 markers on one screen tiled the map and ate every drag.
    assert "top:-9px;right:-9px;\n      bottom:-9px;left:-9px" in page


def test_mobile_map_gestures_and_facility_taps_do_not_conflict():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    markers = [{"type": "restroom", "name": "화장실", "at_km": 0.1, "lat": 37.566, "lon": 126.978}]
    page = preview_html(course, markers, "https://runnywhere.example", page="edit")

    # An active tool owns one finger; two touch pointers pan the map instead of
    # accidentally submitting a stroke.
    assert "event.pointerType==='touch'&&editPointers.size>=2" in page
    assert "twoFingerPan=true" in page
    # panByPixels, not panBy: Kakao's panBy animates, so one call per
    # pointermove restarts the animation every frame and the map barely moves.
    assert "panByPixels(panCenter.x-next.x,panCenter.y-next.y)" in page
    assert "map.setCenter(projection.coordsFromContainerPoint(" in page
    # Kakao's MapProjection snapshots the map when it is fetched, so it has to
    # be read fresh or every conversion after the first pan is wrong.
    assert "containerPointFromCoords:latlng=>map.getProjection()" in page
    assert "coordsFromContainerPoint:point=>map.getProjection()" in page
    # Facility markers distinguish a short tap from a drag and use Kakao's own
    # propagation guard rather than relying on DOM bubbling alone.
    assert "const FACILITY_TAP_SLOP = 8" in page
    assert "kakao.maps.event.preventMap" in page
    assert "if(!facilityPointer.moved)toggle()" in page
    assert "panByPixels(facilityPointer.lastX-ev.clientX" in page
    assert "clickable:true" in page
    assert "kakao.maps.event.addListener(map, 'click', closePop)" in page
    # Desktop hover remains the primary quick preview for convenience markers.
    assert "if(canHover){el.addEventListener('mouseenter',show);el.addEventListener('mouseleave',hide);}" in page


def test_edit_projection_uses_container_coordinates():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")
    assert "projection.containerPointFromCoords" in page
    # The eraser only maps road nodes to screen pixels; the pencil needs the
    # inverse, but its stroke is snapped to walkable roads by the server
    # rather than becoming route coordinates as drawn.
    assert "projection.coordsFromContainerPoint" in page
    assert "distanceToSegment" in page


def test_editor_erases_a_swept_range_and_draws_its_replacement():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert "const nearestSegment = (point, lo, hi)" in page
    assert "const eraseAt" in page
    # The prompt became a hint and the action became a button beside it.
    assert "지울 구간을 골랐어요. 선택 구간 지우기를 누르세요." in page
    # The action moved into the one primary button, the way 저장 becomes
    # 도보 경로 확인. A separate pill floating over the map was a second place
    # to look for the decision.
    assert 'id="selErase"' not in page
    assert "action==='erase'?'선택 구간 지우기'" in page
    assert "strokeColor:'#e0522d'" in page
    assert 'class="edit-anchor" data-end=' in page
    # Erasing leaves the exact old geometry as translucent red guidance.
    assert "strokeColor:'#e5322e',strokeWeight:10,strokeOpacity:.32" in page
    assert "const eraseSelection" in page
    assert "action:'reroute'" not in page and "action:'via'" not in page
    # Drawing is local freehand until an explicit walkable preview request.
    assert "const beginFreeDraw" in page
    assert "const appendFreeDraw" in page
    assert "const previewDrawnRoute" in page
    assert "action:'snap'" in page
    # Saving an unconfirmed sketch snaps it on the way rather than refusing.
    assert "action:'save_draft'" in page


def test_revert_is_undoable_and_separated_from_save():
    """Reset restores the entry route without leaving the editor and is undoable."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert "label:'실행 취소'" in page
    assert "원본 코스로 되돌렸어요." in page
    assert "const hadEdits=" in page          # no undo offer when nothing was discarded
    assert "setEditDistance(initialLengthKm)" in page
    assert "clearDraft();undoStack=[];redoStack=[];editMode=null;applyMode();" in page
    assert "setEditing(false)" not in page
    # Save is the only filled action; reset is a quiet icon in the corner stack.
    assert ".edit-quick-btn.reset{color:#a23c31}" in page
    assert ".edit-primary{" in page and "background:#087b59" in page
    # and no confirmation dialog was introduced
    assert "confirm(" not in page


def test_undo_once_and_reset_all_use_unmistakably_different_icons():
    """A directional undo and full-reset arrow cannot be mistaken for each other."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert 'aria-label="마지막 수정 실행 취소"' in page
    assert 'title="한 번 되돌리기"' in page
    assert 'aria-label="모든 수정 초기화"' in page
    assert 'title="전체 초기화"' in page
    assert "M6.5 10v9h11v-9" not in page       # old roof/door house path
    assert "m8 7-4 4 4 4" in page               # one-step directional undo
    assert "M4 12a8 8 0 1 0 2.3-5.7" in page   # reset-all circular arrow
    assert "M12 7v5l4 2" not in page            # old history glyph


def test_zoom_control_is_removed_while_editing():
    """F-10: setZoomable(false) only stops wheel/pinch; a zoom press mid-gesture
    would move the ground under the screen-space stroke being collected."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert "const zoomControl = new kakao.maps.ZoomControl()" in page
    assert "map.removeControl(zoomControl)" in page
    assert "showZoomControl(!value)" in page


def test_detail_panels_remain_on_the_last_valid_route_while_drafting():
    """An unfinished freehand line must not rewrite metrics as if it were a course."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    # every element the summary rewrites has to be addressable
    for element_id in ("courseTitle", "courseBadges", "mLength", "mDuration", "mAscent"):
        assert f'id="{element_id}"' in page, element_id
    assert "const initialSummary =" in page
    # Undo and reset restore the last valid summary together with the line.
    assert "applySummary(state.summary)" in page
    assert "applySummary(initialSummary)" in page
    # The unfinished guide does not touch the metrics; only the server-snapped
    # walkable preview becomes the new valid route summary.
    assert "const previewDrawnRoute" in page
    assert "setEditDistance(payload.length_km);applySummary(payload.summary)" in page


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
    page = preview_html(course, facilities, "https://runnywhere.example", page="edit")

    assert summary["title"] == course_title(course)
    assert summary["length_km"] == round(course.length_km, 2)
    assert summary["ascent_m"] == round(course.ascent_m)
    # Every facility row the summary carries is on the page a load renders.
    for row in summary["facility_rows"]:
        assert row["name"] in page and row["at_km"] in page
    # Running-friendliness was removed from the page; it must not come back
    # through the live-update payload either.
    assert "rfs" not in summary
    assert "러닝 친화도" not in page


def test_editing_offers_an_explicit_map_pan_tool():
    """Two-finger panning exists but nothing on screen says so; one-finger drag
    is the gesture people reach for."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert 'id="panTool"' in page
    assert 'aria-label="지도 이동"' in page
    assert "setMode('pan')" in page
    # pan is the default on entering edit mode, and it leaves the map draggable
    assert 'id="panTool" class="edit-tool mode" type="button" aria-label="지도 이동" aria-pressed="true"' in page
    assert "syncMapInteraction();" in page
    assert "map.setDraggable(!selecting)" in page


def test_drawing_overlay_is_absent_unless_a_drawing_tool_is_active():
    """A full-bleed touch-action:none layer over the map is exactly what eats a
    drag on mobile -- keep it out of the tree unless it is being drawn on."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert "touch-action:none;pointer-events:none;display:none" in page
    assert "body.editing.tool-active .edit-overlay{display:block}" in page


def test_route_decorations_do_not_swallow_map_drags():
    """km bubbles, direction arrows and the start pin are labels, not controls."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")
    assert ".km-marker,.dir-marker,.start-marker,.finish-marker{pointer-events:none}" in page
    assert "routePath[routePath.length - 1]" in page
    assert "sameEndpoint" in page


def test_live_tracking_centers_the_runner_and_keeps_white_arrows_on_colored_route():
    """GPS tracking follows the runner without flattening route colours."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert "map.setCenter(posLatLng)" in page
    assert "strokeColor:color(s)" in page
    assert '<svg viewBox="0 0 10 10"' in page
    assert "fill:#fff" in page
    assert "width:9px;height:9px" in page
    assert "➤" not in page


def test_direction_chevrons_repeat_frequently_across_the_whole_course():
    from runart.render import _direction_markers

    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    markers = _direction_markers(course.points)

    # Roughly every 120m on a normal city loop, capped on very long courses:
    # enough cues to cover short blocks without making a solid arrow ribbon.
    assert 35 <= len(markers) <= 80
    assert markers[0]["lat"] != markers[-1]["lat"]


def test_editor_page_needs_no_entry_control_on_the_map():
    """Editing used to start from a button pinned to the map. The editor is
    its own page now, so arriving is the intent and the map keeps only the
    view switch."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert 'id="editRoute"' not in page
    assert "코스 선 수정" not in page
    assert "if (PAGE === 'edit' && editEnabled) setEditing(true);" in page
    assert ".view-toggle{position:absolute;z-index:530;right:14px;top:14px" in page
    assert 'class="map-hud"' not in page


def test_map_carries_no_controls_beyond_the_view_switch():
    """Editing, starting a run and switching views were all pinned to the
    map at once. Only the view switch belongs to the map itself now."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert 'class="run-locate"' not in page
    assert 'id="runStart"' not in page
    assert 'id="editRoute"' not in page
    assert 'class="view-toggle"' in page
    assert 'class="map-hud"' not in page
    assert 'class="pill"' not in page
    assert 'class="run-status"' not in page
    assert 'GPS 안내 대기' not in page
    assert 'id="inlineRunStart"' not in page
    assert 'id="mobileRunStart"' not in page


def test_course_title_and_badges_share_a_row():
    course = generate_course(CourseParams(lat=CITY_HALL["lat"], lon=CITY_HALL["lon"],
                                          distance_km=5.0, location_name="강남대로 401-2"))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert "강남대로런" in page and "401-2런" not in page
    assert 'class="course-head"' in page
    assert 'class="course-badges"' in page
    # badges are labelled, never emoji-only
    assert 'class="badge" type="button" aria-label="일반 러닝 코스"' in page


def test_map_container_is_positioned_for_its_absolute_controls():
    """The HUD, toolbar and toast are absolutely positioned inside #map; do not
    rely on the Kakao SDK setting position:relative at runtime."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")
    assert "#map{position:relative" in page


def test_animal_preview_explains_save_as_new_editing():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    course.params = course.params.model_copy(update={"shape": "dog"})
    page = preview_html(course, [], "https://runnywhere.example", page="edit")
    assert "원본 동물 코스는 유지" in page
    assert "직접 편집한 코스" in page
    # F-02: the warning has to be shown, not buried in screen-reader-only text.
    assert "원본 동물 코스는 유지" not in _hidden_text(page)
    assert "const editNotice =" in page
    assert "setEditStatus(editNotice" in page


def test_plain_course_has_no_animal_edit_notice():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")
    assert 'const editNotice = ""' in page
    assert "원본 동물 코스는 유지" not in page


def test_preview_keeps_a_local_course_editor_available_without_map_sdk():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert "initLocalCourseEditor" in page
    assert "로컬 코스 편집 체험" in page
    assert "SVGPoint" in page
    assert "const arrowsFor=points" in page
    assert 'fill="#fff" stroke="#064f38"' in page
    assert "구간 선택을 누른 뒤 바꿀 코스 선을 탭하세요" in page
    assert 'id="localEditRoute"' in page
    assert 'id="localSegment"' in page
    assert 'id="localEditSave"' in page


def _straight_stroke(graph, path, a, b, n=40):
    from runart.models import CourseWaypoint

    pa, pb = graph.nodes[path[a]], graph.nodes[path[b]]
    return [CourseWaypoint(lat=pa["lat"] + (pb["lat"] - pa["lat"]) * i / (n - 1),
                           lon=pa["lon"] + (pb["lon"] - pa["lon"]) * i / (n - 1))
            for i in range(n)]


def test_a_straight_stroke_does_not_produce_a_zigzag():
    """Forcing the route through ~18 independently snapped stroke samples made
    neighbouring samples land on different ways, so the path detoured out and
    back to touch each one: a 489m straight line came back 1,515m long."""
    from runart import graph as graphmod
    from runart.course import snap_drawn_segment
    from runart.geo import haversine_m

    g = graphmod.get_graph()
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    a, b = 8, 28
    edited = snap_drawn_segment(course.params, course.path, a, b,
                                _straight_stroke(g, course.path, a, b))
    tail = len(course.path) - b - 1
    replacement = edited.path[a:len(edited.path) - tail]

    pa, pb = g.nodes[course.path[a]], g.nodes[course.path[b]]
    direct = haversine_m(pa["lat"], pa["lon"], pb["lat"], pb["lon"])
    walked = sum(
        haversine_m(g.nodes[u]["lat"], g.nodes[u]["lon"],
                    g.nodes[v]["lat"], g.nodes[v]["lon"])
        for u, v in zip(replacement, replacement[1:])
    )

    # No node is visited twice: an out-and-back spur is exactly a repeat.
    assert len(set(replacement)) == len(replacement)
    # Streets are not straight lines, but 2x the crow-flight distance is a
    # detour, not a road.
    assert walked <= direct * 2.0, f"{walked:.0f}m for a {direct:.0f}m line"


def test_drawn_replacement_stays_on_walkable_ways():
    from runart import graph as graphmod
    from runart.course import edge_is_runnable, snap_drawn_segment

    g = graphmod.get_graph()
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    a, b = 8, 28
    edited = snap_drawn_segment(course.params, course.path, a, b,
                                _straight_stroke(g, course.path, a, b))

    assert all(edge_is_runnable(g.edges[u, v])
               for u, v in zip(edited.path, edited.path[1:]))


def _edit_error(course, body):
    """POST an edit payload through the route and return its error text."""
    import json as _json

    from runart.models import encode_course_id

    cid = encode_course_id(course.params)

    async def _call():
        request = Request({
            "type": "http", "method": "POST",
            "path": f"/c/{cid}/edit",
            "path_params": {"course_id": cid},
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
        })
        payload = _json.dumps(body).encode()
        request._body = payload
        return await server.edit_course_route(request)

    response = asyncio.run(_call())
    return _json.loads(bytes(response.body).decode())


def test_an_ordinary_pencil_stroke_is_not_rejected_for_its_length():
    """The stroke cap was 96 points. A finger crossing a phone screen makes
    hundreds, so drawing normally produced a generic "check the line" error."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    start, finish = course.points[5], course.points[40]
    stroke = [
        {"lat": start[0] + (finish[0] - start[0]) * i / 299,
         "lon": start[1] + (finish[1] - start[1]) * i / 299}
        for i in range(300)
    ]
    result = _edit_error(course, {
        "action": "snap", "path": course.path,
        "from_index": 5, "to_index": 40, "stroke": stroke,
    })

    assert "error" not in result, result.get("error")
    assert result["length_km"] > 0


def test_each_edit_failure_names_its_own_cause():
    """One generic message for every rejection hid a plain length cap behind
    advice the runner could not act on."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    outside = [{"lat": 35.1, "lon": 129.0}, {"lat": 35.2, "lon": 129.1}]

    causes = {
        "서울 밖": _edit_error(course, {
            "action": "snap", "path": course.path,
            "from_index": 5, "to_index": 40, "stroke": outside}),
        "점 과다": _edit_error(course, {
            "action": "snap", "path": course.path, "from_index": 5, "to_index": 40,
            "stroke": [{"lat": 37.5665, "lon": 126.9780}] * 700}),
        "동작": _edit_error(course, {"action": 123, "path": course.path}),
    }
    messages = [value["error"] for value in causes.values()]

    assert all("코스 선 정보를 확인해 주세요" not in m for m in messages)
    assert len(set(messages)) == len(messages)   # each cause reads differently
    assert "서울 밖" in causes["서울 밖"]["error"]
    assert "700개" in causes["점 과다"]["error"]


def test_a_line_drawn_over_itself_keeps_the_out_and_back():
    """One line down a street asks for a route along it; the same street drawn
    twice asks for an out-and-back on it, and backtrack removal must not
    quietly undo the second intent."""
    from runart.course import drop_backtracking, stroke_is_doubled

    from runart.models import CourseWaypoint

    straight = [CourseWaypoint(lat=37.5665 + i * 0.0001, lon=126.9780)
                for i in range(20)]
    there_and_back = straight + list(reversed(straight))

    assert not stroke_is_doubled(straight)
    assert stroke_is_doubled(there_and_back)
    # The spur remover itself is unchanged; it is simply not applied.
    assert drop_backtracking([1, 2, 3, 2, 1, 4]) == [1, 4]


def test_drawing_a_spur_twice_keeps_the_out_and_back_in_the_route():
    """The two intents have to produce different routes: one pass down a
    street is a route along it, two passes are an out-and-back on it."""
    from runart import graph as graphmod
    from runart.course import snap_drawn_segment
    from runart.models import CourseWaypoint

    def between(p, q, n):
        return [CourseWaypoint(lat=p[0] + (q[0] - p[0]) * i / (n - 1),
                               lon=p[1] + (q[1] - p[1]) * i / (n - 1))
                for i in range(n)]

    g = graphmod.get_graph()
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    a, b = 8, 28
    start = (g.nodes[course.path[a]]["lat"], g.nodes[course.path[a]]["lon"])
    end = (g.nodes[course.path[b]]["lat"], g.nodes[course.path[b]]["lon"])
    aside = (start[0] + 0.0022, start[1])

    once = snap_drawn_segment(course.params, course.path, a, b,
                              between(start, end, 40))
    twice = snap_drawn_segment(
        course.params, course.path, a, b,
        between(start, aside, 20) + between(aside, start, 20)
        + between(start, end, 30))

    def replacement(edited):
        tail = len(course.path) - b - 1
        return edited.path[a:len(edited.path) - tail]

    once_repl, twice_repl = replacement(once), replacement(twice)

    # One pass: the router is free to follow the road and nothing repeats.
    assert len(set(once_repl)) == len(once_repl)
    # Two passes: the out-and-back the runner drew is still in the route.
    assert len(twice_repl) - len(set(twice_repl)) > 1
    assert twice.length_km > once.length_km


def test_an_unsteady_hand_still_draws_a_straight_route():
    """A finger drawing a straight line wobbles. A wobble of a few tens of
    metres brought the line back within reach of ground it had just covered,
    which read as a deliberate double pass, skipped spur removal and returned
    a route 2.4x the length of the line with ten repeated nodes."""
    import random

    from runart import graph as graphmod
    from runart.course import snap_drawn_segment, stroke_is_doubled
    from runart.geo import haversine_m
    from runart.models import CourseWaypoint

    g = graphmod.get_graph()
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    a, b = 8, 30
    pa, pb = g.nodes[course.path[a]], g.nodes[course.path[b]]
    direct = haversine_m(pa["lat"], pa["lon"], pb["lat"], pb["lon"])

    for jitter_m in (0, 10, 25, 45, 60):
        random.seed(11)
        stroke = []
        for i in range(60):
            t = i / 59
            wobble = random.uniform(-jitter_m, jitter_m)
            stroke.append(CourseWaypoint(
                lat=pa["lat"] + (pb["lat"] - pa["lat"]) * t + wobble / 111_000.0,
                lon=pa["lon"] + (pb["lon"] - pa["lon"]) * t + wobble / 88_000.0))

        assert not stroke_is_doubled(stroke), f"±{jitter_m}m read as a double pass"
        edited = snap_drawn_segment(course.params, course.path, a, b, stroke)
        tail = len(course.path) - b - 1
        replacement = edited.path[a:len(edited.path) - tail]
        walked = sum(
            haversine_m(g.nodes[u]["lat"], g.nodes[u]["lon"],
                        g.nodes[v]["lat"], g.nodes[v]["lon"])
            for u, v in zip(replacement, replacement[1:])
        )

        assert len(set(replacement)) == len(replacement), f"±{jitter_m}m backtracked"
        assert walked <= direct * 2.0, f"±{jitter_m}m: {walked:.0f}m for {direct:.0f}m"


def test_a_drawn_detour_survives_spur_removal():
    """Drawing a line out around something and back is an excursion, and so
    is a spur the router invented -- both repeat nodes. Cutting every repeat
    threw the drawn detour away: a line drawn north around 경복궁 came back as
    the original route, its northernmost point lower than before."""
    from runart import graph as graphmod
    from runart.course import snap_drawn_segment
    from runart.models import CourseWaypoint

    g = graphmod.get_graph()
    course = generate_course(CourseParams(**CITY_HALL, distance_km=8.0))
    top = max(range(len(course.path)),
              key=lambda i: g.nodes[course.path[i]]["lat"])
    a, b = max(0, top - 4), min(len(course.path) - 2, top + 4)
    pa, pb = g.nodes[course.path[a]], g.nodes[course.path[b]]

    def between(p, q, n=14):
        return [CourseWaypoint(lat=p[0] + (q[0] - p[0]) * i / (n - 1),
                               lon=p[1] + (q[1] - p[1]) * i / (n - 1))
                for i in range(n)]

    via = [(pa["lat"], pa["lon"]), (37.5860, 126.9840), (37.5866, 126.9748),
           (37.5820, 126.9760), (pb["lat"], pb["lon"])]
    stroke = [point for x, y in zip(via, via[1:]) for point in between(x, y)]

    before = max(g.nodes[n]["lat"] for n in course.path)
    edited = snap_drawn_segment(course.params, course.path, a, b, stroke)
    after = max(g.nodes[n]["lat"] for n in edited.path)

    assert after > before, "the drawn northern detour was thrown away"
    assert edited.length_km > course.length_km


def test_only_waypoints_far_off_the_line_count_as_a_detour():
    """An unsteady hand scatters waypoints along the line it meant to draw.
    Protecting those from spur removal makes every wobble a detour the route
    has to honour, which is the zigzag all over again."""
    from runart import graph as graphmod
    from runart.course import DETOUR_WAYPOINT_MIN_M, _detour_nodes

    g = graphmod.get_graph()
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    start, end = course.path[8], course.path[30]
    on_the_line = course.path[15]

    assert DETOUR_WAYPOINT_MIN_M >= 100
    # A node the route already passes through is never a detour.
    assert on_the_line not in _detour_nodes(g, [on_the_line], start, end)


def test_a_replacement_that_cannot_differ_says_why():
    """Some stretches are the only walkable link between two parts of the
    city -- an unnamed 660m hillside footway by 개운산 is a cut edge in the
    graph -- so erasing one and drawing over it returns the same line.
    Saying nothing made the editor look broken."""
    from runart.server import _unchanged_note

    same = [1, 2, 3, 4]
    assert _unchanged_note(same, list(same))
    assert "유일한 길" in _unchanged_note(same, list(same))
    assert _unchanged_note(same, [1, 5, 4]) == ""


def test_the_editor_never_blocks_a_draft_for_where_it_was_drawn():
    """Free editing is the point of the screen. A sketch that does not reach
    the ends of the erased gap is joined to the course where it does touch,
    and one that was never confirmed is snapped on the way to being saved."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert "draftConnection" not in page
    assert "코스 선이 이어지지 않았어요" not in page
    assert "먼저 도보 경로를 확인해 주세요" not in page
    assert "먼저 지우기로 바꿀 구간을 붉게 표시해 주세요" not in page
    # The gap travels as a hint; the span comes from where the stroke landed.
    assert "if(gapRange){body.from_index=gapRange[0];body.to_index=gapRange[1];}" in page
    assert "const draftStroke" in page


def test_the_primary_button_is_whatever_the_editor_is_in_the_middle_of():
    """One decision at a time, in one place, coloured by what it does:
    red to erase, blue to confirm a sketch, green to save."""
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example", page="edit")

    assert 'id="editSave" class="edit-primary"' in page
    assert "const syncPrimary" in page
    assert "selectedRange?'erase':(gapRange||drawn)?'verify':'save'" in page
    assert "action==='erase'?'선택 구간 지우기':action==='verify'?'도보 경로 확인':'저장'" in page
    assert '.edit-primary[data-action="erase"]{background:#c0392b' in page
    assert '.edit-primary[data-action="verify"]{background:#1668dc' in page
    assert "if(action==='erase'){eraseSelection();return;}" in page


# ---------------------------------------------------------------------------
# Naming a saved course
# ---------------------------------------------------------------------------

def test_a_typed_name_joins_the_distance_rather_than_replacing_it():
    """A runner scanning saved courses reads the number first, so "AA런" typed
    on a 4.8km course has to come back as "4.8km AA런"."""
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "save", "path": source.path, "name": "AA런",
    })))
    payload = json.loads(response.body)
    saved = decode_course_id(payload["course_id"])
    assert response.status_code == 200
    assert saved.custom_name == "AA런"

    from runart.course import course_from_path
    named = course_from_path(source.params, source.path, "AA런")
    assert course_title(named) == f"{named.length_km:.1f}km AA런"


def test_saving_without_typing_keeps_the_generated_name():
    """The field opens empty with the current name behind it in grey, so an
    untouched save must leave the title exactly as it was."""
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "save", "path": source.path, "name": "",
    })))
    saved = decode_course_id(json.loads(response.body)["course_id"])
    assert saved.custom_name == ""


def test_an_unnamed_course_id_is_byte_identical_to_the_ids_minted_before_names():
    """custom_name is popped from canonical() when empty, so every link ever
    shared still decodes -- and still encodes to the same string."""
    params = CourseParams(**CITY_HALL, distance_km=5)
    assert "custom_name" not in params.canonical()
    assert encode_course_id(params) == encode_course_id(
        CourseParams(**params.canonical()))
    named = params.model_copy(update={"custom_name": "AA런"})
    assert decode_course_id(encode_course_id(named)).custom_name == "AA런"


def test_a_course_name_cannot_smuggle_control_characters_into_the_page():
    from runart.models import clean_course_name
    assert clean_course_name("  AA \n 런  ") == "AA 런"
    assert clean_course_name("A‮B") == "AB"
    assert clean_course_name("가" * 60) == "가" * 24
    assert clean_course_name(None) == ""

    course = generate_course(CourseParams(
        **CITY_HALL, distance_km=5, custom_name="<script>x</script>"))
    page = preview_html(course, [], "https://runnywhere.example")
    assert "<script>x</script>" not in page.split("<script>\n const segs")[0]
    assert "&lt;script&gt;" in page


# ---------------------------------------------------------------------------
# Erasing reconnects on its own (the protruding-spur case)
# ---------------------------------------------------------------------------

def _course_with_dead_end_spur():
    """A loop with a real out-and-back onto a graph leaf grafted into it.

    A leaf is the honest version of "삐죽 튀어나온 선": the only way back is the
    way you came, so no alternative route exists and the span can only be cut.
    """
    from runart.course import course_from_path, edge_is_runnable
    g = graphmod.get_graph()
    base = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    for index in range(5, len(base.path) - 5):
        node = base.path[index]
        for neighbour in g[node]:
            if g.degree(neighbour) == 1 and edge_is_runnable(g.edges[node, neighbour]):
                spurred = base.path[:index + 1] + [neighbour, node] + base.path[index + 1:]
                return base, course_from_path(base.params, spurred), index, neighbour
    raise AssertionError("no dead-end spur available in the bundled graph")


def test_erasing_a_dead_end_spur_cuts_it_away_instead_of_refusing():
    """The commonest reason to reach for the eraser was the one thing it could
    not do: there is no alternative path along a spur, so the request to find
    one correctly failed and the runner was told to draw instead."""
    from runart.course import reroute_segment
    base, spurred, index, tip = _course_with_dead_end_spur()

    edited = reroute_segment(base.params, spurred.path, index, index + 1)

    assert tip not in edited.path
    assert edited.path == base.path
    assert "왕복으로 튀어나온" in edited.note


def test_a_span_with_a_real_alternative_still_gets_the_alternative():
    """Cutting is the fallback, not the behaviour: a span that another walkable
    way can replace is replaced, not deleted."""
    from runart.course import reroute_segment
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    edited = reroute_segment(source.params, source.path, 1, 2)
    assert edited.path != source.path
    assert edited.note == ""


def test_collapsing_an_excursion_never_opens_the_loop():
    from runart.course import collapse_excursion
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    for lo, hi in ((1, 3), (0, 2), (len(source.path) - 4, len(source.path) - 2)):
        trimmed = collapse_excursion(source.path, lo, hi)
        if trimmed is not None:
            assert trimmed[0] == trimmed[-1]
            assert len(trimmed) >= 3


# ---------------------------------------------------------------------------
# Tapping a place to go through
# ---------------------------------------------------------------------------

def test_tapping_a_place_routes_the_span_through_it():
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    lat, lon = source.points[20]
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "via", "path": source.path, "from_index": 8, "to_index": 34,
        "vias": [{"lat": lat + 0.0015, "lon": lon + 0.0015}],
    })))
    payload = json.loads(response.body)
    assert response.status_code == 200
    nodes = [point[0] for point in payload["path"]]
    assert nodes[0] == source.path[0] and nodes[-1] == source.path[-1]
    assert nodes != source.path
    assert payload["length_km"] > 0
    assert "geometry" in payload and len(payload["geometry"]) == len(nodes) - 1


def test_a_via_request_without_a_span_is_refused_by_name():
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "via", "path": source.path,
        "vias": [{"lat": source.points[5][0], "lon": source.points[5][1]}],
    })))
    assert response.status_code == 400
    assert "구간" in json.loads(response.body)["error"]


def test_too_many_taps_are_refused_before_any_routing_work():
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    cid = encode_course_id(source.params)
    response = asyncio.run(server.edit_course_route(_json_request(cid, {
        "action": "via", "path": source.path, "from_index": 4, "to_index": 30,
        "vias": [{"lat": 37.56 + i * 0.0002, "lon": 126.97} for i in range(40)],
    })))
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Saying why a drawn line is not there
# ---------------------------------------------------------------------------

def test_a_staircase_is_named_as_the_reason_a_span_was_not_taken():
    """"선을 그렸는데 안 그려진다" was always true and never explained. Stairs
    and steep unpaved paths are excluded by edge_is_runnable, so a tap on one
    has to come back with the reason rather than a silently different route."""
    from runart.course import blocked_reason_near, highway_class
    g = graphmod.get_graph()
    explained = 0
    checked = 0
    for u, v, attrs in g.edges(data=True):
        if highway_class(attrs) != "steps":
            continue
        checked += 1
        middle = ((g.nodes[u]["lat"] + g.nodes[v]["lat"]) / 2,
                  (g.nodes[u]["lon"] + g.nodes[v]["lon"]) / 2)
        if "계단" in blocked_reason_near(*middle):
            explained += 1
        if checked >= 40:
            break
    # Deliberately conservative: silence when a runnable way is just as close.
    assert explained >= checked // 2, f"only {explained}/{checked} staircases explained"


def test_a_steep_path_is_named_by_its_grade():
    from runart.course import blocked_reason_near, highway_class
    g = graphmod.get_graph()
    for u, v, attrs in g.edges(data=True):
        if highway_class(attrs) != "path":
            continue
        if abs(float(attrs.get("slope_pct", 0) or 0)) <= 12:
            continue
        middle = ((g.nodes[u]["lat"] + g.nodes[v]["lat"]) / 2,
                  (g.nodes[u]["lon"] + g.nodes[v]["lon"]) / 2)
        reason = blocked_reason_near(*middle)
        if reason:
            assert "경사" in reason and "%" in reason
            return
    raise AssertionError("no steep path explained anywhere in the graph")


def test_an_ordinary_pavement_is_not_apologised_for():
    """The note must mean something. A tap next to a perfectly runnable way
    gets silence, not a manufactured excuse."""
    from runart.course import blocked_reason_near
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    for lat, lon in source.points[:25]:
        assert blocked_reason_near(lat, lon) == ""


def test_an_unreachable_tap_is_reported_with_the_edited_course():
    from runart.course import unreached_point_note
    g = graphmod.get_graph()
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    assert unreached_point_note(g, source.path, [source.points[3]]) == ""
    far = (source.points[3][0] + 0.02, source.points[3][1] + 0.02)
    assert unreached_point_note(g, source.path, [far]) != ""


# ---------------------------------------------------------------------------
# One course, one line
# ---------------------------------------------------------------------------

def test_the_editor_is_handed_the_same_street_shapes_the_other_pages_draw():
    """The editor drew straight chords between graph nodes while info and run
    drew real OSM way geometry, so the same course had two different lines and
    the edited one cut corners through blocks it never enters."""
    from runart.render import edit_path_geometry, edit_path_nodes, route_points
    source = generate_course(CourseParams(**CITY_HALL, distance_km=5))
    nodes = edit_path_nodes(source.path)
    geometry = edit_path_geometry(source.path)

    assert len(geometry) == len(nodes) - 1
    assert any(shape for shape in geometry), "no edge geometry at all"

    expanded = []
    for index, node in enumerate(nodes):
        expanded.append((node[1], node[2]))
        if index < len(geometry):
            expanded.extend(tuple(point) for point in (geometry[index] or []))
    drawn = [(round(lat, 6), round(lon, 6)) for lat, lon in route_points(source)]
    assert expanded == drawn, "the editor's line and the info page's line differ"
