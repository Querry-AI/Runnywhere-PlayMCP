"""Build-time animal-course presets for the bundled station catalogue.

The preset is a performance layer.  It is deliberately tied to the routing
graph fingerprint, so deploying a new graph can never serve stale node paths.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .course import Course
from .data_integrity import verify_data_file
from .models import CourseParams


def _data_path(filename: str) -> Path:
    """Same search order as graph.py/facilities.py: RUNART_DATA_DIR, then the
    working directory (the deploy image installs the package into
    site-packages and keeps data/ under WORKDIR), then the repo checkout."""
    candidates = []
    if os.environ.get("RUNART_DATA_DIR"):
        candidates.append(Path(os.environ["RUNART_DATA_DIR"]))
    candidates.extend([
        Path.cwd() / "data",
        Path(__file__).resolve().parents[2] / "data",
    ])
    for base in candidates:
        path = base / filename
        if path.exists():
            return path
    return candidates[0] / filename


PRESET_PATH = _data_path("animal_station_presets.json.gz")
GRAPH_PATH = _data_path("seoul_graph.pkl")
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


_load_status = "not loaded yet"


@lru_cache(maxsize=1)
def _load() -> dict | None:
    """Verified preset entries, or None with the failure recorded in
    preset_status() — a silent None here previously made a broken deploy
    (stale image, regenerated graph) indistinguishable from 'no presets'."""
    global _load_status
    if not PRESET_PATH.exists():
        _load_status = f"preset file missing: {PRESET_PATH}"
        return None
    try:
        verify_data_file(PRESET_PATH)
    except Exception as e:
        _load_status = f"integrity check failed: {e}"
        return None
    try:
        with gzip.open(PRESET_PATH, "rt", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as e:
        _load_status = f"unreadable preset file: {e}"
        return None
    if payload.get("format_version") != FORMAT_VERSION:
        _load_status = (f"format_version {payload.get('format_version')!r} != "
                        f"{FORMAT_VERSION} (rebuild with scripts/build_animal_presets.py)")
        return None
    try:
        fingerprint = graph_fingerprint()
    except OSError as e:
        _load_status = f"graph file unavailable: {e}"
        return None
    if payload.get("graph_fingerprint") != fingerprint:
        _load_status = ("graph fingerprint mismatch — the deployed graph differs from "
                        "the one the presets were built against "
                        "(rebuild with scripts/build_animal_presets.py)")
        return None
    entries = payload.get("entries", {})
    verified = sum(1 for value in entries.values() if value)
    _load_status = f"ok: {verified} verified courses ({len(entries)} entries)"
    return entries


def preset_status() -> str:
    """Human-readable preset availability for startup logs and diagnostics."""
    _load()
    return _load_status


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


def find_nearby_animal_presets(params: CourseParams,
                               max_distance_m: float = 2000.0
                               ) -> list[PresetMatch]:
    """All verified presets for a shape within max_distance_m, nearest first.

    This is a small in-memory scan (276 station points), used as a
    deterministic sub-3-second fallback when the requested point has no
    clean silhouette."""
    if (not params.shape or params.include_hills or params.night_mode
            or params.need_facilities):
        return []
    entries = _load()
    if entries is None:
        return []
    suffix = f",{params.shape}"
    matches: list[PresetMatch] = []
    for key, raw in entries.items():
        if raw is None or not key.endswith(suffix):
            continue
        try:
            lat_text, lon_text, _ = key.split(",", 2)
            distance = _distance_m(params.lat, params.lon,
                                   float(lat_text), float(lon_text))
        except (TypeError, ValueError):
            continue
        if distance > max_distance_m:
            continue
        saved_params = CourseParams(**raw["params"])
        matches.append(PresetMatch(Course(
            params=saved_params, path=raw["path"],
            points=[tuple(point) for point in raw["points"]],
            length_m=raw["length_m"], ascent_m=raw["ascent_m"],
            rfs=raw["rfs"], shape_similarity=raw.get("shape_similarity"),
        ), distance))
    matches.sort(key=lambda m: m.distance_m)
    return matches


def find_nearest_animal_preset(params: CourseParams,
                               max_distance_m: float = 2000.0
                               ) -> PresetMatch | None:
    """Nearest verified preset for a shape, including arbitrary Seoul points."""
    matches = find_nearby_animal_presets(params, max_distance_m)
    return matches[0] if matches else None


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
