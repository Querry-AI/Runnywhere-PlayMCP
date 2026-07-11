"""Build-time animal-course presets for the bundled station catalogue.

The preset is a performance layer.  It is deliberately tied to the routing
graph fingerprint, so deploying a new graph can never serve stale node paths.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .course import Course
from .data_integrity import verify_data_file
from .models import CourseParams

PRESET_PATH = Path(__file__).resolve().parents[2] / "data" / "animal_station_presets.json.gz"
GRAPH_PATH = Path(__file__).resolve().parents[2] / "data" / "seoul_graph.pkl"
FORMAT_VERSION = 1
MISSING = object()


@dataclass(frozen=True)
class PresetMatch:
    course: Course
    distance_m: float

    @property
    def is_exact(self) -> bool:
        # Different lines/exits of one transfer station can be tens of metres
        # apart; presenting that as a different departure is confusing.
        return self.distance_m < 150.0


def graph_fingerprint(path: Path = GRAPH_PATH) -> str:
    """Cheap deployment fingerprint without hashing the whole ~60MB graph."""
    stat = path.stat()
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            f.seek(max(0, stat.st_size - 1024 * 1024))
            h.update(f.read(1024 * 1024))
    h.update(str(stat.st_size).encode())
    return h.hexdigest()[:20]


def preset_key(lat: float, lon: float, shape: str) -> str:
    return f"{lat:.5f},{lon:.5f},{shape}"


@lru_cache(maxsize=1)
def _load() -> dict | None:
    try:
        verify_data_file(PRESET_PATH)
        with gzip.open(PRESET_PATH, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        if (payload.get("format_version") != FORMAT_VERSION
                or payload.get("graph_fingerprint") != graph_fingerprint()):
            return None
        return payload.get("entries", {})
    except (OSError, ValueError, TypeError):
        return None


def get_animal_preset(params: CourseParams):
    """Return Course, None (known unavailable), or MISSING (not covered)."""
    if (not params.shape or params.include_hills or params.night_mode
            or params.need_facilities):
        return MISSING
    entries = _load()
    if entries is None:
        return MISSING
    raw = entries.get(preset_key(params.lat, params.lon, params.shape), MISSING)
    if raw is MISSING:
        return MISSING
    if raw is None:
        return None
    saved_params = CourseParams(**raw["params"])
    return Course(
        params=saved_params,
        path=raw["path"],
        points=[tuple(point) for point in raw["points"]],
        length_m=raw["length_m"],
        ascent_m=raw["ascent_m"],
        rfs=raw["rfs"],
        shape_similarity=raw.get("shape_similarity"),
    )


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    mean_lat = math.radians((lat1 + lat2) / 2)
    x = math.radians(lon2 - lon1) * math.cos(mean_lat)
    y = math.radians(lat2 - lat1)
    return 6_371_000.0 * math.hypot(x, y)


def find_nearest_animal_preset(params: CourseParams,
                               max_distance_m: float = 2000.0
                               ) -> PresetMatch | None:
    """Nearest verified preset for a shape, including arbitrary Seoul points.

    This is a small in-memory scan (276 station points), used as a deterministic
    sub-3-second fallback when the requested point has no clean silhouette.
    """
    if (not params.shape or params.include_hills or params.night_mode
            or params.need_facilities):
        return None
    entries = _load()
    if entries is None:
        return None
    suffix = f",{params.shape}"
    best = None
    best_distance = max_distance_m + 1.0
    for key, raw in entries.items():
        if raw is None or not key.endswith(suffix):
            continue
        try:
            lat_text, lon_text, _ = key.split(",", 2)
            distance = _distance_m(params.lat, params.lon,
                                   float(lat_text), float(lon_text))
        except (TypeError, ValueError):
            continue
        if distance < best_distance:
            saved_params = CourseParams(**raw["params"])
            best = Course(
                params=saved_params, path=raw["path"],
                points=[tuple(point) for point in raw["points"]],
                length_m=raw["length_m"], ascent_m=raw["ascent_m"],
                rfs=raw["rfs"], shape_similarity=raw.get("shape_similarity"),
            )
            best_distance = distance
    return PresetMatch(best, best_distance) if best is not None else None


def serialize_course(course: Course) -> dict:
    return {
        "params": course.params.canonical(),
        "path": course.path,
        "points": course.points,
        "length_m": course.length_m,
        "ascent_m": course.ascent_m,
        "rfs": course.rfs,
        "shape_similarity": course.shape_similarity,
    }


@lru_cache(maxsize=1)
def all_verified_animal_presets() -> tuple[Course, ...]:
    """All build-time verified courses for the exploration atlas."""
    entries = _load()
    if entries is None:
        return ()
    courses = []
    for raw in entries.values():
        if raw is None:
            continue
        courses.append(Course(
            params=CourseParams(**raw["params"]), path=raw["path"],
            points=[tuple(point) for point in raw["points"]],
            length_m=raw["length_m"], ascent_m=raw["ascent_m"],
            rfs=raw["rfs"], shape_similarity=raw.get("shape_similarity"),
        ))
    return tuple(courses)
