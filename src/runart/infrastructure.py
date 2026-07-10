"""Streetlight and pedestrian-signal counts near a course."""

import functools
import math
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
_CELL = 0.001


@functools.lru_cache(maxsize=1)
def get_infra_points() -> dict[str, list[tuple[float, float]]]:
    if not INFRA_PATH.exists():
        return {"streetlight": [], "pedestrian_signal": []}
    with INFRA_PATH.open("rb") as f:
        raw = pickle.load(f)
    # Do not let one malformed projected-coordinate record silently poison
    # nearest-point checks or appear as a Seoul streetlight at runtime.
    return {
        kind: [(lat, lon) for lat, lon in points
               if 37.3 < lat < 37.8 and 126.6 < lon < 127.3]
        for kind, points in raw.items()
    }


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


# A signalized crosswalk's poles stand at the curb corners, typically within
# a few tens of meters of the road-centerline intersection node.
CROSSING_SIGNAL_RADIUS_M = 25.0
STRAIGHT_THROUGH_MAX_DEG = 45.0


def pedestrian_signals_crossed(graph, path: list,
                               radius_m: float = CROSSING_SIGNAL_RADIUS_M,
                               straight_max_deg: float = STRAIGHT_THROUGH_MAX_DEG,
                               ) -> int:
    """Signalized crossing points the route actually CROSSES.

    Merely running past a signal pole must not count. On a centerline graph a
    crossing happens when the route passes straight through an intersection
    (degree >= 3): the runner traverses the transverse street's crosswalk and
    waits for its signal. Turning at the corner keeps the runner on the same
    block edge, so signals there are excluded.

    The source data stores individual signal poles. Several poles can belong
    to one crosswalk/intersection, but the UI should report the crossing event,
    not every pole around that event.
    """
    buckets = _infra_buckets().get("pedestrian_signal", {})
    crossed_nodes: set = set()
    n = len(path)
    if n < 3:
        return 0
    closed = path[0] == path[-1]
    # For a closed loop the shared start/end node is also a potential crossing.
    indices = list(range(1, n - 1)) + ([0] if closed else [])
    for i in indices:
        b = path[i]
        if graph.degree(b) < 3:
            continue  # mid-block node, nothing to cross
        a = path[i - 1] if i > 0 else path[-2]
        c = path[i + 1]
        na, nb, nc = graph.nodes[a], graph.nodes[b], graph.nodes[c]
        ax, ay = to_xy(na["lat"], na["lon"], nb["lat"], nb["lon"])
        cx, cy = to_xy(nc["lat"], nc["lon"], nb["lat"], nb["lon"])
        m1, m2 = math.hypot(ax, ay), math.hypot(cx, cy)
        if m1 < 1e-9 or m2 < 1e-9:
            continue
        # Direction change between incoming and outgoing legs: straight
        # through = crossing the side street; a sharp turn = staying on the
        # same corner without crossing anything.
        cos_t = max(-1.0, min(1.0, (-ax * cx - ay * cy) / (m1 * m2)))
        if math.degrees(math.acos(cos_t)) > straight_max_deg:
            continue
        ci, cj = int(nb["lat"] / _CELL), int(nb["lon"] / _CELL)
        has_signal = False
        for gi in (ci - 1, ci, ci + 1):
            for gj in (cj - 1, cj, cj + 1):
                for plat, plon in buckets.get((gi, gj), ()):
                    if haversine_m(nb["lat"], nb["lon"], plat, plon) <= radius_m:
                        has_signal = True
                        break
                if has_signal:
                    break
            if has_signal:
                break
        if has_signal:
            crossed_nodes.add(b)
    return len(crossed_nodes)
