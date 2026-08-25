"""Running Friendliness Score (PRD §5.7).

Weighted blend of Seoul Open Data Plaza-derived edge attributes. Two weight
profiles: default (safety-first is the default routing behavior) and night
mode (lighting/CCTV boosted). include_hills flips the slope term into a
training-grade bonus.
"""

WEIGHTS_DEFAULT = {
    "sidewalk": 0.25,
    "slope": 0.20,
    "lighting": 0.15,
    "cctv": 0.10,
    "park": 0.20,
    "crossing": 0.10,
}
WEIGHTS_NIGHT = {
    "sidewalk": 0.15,
    "slope": 0.10,
    "lighting": 0.30,
    "cctv": 0.25,
    "park": 0.10,
    "crossing": 0.10,
}

FLAT_MAX_SLOPE_PCT = 3.0
HILL_SWEET_LO, HILL_SWEET_HI = 3.0, 8.0
SCORE_FLOOR = 0.25

COMPONENT_LABELS_KO = {
    "sidewalk": "보도 넓음",
    "slope": "낮은 경사",
    "lighting": "조명 양호",
    "cctv": "안심 CCTV",
    "park": "녹지·강변길",
    "crossing": "보행 신호 적음",
}


def _slope_score(slope_pct: float, include_hills: bool) -> float:
    if include_hills:
        # Training mode: reward the 3-8% sweet spot.
        if HILL_SWEET_LO <= slope_pct <= HILL_SWEET_HI:
            return 1.0
        return max(0.0, 1.0 - abs(slope_pct - HILL_SWEET_LO) / 6.0)
    return max(0.0, 1.0 - max(0.0, slope_pct - 1.0) / FLAT_MAX_SLOPE_PCT)


def edge_rfs(attrs: dict, night_mode: bool = False, include_hills: bool = False) -> float:
    """RFS in [0,1] for one edge. Missing data → neutral 0.5 (PRD: never
    pretend absent data exists)."""
    w = WEIGHTS_NIGHT if night_mode else WEIGHTS_DEFAULT
    components = {
        "sidewalk": attrs.get("sidewalk_score", 0.5),
        "slope": _slope_score(attrs.get("slope_pct", 2.0), include_hills),
        "lighting": attrs.get("lighting_score", 0.5),
        "cctv": attrs.get("cctv_score", 0.5),
        "park": attrs.get("park_score", 0.0),
        "crossing": attrs.get("crossing_score", 0.5),
    }
    raw = sum(w[k] * v for k, v in components.items())
    # Seoul open-data coverage is patchy by segment. Keep relative ordering,
    # but avoid presenting otherwise runnable routes as harshly low-scored
    # just because one component has sparse or conservative source data.
    return SCORE_FLOOR + (1.0 - SCORE_FLOOR) * raw


def route_rfs_summary(
    graph, path: list, night_mode: bool = False, include_hills: bool = False
) -> dict:
    """Distance-weighted RFS for a node path + top contributing factors."""
    w = WEIGHTS_NIGHT if night_mode else WEIGHTS_DEFAULT
    total_len = 0.0
    score_len = 0.0
    comp_len: dict[str, float] = {k: 0.0 for k in w}
    park_len = 0.0
    for u, v in zip(path, path[1:]):
        attrs = graph.edges[u, v]
        length = attrs["length"]
        total_len += length
        score_len += edge_rfs(attrs, night_mode, include_hills) * length
        comp_len["sidewalk"] += attrs.get("sidewalk_score", 0.5) * length
        comp_len["slope"] += _slope_score(attrs.get("slope_pct", 2.0), include_hills) * length
        comp_len["lighting"] += attrs.get("lighting_score", 0.5) * length
        comp_len["cctv"] += attrs.get("cctv_score", 0.5) * length
        comp_len["park"] += attrs.get("park_score", 0.0) * length
        comp_len["crossing"] += attrs.get("crossing_score", 0.5) * length
        if attrs.get("park_score", 0.0) >= 0.8:
            park_len += length
    if total_len == 0:
        return {"score": 0, "highlights": [], "park_ratio": 0.0}
    comps = {k: v / total_len for k, v in comp_len.items()}
    top = sorted(comps.items(), key=lambda kv: kv[1] * w[kv[0]], reverse=True)[:3]
    highlights = [COMPONENT_LABELS_KO[k] for k, v in top if v >= 0.6]
    park_ratio = park_len / total_len
    if park_ratio >= 0.3:
        highlights.insert(0, f"녹지·강변길 {park_ratio:.0%}")
        highlights = [h for h in highlights if h != COMPONENT_LABELS_KO["park"]]
    score01 = score_len / total_len
    return {
        "score": round(100 * score01),
        "top_percent": citywide_top_percent(score01),
        "highlights": highlights[:3],
        "park_ratio": park_ratio,
        "components": {k: round(v, 2) for k, v in comps.items()},
        "weights": w,
    }


import bisect
import functools


@functools.lru_cache(maxsize=1)
def _citywide_sample() -> list[float]:
    """Sorted RFS sample across the whole network — turns an absolute score
    into a citywide percentile the user can actually interpret."""
    from . import graph as graphmod
    g = graphmod.get_graph()
    edges = list(g.edges(data=True))
    step = max(1, len(edges) // 20000)
    return sorted(edge_rfs(a) for _, _, a in edges[::step])


def citywide_top_percent(score01: float) -> int:
    sample = _citywide_sample()
    below = bisect.bisect_left(sample, score01)
    return max(1, round(100 * (1 - below / len(sample))))


def weight_value(attrs: dict, night_mode: bool, include_hills: bool) -> float:
    """Edge weight for shortest-path search: distance inflated by RFS deficit,
    so equal-length friendlier edges win (PRD §5.3). Flat mode adds an
    explicit grade penalty — the RFS slope term alone is too soft to steer a
    loop around a hill."""
    cost = 1.0 + 1.0 * (1.0 - edge_rfs(attrs, night_mode, include_hills))
    if not include_hills:
        cost += 0.22 * max(0.0, attrs.get("slope_pct", 2.0) - 1.0)
    return attrs["length"] * cost


def weight_attr(night_mode: bool, include_hills: bool) -> str:
    """Attribute name of the precomputed weight (see graph.get_graph).
    String weights let Dijkstra do dict lookups instead of calling a Python
    function per edge — the single biggest latency lever we have."""
    return f"w_{'n' if night_mode else 'd'}{'h' if include_hills else 'f'}"


# Ticketed palace grounds, closed overnight. OSM maps their internal paths in
# detail -- 경복궁 alone contributes 168 unnamed footway/steps edges -- so a
# route could be sent through a place that charges admission and locks its
# gates, and Kakao's basemap draws none of it, leaving the line over grass.
# Boxes are deliberately tight around the walled grounds.
GATED_GROUNDS = (
    ("경복궁", 37.5738, 37.5860, 126.9740, 126.9805),
    ("창덕궁", 37.5765, 37.5845, 126.9885, 126.9955),
    ("창경궁", 37.5760, 37.5820, 126.9925, 126.9985),
    ("덕수궁", 37.5640, 37.5680, 126.9730, 126.9775),
    ("종묘", 37.5715, 37.5765, 126.9910, 126.9970),
)


def _inside_gated(lat: float, lon: float) -> bool:
    return any(lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon
               for _, lo_lat, hi_lat, lo_lon, hi_lon in GATED_GROUNDS)


# The walled grounds are a footway network. Anything else inside a box is a
# public street the box happens to clip, and a coarse box must never take one:
# an unnamed residential road by 덕수궁 broke a bundled preset the first time.
# highway=pedestrian is a public plaza or mall by definition -- 광화문광장 sits
# against 경복궁's south wall -- so it stays out of this set.
GATED_HIGHWAYS = frozenset({"footway", "path", "steps"})


def mark_gated_edges(g) -> int:
    """Flag paths that lie wholly inside ticketed, gated grounds.

    Named roads are left alone: 삼청로 runs along a palace wall and is a public
    street. What is flagged is the unnamed footway network inside the walls.
    """
    flagged = 0
    for u, v, attrs in g.edges(data=True):
        if attrs.get("name"):
            continue
        highway = attrs.get("highway")
        if isinstance(highway, (list, tuple)):
            highway = highway[0] if highway else ""
        if str(highway or "") not in GATED_HIGHWAYS:
            continue
        a, b = g.nodes[u], g.nodes[v]
        if _inside_gated(a["lat"], a["lon"]) and _inside_gated(b["lat"], b["lon"]):
            attrs["gated"] = True
            flagged += 1
    return flagged


# Generation routes on the precomputed string weights, which cannot call a
# filter, so gated grounds are priced out instead of removed. Left traversable
# rather than deleted: a start point inside the walls must still find its way
# out rather than fail to generate at all.
GATED_WEIGHT_FACTOR = 60.0


def precompute_weights(g) -> None:
    """Bake all four routing-weight variants into edge attributes (startup)."""
    mark_gated_edges(g)
    for _, _, attrs in g.edges(data=True):
        penalty = GATED_WEIGHT_FACTOR if attrs.get("gated") else 1.0
        for night in (False, True):
            for hills in (False, True):
                attrs[weight_attr(night, hills)] = (
                    weight_value(attrs, night, hills) * penalty)


def routing_weight(night_mode: bool, include_hills: bool) -> str:
    """Weight argument for nx.dijkstra_path — precomputed attr name."""
    return weight_attr(night_mode, include_hills)
