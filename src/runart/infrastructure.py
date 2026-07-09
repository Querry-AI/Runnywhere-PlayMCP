"""Streetlight and pedestrian-signal counts near a course."""

import functools
import os
import pickle
from pathlib import Path

from .geo import haversine_m


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


INFRA_PATH = _data_path("infra_points.pkl")
NEAR_COURSE_M = 100.0
_CELL = 0.001


@functools.lru_cache(maxsize=1)
def get_infra_points() -> dict[str, list[tuple[float, float]]]:
    if not INFRA_PATH.exists():
        return {"streetlight": [], "pedestrian_signal": []}
    with INFRA_PATH.open("rb") as f:
        return pickle.load(f)


@functools.lru_cache(maxsize=1)
def _infra_buckets() -> dict[str, dict[tuple[int, int], list[tuple[float, float]]]]:
    out: dict[str, dict[tuple[int, int], list[tuple[float, float]]]] = {}
    for kind, pts in get_infra_points().items():
        buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for lat, lon in pts:
            key = (int(lat / _CELL), int(lon / _CELL))
            buckets.setdefault(key, []).append((lat, lon))
        out[kind] = buckets
    return out


def infra_count_along(points: list[tuple[float, float]], kind: str,
                      radius_m: float = NEAR_COURSE_M) -> int:
    """Unique infrastructure points within radius_m of sampled route points."""
    buckets = _infra_buckets().get(kind, {})
    seen = set()
    for lat, lon in points:
        ci, cj = int(lat / _CELL), int(lon / _CELL)
        for i in (ci - 1, ci, ci + 1):
            for j in (cj - 1, cj, cj + 1):
                for plat, plon in buckets.get((i, j), ()):
                    if haversine_m(lat, lon, plat, plon) <= radius_m:
                        seen.add((round(plat, 6), round(plon, 6)))
    return len(seen)
