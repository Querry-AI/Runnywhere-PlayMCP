"""Streetlight and pedestrian-signal counts near a course."""

import functools
import math
import os
# Pickle is restricted to the checksum-verified image artifact below.
import pickle  # nosec B403
from pathlib import Path

from .geo import haversine_m, to_xy
from .data_integrity import verify_data_file


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
    verify_data_file(INFRA_PATH)
    with INFRA_PATH.open("rb") as f:
        raw = pickle.load(f)  # nosec B301
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
# One physical intersection is often modelled as a few centerline nodes a few
# meters apart (dual carriageways, split crosswalk nodes). Qualifying nodes
# closer than this along the route belong to the same real crossing event.
CROSSING_MERGE_M = 40.0


def pedestrian_signals_crossed(graph, path: list,
                               radius_m: float = CROSSING_SIGNAL_RADIUS_M,
                               straight_max_deg: float = STRAIGHT_THROUGH_MAX_DEG,
                               merge_m: float = CROSSING_MERGE_M,
                               ) -> int:
    """Number of times the route actually crosses a signalized street.

    Merely running past a signal pole must not count. On a centerline graph a
    crossing happens when the route passes straight through an intersection
    (degree >= 3): the runner traverses the transverse street's crosswalk and
    waits for its signal. Turning at the corner keeps the runner on the same
    block edge, so signals there are excluded.

    The count is per crossing EVENT, not per node or per pole: several signal
    poles serve one crosswalk, and one wide intersection can span several
    graph nodes within a few meters of each other — those must read as a
    single crossing. Conversely, a loop that returns through the same
    intersection later crosses the street again, and that counts again.
    """
    buckets = _infra_buckets().get("pedestrian_signal", {})
    n = len(path)
    if n < 3:
        return 0
    closed = path[0] == path[-1]
    # Cumulative distance along the path lets nearby qualifying nodes merge
    # into one crossing event while distant revisits count separately.
    cum = [0.0]
    for u, v in zip(path, path[1:]):
        du, dv = graph.nodes[u], graph.nodes[v]
        cum.append(cum[-1] + haversine_m(du["lat"], du["lon"],
                                         dv["lat"], dv["lon"]))
    # For a closed loop the shared start/end node is also a potential crossing.
    indices = list(range(1, n - 1)) + ([0] if closed else [])
    crossings = 0
    last_event_at: float | None = None
    for i in indices:
        b = path[i]
        at_m = cum[i] if i > 0 else cum[-1]
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
        if not has_signal:
            continue
        if last_event_at is not None and at_m - last_event_at <= merge_m:
            # Same physical intersection as the crossing just counted.
            last_event_at = at_m
            continue
        crossings += 1
        last_event_at = at_m
    return crossings
