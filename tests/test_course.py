import pytest

from runart.course import (Course, CourseError, course_route_issues,
                           edge_is_runnable, generate_course,
                           rebase_closed_course_start)
from runart.models import CourseParams, decode_course_id, encode_course_id
from runart.render import course_thumbnail_svg, preview_html

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="시청")


def test_loop_returns_to_start_within_tolerance():
    params = CourseParams(**CITY_HALL, distance_km=5.0)
    course = generate_course(params)
    assert course.points[0] == course.points[-1]
    assert abs(course.length_km - 5.0) / 5.0 <= 0.10
    assert 0 <= course.rfs["score"] <= 100


def test_closed_course_start_is_rebased_to_nearest_requested_point():
    params = CourseParams(**CITY_HALL, distance_km=5.0, shape="rabbit")
    original = Course(
        params=params,
        path=[10, 20, 30, 10],
        points=[
            (37.5750, 126.9900),
            (37.5666, 126.9781),
            (37.5600, 126.9700),
            (37.5750, 126.9900),
        ],
        length_m=5000,
        ascent_m=20,
        rfs={"score": 80},
    )

    rebased = rebase_closed_course_start(original)

    assert rebased.path == [20, 30, 10, 20]
    assert rebased.points[0] == rebased.points[-1] == (37.5666, 126.9781)
    assert rebased.length_m == original.length_m
    assert original.path == [10, 20, 30, 10]


def test_course_thumbnail_places_route_over_the_real_osm_street_network():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))

    svg = course_thumbnail_svg(course)

    assert 'viewBox="0 0 320 320"' in svg
    assert "<polyline" in svg and "<circle" in svg
    assert '<clipPath id="map-clip">' in svg
    assert svg.count("<path") >= 4
    assert "M0 80H320" not in svg  # decorative grid was not a real map
    assert 'stroke="#fff" stroke-width="2.2"' in svg
    assert "<text" not in svg
    assert "시청" not in svg and "5.0km" not in svg


@pytest.mark.parametrize("highway", [
    "trunk", "trunk_link", "primary_link", "secondary_link", "steps",
    "track", "bridleway", "busway", "corridor",
])
def test_non_runnable_highway_classes_are_blocked(highway):
    assert not edge_is_runnable({"highway": highway, "slope_pct": 0})


def test_steep_mountain_path_and_far_station_start_are_rejected():
    import networkx as nx

    params = CourseParams(**CITY_HALL, distance_km=5.0, shape="rabbit")
    course = Course(
        params=params,
        path=[1, 2, 1],
        points=[
            (37.5700, 126.9820),
            (37.5710, 126.9830),
            (37.5700, 126.9820),
        ],
        length_m=5000,
        ascent_m=20,
        rfs={"score": 70},
    )
    graph = nx.Graph()
    graph.add_edge(1, 2, length=2500, highway="path", slope_pct=14)

    issues = course_route_issues(course, graph)

    assert "start_too_far" in issues
    assert "blocked_highway:path" in issues


def test_flat_course_avoids_hills():
    # 여의도한강공원 — genuinely flat riverside; downtown always rolls a bit.
    params = CourseParams(lat=37.5285, lon=126.9328, location_name="여의도한강공원",
                          distance_km=4.0, include_hills=False)
    course = generate_course(params)
    assert course.is_flat


def test_night_generation_rejects_poor_lighting_even_with_high_overall_score(monkeypatch):
    monkeypatch.setattr("runart.course.route_rfs_summary", lambda *args: {
        "score": 99, "components": {"lighting": .3}})
    with pytest.raises(CourseError, match="가로등이 충분한"):
        generate_course(CourseParams(**CITY_HALL, distance_km=4.0, night_mode=True))
    monkeypatch.setattr("runart.course.route_rfs_summary", lambda *args: {
        "score": 80, "components": {"lighting": .9}})
    night = generate_course(CourseParams(**CITY_HALL, distance_km=4.0, night_mode=True))
    assert night.rfs["components"]["lighting"] == .9


@pytest.mark.parametrize("score,observed,expected", [
    (None, None, False), (.30, None, False), (.32, None, False),
    (.33, None, True), (.4, None, True), (.5, None, False),
    (.5, 0, False), (.5, 1, True), (.6, 0, False), (.6, None, True),
    (float("nan"), None, False), (True, None, False), (1.1, None, False),
])
def test_night_threshold_accepts_ordinary_but_not_dark_or_unknown_lighting(score, observed, expected):
    from runart.rfs import has_sufficient_night_lighting
    summary = {"components": {"lighting": score}}
    if observed is not None:
        summary["lighting_observed_ratio"] = observed
    assert has_sufficient_night_lighting(summary) is expected


def test_unknown_edge_defaults_cannot_pass_as_ordinary_lighting(monkeypatch):
    import networkx as nx
    from runart import rfs
    monkeypatch.setattr(rfs, "citywide_top_percent", lambda score: 50)
    graph = nx.Graph()
    graph.add_edges_from([(1, 2), (2, 3), (3, 1)], length=100)
    summary = rfs.route_rfs_summary(graph, [1, 2, 3, 1], night_mode=True)
    assert summary["components"]["lighting"] == .5
    assert summary["lighting_observed_ratio"] == 0
    assert not rfs.has_sufficient_night_lighting(summary)
    graph.edges[1, 2]["lighting_score"] = .4
    graph.edges[2, 3]["lighting_score"] = .6
    measured = rfs.route_rfs_summary(graph, [1, 2, 3, 1], night_mode=True)
    assert measured["components"]["lighting"] == .5
    assert rfs.has_sufficient_night_lighting(measured)


def test_out_of_area_raises_guidance():
    with pytest.raises(CourseError):
        generate_course(CourseParams(lat=37.41, lon=127.10, distance_km=5.0))


def test_course_id_roundtrip():
    params = CourseParams(**CITY_HALL, distance_km=5.0, night_mode=True,
                          need_facilities=["restroom", "water"])
    cid = encode_course_id(params)
    restored = decode_course_id(cid)
    assert restored.canonical() == params.canonical()
    assert encode_course_id(restored) == cid  # deterministic / stateless


def test_default_route_ids_stay_compatible_and_variants_roundtrip():
    params = CourseParams(**CITY_HALL, distance_km=5)
    assert "route_variant" not in params.canonical()
    legacy_id = encode_course_id(params)
    variant = params.model_copy(update={"route_variant": 2})
    variant_id = encode_course_id(variant)
    assert variant_id != legacy_id
    assert decode_course_id(variant_id).route_variant == 2
    first = generate_course(variant)
    restored = generate_course(decode_course_id(variant_id))
    assert first.path == restored.path


def test_course_id_rejects_oversized_input():
    with pytest.raises(ValueError, match="too large"):
        decode_course_id("A" * 4097)


def test_preview_uses_kakao_maps_without_leaflet():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example",
                        kakao_javascript_key="javascript-key", page="run")
    assert "dapi.kakao.com/v2/maps/sdk.js?appkey=javascript-key" in page
    assert "new kakao.maps.Map" in page
    assert "Leaflet" not in page and "leaflet" not in page
    assert "basemaps.cartocdn.com" not in page
    assert "© OpenStreetMap contributors" in page
    assert "PretendardVariable.woff2" in page
    # Starting a run is the run page's own control; the map keeps no
    # duplicate of it.
    assert 'class="run-locate"' not in page
    assert 'class="run-start"' in page
    assert "prefers-reduced-motion" in page
    assert "동물 실루엣" not in page  # plain courses use the neutral label
    assert "코스 라인" in page


def test_preview_explains_missing_kakao_javascript_key():
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    page = preview_html(course, [], "https://runnywhere.example")
    assert "dapi.kakao.com/v2/maps/sdk.js" not in page
    assert "지도를 불러오지 못했어요" in page
    assert "KAKAO_JAVASCRIPT_KEY" not in page


def test_generation_prefers_a_real_circuit_over_walking_out_and_back():
    """A 4.6km park loop that was 92% the same path out and back scored as
    well as a genuine circuit, because nothing in the ranking saw the overlap."""
    from runart.course import RETRACE_PENALTY, retrace_share
    from runart import graph as graphmod

    g = graphmod.get_graph()
    assert RETRACE_PENALTY > 0
    # 어린이대공원: a tree-shaped footpath network next to a street grid.
    course = generate_course(CourseParams(
        lat=37.5497, lon=127.0815, location_name="어린이대공원", distance_km=5.0))

    assert retrace_share(g, course.path) < 0.25


def test_ticketed_palace_grounds_are_not_run_through():
    """OSM maps 경복궁's internal paths in detail -- 168 unnamed footway and
    steps edges -- so a route could be sent through grounds that charge
    admission and lock their gates overnight, over a part of Kakao's basemap
    that draws no path at all."""
    from runart import graph as graphmod
    from runart.course import easy_route_weight, routing_weight
    from runart.geo import haversine_m
    from runart.rfs import GATED_GROUNDS, _inside_gated

    g = graphmod.get_graph()
    inside = {n for n, d in g.nodes(data=True)
              if haversine_m(37.5796, 126.9770, d["lat"], d["lon"]) <= 220}
    palace_edges = [a for u, v, a in g.edges(data=True)
                    if u in inside and v in inside]

    assert palace_edges, "expected the palace footway network in the graph"
    # Routing refuses them; edge_is_runnable still validates bundled presets.
    # Priced out rather than removed: a runner standing inside the walls must
    # still be able to route out instead of failing to generate at all.
    weight = easy_route_weight(routing_weight(False, False))
    gated = [a for a in palace_edges if a.get("gated")]
    open_edge = next(a for _, _, a in g.edges(data=True)
                     if not a.get("gated") and a.get("name")
                     and abs(float(a["length"]) - float(gated[0]["length"])) < 5)
    assert gated
    assert weight(0, 0, gated[0]) > weight(0, 0, open_edge) * 10
    # A named public street beside a palace wall stays runnable.
    assert _inside_gated(37.5796, 126.9770)
    assert not _inside_gated(37.5796, 127.0100)   # well east of every box
    assert len(GATED_GROUNDS) >= 4


def test_the_exclusion_is_small_enough_to_leave_the_city_intact():
    """Excluding by area is only defensible if it stays surgical."""
    from runart import graph as graphmod

    g = graphmod.get_graph()
    gated = sum(float(a.get("length", 0.0))
                for _, _, a in g.edges(data=True) if a.get("gated"))
    total = sum(float(a.get("length", 0.0)) for _, _, a in g.edges(data=True))

    assert gated / total < 0.01          # well under one percent of the network
    for _, _, a in g.edges(data=True):
        if a.get("gated"):
            assert not a.get("name")     # never a named public street
            highway = a.get("highway")
            if isinstance(highway, (list, tuple)):
                highway = highway[0] if highway else ""
            assert str(highway) in {"footway", "path", "steps"}
