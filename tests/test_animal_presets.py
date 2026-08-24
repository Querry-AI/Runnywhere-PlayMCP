import gzip
import json

from runart import animal_presets
from runart.course import (MAX_COURSE_START_OFFSET_M, Course,
                           course_route_issues)
from runart.geo import haversine_m
from runart.graph import get_graph
from runart.models import CourseParams
from runart.shapes import SHAPES
from runart.stations import SEOUL_METRO_STATIONS


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
    assert loaded.path == [2, 1, 2]
    assert loaded.points[0] == loaded.points[-1] == (37.51, 127.01)
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


def test_bundled_presets_cover_every_station_shape_slot_and_match_metadata():
    entries = animal_presets._load()
    assert entries is not None
    stations = {
        f"{lat:.5f},{lon:.5f}": name if name.endswith("역") else f"{name}역"
        for _, name, lat, lon, *_ in SEOUL_METRO_STATIONS
    }
    expected_keys = {
        animal_presets.preset_key(lat, lon, shape)
        for _, _, lat, lon, *_ in SEOUL_METRO_STATIONS
        for shape in SHAPES
    }
    assert set(entries) == expected_keys
    for key, raw in entries.items():
        if raw is None:
            continue
        station_key, _ = key.rsplit(",", 1)
        assert raw["params"]["location_name"] == stations[station_key]
        assert not raw["params"]["location_name"].endswith("역역")


def test_every_bundled_course_starts_near_station_and_is_runnable():
    graph = get_graph()
    courses = animal_presets.all_verified_animal_presets()
    assert courses
    for course in courses:
        first_lat, first_lon = course.points[0]
        offset_m = haversine_m(
            course.params.lat, course.params.lon, first_lat, first_lon
        )
        assert offset_m <= MAX_COURSE_START_OFFSET_M
        assert course.points[0] == course.points[-1]
        assert course_route_issues(course, graph) == []
