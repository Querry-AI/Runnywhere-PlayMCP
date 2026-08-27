"""Rank real courses by start/effort, then shape, then optional preferences."""
from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import groupby
from typing import Sequence

from .animal_presets import PresetMatch
from .course import Course
from .facilities import facility_requirement_score
from .models import DEFAULT_PACE_MIN_PER_KM, encode_course_id
from .naming import TRACK_EMOJI, course_title
from .shapes import SHAPES

NEARBY_RADIUS_M = 2000.0
SAME_START_M = 150.0
EFFORT_TOLERANCE = 0.10
CASE_EXACT = "exact"
CASE_NEARBY = "nearby"
CASE_FAR = "far"
KIND_REQUESTED = "requested_animal"
KIND_OTHER = "other_animal"
KIND_STANDARD = "standard"


@dataclass(frozen=True)
class CourseChoice:
    course: Course
    course_id: str
    kind: str
    distance_m: float = 0.0
    match_note: str = ""

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


def requested_distance(distance_km: float | None,
                       duration_min: float | None) -> float | None:
    """Use the same conversion as generation; explicit distance wins."""
    if distance_km is not None:
        return distance_km
    return round(duration_min / DEFAULT_PACE_MIN_PER_KM, 1) if duration_min else None


def _preference_misses(course: Course, *, include_hills: bool,
                       night_mode: bool, need_facilities: Sequence[str]) -> int:
    # Measured route properties, not the flags with which it was requested.
    hits, wanted = facility_requirement_score(course.points, list(need_facilities))
    misses = wanted - hits
    if include_hills and course.is_flat:
        misses += 1
    # Use the same 0.6 threshold as RFS highlights; absent data is unverified.
    if night_mode and any(course.rfs.get("components", {}).get(key, 0) < 0.60
                          for key in ("lighting", "cctv")):
        misses += 1
    return misses


def build_course_plan(
    *, requested_name: str | None, shape: str | None,
    exact: Course | None, shape_matches: Sequence[PresetMatch],
    animal_matches: Sequence[PresetMatch], standard: Course | None,
    distance_km: float | None = None, duration_min: float | None = None,
    include_hills: bool = False, night_mode: bool = False,
    need_facilities: Sequence[str] = (),
) -> CoursePlan | None:
    """Full matches first; rank all alternatives with the same priority.

    Start (150m station-exit tolerance) and actual distance (±10%) form the
    first tier. Among matches in that tier shape wins, then preferences.
    If no candidate matches both, fewer violations and smaller normalized
    deviations win before shape or preferences. Unspecified animal distance
    stays open rather than silently imposing a new 5km constraint.
    """
    target = requested_distance(distance_km, duration_min)
    wants_animal = shape not in (None, "standard")
    name = requested_name or "요청한 출발지"
    candidates: dict[str, CourseChoice] = {}

    def add(course: Course | None, moved: float) -> None:
        if course is None:
            return
        kind = (KIND_STANDARD if not course.params.shape else
                KIND_REQUESTED if shape == "best_animal" or course.params.shape == shape
                else KIND_OTHER)
        cid = encode_course_id(course.params)
        if cid not in candidates or moved < candidates[cid].distance_m:
            candidates[cid] = CourseChoice(course, cid, kind, moved)

    add(exact, 0)
    add(standard, 0)
    for match in (*shape_matches, *animal_matches):
        add(match.course, match.distance_m)

    def evaluate(choice: CourseChoice, *, with_preferences: bool = True) -> tuple[tuple, CourseChoice]:
        course = choice.course
        error = abs(course.length_km - target) / target if target else 0
        effort_miss = error > EFFORT_TOLERANCE + 1e-9
        moved = choice.is_detour
        shape_miss = (not course.params.shape if shape == "best_animal" else
                      course.params.shape != (shape if wants_animal else None))
        features = (_preference_misses(course, include_hills=include_hills,
                    night_mode=night_mode, need_facilities=need_facilities)
                    if with_preferences else 0)
        notes = []
        if moved:
            notes.append(f"출발지 {choice.distance_m / 1000:.1f}km 이동")
        if effort_miss:
            if duration_min and distance_km is None:
                minutes = round(course.length_km * DEFAULT_PACE_MIN_PER_KM)
                notes.append(f"요청 {duration_min:g}분 → 약 {minutes}분")
            else:
                notes.append(f"요청 {target:g}km → {course.length_km:.1f}km")
        if shape_miss:
            notes.append("일반 코스 대안" if not course.params.shape else "다른 모양 대안")
        if features:
            notes.append("선택 특징 일부 미충족·미확인")
        if not notes:
            notes.append("요청 조건 일치" if target else "요청 장소·모양 일치")
        score = (
            int(moved) + int(effort_miss),
            max(max(0, choice.distance_m / SAME_START_M - 1) if moved else 0,
                max(0, error / EFFORT_TOLERANCE - 1) if effort_miss else 0),
            int(shape_miss), features, choice.distance_m, error, choice.course_id,
        )
        return score, replace(choice, match_note=" · ".join(notes))

    # Facilities need a geometry scan. Only evaluate tied priority groups that
    # can reach the three visible cards, never hundreds of already-lower tiers.
    ordered = sorted((evaluate(c, with_preferences=False) for c in candidates.values()),
                     key=lambda pair: pair[0])
    ranked = []
    for _, group in groupby(ordered, key=lambda pair: pair[0][:3]):
        ranked.extend(sorted((evaluate(c) for _, c in group), key=lambda pair: pair[0]))
        if len(ranked) >= 3:
            break
    if not ranked:
        return None
    primary_score, primary = ranked[0]
    case = (CASE_NEARBY if primary.is_detour else
            CASE_EXACT if not primary_score[0] and not primary_score[2] else CASE_FAR)
    spec = SHAPES.get(shape or "")
    shape_name = spec.name_ko if spec else "동물 모양" if wants_animal else "일반"
    target_text = (f"약 {duration_min:g}분" if duration_min and distance_km is None else
                   f"{target:g}km" if target else "거리 지정 없이" if wants_animal else "기본 5km")
    if primary_score[0] == 0 and primary_score[2] == 0 and primary_score[3] == 0:
        lead = f"{name}에서 {target_text} 기준으로 {shape_name} 코스를 먼저 골랐어요."
    elif wants_animal and (primary.is_detour or primary_score[2]):
        shape_label = f"{shape_name} 모양" if spec else shape_name
        local_requested = any(c.kind == KIND_REQUESTED and not c.is_detour
                              for c in candidates.values())
        if not local_requested:
            reason = f"{name}에서 출발하는 {shape_label} 코스를 찾지 못해"
        elif target:
            reason = f"{name}의 {shape_label} 코스가 요청한 {target_text}에 맞지 않아"
        else:
            reason = f"{name}에서 요청 조건에 맞는 {shape_label} 코스를 찾지 못해"
        lead = (f"{reason} 다른 추천 코스를 준비했어요. "
                f"첫 코스: {primary.start_name} · {primary.match_note}.")
    else:
        lead = (f"요청한 {shape_name} 코스를 찾되, {name}의 장소·시간(거리)을 먼저, "
                "모양과 선택 특징을 그다음으로 고려했어요. "
                f"첫 코스: {primary.start_name} · {primary.match_note}.")
    return CoursePlan(case, lead, primary, tuple(c for _, c in ranked[1:3]))
