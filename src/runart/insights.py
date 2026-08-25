"""Readable course character: trait chips, good points and caveats.

Presentation support for the detail page and the Kakao card.  Every sentence
here is derived from something the course already carries -- distance-weighted
RFS components, cumulative ascent, pedestrian crossings, facilities on the
route -- so a runner can check each claim against the map in front of them.
Nothing is generated or estimated here.

Imports course/rfs/facilities and is imported by render/widget/server; nothing
in the routing layers may import it back.
"""

from dataclasses import dataclass

from . import graph as graphmod
from .course import Course, retrace_share
from .facilities import facilities_along
from .geo import haversine_m
from .infrastructure import pedestrian_signals_crossed
from .naming import GREEN_SHARE_MIN, green_share

# Four chips is one comfortable line on a phone; past that they wrap into a
# block that competes with the headline numbers.
TRAIT_LIMIT = 4
NOTE_LIMIT = 3

FACT_FACILITY_TYPES = ("convenience_store", "restroom")
# Facility lookups are for display, so a generous cap costs ~1ms and keeps the
# counts honest for long courses.
FACILITY_SCAN_LIMIT = 80

# An RFS component is a distance-weighted share in [0,1] where 0.5 is the
# "no data" default. Seoul's open-data coverage differs sharply by component,
# so absolute cutoffs do not transfer between them. Measured over a 43-station
# citywide sample of 5km courses:
#
#   component  p10   median  p90    verdict
#   lighting   0.32  0.33    0.48   whole city sits below the no-data default
#   cctv       0.30  0.30    0.30   effectively constant -- never say anything
#   sidewalk   0.43  0.49    0.61
#   crossing   0.39  0.45    0.57
#
# A flat 0.45 "poor" line therefore labelled three courses in four 조명 어두움,
# which describes the dataset, not the route. Each band below sits outside its
# own citywide middle, so a chip means "unusual for Seoul" -- the only claim a
# runner comparing two courses can act on. cctv is deliberately absent.
COMPONENT_BANDS = {
    "crossing": (0.57, 0.39, ("🚦", "신호 적음"), ("🚦", "신호 잦음")),
    "sidewalk": (0.60, 0.43, ("🚸", "보도 넓음"), ("🚸", "보도 좁음")),
    "lighting": (0.60, 0.32, ("🔦", "조명 좋음"), ("🌒", "조명 어두움")),
}
# Cumulative gain per km above which "오르막 포함" deserves a caveat sentence.
CLIMB_NOTE_GAIN_PER_KM = 15.0
# Where the walkable network is a tree rather than a mesh -- most parks -- the
# only loop of the right length doubles back. Generation now prefers real
# circuits, but when none exists the runner should hear it before setting off.
RETRACE_NOTE_MIN = 0.25

# A caution is only worth a position when the weak part is actually a part.
# Below this length it is noise a runner passes in under two minutes; above
# LOCALISED_MAX_SHARE of the route it is the route itself, and "0km 지점부터
# 6.5km" reads worse than the plain sentence.
WEAK_STRETCH_MIN_M = 300.0
LOCALISED_MAX_SHARE = 0.6
# Per-edge attribute cutoffs, one notch below the route-average "poor" line:
# a single edge has to be worse than a merely below-average route to count.
EDGE_POOR = {"lighting": 0.30, "sidewalk": 0.40, "crossing": 0.35}

GRADE_EMOJI = {"평지 위주": "🛣️", "완만한 경사": "🌤️", "오르막 포함": "⛰️"}
# Start and finish within this distance is one loop, not a point-to-point run.
# Generation always aims for a closed loop, but an edited course need not be.
LOOP_CLOSE_M = 60.0


@dataclass(frozen=True)
class CourseFacts:
    """Everything the course says about itself in plain Korean."""

    traits: tuple[dict, ...]
    highlights: tuple[str, ...]
    cautions: tuple[str, ...]
    signals: int
    facility_counts: dict[str, int]


def is_loop(course: Course) -> bool:
    """Does the course finish where it started?

    Every generated course is a loop, which is exactly why the page never
    said so -- and a runner working out whether they need a ride home cannot
    infer it from a route drawing.
    """
    points = course.points
    if len(points) < 2:
        return False
    return haversine_m(*points[0], *points[-1]) <= LOOP_CLOSE_M


def _components(course: Course) -> dict:
    """Distance-weighted RFS components, or an empty opinion if absent."""
    components = course.rfs.get("components") if course.rfs else None
    return components if isinstance(components, dict) else {}


def _verdict(comps: dict, key: str) -> str | None:
    """Return "good", "poor", or None when the course is unremarkable."""
    band = COMPONENT_BANDS.get(key)
    value = comps.get(key)
    if band is None or value is None:
        return None
    good_at, poor_at, _, _ = band
    if value >= good_at:
        return "good"
    if value <= poor_at:
        return "poor"
    return None


def weak_stretch(course: Course, key: str) -> tuple[float, float] | None:
    """Longest run of consecutive edges scoring poorly on ``key``.

    Returns ``(start_km, length_m)``, or None when there is no such run, when
    it is too short to matter, or when it covers so much of the route that
    naming it adds nothing.  komoot's route alerts do the same thing: they
    point at "After 3.4 mi for 1.86 mi" rather than warning about the whole
    ride, because only the former is something a runner can look up.
    """
    cutoff = EDGE_POOR.get(key)
    if cutoff is None or not course.path:
        return None
    graph = graphmod.get_graph()
    attribute = f"{key}_score"
    best_start = best_length = 0.0
    run_start = run_length = 0.0
    cumulative = 0.0
    for u, v in zip(course.path, course.path[1:]):
        attrs = graph.edges[u, v]
        length = float(attrs.get("length", 0.0))
        if float(attrs.get(attribute, 0.5)) <= cutoff:
            if run_length == 0.0:
                run_start = cumulative
            run_length += length
            if run_length > best_length:
                best_start, best_length = run_start, run_length
        else:
            run_length = 0.0
        cumulative += length
    if best_length < WEAK_STRETCH_MIN_M:
        return None
    if course.length_m and best_length / course.length_m > LOCALISED_MAX_SHARE:
        return None
    return (best_start / 1000.0, best_length)


def _rounded_length(length_m: float) -> str:
    """Distances a runner reads off a course: metres up to 1km, then km."""
    # Round first: 980m rounds to 1000, which must print as 1.0km, not 1000m.
    rounded = round(length_m / 50) * 50
    if rounded < 1000:
        return f"{rounded:.0f}m"
    return f"{rounded / 1000.0:.1f}km"


def _at_stretch(course: Course, key: str) -> str:
    """" 2.4km 지점부터 약 600m 구간이에요." -- appended to a caution, or ""."""
    found = weak_stretch(course, key)
    if found is None:
        return ""
    start_km, length_m = found
    return f" {start_km:.1f}km 지점부터 약 {_rounded_length(length_m)} 구간이에요."


def _facility_counts(facilities: list[dict]) -> dict[str, int]:
    return {
        kind: sum(1 for f in facilities if f["type"] == kind)
        for kind in FACT_FACILITY_TYPES
    }


def course_traits(course: Course) -> tuple[dict, ...]:
    """Up to TRAIT_LIMIT one-line chips, most decision-relevant first.

    Grade leads because it is what a runner filters on before anything else;
    terrain follows, then whichever running-surface qualities the route is
    actually opinionated about.
    """
    grade = course.grade_label
    traits = [{"emoji": GRADE_EMOJI[grade], "label": grade}]

    if course.path and green_share(course) >= GREEN_SHARE_MIN:
        traits.append({"emoji": "🌳", "label": "공원·강변 위주"})
    else:
        traits.append({"emoji": "🏙️", "label": "도심 위주"})

    if course.params.night_mode:
        traits.append({"emoji": "💡", "label": "야간 안전 코스"})

    comps = _components(course)
    # Ordered by how much the quality changes the run, not by RFS weight.
    for key, (_, _, good, poor) in COMPONENT_BANDS.items():
        verdict = _verdict(comps, key)
        if verdict is None:
            continue
        emoji, label = good if verdict == "good" else poor
        traits.append({"emoji": emoji, "label": label})

    return tuple(traits[:TRAIT_LIMIT])


def course_highlights(course: Course, counts: dict[str, int],
                      signals: int) -> tuple[str, ...]:
    """Why this route is worth running, strongest reason first."""
    comps = _components(course)
    park_ratio = float(course.rfs.get("park_ratio", 0.0)) if course.rfs else 0.0
    notes: list[str] = []

    if course.grade_label == "평지 위주":
        notes.append("경사가 완만해 페이스를 유지하기 좋아요.")
    if park_ratio >= GREEN_SHARE_MIN:
        notes.append(f"녹지·강변 구간이 전체의 {park_ratio:.0%}예요.")
    if _verdict(comps, "sidewalk") == "good":
        notes.append("보도가 넓은 길 위주라 달리기 편해요.")
    if _verdict(comps, "crossing") == "good":
        notes.append(f"보행 신호가 {signals}개라 흐름이 잘 끊기지 않아요.")
    if _verdict(comps, "lighting") == "good":
        notes.append("서울 평균보다 가로등이 잘 갖춰진 구간이 많아요.")
    stores, restrooms = counts["convenience_store"], counts["restroom"]
    if stores and restrooms:
        notes.append(f"편의점 {stores}곳·화장실 {restrooms}곳을 지나요.")
    elif stores:
        notes.append(f"코스 위에서 편의점 {stores}곳을 지나요.")

    return tuple(notes[:NOTE_LIMIT])


def course_cautions(course: Course, counts: dict[str, int],
                    signals: int) -> tuple[str, ...]:
    """What to know before starting, most limiting first."""
    comps = _components(course)
    gain_per_km = course.ascent_m / course.length_km if course.length_km else 0.0
    notes: list[str] = []

    if _verdict(comps, "crossing") == "poor" and signals:
        notes.append(f"보행 신호를 {signals}번 건너요. 대기 시간이 생겨요.")
    if gain_per_km >= CLIMB_NOTE_GAIN_PER_KM:
        notes.append(f"누적 오르막이 {course.ascent_m:.0f}m라 힘이 들어요.")
    if _verdict(comps, "lighting") == "poor":
        notes.append("가로등 정보가 적어 야간에는 주의하세요."
                     + _at_stretch(course, "lighting"))
    if _verdict(comps, "sidewalk") == "poor":
        notes.append("보도가 좁거나 없는 구간이 섞여 있어요."
                     + _at_stretch(course, "sidewalk"))
    if course.path:
        retraced = retrace_share(graphmod.get_graph(), course.path)
        if retraced >= RETRACE_NOTE_MIN:
            notes.append(f"같은 길을 {retraced:.0%} 되돌아오는 코스예요.")
    if not counts["restroom"]:
        notes.append("코스 10m 안에 화장실이 없어요. 미리 들러 주세요.")
    if not counts["convenience_store"]:
        notes.append("코스 10m 안에 편의점이 없어요. 물을 챙겨 주세요.")

    return tuple(notes[:NOTE_LIMIT])


def course_facts(course: Course, facilities: list[dict] | None = None) -> CourseFacts:
    """Everything the pages say about a course, computed once.

    ``facilities`` lets a caller that already scanned the route (the detail
    page does) avoid a second lookup.  A course with no graph path -- the
    presentation-only shape the widget layer sometimes holds -- still gets its
    distance-derived traits rather than nothing.
    """
    if facilities is None:
        facilities = (
            facilities_along(course.points, list(FACT_FACILITY_TYPES),
                             limit=FACILITY_SCAN_LIMIT)
            if course.points else []
        )
    counts = _facility_counts(facilities)
    signals = (
        pedestrian_signals_crossed(graphmod.get_graph(), course.path)
        if course.path else 0
    )
    return CourseFacts(
        traits=course_traits(course),
        highlights=course_highlights(course, counts, signals),
        cautions=course_cautions(course, counts, signals),
        signals=signals,
        facility_counts=counts,
    )
