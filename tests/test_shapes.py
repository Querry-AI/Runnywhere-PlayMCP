import pytest

from runart.course import CourseError
from runart.models import CourseParams
from runart.shapes import SHAPES, SIMILARITY_GATE, generate_shape_course, list_shapes

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="시청")
# Grid-like road network — the friendliest terrain for GPS art.
GANGNAM = dict(lat=37.4979, lon=127.0276, location_name="강남역")


def test_all_five_shapes_listed():
    keys = {s["shape"] for s in list_shapes()}
    assert {"cat", "dog", "giraffe", "rabbit", "whale"} <= keys


@pytest.mark.parametrize("shape", ["cat", "dog", "rabbit", "whale"])
def test_shape_course_passes_similarity_gate(shape):
    params = CourseParams(**GANGNAM, distance_km=max(4.0, SHAPES[shape].min_km), shape=shape)
    course = generate_shape_course(params)
    assert course.shape_similarity is not None
    assert course.shape_similarity >= SIMILARITY_GATE
    assert course.points[0] == course.points[-1]


def test_too_short_for_giraffe_suggests_alternatives():
    params = CourseParams(**CITY_HALL, distance_km=2.5, shape="giraffe")
    with pytest.raises(CourseError) as e:
        generate_shape_course(params)
    assert "기린" in str(e.value)
    assert "가능" in str(e.value)  # alternatives offered, not a bare refusal


def test_unknown_shape_lists_options():
    params = CourseParams(**CITY_HALL, distance_km=5.0, shape="dragon")
    with pytest.raises(CourseError) as e:
        generate_shape_course(params)
    assert "cat" in str(e.value)
