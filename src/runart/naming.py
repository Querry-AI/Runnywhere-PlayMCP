"""Course display names and characteristic badges.

The course title is the first thing a runner reads, so it names the run rather
than describing the generator: "5.1km 댕댕런", "5.1km 강남대로런". Badges add at
most three emoji for shape, terrain and night suitability.

Presentation only -- imports shapes/graph but nothing imports it back.
"""
import re

from . import graph as graphmod
from .models import clean_course_name
from .shapes import SHAPES
from .rfs import has_sufficient_night_lighting, night_lighting_label

COURSE_EDIT_NOTICE = "코스를 열면 경로를 직접 편집할 수 있어요."

# Animal courses are named after the run, not the species.
RUN_NAMES_KO = {
    "dog": "댕댕런",
    "cat": "야옹런",
    "rabbit": "깡총런",
    "whale": "고래런",
}

TRACK_EMOJI = "🏟️"
GREEN_EMOJI = "🌳"
CITY_EMOJI = "🏙️"
NIGHT_EMOJI = "💡"

# Share of course length on park/riverside edges above which the course reads
# as a green run rather than a city-street run.
GREEN_SHARE_MIN = 0.30
PARK_EDGE_MIN = 0.5
PLACE_MAX_CHARS = 12

# Trailing lot numbers ("401-2", "123번지") carry no meaning in a run name.
_LOT_SUFFIX = re.compile(r"\s*\d+(-\d+)?(번지|호)?$")
# A token starting with a digit is a qualifier, not a place: "2번출구", "401-2".
_NUMERIC_TOKEN = re.compile(r"^\d")


def short_place(location_name: str) -> str:
    """The most specific readable part of an address.

    "강남대로 401-2" -> "강남대로"; "서울특별시 강남구 테헤란로 123" -> "테헤란로";
    "잠실역 2번출구" -> "잠실역". A bare place name ("서울시청") is unchanged, and a
    name that is *only* a numeric token ("63빌딩") is kept rather than emptied.
    """
    name = (location_name or "").strip()
    if not name:
        return ""
    tokens = [t for t in name.split() if not _NUMERIC_TOKEN.match(t)] or name.split()
    place = _LOT_SUFFIX.sub("", tokens[-1]).strip() or tokens[-1]
    return place[:PLACE_MAX_CHARS]


def auto_course_title(course) -> str:
    """The generated name, ignoring anything the runner typed."""
    p = course.params
    km = f"{course.length_km:.1f}km"
    run = RUN_NAMES_KO.get(p.shape or "")
    if run:
        return f"{km} {run}"
    place = short_place(p.location_name)
    return f"{km} {place}런" if place else f"{km} 러닝 코스"


def course_name_placeholder(course) -> str:
    """The generated name without its distance -- what the rename field shows
    in grey. Saving without typing keeps exactly this."""
    return auto_course_title(course).split(" ", 1)[-1]


def course_title(course) -> str:
    """Plain-text course name. Callers escape it for their own output.

    The distance always leads: a runner scanning a list of saved courses reads
    the number first, so a typed name joins it rather than replacing it --
    "AA런" saved on a 4.8km course reads "4.8km AA런".
    """
    custom = clean_course_name(getattr(course.params, "custom_name", ""))
    if custom:
        return f"{course.length_km:.1f}km {custom}"
    return auto_course_title(course)


def green_share(course) -> float:
    """Fraction of course length on park or riverside edges."""
    g = graphmod.get_graph()
    total = 0.0
    green = 0.0
    for u, v in zip(course.path, course.path[1:]):
        attrs = g.edges[u, v]
        length = float(attrs.get("length", 0.0))
        total += length
        if float(attrs.get("park_score", 0.0)) >= PARK_EDGE_MIN:
            green += length
    return green / total if total else 0.0


def course_badges(course) -> list[dict]:
    """1-3 badges: shape, terrain, and night suitability.

    Every badge carries a Korean label -- an emoji alone conveys nothing to a
    screen reader, and several of these differ only by hue at small sizes.
    ``detail`` says *why* the badge is there, because the label alone still
    leaves a runner guessing what "도심 위주" was measured against; the page
    shows it in a tooltip on hover and on tap.
    """
    p = course.params
    badges = []

    shape = SHAPES.get(p.shape) if p.shape else None
    if shape:
        badges.append({
            "emoji": shape.emoji,
            "label": f"{shape.name_ko} 모양 코스",
            "detail": f"달린 자취가 {shape.name_ko} 모양으로 그려지는 GPS 아트 코스예요.",
        })
    else:
        badges.append({
            "emoji": TRACK_EMOJI,
            "label": "일반 러닝 코스",
            "detail": "모양 없이 달리기 좋은 길만 골라 이은 코스예요.",
        })

    # park_score merges parks and riverside paths in the source data, so this
    # is a green/city split -- it does not claim to identify a river.
    green = green_share(course)
    if green >= GREEN_SHARE_MIN:
        badges.append({
            "emoji": GREEN_EMOJI,
            "label": "공원·강변 위주",
            "detail": f"코스의 {green:.0%}가 공원·강변길이에요. 차와 마주칠 일이 적어요.",
        })
    else:
        badges.append({
            "emoji": CITY_EMOJI,
            "label": "도심 위주",
            "detail": (f"공원·강변길이 {green:.0%}뿐이라 도심 도로 위주예요. "
                       "신호와 사람이 많을 수 있어요."),
        })

    if p.night_mode and has_sufficient_night_lighting(course.rfs):
        badges.append({
            "emoji": NIGHT_EMOJI,
            "label": night_lighting_label(course.rfs),
            "detail": "가로등 데이터가 야간 추천 최소 기준을 통과했어요. 현장 안전을 보장하지는 않아요.",
        })

    return badges
