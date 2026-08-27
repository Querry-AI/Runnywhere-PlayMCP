"""Case matrix for an animal course request: what we offer when the exact
course does not exist at the requested start.

The product rule the plan encodes (three cases, three ordered choices each):

1. exact  — the requested animal exists at the requested start
            → requested animal, another animal here, plain course here
2. nearby — not here, but a verified preset is within 2km
            → nearest same-animal, nearest any-animal, plain course here
3. far    — nothing within 2km
            → plain course here, nearest same-animal, nearest any-animal
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
    assert [choice.kind for choice in plan.alternatives] == [
        "other_animal", "standard"]
    assert plan.alternatives[0].course is here_rabbit
    assert plan.alternatives[1].course is standard
    assert "강남역" in plan.lead and "강아지" in plan.lead


def test_nearby_case_leads_with_the_closest_same_animal_and_names_the_detour():
    near_dog = _course(location_name="낙성대(강감찬)역", shape="dog")
    near_cat = _course(location_name="봉천역", shape="cat", km=6.0)
    standard = _course(location_name="서울대입구역", shape=None)

    plan = build_course_plan(
        requested_name="서울대입구역", shape="dog", exact=None,
        shape_matches=[PresetMatch(near_dog, 400.0)],
        animal_matches=[PresetMatch(near_dog, 400.0), PresetMatch(near_cat, 900.0)],
        standard=standard,
    )

    assert plan.case == "nearby"
    assert plan.primary.course is near_dog
    assert plan.primary.kind == "requested_animal"
    assert plan.primary.distance_m == pytest.approx(400.0)
    assert [choice.kind for choice in plan.alternatives] == [
        "other_animal", "standard"]
    assert plan.alternatives[0].course is near_cat
    # The lead must never let the model claim the requested start.
    assert "낙성대(강감찬)역" in plan.lead
    assert "0.4km" in plan.lead


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
        "requested_animal", "other_animal"]
    assert plan.alternatives[0].course is far_dog
    assert plan.alternatives[1].course is far_whale
    assert "1~2km" in plan.lead and "강아지" in plan.lead
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

    assert inside.case == "nearby"
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
    assert [choice.kind for choice in plan.alternatives] == ["standard"]


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
    assert "1.0km 떨어진 봉천역" in plan.lead
