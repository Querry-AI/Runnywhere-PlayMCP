"""Ordered course choices for one animal request.

Pure decision layer between the courses that are already available and the
Kakao widget.  It never generates or restores a course: callers hand it the
candidates they could afford inside the latency budget, and it decides which
one to lead with, which two to offer beside it, and how to say why.

The product rule it encodes -- three cases, three ordered choices each:

1. exact  -- the requested animal exists at the requested start
             -> requested animal, another animal here, plain course here
2. nearby -- not here, but a verified preset is within NEARBY_RADIUS_M
             -> nearest same animal, nearest any animal, plain course here
3. far    -- nothing within NEARBY_RADIUS_M
             -> plain course here, nearest same animal, nearest any animal

A request that has nothing to offer returns None; the caller keeps its own
Markdown guidance in that case.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .animal_presets import PresetMatch
from .course import Course
from .models import encode_course_id
from .naming import TRACK_EMOJI, course_title
from .shapes import SHAPES

# A verified course this far from the requested start still reads as "here,
# roughly" to a runner; beyond it the request has effectively failed.
NEARBY_RADIUS_M = 2000.0
# Different exits of one station sit tens of metres apart. That is the same
# departure, not a detour worth naming.
SAME_START_M = 150.0

CASE_EXACT = "exact"
CASE_NEARBY = "nearby"
CASE_FAR = "far"

KIND_REQUESTED = "requested_animal"
KIND_OTHER = "other_animal"
KIND_STANDARD = "standard"


@dataclass(frozen=True)
class CourseChoice:
    """One clickable course, with the detour it costs from the request."""

    course: Course
    course_id: str
    kind: str
    distance_m: float = 0.0

    @property
    def emoji(self) -> str:
        spec = SHAPES.get(self.course.params.shape or "")
        return spec.emoji if spec else TRACK_EMOJI

    @property
    def title(self) -> str:
        return course_title(self.course)

    @property
    def start_name(self) -> str:
        return self.course.params.location_name or "가까운 출발점"

    @property
    def is_detour(self) -> bool:
        return self.distance_m >= SAME_START_M


@dataclass(frozen=True)
class CoursePlan:
    case: str
    lead: str
    primary: CourseChoice
    alternatives: tuple[CourseChoice, ...]


def _shape_name(shape: str) -> str:
    spec = SHAPES.get(shape)
    return spec.name_ko if spec else "동물"


def _lead_text(case: str, name: str, shape_ko: str, primary: CourseChoice) -> str:
    if case == CASE_EXACT:
        return f"{name}에서 바로 뛸 수 있는 검증된 {shape_ko} 코스를 찾았어요."
    if case == CASE_NEARBY:
        km = primary.distance_m / 1000.0
        return (f"{name}에는 검증된 {shape_ko} 코스가 없어서, "
                f"{km:.1f}km 떨어진 {primary.start_name}의 {shape_ko} 코스를 "
                "골랐어요.")
    missing = f"{name} 주변 1~2km 안에는 검증된 {shape_ko} 코스가 없어요."
    if primary.kind == KIND_STANDARD:
        return f"{missing} 대신 {name}에서 가장 뛰기 좋은 코스를 준비했어요."
    km = primary.distance_m / 1000.0
    return (f"{missing} 가장 가까운 {shape_ko} 코스는 "
            f"{km:.1f}km 떨어진 {primary.start_name}에 있어요.")


def build_course_plan(
    *,
    requested_name: str | None,
    shape: str,
    exact: Course | None,
    shape_matches: Sequence[PresetMatch],
    animal_matches: Sequence[PresetMatch],
    standard: Course | None,
) -> CoursePlan | None:
    """Order the choices for one animal request.

    ``shape_matches`` are verified presets of the requested animal and
    ``animal_matches`` verified presets of any animal, both nearest first and
    both allowed to be empty.  ``exact`` is the requested animal drawn from
    the requested start, and ``standard`` the plain course there; either may
    be None when it could not be produced inside the budget.
    """
    name = requested_name or "요청한 출발지"
    shape_ko = _shape_name(shape)
    taken: set[str] = set()

    def take(course: Course | None, kind: str, distance_m: float
             ) -> CourseChoice | None:
        if course is None:
            return None
        course_id = encode_course_id(course.params)
        if course_id in taken:
            return None
        taken.add(course_id)
        return CourseChoice(course, course_id, kind, distance_m)

    nearest_shape = shape_matches[0] if shape_matches else None

    if exact is not None:
        case = CASE_EXACT
        primary = take(exact, KIND_REQUESTED, 0.0)
    elif nearest_shape is not None and nearest_shape.distance_m <= NEARBY_RADIUS_M:
        # A verified preset standing at the requested start is that start's
        # course, not a trip to somewhere else. Splitting on NEARBY_RADIUS_M
        # alone made the lead argue with itself: "시청에는 검증된 고양이 코스가
        # 없어서, 0.0km 떨어진 시청역의 고양이 코스를 골랐어요." SAME_START_M
        # already draws this line for the card; the case has to use it too.
        case = (CASE_EXACT if nearest_shape.distance_m < SAME_START_M
                else CASE_NEARBY)
        primary = take(nearest_shape.course, KIND_REQUESTED,
                       nearest_shape.distance_m)
    else:
        case = CASE_FAR
        primary = take(standard, KIND_STANDARD, 0.0)
        if primary is None and nearest_shape is not None:
            primary = take(nearest_shape.course, KIND_REQUESTED,
                           nearest_shape.distance_m)
    if primary is None:
        return None

    alternatives: list[CourseChoice] = []
    if case == CASE_FAR:
        if nearest_shape is not None:
            alternatives.append(
                take(nearest_shape.course, KIND_REQUESTED,
                     nearest_shape.distance_m))
    for match in animal_matches:
        choice = take(match.course, KIND_OTHER, match.distance_m)
        if choice is not None:
            alternatives.append(choice)
            break
    if case != CASE_FAR:
        alternatives.append(take(standard, KIND_STANDARD, 0.0))

    return CoursePlan(
        case=case,
        lead=_lead_text(case, name, shape_ko, primary),
        primary=primary,
        alternatives=tuple(c for c in alternatives if c is not None),
    )
