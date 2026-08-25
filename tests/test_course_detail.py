"""Course detail page: what a runner needs, in the order they need it.

The page answers three questions in sequence -- what kind of run is this,
what will I find on the way, and how do I actually start it. These tests pin
that order and the live-update parity that keeps an edited course honest.
"""

import json

from runart.course import generate_course
from runart.insights import course_facts
from runart.models import CourseParams
from runart.render import course_edit_summary, preview_html, route_points

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="서울시청")


def _course(**overrides):
    params = {"distance_km": 5.0, **overrides}
    return generate_course(CourseParams(**CITY_HALL, **params))


def _facilities(course):
    from runart.facilities import facilities_along

    return facilities_along(route_points(course),
                            ["convenience_store", "restroom"], limit=80)


def test_character_panel_spells_out_traits_instead_of_bare_emoji():
    """An emoji badge row tells a runner nothing they can act on."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example")
    facts = course_facts(course)

    assert "이 코스는 이런 코스예요" in page
    assert 'id="courseTraits"' in page
    for trait in facts.traits:
        assert trait["label"] in page


def test_good_points_and_caveats_are_separate_labelled_groups():
    """Merging them into one list is how a caveat gets read as a feature."""
    course = _course()
    page = preview_html(course, _facilities(course), "https://runnywhere.example")

    assert "좋은 점" in page and "참고할 점" in page
    assert 'id="courseGood"' in page and 'id="courseCare"' in page


def test_headline_row_shows_distance_time_and_climb_together():
    """These three decide whether the run fits the runner's day."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example")

    assert 'class="headline-stats"' in page
    for element_id in ("mLength", "mDuration", "mAscent"):
        assert f'id="{element_id}"' in page
    # The climb must not also sit in the lower metric grid.
    assert page.count('id="mAscent"') == 1
    assert "예상 시간" in page and "누적 오르막" in page


def test_facilities_are_listed_in_course_order_with_their_km_mark():
    course = _course()
    facilities = _facilities(course)
    page = preview_html(course, facilities, "https://runnywhere.example")

    assert "러닝 중 편의시설" in page
    assert 'id="facilityList"' in page
    if facilities:
        first = facilities[0]
        assert f"{first['at_km']:g}km" in page
    else:
        assert "편의점·화장실이 없어요" in page


def test_how_to_run_section_keeps_only_the_other_ways_to_run_it():
    """Running it here is the product; GPX and Kakao Map are the escape
    hatches for people who want another app, and they close the run page."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example", page="run")

    assert "다른 앱으로 달리기" in page
    assert "GPX 파일 받기" in page and "카카오맵에서 열기" in page
    # The summary table repeated numbers the page had already shown twice.
    assert "코스 정보 요약" not in page
    assert 'id="summaryStart"' not in page


def test_edit_summary_carries_every_panel_the_page_now_shows():
    """An edited course must not leave stale traits or caveats on screen."""
    course = _course()
    summary = course_edit_summary(course)
    page = preview_html(course, _facilities(course), "https://runnywhere.example")

    for key in ("traits", "highlights", "cautions", "facility_rows",
                "duration_min", "signals"):
        assert key in summary
    assert summary["traits"] == [dict(t) for t in course_facts(course).traits]
    # The browser has to be told to repaint them, not just handed the data.
    for hook in ("courseTraits", "courseGood", "courseCare", "facilityList",
                 "gpxLink"):
        assert hook in page
    assert json.dumps(summary, ensure_ascii=False)  # serialisable for the page


def test_animal_course_detail_keeps_its_silhouette_and_gains_the_panels():
    course = _course()
    course.params = course.params.model_copy(update={"shape": "dog"})
    page = preview_html(course, [], "https://runnywhere.example")

    assert "동물 실루엣" in page
    assert "이 코스는 이런 코스예요" in page


def test_page_shows_that_the_course_returns_to_where_it_started():
    """AllTrails gives "Circular trail" a stat slot. Runnywhere says it twice
    already -- in the start line and by naming the same station at both ends
    of the facility list -- so it earns no third row of its own."""
    course = _course()
    page = preview_html(course, _facilities(course), "https://runnywhere.example")
    start = course.params.location_name

    assert f"{start} 출발·도착" in page
    assert f"{start} 출발</span>" in page
    assert f"{start} 도착</span>" in page


def test_facility_list_is_bracketed_by_the_start_and_the_finish():
    """komoot brackets its waypoints with Starting Point / End Point. A list
    that begins at 1.9km leaves the runner guessing where 0 and the finish are."""
    course = _course()
    page = preview_html(course, _facilities(course), "https://runnywhere.example")

    assert 'class="facility-row anchor"' in page
    assert "출발" in page and "도착" in page


def test_pace_track_says_which_end_is_faster():
    """Alma labels its rate slider Slower / Faster and pins the recommended
    value. A bare gradient track cannot be read without dragging it."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example", page="run")

    assert 'class="pace-scale"' in page
    assert "느리게" in page and "빠르게" in page
    assert "기본" in page


def test_run_page_start_control_drives_the_tracking_handler():
    """The product is running the course here, with location."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example", page="run")

    assert 'class="run-start"' in page
    assert 'id="runCta"' in page
    # It drives the same tracking handler the map control uses.
    assert "const startRun = () =>" in page
    assert "runCta.addEventListener('click', startRun)" in page
    assert "startBtn.addEventListener('click', startRun)" in page
    assert 'href="#howto"' not in page


def test_live_tracking_status_is_visible_not_only_announced():
    """"코스에서 약 120m 벗어남" is the whole point of running it here. It
    used to exist only in an sr-only node, where nobody could see it."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example", page="run")

    assert 'id="runStatus"' in page
    assert 'class="run-hud"' in page
    assert 'id="runStatus" class="sr-only"' not in page
    # It must outlive the map-error path, which replaces #map wholesale.
    assert page.index('class="run-hud"') > page.index('id="map"')
    assert 'class="map-wrap"' in page


def test_summary_numbers_are_not_wrapped_in_their_own_boxes():
    """Runna, adidas and AllTrails all set run stats straight on the surface.
    A border around every number turns a page into a spreadsheet."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example")

    # The three figure groups must not each carry their own border+fill.
    for boxed in (
        ".course-metrics>div{min-width:0;padding:12px;border:",
        ".fact{border:1px solid",
        ".pace-picker{border:1px solid",
    ):
        assert boxed not in page
    assert page.count("border-radius:14px;background:#f7faf5") == 0


def test_run_cta_explains_itself_when_the_map_cannot_load():
    """Every tracking control lives inside kakao.maps.load(); without a map
    the bar's primary action has nothing behind it and must say so rather
    than absorbing taps in silence."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example")

    assert "deadCta.disabled = true" in page
    assert "지도 연결 필요" in page
    assert "실시간 코스 안내를 시작할 수 없어요" in page


def test_edited_course_hands_out_the_edited_file_not_the_original():
    """The panels follow an edit but the download link used to keep the
    original course id, so a runner who edited and tapped GPX without saving
    got the route they had just changed away from."""
    from runart.models import decode_course_id

    course = _course()
    summary = course_edit_summary(course)
    page = preview_html(course, [], "https://runnywhere.example", page="run")

    assert "course_id" in summary
    assert decode_course_id(summary["course_id"]).canonical() == course.params.canonical()
    # And the page knows how to repoint the link when a summary arrives.
    assert 'id="gpxLink"' in page
    assert "setCourseLinks(" in page


def test_page_drops_the_copy_that_only_narrated_the_interface():
    """Captions that restate what the next element already shows cost height
    on a phone and are read once, if ever."""
    course = _course()
    page = preview_html(course, _facilities(course), "https://runnywhere.example")

    for narration in (
        "아래 숫자가 이 페이스에 맞춰 바뀌어요",
        "실제 통행·공사 상황을 확인하고",
        "코스 10m 안 · 편의점",
        "시계나 쓰던 앱으로 뛰고 싶다면",
        "원하는 방법을 선택해 주세요",
        "러니웨어 팁",
    ):
        assert narration not in page


def test_pace_keeps_its_band_name_but_not_the_band_buttons():
    """The tier beside the pace already names the effort; five buttons that
    jump to the same values are a second control for one number."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example", page="run")

    assert 'id="paceTier"' in page
    assert 'class="pace-chips"' not in page
    assert 'class="pace-chip"' not in page


def test_elevation_profile_is_gone_and_its_band_stays_a_number():
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example")

    page_run = preview_html(course, [], "https://runnywhere.example", page="run")
    assert 'class="profile"' not in page
    assert 'class="profile-axis"' not in page
    assert "고도 프로파일" not in page
    assert 'id="mElev"' in page_run  # the range survives as one figure


def test_gpx_is_offered_once_and_only_where_a_runner_is_about_to_run():
    """Taking the course to another app is decided while looking at running
    it, so it closes the run page and appears nowhere else."""
    from runart.models import encode_course_id

    course = _course()
    course_id = encode_course_id(course.params)

    run = preview_html(course, [], "https://runnywhere.example", page="run")
    assert run.count(f"/c/{course_id}.gpx") == 1
    assert "위 GPX 파일 받기를 눌러" in run
    for page in ("info", "edit"):
        other = preview_html(course, [], "https://runnywhere.example", page=page)
        assert f"/c/{course_id}.gpx" not in other


def test_sharing_an_edited_course_sends_the_edited_link():
    """Share had the original id baked into the handler, so an edited course
    was shared as the route the runner had changed away from."""
    course = _course()
    page = preview_html(course, [], "https://runnywhere.example")

    assert "let currentCourseUrl =" in page
    assert "const url = currentCourseUrl;" in page
    assert "currentCourseUrl = " in page.split("const setCourseLinks")[1]
