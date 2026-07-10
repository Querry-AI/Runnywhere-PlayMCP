import pytest

from runart.course import CourseError
from runart import graph as graphmod
from runart.geo import to_xy
from runart.models import CourseParams
from runart.render import _shape_only_route
from runart.shapes import (
    BACKTRACK_MAX_FRAC,
    MAX_ANCHORS,
    MAX_SHAPE_DISTANCE_ERROR,
    MIN_ANCHORS,
    SHAPES,
    SHAPE_STYLES,
    backtrack_fraction,
    count_self_intersections,
    diagonal_rotation_error,
    find_min_clean_course,
    generate_shape_course,
    list_shapes,
    ordered_similarity,
    outline_fraction,
    small_zigzag_penalty,
)

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="시청")
# Grid-like road network — the friendliest terrain for GPS art.
GANGNAM = dict(lat=37.4979, lon=127.0276, location_name="강남역")
_CLEAN_COURSES = {}


def _clean_course(shape):
    if shape not in _CLEAN_COURSES:
        params = CourseParams(**GANGNAM, distance_km=SHAPES[shape].min_km, shape=shape)
        _CLEAN_COURSES[shape] = find_min_clean_course(params, per_try_s=2.0)
    course = _CLEAN_COURSES[shape]
    assert course is not None, f"no clean {shape} course found in survey range"
    return course


def test_all_five_shapes_listed():
    keys = {s["shape"] for s in list_shapes()}
    assert {"cat", "dog", "giraffe", "rabbit", "whale"} <= keys


@pytest.mark.parametrize("shape", ["cat", "dog", "rabbit", "whale"])
def test_shape_course_passes_similarity_gate(shape):
    course = _clean_course(shape)
    assert course.shape_similarity is not None
    assert course.shape_similarity >= SHAPE_STYLES[shape].similarity_gate
    assert course.points[0] == course.points[-1]
    assert len(course.points) == len(course.path)
    assert all(graphmod.get_graph().has_edge(a, b) for a, b in zip(course.path, course.path[1:]))
    assert course.points == [
        (graphmod.get_graph().nodes[n]["lat"], graphmod.get_graph().nodes[n]["lon"])
        for n in course.path
    ]


@pytest.mark.parametrize("shape", ["cat", "dog", "rabbit", "whale"])
def test_shape_course_does_not_overshoot_requested_distance(shape):
    course = _clean_course(shape)
    target = course.params.distance_km
    assert abs(course.length_km - target) / target <= MAX_SHAPE_DISTANCE_ERROR


@pytest.mark.parametrize("shape", ["cat", "dog", "rabbit", "whale"])
def test_shape_course_prefers_simple_closed_non_intersecting_loop(shape):
    course = _clean_course(shape)
    params = course.params
    xy = [to_xy(lat, lon, params.lat, params.lon) for lat, lon in course.points]
    anchors = list(SHAPES[shape].outline[:-1])

    assert course.points[0] == course.points[-1]
    assert count_self_intersections(xy) == 0
    assert MIN_ANCHORS <= len(anchors) <= MAX_ANCHORS
    assert outline_fraction(anchors) >= SHAPE_STYLES[shape].outline_min_frac
    assert backtrack_fraction(graphmod.get_graph(), course.path) <= BACKTRACK_MAX_FRAC


def test_templates_are_full_body_silhouettes():
    extents = {}
    for shape in ["cat", "dog", "rabbit", "whale"]:
        pts = list(SHAPES[shape].outline[:-1])
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        extents[shape] = (max(xs) - min(xs), max(ys) - min(ys), pts)

    # Poses follow the reference GPS-art routes: dog/cat are standing side
    # profiles, whale is long with a V fluke, rabbit is dominated by ears.
    assert extents["dog"][0] > extents["dog"][1]
    assert extents["whale"][0] > 1.8 * extents["whale"][1]
    assert extents["cat"][0] > extents["cat"][1]
    assert extents["rabbit"][1] > extents["rabbit"][0]

    # Land animals need short ground contacts so legs/feet survive simplification.
    for shape in ["cat", "dog", "rabbit"]:
        _, height, pts = extents[shape]
        min_y = min(y for _, y in pts)
        foot_points = [p for p in pts if p[1] <= min_y + 0.15 * height]
        assert len(foot_points) >= 2


def test_requested_animals_prioritize_readable_side_profile():
    for shape in ["cat", "dog", "rabbit", "whale"]:
        rotations = SHAPE_STYLES[shape].rotations
        assert diagonal_rotation_error(rotations[0]) == 0.0
        assert diagonal_rotation_error(rotations[1]) == 0.0


def test_templates_keep_species_ratio_contracts():
    # The species features must stay DEEP relative to the shape diameter —
    # anything shallower than the feature-coverage tolerance (8% of the
    # diameter) can be shortcut by street routing without any metric
    # noticing, which is how blob courses used to win.
    def bbox(shape):
        xs = [x for x, _ in SHAPES[shape].outline]
        ys = [y for _, y in SHAPES[shape].outline]
        return max(xs) - min(xs), max(ys) - min(ys)

    cat_w, cat_h = bbox("cat")
    assert 0.10 <= (6.8 - 5.4) / cat_h <= 0.30    # ear notch depth
    assert 0.12 <= (5.9 - 4.0) / cat_h <= 0.35    # raised tail height
    assert 0.25 <= 3.0 / cat_w <= 0.45             # oversized head width

    dog_w, dog_h = bbox("dog")
    assert 0.18 <= 1.3 / dog_h <= 0.35            # short leg depth
    assert 0.30 <= 2.8 / dog_w <= 0.50            # oversized blocky head
    assert 0.20 <= (6.2 - 4.9) / dog_h <= 0.35    # tall reference-like ear
    assert 0.35 <= (5.9 - 3.0) / dog_w <= 0.55    # broad rectangular body

    whale_w, whale_h = bbox("whale")
    assert 0.35 <= whale_h / whale_w <= 0.50
    assert 0.30 <= (3.6 - 1.7) / whale_h <= 0.60  # fluke lobe depth

    rabbit_w, rabbit_h = bbox("rabbit")
    assert 0.30 <= (7.0 - 4.2) / rabbit_h <= 0.50  # ear separation depth
    assert 0.12 <= (1.3 - 0.5) / rabbit_w <= 0.30  # each ear is a wide loop


def test_dog_prioritizes_simple_reference_outline():
    dog = SHAPE_STYLES["dog"]
    cat = SHAPE_STYLES["cat"]

    assert dog.simplicity_weight >= cat.simplicity_weight
    assert dog.zigzag_weight >= cat.zigzag_weight
    # Blocky axis-aligned templates must stay near-upright: steep tilts turn
    # straight strokes into staircases on Seoul's mostly axis-aligned grid.
    assert all(r in (0, 15, 345) for r in dog.rotations)
    assert len(SHAPES["dog"].outline) <= 24


def test_small_zigzag_penalty_detects_staircase_noise():
    straight = [(0.0, 0.0), (100.0, 0.0), (200.0, 0.0), (300.0, 0.0)]
    staircase = [
        (0.0, 0.0), (80.0, 0.0), (80.0, 80.0), (160.0, 80.0),
        (160.0, 0.0), (240.0, 0.0), (240.0, 80.0), (320.0, 80.0),
        (320.0, 0.0), (400.0, 0.0), (400.0, 80.0), (480.0, 80.0),
        (480.0, 0.0), (560.0, 0.0),
    ]

    assert small_zigzag_penalty(straight, 300.0) == 0.0
    assert small_zigzag_penalty(staircase, 1200.0) > 0.0


def test_shape_preview_matches_guided_route_points():
    course = _clean_course("dog")
    visual = _shape_only_route(course)

    assert visual == [[round(lat, 6), round(lon, 6)] for lat, lon in course.points]


def test_forced_short_dog_is_rejected_instead_of_shipping_blob():
    params = CourseParams(**GANGNAM, distance_km=5.0, shape="dog")
    with pytest.raises(CourseError):
        generate_shape_course(params)


def test_ordered_similarity_rejects_feature_reordering():
    template = list(SHAPES["cat"].outline[:-1])
    reordered = template[:5] + list(reversed(template[5:10])) + template[10:]
    diameter = max(max(x for x, _ in template) - min(x for x, _ in template),
                   max(y for _, y in template) - min(y for _, y in template))
    assert ordered_similarity(template, template, diameter) > 0.99
    assert ordered_similarity(reordered, template, diameter) < 0.85


def test_too_short_for_giraffe_suggests_alternatives():
    # 3.5km: 토끼·고양이·고래(min 3km)는 가능, 기린(min 6km)은 불가 → 대안 제시
    params = CourseParams(**CITY_HALL, distance_km=3.5, shape="giraffe")
    with pytest.raises(CourseError) as e:
        generate_shape_course(params)
    assert "기린" in str(e.value)
    assert "가능" in str(e.value)  # alternatives offered, not a bare refusal


def test_unknown_shape_lists_options():
    params = CourseParams(**CITY_HALL, distance_km=5.0, shape="dragon")
    with pytest.raises(CourseError) as e:
        generate_shape_course(params)
    assert "cat" in str(e.value)
