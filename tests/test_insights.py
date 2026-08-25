"""Derived course character: readable traits, good points and caveats.

Everything here must come from data the course already carries (RFS
components, ascent, crossings, facilities). A sentence the runner cannot
verify on the map is worse than no sentence.
"""

import pytest

from runart.course import generate_course
from runart.insights import (
    NOTE_LIMIT,
    TRAIT_LIMIT,
    CourseFacts,
    course_facts,
)
from runart.models import CourseParams

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="서울시청")


def _course(**overrides):
    params = {"distance_km": 5.0, **overrides}
    return generate_course(CourseParams(**CITY_HALL, **params))


def test_traits_are_labelled_bounded_and_lead_with_terrain():
    facts = course_facts(_course())

    assert isinstance(facts, CourseFacts)
    assert 1 <= len(facts.traits) <= TRAIT_LIMIT
    assert all(trait["label"] and trait["emoji"] for trait in facts.traits)
    # Grade is the first thing a runner filters on, so it leads.
    assert facts.traits[0]["label"] in ("평지 위주", "완만한 경사", "오르막 포함")
    # Chips are UI labels: single line, no markup, no duplicates.
    labels = [trait["label"] for trait in facts.traits]
    assert len(labels) == len(set(labels))
    assert all(len(label) <= 12 and "\n" not in label for label in labels)


def test_night_mode_course_says_so_in_its_traits():
    facts = course_facts(_course(night_mode=True))
    assert any("야간" in trait["label"] for trait in facts.traits)


def test_notes_are_bounded_full_sentences_and_never_both_empty():
    facts = course_facts(_course())

    assert len(facts.highlights) <= NOTE_LIMIT
    assert len(facts.cautions) <= NOTE_LIMIT
    assert facts.highlights or facts.cautions
    for note in (*facts.highlights, *facts.cautions):
        assert note.endswith(("요.", "다.", "세요.")) and len(note) <= 90


def test_notes_only_state_numbers_the_course_actually_has():
    course = _course()
    facts = course_facts(course)
    joined = " ".join((*facts.highlights, *facts.cautions))

    if f"{facts.signals}" in joined:
        assert facts.signals >= 0
    # A missing facility is a caveat, never a highlight.
    if facts.facility_counts["restroom"] == 0:
        assert any("화장실" in note for note in facts.cautions)
        assert not any("화장실" in note for note in facts.highlights)


def test_hilly_course_is_described_as_a_climb_not_as_flat():
    hilly = _course(include_hills=True, distance_km=6.0)
    facts = course_facts(hilly)
    grade = facts.traits[0]["label"]

    per_km = hilly.ascent_m / hilly.length_km
    if per_km >= 15.0:
        assert grade == "오르막 포함"
        assert any("오르막" in note for note in facts.cautions)


def test_facts_survive_a_presentation_only_course_without_a_graph_path():
    """The widget layer builds cards from cached courses that carry no path."""
    from runart.course import Course

    bare = Course(
        params=CourseParams(**CITY_HALL, distance_km=9.0, shape="dog"),
        path=[],
        points=[],
        length_m=9_040.0,
        ascent_m=31.0,
        rfs={"score": 86, "highlights": []},
    )
    facts = course_facts(bare)

    assert facts.signals == 0
    assert facts.facility_counts == {"convenience_store": 0, "restroom": 0}
    assert facts.traits  # grade is computable from length and ascent alone


def test_facts_are_deterministic_for_the_same_course():
    course = _course()
    assert course_facts(course) == course_facts(course)


@pytest.mark.parametrize("shape", ["dog", "cat"])
def test_animal_courses_get_facts_too(shape):
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    course.params = course.params.model_copy(update={"shape": shape})
    assert course_facts(course).traits


def test_a_typical_seoul_course_gets_no_surface_quality_chip():
    """Seoul's lighting data sits citywide below the 0.5 no-data default. A
    chip that fires on three courses in four describes the dataset, not the
    route, and stops helping a runner compare two of them."""
    from runart.course import Course
    from runart.insights import COMPONENT_BANDS, course_traits

    median = Course(
        params=CourseParams(**CITY_HALL, distance_km=5.0),
        path=[], points=[], length_m=5_000.0, ascent_m=20.0,
        # Measured citywide medians.
        rfs={"components": {"lighting": 0.33, "cctv": 0.30,
                            "sidewalk": 0.49, "crossing": 0.45},
             "park_ratio": 0.13},
    )
    labels = [trait["label"] for trait in course_traits(median)]

    assert labels == ["평지 위주", "도심 위주"]
    # cctv is constant across the whole city; it can never distinguish courses.
    assert "cctv" not in COMPONENT_BANDS


def test_an_unusually_dark_or_bright_course_still_says_so():
    from runart.course import Course
    from runart.insights import course_traits

    def traits_for(lighting):
        course = Course(
            params=CourseParams(**CITY_HALL, distance_km=5.0),
            path=[], points=[], length_m=5_000.0, ascent_m=20.0,
            rfs={"components": {"lighting": lighting}, "park_ratio": 0.0},
        )
        return [trait["label"] for trait in course_traits(course)]

    assert "조명 어두움" in traits_for(0.30)
    assert "조명 좋음" in traits_for(0.72)


def test_a_localised_caution_says_where_on_the_route_it_applies():
    """komoot's route alerts name the segment ("After 3.4 mi for 1.86 mi").
    "조명이 부족한 구간이 있어요" without a position is not checkable against
    the map, so a runner cannot act on it."""
    course = _course()
    facts = course_facts(course)
    located = [note for note in facts.cautions if "지점부터" in note]

    for note in located:
        # Every located caution must name a start km inside the course.
        km = float(note.split("km 지점부터")[0].split()[-1])
        assert 0 <= km <= course.length_km


def test_a_weak_stretch_is_only_named_when_it_is_actually_localised():
    """A route that is uniformly dim has no "dark stretch" to point at; saying
    "0km 지점부터 6.5km" would be a worse sentence than the general one."""
    from runart.insights import LOCALISED_MAX_SHARE, weak_stretch

    edges = [(0.0, 200.0, 0.2)] * 10          # (start_m, length_m, score)
    assert weak_stretch(_uniform(edges), "lighting") is None


def _uniform(_edges):
    """A course whose every edge is equally poor -- no stretch stands out."""
    from runart.course import Course

    return Course(
        params=CourseParams(**CITY_HALL, distance_km=2.0),
        path=[], points=[], length_m=2000.0, ascent_m=5.0,
        rfs={"components": {"lighting": 0.2}},
    )


def test_a_course_that_doubles_back_says_so():
    """Park footpaths are often a tree, so the only loop of the right length
    walks out and back. Generation prefers real circuits; when none exists the
    runner hears it rather than discovering it at the turnaround."""
    from runart.course import retrace_share
    from runart import graph as graphmod

    course = _course()
    retraced = retrace_share(graphmod.get_graph(), course.path)
    cautions = course_facts(course).cautions

    if retraced >= 0.25:
        assert any("되돌아오는" in note for note in cautions)
    else:
        assert not any("되돌아오는" in note for note in cautions)
