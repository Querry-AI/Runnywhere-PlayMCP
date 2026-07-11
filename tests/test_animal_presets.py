import gzip
import json

from runart import animal_presets
from runart.course import Course
from runart.models import CourseParams


def _params(shape="dog"):
    return CourseParams(lat=37.56658, lon=126.97824,
                        location_name="시청역", distance_km=9, shape=shape)


def test_preset_roundtrip_and_known_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNART_ALLOW_UNVERIFIED_DATA", "1")
    params = _params()
    course = Course(params=params, path=[1, 2, 1],
                    points=[(37.5, 127.0), (37.51, 127.01), (37.5, 127.0)],
                    length_m=9000, ascent_m=12, rfs={"score": 80},
                    shape_similarity=0.8)
    entries = {
        animal_presets.preset_key(params.lat, params.lon, "dog"):
            animal_presets.serialize_course(course),
        animal_presets.preset_key(params.lat, params.lon, "cat"): None,
    }
    path = tmp_path / "presets.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"format_version": animal_presets.FORMAT_VERSION,
                   "graph_fingerprint": "test", "entries": entries}, f)
    monkeypatch.setattr(animal_presets, "PRESET_PATH", path)
    monkeypatch.setattr(animal_presets, "graph_fingerprint", lambda: "test")
    animal_presets._load.cache_clear()

    loaded = animal_presets.get_animal_preset(params)
    assert isinstance(loaded, Course)
    assert loaded.path == course.path
    assert loaded.shape_similarity == 0.8
    assert animal_presets.get_animal_preset(_params("cat")) is None
    assert animal_presets.get_animal_preset(_params("whale")) is animal_presets.MISSING
    animal_presets._load.cache_clear()


def test_nondefault_routing_options_do_not_use_station_preset():
    params = _params().model_copy(update={"night_mode": True})
    assert animal_presets.get_animal_preset(params) is animal_presets.MISSING


def test_nearest_verified_preset_falls_back_with_distance(monkeypatch):
    course = Course(params=_params(), path=[1, 2, 1],
                    points=[(37.56658, 126.97824)], length_m=9000,
                    rfs={"score": 80}, shape_similarity=0.8)
    entries = {
        animal_presets.preset_key(course.params.lat, course.params.lon, "dog"):
            animal_presets.serialize_course(course)
    }
    monkeypatch.setattr(animal_presets, "_load", lambda: entries)
    nearby_request = _params().model_copy(update={"lat": 37.5700, "lon": 126.97824})
    match = animal_presets.find_nearest_animal_preset(nearby_request)
    assert match is not None
    assert 350 < match.distance_m < 400
    assert match.course.params.location_name == "시청역"


def test_nearest_preset_respects_radius(monkeypatch):
    course = Course(params=_params(), path=[1, 2, 1], points=[],
                    length_m=9000, rfs={"score": 80})
    monkeypatch.setattr(animal_presets, "_load", lambda: {
        animal_presets.preset_key(course.params.lat, course.params.lon, "dog"):
            animal_presets.serialize_course(course)
    })
    far_request = _params().model_copy(update={"lat": 37.60})
    assert animal_presets.find_nearest_animal_preset(
        far_request, max_distance_m=500) is None
