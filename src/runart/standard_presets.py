"""Build-time presets for ordinary (non-shape) courses.

Same contract as animal_presets: entries are tied to the routing graph
fingerprint, so deploying a new graph can never serve stale node paths.

The stored route is exactly what generate_course returns for those parameters.
Generation is deterministic across processes and independent of the search
budget (0.8s and 20s produce the same route), so a preset hit and a live miss
answer with the same course -- the preset only removes the wait.
"""

from __future__ import annotations

import gzip
import json
import os
from functools import lru_cache
from pathlib import Path

from . import graph as graphmod
from .course import (Course, CourseError, DistanceMissError,
                     course_route_issues)
from .data_integrity import verify_data_file
from .geo import haversine_m
from .models import CourseParams, decode_course_id, encode_course_id


def _data_path(filename: str) -> Path:
    """Same search order as graph.py/animal_presets.py."""
    candidates = []
    if os.environ.get("RUNART_DATA_DIR"):
        candidates.append(Path(os.environ["RUNART_DATA_DIR"]))
    candidates.extend([Path.cwd() / "data",
                       Path(__file__).resolve().parents[2] / "data"])
    for base in candidates:
        path = base / filename
        if path.exists():
            return path
    return candidates[0] / filename


PRESET_PATH = _data_path("standard_course_presets.json.gz")
FORMAT_VERSION = 1
# The distances the catalogue is built for. Shared so the build script and the
# gazetteer audit cannot drift from what the runtime actually looks up.
DEFAULT_DISTANCES_KM = (3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0)
BLOCKED = object()

_load_status = "not loaded yet"


def preset_id(params: CourseParams) -> str:
    """The runtime's own cache key, so a hit is byte-identical to a miss."""
    return encode_course_id(params)


def serialize_course(course: Course) -> dict:
    """Store the exact parameters, not the canonical (rounded) form.

    animal_presets.serialize_course writes params.canonical(), which rounds
    lat/lon to five decimals (~1m). The course id is unaffected, but a shifted
    start coordinate can flip a distance comparison that sits on the
    SAME_START_M / NEARBY_RADIUS_M boundary, so a preset hit would answer
    differently from a live miss. Keeping the raw values removes that gap.
    """
    return {
        "params": course.params.model_dump(),
        "path": course.path,
        "points": [list(point) for point in course.points],
        "length_m": course.length_m,
        "ascent_m": course.ascent_m,
        "rfs": course.rfs,
        "shape_similarity": course.shape_similarity,
    }


def _restore(raw: dict) -> Course:
    """Rebuild verbatim. Standard routes are not re-anchored: the stored start
    is the one generate_course produced for these exact parameters."""
    return Course(
        params=CourseParams(**raw["params"]),
        path=raw["path"],
        points=[tuple(point) for point in raw["points"]],
        length_m=raw["length_m"],
        ascent_m=raw["ascent_m"],
        rfs=raw["rfs"],
        shape_similarity=raw.get("shape_similarity"),
    )


def _load() -> dict | None:
    return _load_for_graph(graphmod.get_graph())


@lru_cache(maxsize=1)
def _load_for_graph(graph) -> dict | None:
    """Verified entries, or None with the reason recorded in preset_status()."""
    global _load_status
    if not PRESET_PATH.exists():
        _load_status = f"preset file missing: {PRESET_PATH}"
        return None
    try:
        verify_data_file(PRESET_PATH)
    except Exception as exc:  # noqa: BLE001 - a bad artifact must not crash boot
        _load_status = f"integrity check failed: {exc}"
        return None
    try:
        with gzip.open(PRESET_PATH, "rt", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as exc:
        _load_status = f"unreadable preset file: {exc}"
        return None
    if payload.get("format_version") != FORMAT_VERSION:
        _load_status = (f"format_version {payload.get('format_version')!r} != "
                        f"{FORMAT_VERSION} (rebuild with "
                        "scripts/build_standard_presets.py)")
        return None
    from .animal_presets import graph_fingerprint
    try:
        fingerprint = graph_fingerprint()
    except OSError as exc:
        _load_status = f"graph file unavailable: {exc}"
        return None
    if payload.get("graph_fingerprint") != fingerprint:
        _load_status = ("graph fingerprint mismatch - the deployed graph differs "
                        "from the one the presets were built against")
        return None
    entries = payload.get("entries", {})
    blocked = 0
    for key, raw in entries.items():
        if raw is None or "error" in raw:
            continue
        if isinstance(raw, dict) and raw.get("error"):
            continue
        try:
            issues = course_route_issues(_restore(raw), graph)
        except (KeyError, TypeError, ValueError):
            issues = ["invalid_route"]
        if issues:
            entries[key] = BLOCKED
            blocked += 1
    verified = sum(1 for v in entries.values()
                   if v is not None and v is not BLOCKED and "error" not in v)
    _load_status = (f"ok: {verified} verified courses ({len(entries)} entries, "
                    f"{blocked} blocked by current routing rules)")
    return entries


_load.cache_clear = _load_for_graph.cache_clear
_load.cache_info = _load_for_graph.cache_info


def preset_status() -> str:
    _load()
    return _load_status


def serialize_failure(exc: CourseError) -> dict:
    """Store a build-time failure so the runtime stops re-running it.

    The stored message is the one generation produced, so replaying it raises
    the same error a live attempt would -- the wait is removed, not the answer.
    """
    if isinstance(exc, DistanceMissError):
        return {"error": "DistanceMissError", "target_km": exc.target_km,
                "nearest_km": exc.nearest_km}
    return {"error": "CourseError", "message": str(exc)}


def _restore_failure(raw: dict) -> CourseError:
    if raw["error"] == "DistanceMissError":
        return DistanceMissError(raw["target_km"], raw["nearest_km"])
    return CourseError(raw["message"])


def get_standard_preset(params: CourseParams) -> Course | CourseError | None:
    """The build-time outcome for these exact parameters, or None to generate.

    Returns the route on success and the identical CourseError on a build-time
    failure; None means the combination was never built.
    """
    if params.shape or params.manual_path or params.manual_waypoints:
        return None
    entries = _load()
    if entries is None:
        return None
    raw = entries.get(preset_id(params))
    if raw is None or raw is BLOCKED:
        return None
    if "error" in raw:
        return _restore_failure(raw)
    return _restore(raw)


@lru_cache(maxsize=1)
def _start_index_for_graph(graph) -> tuple:
    """Primary entries indexed by start, so a failed request can name a real
    nearby alternative instead of inventing one."""
    entries = _load_for_graph(graph)
    if entries is None:
        return ()
    rows = []
    for key, raw in entries.items():
        if raw is None or raw is BLOCKED or raw.get("error"):
            continue
        try:
            p = decode_course_id(key)
        except Exception:  # noqa: BLE001 - a bad key must not break the index
            continue
        if p.route_variant or p.shape or p.night_mode or p.include_hills or p.need_facilities:
            continue
        rows.append((p.lat, p.lon, p.location_name, p.distance_km, key))
    return tuple(rows)


def nearest_start_preset(lat: float, lon: float, distance_km: float | None,
                         max_distance_m: float = 6000.0):
    """Nearest build-verified ordinary course to this point, or None.

    Returns (Course, metres from the requested point). Only entries that exist
    in the catalogue are offered: the caller must never suggest a start it has
    not measured.
    """
    rows = _start_index_for_graph(graphmod.get_graph())
    if not rows:
        return None
    target = distance_km if distance_km else 5.0
    best = None
    for plat, plon, _name, pdist, key in rows:
        if abs(pdist - target) > target * 0.10 + 1e-9:
            continue
        d = haversine_m(lat, lon, plat, plon)
        if d > max_distance_m:
            continue
        if best is None or d < best[0]:
            best = (d, key)
    if best is None:
        return None
    entries = _load()
    raw = entries.get(best[1]) if entries else None
    if not isinstance(raw, dict) or raw.get("error"):
        return None
    return _restore(raw), best[0]
