"""Running Friendliness Score (PRD §5.7).

Weighted blend of Seoul Open Data Plaza-derived edge attributes. Two weight
profiles: default (safety-first is the default routing behavior) and night
mode (lighting/CCTV boosted). include_hills flips the slope term into a
training-grade bonus.
"""

import re

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
# The presentation's "good" band is not the minimum for a usable night run.
# Components are rounded to two decimals; .33 is the first non-poor band
# above the existing <= .32 dark-course cutoff (citywide median is .33).
NIGHT_LIGHTING_MIN = 0.33
GOOD_LIGHTING_MIN = 0.60


def has_sufficient_night_lighting(summary: dict) -> bool:
    """Allow ordinary lighting, but never treat an unknown .5 prior as data."""
    value = (summary.get("components") or {}).get("lighting")
    if not (isinstance(value, (int, float)) and not isinstance(value, bool)
            and NIGHT_LIGHTING_MIN <= value <= 1):
        return False
    observed = summary.get("lighting_observed_ratio")
    if observed is not None:
        return (isinstance(observed, (int, float)) and not isinstance(observed, bool)
                and 0 < observed <= 1)
    # Older preset summaries have no observation field. Any non-neutral
    # aggregate proves some measured input, but .5 alone could be all defaults.
    return value != .5


def night_lighting_label(summary: dict) -> str:
    if not has_sufficient_night_lighting(summary):
        return ""
    return ("야간 조명 양호" if summary["components"]["lighting"] >= GOOD_LIGHTING_MIN
            else "야간 조명 보통")


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
    lighting_observed_len = 0.0
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
        # Missing/untagged graph inputs use .5. A measured .5 can be known
        # from the raw OSM tag; other non-neutral scores include ETL evidence.
        lighting = attrs.get("lighting_score")
        if (isinstance(lighting, (int, float)) and not isinstance(lighting, bool)
                and 0 <= lighting <= 1
                and (lighting != .5 or attrs.get("lit") in ("yes", "no"))):
            lighting_observed_len += length
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
        "lighting_observed_ratio": lighting_observed_len / total_len,
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


# Kakao's basemap labels named streets and draws most unnamed footpaths not at
# all, so a route over them reads as a line floating across a green polygon.
# Measured over the whole graph, the name tag separates the two cleanly:
# residential 92% named, primary 98%, tertiary 97% — against footway 3% and
# path 12%. Preferring the named way where one exists is what keeps the drawn
# course and the map underneath it telling the same story.
UNNAMED_WAY_PENALTY = 2.2
UNNAMED_PENALISED_HIGHWAYS = frozenset({"footway", "path"})


def _is_unnamed_path(attrs: dict) -> bool:
    if attrs.get("name"):
        return False
    highway = attrs.get("highway")
    if isinstance(highway, (list, tuple)):
        highway = highway[0] if highway else ""
    return str(highway or "") in UNNAMED_PENALISED_HIGHWAYS


def prefers_park_paths(need_facilities: list[str] | None) -> bool:
    """Whether the user explicitly asked to run through a park.

    ``park`` already is the public MCP contract for a park-routing request.
    Keeping the signal there preserves old course ids and avoids inferring a
    preference merely because the start happens to be near green space.
    """
    return "park" in (need_facilities or ())


def map_alignment_factor(attrs: dict, prefer_parks: bool = False) -> float:
    """Strongly prefer OSM ways that are also likely visible on Kakao Maps."""
    if prefer_parks or not _is_unnamed_path(attrs):
        return 1.0
    return UNNAMED_WAY_PENALTY


def weight_value(attrs: dict, night_mode: bool, include_hills: bool,
                 prefer_parks: bool = False) -> float:
    """Edge weight for shortest-path search: distance inflated by RFS deficit,
    so equal-length friendlier edges win (PRD §5.3). Flat mode adds an
    explicit grade penalty — the RFS slope term alone is too soft to steer a
    loop around a hill.

    ``prefer_parks`` drops the unnamed-path penalty: park and riverside paths
    are exactly the ways Kakao does not draw, so a runner who asks for one has
    to be able to get it.
    """
    cost = 1.0 + 1.0 * (1.0 - edge_rfs(attrs, night_mode, include_hills))
    if not include_hills:
        cost += 0.22 * max(0.0, attrs.get("slope_pct", 2.0) - 1.0)
    cost *= map_alignment_factor(attrs, prefer_parks)
    return attrs["length"] * cost


def weight_attr(night_mode: bool, include_hills: bool,
                prefer_parks: bool = False) -> str:
    """Attribute name of the precomputed weight (see graph.get_graph).
    String weights let Dijkstra do dict lookups instead of calling a Python
    function per edge — the single biggest latency lever we have."""
    return (f"w_{'n' if night_mode else 'd'}{'h' if include_hills else 'f'}"
            f"{'p' if prefer_parks else ''}")


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


# Yongsan Garrison. OSM maps the base in full -- 204 of its internal roads even
# carry their US names -- and nothing in the bundled graph says "military", so
# generated courses ran straight through it: measured 18.4% of an 8km loop from
# 녹사평, and 11-17% from 이태원, 삼각지 and 서빙고.
#
# The box is derived from the data, not from memory: single-link clustering of
# every Latin-named way in the graph puts the garrison's roads in one cluster
# spanning exactly these bounds, and the only other Latin-named ways in Seoul
# (명동 지하상가, a park path, a park car park road) are far outside it.
MILITARY_GROUNDS = (37.5205, 37.5454, 126.9699, 126.9916)

# A romanised Korean street name is a public road with an English label, not a
# base road: 회나무로 44da-gil and 아차산로 53-gil both appear this way.
_ROMANISED_KOREAN = re.compile(r"-(?:ro|gil|daero|dong)\b", re.IGNORECASE)
_LATIN_NAME = re.compile(r"^[A-Za-z0-9 .'\-/&()]+$")


def _is_garrison_name(name: str) -> bool:
    """A US-army road name -- 8th Army Drive, X Corps Boulevard, Dunn Street."""
    return bool(
        name
        and _LATIN_NAME.match(name)
        and re.search(r"[A-Za-z]{3}", name)
        and not _ROMANISED_KOREAN.search(name)
    )


def _edge_name(attrs: dict) -> str:
    name = attrs.get("name")
    if isinstance(name, (list, tuple)):
        name = name[0] if name else ""
    return str(name or "").strip()


def mark_military_edges(g) -> int:
    """Flag the roads inside Yongsan Garrison.

    Seeded on the certainly-military names, then grown through *unnamed service
    roads only* so the base's internal network is caught without touching the
    public streets that cross and border it. Every Korean-named way inside the
    box -- 이태원로, 한강대로, 녹사평대로 and 396 named residential streets --
    is left exactly as it was. Measured containment: 753 of the flagged edges
    lie within 100m of a named garrison road and only 13 beyond 500m.
    """
    lo_lat, hi_lat, lo_lon, hi_lon = MILITARY_GROUNDS

    def inside(node) -> bool:
        data = g.nodes[node]
        return (lo_lat <= data["lat"] <= hi_lat
                and lo_lon <= data["lon"] <= hi_lon)

    frontier: set = set()
    for u, v, attrs in g.edges(data=True):
        if inside(u) and inside(v) and _is_garrison_name(_edge_name(attrs)):
            attrs["military"] = True
            frontier |= {u, v}
    flagged = len(frontier)
    while frontier:
        following = set()
        for node in frontier:
            for neighbour in g[node]:
                attrs = g.edges[node, neighbour]
                if attrs.get("military") or not inside(neighbour):
                    continue
                if _edge_name(attrs):
                    continue                     # a named way is public
                highway = attrs.get("highway")
                if isinstance(highway, (list, tuple)):
                    highway = highway[0] if highway else ""
                if str(highway or "") != "service":
                    continue
                attrs["military"] = True
                following.add(neighbour)
        frontier = following
    return sum(1 for _, _, a in g.edges(data=True) if a.get("military")) or flagged


# Generation routes on the precomputed string weights, which cannot call a
# filter, so gated grounds are priced out instead of removed. Left traversable
# rather than deleted: a start point inside the walls must still find its way
# out rather than fail to generate at all.
GATED_WEIGHT_FACTOR = 60.0
# A palace is priced because a runner standing inside one has to get out.
# A base is priced far harder for the same reason and no other: nobody
# should be routed through it, and easy_route_weight refuses it outright.
MILITARY_WEIGHT_FACTOR = 500.0


def precompute_weights(g) -> None:
    """Bake all four routing-weight variants into edge attributes (startup)."""
    mark_gated_edges(g)
    mark_military_edges(g)
    for _, _, attrs in g.edges(data=True):
        penalty = GATED_WEIGHT_FACTOR if attrs.get("gated") else 1.0
        if attrs.get("military"):
            penalty = MILITARY_WEIGHT_FACTOR
        for night in (False, True):
            for hills in (False, True):
                for parks in (False, True):
                    attrs[weight_attr(night, hills, parks)] = (
                        weight_value(attrs, night, hills, parks) * penalty)


def routing_weight(night_mode: bool, include_hills: bool,
                   prefer_parks: bool = False) -> str:
    """Weight argument for nx.dijkstra_path — precomputed attr name."""
    return weight_attr(night_mode, include_hills, prefer_parks)
