"""Pedestrian network provider.

Production: loads a prebuilt Seoul-wide graph (etl/build_graph.py +
etl/build_rfs.py bake OSM + Seoul Open Data Plaza attributes into
data/seoul_graph.pkl, shipped inside the container image — no runtime API
calls, PRD §5.7).

Development/demo: a deterministic synthetic grid around Seoul City Hall with
a river band (high RFS park edges) and a hill zone, so the whole pipeline
runs end-to-end before the real ETL lands.
"""

import functools
import hashlib
import math
import os
import pickle
from pathlib import Path

import networkx as nx

from .geo import M_PER_DEG_LAT, m_per_deg_lon

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


GRAPH_PATH = _data_path("seoul_graph.pkl")

# Demo grid: ~4.7km x 4.7km around City Hall, ~80m spacing.
DEMO_CENTER = (37.5665, 126.9780)
DEMO_N = 60
DEMO_STEP_M = 80.0


def _h(*parts) -> float:
    """Deterministic pseudo-random in [0,1) from parts."""
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def build_demo_graph() -> nx.Graph:
    g = nx.Graph()
    lat0, lon0 = DEMO_CENTER
    dlat = DEMO_STEP_M / M_PER_DEG_LAT
    dlon = DEMO_STEP_M / m_per_deg_lon(lat0)
    half = DEMO_N // 2
    for i in range(DEMO_N):
        for j in range(DEMO_N):
            lat = lat0 + (i - half) * dlat
            lon = lon0 + (j - half) * dlon
            g.add_node((i, j), lat=lat, lon=lon)
    for i in range(DEMO_N):
        for j in range(DEMO_N):
            for di, dj in ((0, 1), (1, 0)):
                ni, nj = i + di, j + dj
                if ni < DEMO_N and nj < DEMO_N:
                    g.add_edge((i, j), (ni, nj), **_demo_edge_attrs(i, j, ni, nj))
    return g


def _demo_edge_attrs(i: int, j: int, ni: int, nj: int) -> dict:
    # "River band": rows 8..14 — park/riverside paths, flat, well maintained.
    in_river = 8 <= (i + ni) / 2 <= 14
    # "Hill zone": top-right quadrant gets slope.
    in_hill = (i + ni) / 2 > 42 and (j + nj) / 2 > 42
    slope_pct = 5.0 + 4.0 * _h(i, j, "s") if in_hill else 1.0 * _h(i, j, "s")
    return {
        "length": DEMO_STEP_M,
        "slope_pct": round(slope_pct, 2),
        "sidewalk_score": 1.0 if in_river else 0.4 + 0.6 * _h(i, j, ni, nj, "w"),
        "lighting_score": 0.9 if in_river else 0.3 + 0.7 * _h(i, j, ni, nj, "l"),
        "cctv_score": 0.3 + 0.7 * _h(i, j, ni, nj, "c"),
        "park_score": 1.0 if in_river else (0.6 if _h(i, j, "p") > 0.85 else 0.0),
        "crossing_score": 1.0 if in_river else 0.5 + 0.5 * _h(i, j, ni, nj, "x"),
        "rfs_coverage": 1.0,  # demo data is fully covered; real ETL sets per-edge coverage
    }


@functools.lru_cache(maxsize=1)
def get_graph() -> nx.Graph:
    if GRAPH_PATH.exists():
        with GRAPH_PATH.open("rb") as f:
            g = pickle.load(f)
    else:
        g = build_demo_graph()
    from .rfs import precompute_weights  # local import — no cycle at module load
    precompute_weights(g)
    return g


# Grid-bucket spatial index: O(1) lookups on the Seoul-wide graph (~500k
# nodes) without extra dependencies. Cell ≈ 220m x 175m.
_CELL_DEG = 0.002


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return (int(lat / _CELL_DEG), int(lon / _CELL_DEG))


@functools.lru_cache(maxsize=1)
def _node_index() -> dict[tuple[int, int], list[tuple[float, float, object]]]:
    g = get_graph()
    buckets: dict[tuple[int, int], list[tuple[float, float, object]]] = {}
    for n, d in g.nodes(data=True):
        buckets.setdefault(_cell(d["lat"], d["lon"]), []).append((d["lat"], d["lon"], n))
    return buckets


def nearest_node(lat: float, lon: float):
    """Nearest node + distance in meters, searching outward ring by ring."""
    buckets = _node_index()
    ci, cj = _cell(lat, lon)
    klon = m_per_deg_lon(lat)
    best, best_d2 = None, math.inf
    hit_radius = None
    for radius in range(0, 16):  # up to ~3.5km out — beyond that it's off-map
        # One extra ring past the first hit guards against a closer node
        # sitting just across a cell boundary.
        if hit_radius is not None and radius > hit_radius + 1:
            break
        for i in range(ci - radius, ci + radius + 1):
            for j in range(cj - radius, cj + radius + 1):
                if radius and max(abs(i - ci), abs(j - cj)) != radius:
                    continue  # ring only, inner cells already scanned
                for nlat, nlon, node in buckets.get((i, j), ()):
                    d2 = ((nlat - lat) * M_PER_DEG_LAT) ** 2 + ((nlon - lon) * klon) ** 2
                    if d2 < best_d2:
                        best, best_d2 = node, d2
        if best is not None and hit_radius is None:
            hit_radius = radius
    return best, math.sqrt(best_d2) if best is not None else math.inf


def subgraph_around(lat: float, lon: float, radius_m: float):
    """Subgraph view of nodes within a bbox around (lat, lon). Keeps Dijkstra
    local — the whole-Seoul graph is never searched per request (PRD §7.1)."""
    g = get_graph()
    dlat = radius_m / M_PER_DEG_LAT
    dlon = radius_m / m_per_deg_lon(lat)
    buckets = _node_index()
    i_lo, i_hi = int((lat - dlat) / _CELL_DEG), int((lat + dlat) / _CELL_DEG)
    j_lo, j_hi = int((lon - dlon) / _CELL_DEG), int((lon + dlon) / _CELL_DEG)
    nodes = [
        n
        for i in range(i_lo, i_hi + 1)
        for j in range(j_lo, j_hi + 1)
        for _, _, n in buckets.get((i, j), ())
    ]
    return g.subgraph(nodes)


def coverage_bounds() -> tuple[float, float, float, float]:
    idx = _node_index()
    lats = [p[0] for p in idx]
    lons = [p[1] for p in idx]
    return min(lats), max(lats), min(lons), max(lons)
