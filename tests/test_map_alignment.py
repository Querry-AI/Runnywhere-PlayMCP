"""Kakao-basemap alignment profiles for OSM route generation."""

from runart import graph as graphmod
from runart.course import CourseError, _routing_weight_for, generate_course
from runart.models import CourseParams
from runart.rfs import (UNNAMED_WAY_PENALTY, map_alignment_factor,
                        prefers_park_paths, weight_value)
from runart.shapes import main_road_cost


BASE_EDGE = {
    "length": 100.0,
    "highway": "footway",
    "sidewalk_score": 0.5,
    "slope_pct": 0.0,
    "lighting_score": 0.5,
    "cctv_score": 0.5,
    "park_score": 0.0,
    "crossing_score": 0.5,
}


def test_unnamed_footway_gets_a_strong_default_map_alignment_penalty():
    named = {**BASE_EDGE, "name": "보이는 산책로"}
    unnamed = dict(BASE_EDGE)

    assert UNNAMED_WAY_PENALTY >= 2.0
    assert map_alignment_factor(unnamed) == UNNAMED_WAY_PENALTY
    assert weight_value(unnamed, False, False) > (
        weight_value(named, False, False) * 2)
    assert main_road_cost(unnamed) > main_road_cost(named) * 2


def test_explicit_park_mode_restores_unnamed_park_paths():
    named = {**BASE_EDGE, "name": "보이는 산책로"}
    unnamed = dict(BASE_EDGE)

    assert prefers_park_paths(["park"])
    assert not prefers_park_paths([])
    assert map_alignment_factor(unnamed, prefer_parks=True) == 1.0
    assert weight_value(unnamed, False, False, prefer_parks=True) == (
        weight_value(named, False, False))
    assert main_road_cost(unnamed, prefer_parks=True) == main_road_cost(named)


def test_park_request_selects_the_precomputed_park_routing_profile():
    base = CourseParams(lat=37.5665, lon=126.9780, location_name="시청")
    park = base.model_copy(update={"need_facilities": ["park"]})

    assert _routing_weight_for(base) == "w_df"
    assert _routing_weight_for(park) == "w_dfp"


def _unnamed_share(course, graph):
    from runart.course import highway_class

    total = unnamed = 0.0
    for u, v in zip(course.path, course.path[1:]):
        attrs = graph.edges[u, v]
        length = float(attrs.get("length", 0.0))
        total += length
        if highway_class(attrs) in ("footway", "path") and not attrs.get("name"):
            unnamed += length
    return unnamed / total if total else 0.0


def test_a_generated_course_prefers_ways_the_basemap_actually_draws():
    """Kakao draws named streets and almost no unnamed footpaths, so a route
    over them reads as a line floating across a green polygon."""
    from runart import graph as graphmod
    from runart.course import generate_course
    from runart.models import CourseParams

    g = graphmod.get_graph()
    starts = [("시청", 37.5665, 126.9780), ("여의도", 37.5215, 126.9243),
              ("강남", 37.4979, 127.0276)]

    for name, lat, lon in starts:
        course = generate_course(CourseParams(
            lat=lat, lon=lon, location_name=name, distance_km=5.0))
        assert _unnamed_share(course, g) <= 0.15, name


def test_asking_for_a_park_run_puts_the_park_paths_back():
    """Park and riverside paths are exactly the ways Kakao does not draw, so a
    runner who asks for one has to be able to get it."""
    from runart import graph as graphmod
    from runart.course import generate_course
    from runart.models import CourseParams

    g = graphmod.get_graph()
    where = dict(lat=37.5215, lon=126.9243, location_name="여의도")

    plain = generate_course(CourseParams(**where, distance_km=5.0))
    park = generate_course(CourseParams(**where, distance_km=5.0,
                                        need_facilities=["park"]))

    assert _unnamed_share(park, g) > _unnamed_share(plain, g)


# ---------------------------------------------------------------------------
# Military grounds
# ---------------------------------------------------------------------------

def test_yongsan_garrison_roads_are_marked_military():
    """OSM maps the base in full and nothing in the bundled graph says
    "military", so courses ran straight through it."""
    from runart.rfs import MILITARY_GROUNDS, _edge_name, _is_garrison_name
    g = graphmod.get_graph()
    lo_lat, hi_lat, lo_lon, hi_lon = MILITARY_GROUNDS
    flagged = [(u, v, a) for u, v, a in g.edges(data=True) if a.get("military")]
    assert flagged, "no military edges flagged at all"

    named = [a for _, _, a in flagged if _is_garrison_name(_edge_name(a))]
    assert len(named) >= 150, f"only {len(named)} named garrison roads found"

    for u, v, _ in flagged:
        for node in (u, v):
            data = g.nodes[node]
            assert lo_lat <= data["lat"] <= hi_lat, "flagged outside the box"
            assert lo_lon <= data["lon"] <= hi_lon, "flagged outside the box"


def test_public_streets_inside_the_box_are_never_flagged():
    """이태원로, 한강대로, 녹사평대로 and the named residential grid all cross
    or border the base. A coarse box must not take one -- the same mistake an
    unnamed residential road by 덕수궁 caught the first time round."""
    from runart.rfs import _edge_name, _is_garrison_name
    g = graphmod.get_graph()
    for _, _, attrs in g.edges(data=True):
        if not attrs.get("military"):
            continue
        name = _edge_name(attrs)
        assert not name or _is_garrison_name(name), f"public street flagged: {name}"


def test_a_romanised_korean_street_is_not_a_garrison_road():
    from runart.rfs import _is_garrison_name
    assert _is_garrison_name("8th Army Drive")
    assert _is_garrison_name("X Corps Boulevard")
    assert not _is_garrison_name("Achasan-ro 53-gil")
    assert not _is_garrison_name("Baekbeom-ro 39-gil")
    assert not _is_garrison_name("이태원로")
    assert not _is_garrison_name("")


def test_no_course_near_yongsan_runs_through_the_garrison():
    """Measured before the fix: 18.4% of an 8km loop from 녹사평, and 11-17%
    from 이태원, 삼각지 and 서빙고."""
    g = graphmod.get_graph()
    starts = [("녹사평", 37.5348, 126.9866), ("이태원", 37.5345, 126.9946),
              ("삼각지", 37.5347, 126.9730), ("서빙고", 37.5195, 126.9885)]
    built = 0
    for name, lat, lon in starts:
        for distance in (5.0, 8.0):
            try:
                course = generate_course(CourseParams(
                    lat=lat, lon=lon, location_name=name, distance_km=distance))
            except CourseError:
                continue          # a hemmed-in start may have no loop; that is honest
            built += 1
            through = sum(
                float(g.edges[u, v]["length"])
                for u, v in zip(course.path, course.path[1:])
                if g.edges[u, v].get("military"))
            assert through == 0, f"{name} {distance}km entered the base for {through:.0f}m"
    assert built >= 6, f"only {built} of 8 starts produced a course"


def test_a_waypoint_on_the_base_is_nudged_off_it_or_refused_outright():
    """A single unusable circle waypoint used to abandon the bearing, which
    left seven of sixteen starts around the base unable to build anything. The
    contract is narrow: never hand back a node inside the walls. Deep inside,
    where nothing usable is within reach, refusing is the honest answer and the
    generator simply tries another bearing."""
    from runart.course import (_routing_weight_for, _usable_waypoint,
                               easy_route_weight)
    g = graphmod.get_graph()
    params = CourseParams(lat=37.5348, lon=126.9866, location_name="녹사평")
    weight = easy_route_weight(_routing_weight_for(params), prefer_named_walkways=True)

    military_starts = [
        (g.nodes[u]["lat"], g.nodes[u]["lon"])
        for u, _, a in g.edges(data=True) if a.get("military")][:60]
    assert military_starts
    nudged = 0
    for lat, lon in military_starts:
        node = _usable_waypoint(g, weight, lat, lon)
        if node is None:
            continue
        # A gate node legitimately touches both sides; what matters is that
        # the router can leave it without entering the base.
        assert any(not g.edges[node, other].get("military") for other in g[node]), (
            "handed back a waypoint with no way off the base")
        nudged += 1
    assert nudged, "every waypoint on the base was refused; none was nudged clear"


def test_the_nearest_node_is_still_used_wherever_it_works():
    """course_id re-generates its course, so a waypoint that was already fine
    has to keep resolving to exactly the same node."""
    from runart.course import _usable_waypoint, easy_route_weight, _routing_weight_for
    g = graphmod.get_graph()
    params = CourseParams(lat=37.5665, lon=126.9780, location_name="서울시청")
    weight = easy_route_weight(_routing_weight_for(params), prefer_named_walkways=True)
    for lat, lon in ((37.5665, 126.9780), (37.5216, 126.9243), (37.5133, 127.1000)):
        nearest, _ = graphmod.nearest_node(lat, lon)
        assert _usable_waypoint(g, weight, lat, lon) == nearest
