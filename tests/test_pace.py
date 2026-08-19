import pytest

from runart.course import generate_course
from runart.models import CourseParams
from runart.pace import (DEFAULT_PACE_S, FASTEST_PACE_S, PACE_MODEL, SLOWEST_PACE_S,
                         calories, cadence_spm, clamp_pace_s, effort, format_pace,
                         met, pace_tier, steps)
from runart.render import preview_html

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="서울시청")


def test_default_pace_is_seven_minutes_and_reads_as_jogging():
    assert DEFAULT_PACE_S == 420
    assert format_pace(DEFAULT_PACE_S) == "7'00\""
    assert pace_tier(DEFAULT_PACE_S)[0] == "조깅"


@pytest.mark.parametrize("seconds, expected", [
    (600, "10'00\""), (540, "9'00\""), (420, "7'00\""), (245, "4'05\"")])
def test_pace_formatting(seconds, expected):
    assert format_pace(seconds) == expected


def test_pace_is_clamped_and_snapped_to_the_step():
    assert clamp_pace_s(10) == FASTEST_PACE_S
    assert clamp_pace_s(99_999) == SLOWEST_PACE_S
    assert clamp_pace_s(423) == 420
    assert clamp_pace_s(426) == 430


def test_every_tier_is_reachable_within_the_slider_range():
    seen = {pace_tier(s)[0] for s in range(FASTEST_PACE_S, SLOWEST_PACE_S + 1, 10)}
    assert seen == {t["name"] for t in PACE_MODEL["tiers"]}


def test_faster_pace_means_less_time_and_fewer_steps():
    """Stride lengthens as pace quickens, so a faster run takes fewer steps."""
    fast, slow = effort(5.0, 300), effort(5.0, 540)
    assert fast["duration_min"] < slow["duration_min"]
    assert fast["steps"] < slow["steps"]
    assert cadence_spm(300) > cadence_spm(540)      # ...but at a higher cadence


def test_met_rises_with_speed():
    """An interpolated MET table made calories FALL as pace quickened; the
    ACSM running equation is linear in speed and does not."""
    assert met(600) < met(420) < met(240)
    assert met(450) == pytest.approx(0.952 * 8.0 + 1.0, abs=0.05)   # 8 km/h


def test_calories_stay_in_a_believable_band_across_the_whole_range():
    """Energy cost per km is roughly flat for running; only mildly higher when
    slower. A model that swings wildly with pace is wrong."""
    values = [calories(5.0, s) for s in range(FASTEST_PACE_S, SLOWEST_PACE_S + 1, 30)]
    assert min(values) > 0
    assert max(values) / min(values) < 1.20
    # ~1 kcal per kg per km is the accepted rule of thumb
    per_kg_km = calories(5.0, DEFAULT_PACE_S) / (PACE_MODEL["weight_kg"] * 5.0)
    assert 0.9 < per_kg_km < 1.3


def test_steps_imply_a_plausible_stride():
    for pace_s in (300, 420, 540):
        stride_m = 1000.0 / (steps(1.0, pace_s))
        assert 0.6 < stride_m < 1.3, (pace_s, stride_m)


def test_page_opens_at_the_default_pace_and_ships_the_shared_model():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    baseline = effort(course.length_km, DEFAULT_PACE_S)

    assert 'id="paceRange"' in page
    assert f'value="{DEFAULT_PACE_S}"' in page
    assert "7'00\"" in page
    assert f'id="mSteps">{baseline["steps"]:,}' in page
    assert f'id="mKcal">{baseline["kcal"]}' in page
    # the browser recomputes from the same constants, not a second model
    assert "const PACE = " in page
    assert '"per_kmh"' in page


def test_page_shows_steps_calories_and_elevation_band():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    for label in ("걸음 수", "칼로리", "고도 범위", "총 오르막"):
        assert label in page
    assert "65kg 기준" in page


def test_running_friendliness_is_gone_from_the_page():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert "러닝 친화도" not in page
    assert 'id="mRfs"' not in page
    assert "strokeColor:color(s)" not in page


def test_no_disclosure_toggles_remain_on_the_page():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert "<details" not in page
    assert "<summary" not in page
