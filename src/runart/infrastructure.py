"""Streetlight and pedestrian-signal counts near a course."""

import functools
import os
import pickle
from pathlib import Path

from .geo import haversine_m, to_xy


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


def _point_segment_distance_m(p: tuple[float, float],
                              a: tuple[float, float],
                              b: tuple[float, float]) -> float:
    px, py = to_xy(p[0], p[1], a[0], a[1])
    bx, by = to_xy(b[0], b[1], a[0], a[1])
    denom = bx * bx + by * by
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (px * bx + py * by) / denom))
    return ((px - t * bx) ** 2 + (py - t * by) ** 2) ** 0.5


def infra_count_crossed_by_path(graph, path: list, kind: str,
                                radius_m: float = 1.0) -> int:
    """Unique infra points close to the actual route edges.

    Used for pedestrian signals: this avoids counting every signal merely
    near sampled route points and instead approximates signals the runner
    actually crosses/passes on the route geometry.
    """
    buckets = _infra_buckets().get(kind, {})
    seen = set()
    for u, v in zip(path, path[1:]):
        a = (graph.nodes[u]["lat"], graph.nodes[u]["lon"])
        b = (graph.nodes[v]["lat"], graph.nodes[v]["lon"])
        lat_min, lat_max = sorted((a[0], b[0]))
        lon_min, lon_max = sorted((a[1], b[1]))
        ci0, ci1 = int(lat_min / _CELL) - 1, int(lat_max / _CELL) + 1
        cj0, cj1 = int(lon_min / _CELL) - 1, int(lon_max / _CELL) + 1
        for i in range(ci0, ci1 + 1):
            for j in range(cj0, cj1 + 1):
                for plat, plon in buckets.get((i, j), ()):
                    if _point_segment_distance_m((plat, plon), a, b) <= radius_m:
                        seen.add((round(plat, 6), round(plon, 6)))
    return len(seen)
