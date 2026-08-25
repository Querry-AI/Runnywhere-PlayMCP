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

    assert 'id="editSave"' in edit and 'id="segmentTool"' in edit
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


def test_selection_actions_are_buttons_not_a_link_inside_a_message():
    """Zillow gives drawing its own Clear button and Uber Eats a trash control;
    an action tucked into a status toast is a message, not a control."""
    edit = _page(_course(), "edit")

    assert 'id="selBar"' in edit
    assert 'id="selReroute"' in edit and 'id="selClear"' in edit
    assert "다른 길로 바꾸기" in edit and "선택 지우기" in edit
    # The toast goes back to reporting, with no action riding on it.
    assert "{label:'다른 길로',persist:true,run:replaceSelected}" not in edit


def test_selection_ends_are_draggable_handles_that_grow_the_selection():
    """Fi's "Drag the points to shape the area" -- one tapped edge is rarely
    the stretch a runner wants to replace."""
    edit = _page(_course(), "edit")

    assert 'class="edit-anchor" data-end=' in edit
    assert "nearestNodeIndex" in edit
    assert "onHandleDown" in edit
    assert "map.setDraggable(false)" in edit
