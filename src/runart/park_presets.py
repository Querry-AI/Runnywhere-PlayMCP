"""Five researched park/waterside destinations with build-time road paths."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from functools import lru_cache

from . import graph as graphmod
from .animal_presets import _data_path, graph_fingerprint
from .course import Course, CourseError, course_from_path
from .data_integrity import verify_data_file
from .geo import haversine_m
from .models import CourseParams
from .naming import green_share
from .rfs import has_sufficient_night_lighting


@dataclass(frozen=True)
class ParkSpot:
    id: str
    name: str
    lat: float
    lon: float
    source_url: str


# Representative destinations, not a claimed ranking by visitor numbers.
# Coordinates are the existing offline gazetteer's anchors; the bundled path
# records its actual nearby park-path start separately.
PARK_SPOTS = (
    ParkSpot("yeouido", "여의도한강공원", 37.5285, 126.9328,
             "https://www.seoul.go.kr/policy/view.do?id=1017&lan=KO"),
    ParkSpot("banpo", "반포한강공원", 37.5100, 126.9950,
             "https://news.seoul.go.kr/culture/archives/520899"),
    ParkSpot("ttukseom", "뚝섬한강공원", 37.5310, 127.0660,
             "https://mediahub.seoul.go.kr/archives/2018687"),
    ParkSpot("seoulforest", "서울숲", 37.5444, 127.0374,
             "https://mediahub.seoul.go.kr/archives/2012980"),
    ParkSpot("yangjae", "양재천", 37.4750, 127.0450,
             "https://love.seoul.go.kr/articles/10626"),
)

PRESET_PATH = _data_path("park_course_presets.json")


@lru_cache(maxsize=1)
def _courses_for_graph(graph) -> tuple[tuple[ParkSpot, Course], ...]:
    verify_data_file(PRESET_PATH)
    raw = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    if raw.get("format_version") != 1 or raw.get("graph_fingerprint") != graph_fingerprint():
        raise CourseError("공원 코스 데이터와 현재 보행 지도가 맞지 않아 추천을 준비하지 못했어요.")
    if set(raw["entries"]) != {spot.id for spot in PARK_SPOTS}:
        raise CourseError("공원 추천 5곳의 경로 데이터가 완전하지 않아요.")
    result = []
    for spot in PARK_SPOTS:
        params = CourseParams(**raw["entries"][spot.id])
        course = course_from_path(params, params.manual_path)
        start = graph.nodes[course.path[0]]
        if (params.location_name != spot.name
                or haversine_m(spot.lat, spot.lon, start["lat"], start["lon"]) > 500
                or haversine_m(params.lat, params.lon, start["lat"], start["lon"]) > 10
                or green_share(course) < .9):
            raise CourseError("등록된 공원 코스의 위치·보행로 검증을 통과하지 못했어요.")
        result.append((spot, course))
    return tuple(result)


def park_courses() -> tuple[tuple[ParkSpot, Course], ...]:
    return _courses_for_graph(graphmod.get_graph())


def select_park_courses(origin: tuple[float, float] | None = None, *,
                        night_mode: bool = False) -> list[tuple[ParkSpot, Course]]:
    """One distinct course per destination. Distance is to the actual start."""
    candidates = list(park_courses())
    if night_mode:
        candidates = [item for item in candidates if has_sufficient_night_lighting(item[1].rfs)]
    if origin is None:
        return random.sample(candidates, min(3, len(candidates)))
    candidates.sort(key=lambda item: (
        haversine_m(*origin, item[1].params.lat, item[1].params.lon), item[0].id))
    return candidates[:3]
