"""Three pages, one course: 코스 정보 / 달리기 / 코스 편집.

Doing all three on one screen meant the map carried an edit toolbar, a
tracking control and a page of reading at the same time. Each page now owns
one job and the tab bar moves between them.
"""

import pytest

from runart.course import generate_course
from runart.models import CourseParams, encode_course_id
from runart.render import COURSE_PAGES, preview_html

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="서울시청")


def _course(**overrides):
    params = {"distance_km": 5.0, **overrides}
    return generate_course(CourseParams(**CITY_HALL, **params))


def _page(course, page):
    return preview_html(course, [], "https://runnywhere.example", page=page)


def test_every_page_carries_the_same_three_tab_bar():
    course = _course()
    cid = encode_course_id(course.params)

    for page in COURSE_PAGES:
        markup = _page(course, page)
        assert 'class="tab-bar"' in markup
        assert f'href="https://runnywhere.example/c/{cid}"' in markup
        assert f'href="https://runnywhere.example/c/{cid}/run"' in markup
        assert f'href="https://runnywhere.example/c/{cid}/editor"' in markup
        for label in ("코스 정보", "달리기", "코스 편집"):
            assert label in markup
        # Exactly one tab is current, and it is this page's own.
        assert markup.count('aria-current="page"') == 1


def test_course_info_page_drops_the_effort_figures():
    """Pace, steps, calories and the elevation band belong to running it."""
    info = _page(_course(), "info")

    assert 'id="paceRange"' not in info
    for element_id in ("mSteps", "mKcal", "mElev"):
        assert f'id="{element_id}"' not in info
    # What identifies and qualifies the course stays.
    assert 'id="mLength"' in info and 'id="mAscent"' in info
    assert "이 코스는 이런 코스예요" in info
    assert "러닝 중 편의시설" in info


def test_run_page_owns_the_effort_figures_and_the_start_control():
    run = _page(_course(), "run")

    assert 'id="paceRange"' in run
    for element_id in ("mSteps", "mKcal", "mElev", "runCta"):
        assert f'id="{element_id}"' in run
    # Reading material lives on the info page, not in front of a runner --
    # but the escape hatches close the page a runner is already on.
    assert "러닝 중 편의시설" not in run
    assert "이 코스는 이런 코스예요" not in run
    assert "다른 앱으로 달리기" in run


def test_editor_page_is_only_the_editor():
    edit = _page(_course(), "edit")

    assert 'id="editSave"' in edit and 'id="eraserTool"' in edit
    assert "edit-steps" in edit
    assert 'id="paceRange"' not in edit
    assert "이 코스는 이런 코스예요" not in edit
    # The old in-map entry point is gone; the tab is the way in.
    assert 'id="editRoute"' not in edit


def test_edit_button_is_gone_from_the_map_on_every_page():
    """The pinned "코스 편집" control on the map is what made the map do three
    jobs at once. The tab replaces it."""
    for page in COURSE_PAGES:
        markup = _page(_course(), page)
        assert 'id="editRoute"' not in markup
        assert "#editRoute{" not in markup
    # And the toolbar itself ships only where editing happens.
    for page in ("info", "run"):
        markup = _page(_course(), page)
        assert 'class="edit-tools"' not in markup
        assert 'id="editOverlay"' not in markup


def test_run_mode_follows_the_runner_and_shows_which_way_they_face():
    """Kakao's JS SDK has no bearing API, so north stays fixed on screen and
    the heading rides on the user marker instead."""
    run = _page(_course(), "run")

    assert "deviceorientation" in run
    assert 'class="user-heading"' in run
    assert "RUN_FOLLOW_LEVEL" in run       # the map closes in when a run starts
    assert "map.setLevel(RUN_FOLLOW_LEVEL)" in run


def test_map_failure_is_only_explained_where_it_blocks_something():
    """The editor still works from the offline fallback, so a red banner about
    live guidance belongs on the run page and nowhere else."""
    assert "deadHud && PAGE === 'run'" in _page(_course(), "run")


@pytest.mark.parametrize("page", ["info", "run", "edit"])
def test_pages_share_the_map_and_the_course_identity(page):
    course = _course()
    markup = _page(course, page)

    assert 'id="map"' in markup
    assert "서울시청" in markup


def test_run_page_keeps_start_within_reach_and_ends_with_the_escape_hatches():
    """Strava, Runna and AllTrails all keep Start reachable the whole time;
    taking the course to another app is the last thing on the page."""
    run = _page(_course(), "run")

    assert 'class="run-float"' in run
    assert run.index('class="run-float"') > run.index("다른 앱으로 달리기")
    assert "GPX 파일 받기" in run


def test_tabs_carry_an_unsaved_edit_to_the_other_pages():
    """An edited course is fully described by its id, so switching tabs must
    not drop the edit and describe the original route instead."""
    edit = _page(_course(), "edit")

    assert 'data-page=""' in edit and 'data-page="/run"' in edit
    assert "tab.href = currentCourseUrl + tab.dataset.page" in edit


def test_erasing_is_one_button_and_the_pencil_reconnects():
    """Zillow gives drawing its own Clear button; an action tucked into a
    status toast is a message, not a control. And one gesture deserves one
    button: undo already lives in the toolbar and reconnecting is the pencil."""
    edit = _page(_course(), "edit")

    assert 'id="selErase"' in edit and "지우기</button>" in edit
    assert 'id="selBar"' not in edit
    assert "const replaceSelected" not in edit
    # Erasing opens a gap the pencil is sent to close.
    assert "gapRange=[...selectedRange]" in edit
    assert "setMode('pen')" in edit
    assert "if(gapRange)return [...gapRange]" in edit


def test_selection_ends_are_draggable_handles_that_grow_the_selection():
    """Fi's "Drag the points to shape the area" -- one tapped edge is rarely
    the stretch a runner wants to replace."""
    edit = _page(_course(), "edit")

    assert 'class="edit-anchor" data-end=' in edit
    assert "nearestNodeIndex" in edit
    assert "onHandleDown" in edit
    assert "map.setDraggable(false)" in edit


def test_no_control_is_referenced_through_an_implicit_id_global():
    """`id="x"` makes window.x, so a bare `x` works until the element stops
    shipping -- then it is a ReferenceError that aborts the rest of the
    script. Every control the script touches must be looked up explicitly."""
    import re

    edit = _page(_course(), "edit")
    script = edit.split("<script>")[-1]
    ids = set(re.findall(r'id="([A-Za-z][A-Za-z0-9]*)"', edit))
    declared = set(re.findall(r"(?:const|let|var)\s+([A-Za-z0-9_]+)\s*=\s*"
                              r"document\.(?:getElementById|querySelector)", script))
    for name in sorted(ids):
        if re.search(rf"(?<![.'\"\w]){name}\b\s*(?:\.|\)|&&|\|\|)", script):
            assert name in declared or f"'{name}'" in script, (
                f"{name} is used as a bare identifier but never declared")


def test_info_page_does_not_carry_the_other_apps_box():
    """Taking the course elsewhere is something you decide while looking at
    running it, so it closes the run page only."""
    info = _page(_course(), "info")

    assert "다른 앱으로 달리기" not in info
    assert "GPX 파일 받기" not in info
    assert "다른 앱으로 달리기" in _page(_course(), "run")


def test_editor_offers_an_eraser_and_a_pencil_not_one_select_tool():
    """AllTrails splits its editor into Tap and Draw; erasing what is wrong
    and drawing what is right are different intents and need different tools."""
    edit = _page(_course(), "edit")

    assert 'id="eraserTool"' in edit and 'id="penTool"' in edit
    assert 'id="segmentTool"' not in edit
    assert "지우개" in edit and "연필" in edit
    assert "되돌리기" in edit and "자동으로 잇기" in edit
    # Eraser sweeps a range; pencil collects a stroke and snaps it to roads.
    assert "const eraseAt" in edit
    assert "penStroke" in edit
    assert "action:'snap'" in edit
    assert "coordsFromContainerPoint" in edit


def test_editor_can_step_forward_again_after_stepping_back():
    """Undo without redo makes a wrong undo as costly as a wrong edit."""
    edit = _page(_course(), "edit")

    assert 'id="editRedo"' in edit
    assert "redoStack" in edit
    assert "redoStack.push(snapshot())" in edit
    assert "restore(redoStack.pop())" in edit
    # A fresh edit invalidates the forward history, as in every editor.
    assert "undoStack.shift();redoStack=[]" in edit


def test_map_keeps_touch_gestures_instead_of_scrolling_the_page():
    """A swipe that starts on the map has to pan the map; letting the page
    scroll out from under it is what made dragging feel broken on a phone."""
    for page in COURSE_PAGES:
        markup = _page(_course(), page)
        assert "#map{position:relative" in markup
        assert "touch-action:none}" in markup.split("#map{position:relative")[1][:200]


def test_tools_take_the_map_out_of_drag_while_they_are_active():
    """The mode name changed and this check was left asking for the old one,
    so the map stayed draggable and swallowed every erase and pen stroke."""
    edit = _page(_course(), "edit")

    assert "editMode === 'erase' || editMode === 'pen'" in edit
    assert "editMode === 'segment'" not in edit
