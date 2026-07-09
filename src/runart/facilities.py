"""Facilities along a course (PRD §5.5).

Production source: Seoul Open Data Plaza POI snapshots (public restrooms,
drinking fountains, parks) + OSM convenience stores, baked into
data/facilities.pkl by etl/build_rfs.py. Demo: deterministic POIs placed on
the demo grid so the tool works end-to-end.
"""

import functools
import hashlib
import os
import pickle
from pathlib import Path

from . import graph as graphmod
from .geo import haversine_m
from .models import FACILITY_TYPES

def _data_path(filename: str) -> Path:
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
    return candidates[0] / filename if candidates else Path(filename)


FACILITIES_PATH = _data_path("facilities.pkl")
NEAR_COURSE_M = 100.0  # PRD: within 100m of the course

LABELS_KO = {
    "convenience_store": "편의점",
    "restroom": "화장실",
    "water": "음수대",
    "park": "공원",
}


@functools.lru_cache(maxsize=1)
def get_facilities() -> list[dict]:
    if FACILITIES_PATH.exists():
        with FACILITIES_PATH.open("rb") as f:
            return pickle.load(f)
    return _demo_facilities()


def _demo_facilities() -> list[dict]:
    g = graphmod.get_graph()
    out = []
    for n, d in g.nodes(data=True):
        h = int.from_bytes(hashlib.sha1(f"poi|{n}".encode()).digest()[:4], "big") / 2**32
        if h < 0.985:
            continue
        kind = FACILITY_TYPES[int(h * 1e6) % len(FACILITY_TYPES)]
        out.append({
            "type": kind,
            "name": f"{LABELS_KO[kind]} #{abs(hash(n)) % 900 + 100}",
            "lat": d["lat"],
            "lon": d["lon"],
        })
    return out


# ~110m cells: one ring around a course point covers the 100m search radius.
_CELL = 0.001


@functools.lru_cache(maxsize=1)
def _facility_buckets() -> dict[tuple[int, int], list[dict]]:
    buckets: dict[tuple[int, int], list[dict]] = {}
    for fac in get_facilities():
        key = (int(fac["lat"] / _CELL), int(fac["lon"] / _CELL))
        buckets.setdefault(key, []).append(fac)
    return buckets


def facilities_along(points: list[tuple[float, float]], types: list[str] | None = None,
                     limit: int = 8) -> list[dict]:
    """Facilities within NEAR_COURSE_M of the polyline, annotated with the km
    mark where the course passes closest. Grid-bucketed: O(points), not
    O(points x facilities) — this runs on every tool call."""
    wanted = set(types) & set(FACILITY_TYPES) if types else set(FACILITY_TYPES)
    buckets = _facility_buckets()
    cum = 0.0
    best: dict[int, tuple[float, float]] = {}  # id(fac) -> (dist, km)
    seen: dict[int, dict] = {}
    prev = None
    for lat, lon in points:
        if prev is not None:
            cum += haversine_m(prev[0], prev[1], lat, lon) / 1000.0
        prev = (lat, lon)
        ci, cj = int(lat / _CELL), int(lon / _CELL)
        for i in (ci - 1, ci, ci + 1):
            for j in (cj - 1, cj, cj + 1):
                for fac in buckets.get((i, j), ()):
                    if fac["type"] not in wanted:
                        continue
                    d = haversine_m(lat, lon, fac["lat"], fac["lon"])
                    key = id(fac)
                    if d < best.get(key, (float("inf"),))[0]:
                        best[key] = (d, cum)
                        seen[key] = fac
    found = [
        {**seen[k], "at_km": round(km, 1), "dist_m": round(d)}
        for k, (d, km) in best.items() if d <= NEAR_COURSE_M
    ]
    found.sort(key=lambda f: f["at_km"])
    return found[:limit]


def facility_requirement_score(points: list[tuple[float, float]],
                               types: list[str] | None) -> tuple[int, int]:
    """How many requested facility types are found along the course.

    Course generation uses this as a ranking signal when the user asks for a
    route that passes a restroom/convenience store/etc. The tool can still
    return a best-effort course if the local road candidates cannot satisfy
    every requested type.
    """
    wanted = sorted(set(types or []) & set(FACILITY_TYPES))
    if not wanted:
        return (0, 0)
    found = facilities_along(points, wanted, limit=24)
    hit_types = {f["type"] for f in found}
    return (len(hit_types), len(wanted))
