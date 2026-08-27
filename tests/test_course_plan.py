"""Full matches first, then start/effort > shape > optional characteristics.

The same ordering applies to the primary card and every alternative.
"""

import pytest

from runart.animal_presets import PresetMatch
from runart.course import Course
from runart.courseplan import (CASE_EXACT, CASE_NEARBY, NEARBY_RADIUS_M,
                               build_course_plan)
from runart.models import CourseParams, encode_course_id


def _course(*, location_name: str, shape: str | None, km: float = 5.0) -> Course:
    params = CourseParams(
        lat=37.4986, lon=127.0281, location_name=location_name,
        distance_km=km, shape=shape,
    )
    return Course(
        params=params, path=[], points=[], length_m=km * 1000.0,
        ascent_m=20.0, rfs={"score": 80, "highlights": []},
        shape_similarity=0.9 if shape else None,
    )


def test_exact_case_leads_with_the_requested_animal_then_offers_alternatives():
    exact = _course(location_name="강남역", shape="dog")
    here_rabbit = _course(location_name="강남역", shape="rabbit", km=4.1)
    standard = _course(location_name="강남역", shape=None)

    plan = build_course_plan(
        requested_name="강남역", shape="dog", exact=exact,
        shape_matches=[PresetMatch(exact, 0.0)],
        animal_matches=[PresetMatch(exact, 0.0), PresetMatch(here_rabbit, 0.0)],
        standard=standard,
    )

    assert plan.case == "exact"
    assert plan.primary.course is exact
    assert plan.primary.kind == "requested_animal"
    assert {c.course_id for c in plan.alternatives} == {
        encode_course_id(here_rabbit.params), encode_course_id(standard.params)}
    assert "강남역" in plan.lead


def test_local_plain_course_precedes_nearby_animals_and_names_their_detours():
    near_dog = _course(location_name="낙성대(강감찬)역", shape="dog")
    near_cat = _course(location_name="봉천역", shape="cat", km=6.0)
    standard = _course(location_name="서울대입구역", shape=None)

    plan = build_course_plan(
        requested_name="서울대입구역", shape="dog", exact=None,
        shape_matches=[PresetMatch(near_dog, 400.0)],
        animal_matches=[PresetMatch(near_dog, 400.0), PresetMatch(near_cat, 900.0)],
        standard=standard,
    )

    assert plan.case == "far"
    assert plan.primary.course is standard
    assert plan.primary.kind == "standard"
    assert plan.primary.distance_m == 0
    assert [choice.kind for choice in plan.alternatives] == [
        "requested_animal", "other_animal"]
    assert plan.alternatives[0].course is near_dog
    assert "0.4km 이동" in plan.alternatives[0].match_note
    assert "장소·시간(거리)을 먼저" in plan.lead


def test_far_case_leads_with_the_plain_course_and_says_nothing_is_within_2km():
    far_dog = _course(location_name="잠실역", shape="dog")
    far_whale = _course(location_name="석촌역", shape="whale", km=7.0)
    standard = _course(location_name="응암역", shape=None)

    plan = build_course_plan(
        requested_name="응암역", shape="dog", exact=None,
        shape_matches=[PresetMatch(far_dog, 14_000.0)],
        animal_matches=[PresetMatch(far_dog, 14_000.0),
                        PresetMatch(far_whale, 13_000.0)],
        standard=standard,
    )

    assert plan.case == "far"
    assert plan.primary.course is standard
    assert plan.primary.kind == "standard"
    assert [choice.kind for choice in plan.alternatives] == [
        "other_animal", "requested_animal"]
    assert plan.alternatives[0].course is far_whale
    assert plan.alternatives[1].course is far_dog
    assert "일반 코스 대안" in plan.lead
    assert "응암역" in plan.lead


def test_nearby_radius_boundary_is_two_kilometres():
    dog = _course(location_name="사당역", shape="dog")
    standard = _course(location_name="이수역", shape=None)
    kwargs = dict(requested_name="이수역", shape="dog", exact=None,
                  animal_matches=[], standard=standard)

    inside = build_course_plan(
        shape_matches=[PresetMatch(dog, NEARBY_RADIUS_M - 1.0)], **kwargs)
    outside = build_course_plan(
        shape_matches=[PresetMatch(dog, NEARBY_RADIUS_M + 1.0)], **kwargs)

    assert inside.primary.course is standard
    assert outside.primary.course is standard
    assert inside.case == "far"
    assert outside.case == "far"


def test_plan_never_repeats_one_course_across_choices():
    dog = _course(location_name="낙성대(강감찬)역", shape="dog")
    standard = _course(location_name="서울대입구역", shape=None)

    plan = build_course_plan(
        requested_name="서울대입구역", shape="dog", exact=None,
        shape_matches=[PresetMatch(dog, 400.0)],
        # The same course is also the nearest animal course of any shape.
        animal_matches=[PresetMatch(dog, 400.0)],
        standard=standard,
    )

    ids = [plan.primary.course_id] + [c.course_id for c in plan.alternatives]
    assert len(ids) == len(set(ids))
    assert [choice.kind for choice in plan.alternatives] == ["requested_animal"]


def test_plan_degrades_when_the_plain_course_is_unavailable():
    """A generation timeout on the plain course must not lose the animal."""
    near_dog = _course(location_name="낙성대(강감찬)역", shape="dog")

    plan = build_course_plan(
        requested_name="서울대입구역", shape="dog", exact=None,
        shape_matches=[PresetMatch(near_dog, 400.0)],
        animal_matches=[PresetMatch(near_dog, 400.0)],
        standard=None,
    )

    assert plan.case == "nearby"
    assert plan.primary.course is near_dog
    assert plan.alternatives == ()


def test_plan_is_none_when_there_is_nothing_to_offer():
    assert build_course_plan(
        requested_name="응암역", shape="dog", exact=None,
        shape_matches=[], animal_matches=[], standard=None,
    ) is None


def test_choice_ids_round_trip_to_the_course_that_produced_them():
    exact = _course(location_name="강남역", shape="dog")
    plan = build_course_plan(
        requested_name="강남역", shape="dog", exact=exact,
        shape_matches=[PresetMatch(exact, 0.0)], animal_matches=[], standard=None,
    )
    assert plan.primary.course_id == encode_course_id(exact.params)


def test_a_verified_course_at_the_requested_start_is_not_called_a_detour():
    """"0.0km 떨어진 시청역" is a sentence that argues with itself.

    A preset sitting at the requested start is that start's course. Station
    exits are tens of metres apart, which is why SAME_START_M exists; the case
    split has to use it too, or the lead says the course is both absent from
    here and zero kilometres away.
    """
    here = _course(location_name="시청역", shape="cat")
    plan = build_course_plan(
        requested_name="시청",
        shape="cat",
        exact=None,
        shape_matches=[PresetMatch(here, 0.0)],
        animal_matches=[],
        standard=None,
    )

    assert plan.case == CASE_EXACT
    assert "떨어진" not in plan.lead
    assert "없어서" not in plan.lead
    assert "시청" in plan.lead


def test_a_course_a_real_walk_away_is_still_called_nearby():
    plan = build_course_plan(
        requested_name="서울대입구역",
        shape="whale",
        exact=None,
        shape_matches=[PresetMatch(_course(location_name="봉천역", shape="whale"), 1000.0)],
        animal_matches=[],
        standard=None,
    )

    assert plan.case == CASE_NEARBY
    assert "봉천역" in plan.lead and "1.0km 이동" in plan.lead


def test_actual_distance_beats_requested_shape_even_at_the_same_start():
    dog = _course(location_name="시청", shape="dog", km=12)
    dog.params.distance_km = 5  # Input metadata is not the routed distance.
    plain = _course(location_name="시청", shape=None, km=5.1)
    plan = build_course_plan(requested_name="시청", shape="dog", exact=dog,
        shape_matches=[], animal_matches=[], standard=plain, distance_km=5)
    assert plan.primary.course is plain
    assert "요청 5km → 12.0km" in plan.alternatives[0].match_note


def test_shape_beats_preferences_once_start_and_effort_match():
    dog = _course(location_name="시청", shape="dog", km=5.2)
    plain = _course(location_name="시청", shape=None, km=5)
    plain.rfs = {"components": {"lighting": .9, "cctv": .9}}
    plan = build_course_plan(requested_name="시청", shape="dog", exact=dog,
        shape_matches=[], animal_matches=[], standard=plain, distance_km=5,
        night_mode=True)
    assert plan.primary.course is dog
    assert "선택 특징 일부" in plan.primary.match_note


def test_full_match_wins_and_optional_features_break_shape_ties():
    dog = _course(location_name="시청", shape="dog", km=5)
    lit_dog = _course(location_name="시청", shape="dog", km=5.3)
    lit_dog.rfs = {"components": {"lighting": .9, "cctv": .9}}
    plan = build_course_plan(requested_name="시청", shape="dog", exact=dog,
        shape_matches=[PresetMatch(lit_dog, 0)], animal_matches=[], standard=None,
        distance_km=5, night_mode=True)
    assert plan.primary.course is lit_dog
    assert plan.primary.match_note == "요청 조건 일치"


@pytest.mark.parametrize("kind", ["dog", "best_animal", "standard"])
def test_time_request_uses_requested_effort_for_every_course_type(kind):
    short = _course(location_name="시청", shape=None, km=5)
    correct = _course(location_name="시청", shape="dog", km=6.2)
    plan = build_course_plan(requested_name="시청", shape=kind, exact=correct,
        shape_matches=[], animal_matches=[], standard=short, duration_min=40)
    assert plan.primary.course is correct
    assert "요청 40분" in plan.alternatives[0].match_note
    plan = build_course_plan(requested_name="시청", shape=kind, exact=correct,
        shape_matches=[], animal_matches=[], standard=short, duration_min=40, distance_km=5)
    assert plan.primary.course is short


def test_all_alternatives_rank_start_and_effort_before_shape():
    plain = _course(location_name="시청", shape=None)
    far_dog = _course(location_name="강남", shape="dog")
    here_cat = _course(location_name="시청", shape="cat")
    plan = build_course_plan(requested_name="시청", shape="dog", exact=None,
        shape_matches=[PresetMatch(far_dog, 5000)],
        animal_matches=[PresetMatch(far_dog, 5000), PresetMatch(here_cat, 0)],
        standard=plain, distance_km=5)
    assert {plan.primary.course_id, plan.alternatives[0].course_id} == {
        encode_course_id(plain.params), encode_course_id(here_cat.params)}
    assert plan.alternatives[-1].course is far_dog
