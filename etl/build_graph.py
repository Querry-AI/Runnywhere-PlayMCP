"""Offline ETL step 1: extract the Seoul-wide pedestrian network from OSM.

Run locally (never at runtime — PRD §5.7): the output pickle ships inside the
container image.

    pip install 'runart[etl]'
    python etl/build_graph.py

Output: data/seoul_graph.pkl — networkx.Graph with node attrs (lat, lon) and
edge attrs (length + raw OSM tags that etl/build_rfs.py turns into RFS
component scores).
"""

import pickle
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "seoul_graph.pkl"

# Sidewalk-quality prior by OSM highway class (refined by Seoul open data in
# build_rfs.py when available).
HIGHWAY_SIDEWALK_PRIOR = {
    "footway": 0.9, "path": 0.8, "pedestrian": 1.0, "steps": 0.2,
    "living_street": 0.7, "residential": 0.55, "service": 0.5,
    "unclassified": 0.5, "tertiary": 0.45, "secondary": 0.4, "primary": 0.35,
    "cycleway": 0.75, "track": 0.6,
}
# Crossing-friction prior: separated paths rarely hit signals; big roads do.
HIGHWAY_CROSSING_PRIOR = {
    "footway": 0.85, "path": 0.9, "pedestrian": 0.95, "cycleway": 0.85,
    "living_street": 0.7, "residential": 0.6, "service": 0.6,
    "tertiary": 0.45, "secondary": 0.35, "primary": 0.3,
}


def _first(val):
    return val[0] if isinstance(val, list) else val


def main() -> None:
    try:
        import osmnx as ox
    except ImportError:
        sys.exit("osmnx가 필요합니다: pip install 'runart[etl]'")

    print("Downloading Seoul walk network from OSM (takes a while)...", flush=True)
    g_multi = ox.graph_from_place("Seoul, South Korea", network_type="walk", simplify=True)
    print(f"Raw multigraph: {len(g_multi):,} nodes", flush=True)

    import networkx as nx
    g = nx.Graph()
    for n, d in g_multi.nodes(data=True):
        g.add_node(n, lat=d["y"], lon=d["x"])
    for u, v, d in g_multi.edges(data=True):
        length = float(d.get("length", 0.0))
        if u == v or length <= 0:
            continue
        if g.has_edge(u, v) and g.edges[u, v]["length"] <= length:
            continue
        highway = _first(d.get("highway")) or "unclassified"
        lit = _first(d.get("lit"))
        name = _first(d.get("name")) or ""
        attrs = {
            "length": length,
            "highway": highway,
            "name": name,  # joined against 서울시 보도통계자료 노선명 in build_rfs
            # Raw signal for build_rfs; scores below are priors it refines.
            "lit": lit,
            "sidewalk_score": HIGHWAY_SIDEWALK_PRIOR.get(highway, 0.5),
            "crossing_score": HIGHWAY_CROSSING_PRIOR.get(highway, 0.5),
            "lighting_score": {"yes": 0.95, "no": 0.15}.get(lit, 0.5),
            "cctv_score": 0.5,
            "park_score": 0.0,
            "slope_pct": 2.0,
            "rfs_coverage": 0.0,  # build_rfs raises this per joined dataset
        }
        g.add_edge(u, v, **attrs)

    largest = max(nx.connected_components(g), key=len)
    g = g.subgraph(largest).copy()
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("wb") as f:
        pickle.dump(g, f)
    print(f"Saved {g.number_of_nodes():,} nodes / {g.number_of_edges():,} edges -> {OUT}",
          flush=True)


if __name__ == "__main__":
    main()
