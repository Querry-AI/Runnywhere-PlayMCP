"""Course cards at the MCP boundary, ranked by the user's condition priorities."""

import json

import pytest

from runart import server


@pytest.fixture(scope="module", autouse=True)
def warm_like_production():
    """The budget these cases spend is the deployed one, not a cold start."""
    server._warm()


def _card(result) -> dict:
    payload = json.loads(result.content[0].text)
    assert payload["widget"]["type"] == "Card", result.content[0].text[:400]
    return payload


def _buttons(payload) -> list[dict]:
    found = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "Button":
                found.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload["widget"])
    return found


def _values(payload, kind: str) -> list[str]:
    found = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == kind and value.get("value"):
                found.append(value["value"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload["widget"])
    return found


def _labels(payload) -> list[str]:
    return [button["label"] for button in _buttons(payload)]


def _urls(payload) -> list[str]:
    return [button["onClickAction"]["payload"]["target"]["url"]
            for button in _buttons(payload)]


def _lead(result) -> str:
    lead = result.structuredContent["assistant_text"]
    assert result.content[1].text == lead
    assert result.structuredContent["assistant_text_in_widget"] is False
    assert result.structuredContent["assistant_text_position"] == "before_widget"
    assert result.structuredContent["assistant_text_verbatim"] is True
    return lead


ANIMAL_RUNS = ("댕댕런", "야옹런", "깡총런", "고래런")


def _course_titles(payload) -> list[str]:
    return [row["children"][1]["children"][1]["value"]
            for row in payload["widget"]["children"]
            if row["type"] == "Row" and row["children"][0]["type"] == "Image"]


def test_named_animal_returns_only_that_shape():
    result = server.create_seoul_running_course(course_type="dog", location="강남역")
    payload = _card(result)
    labels = _labels(payload)
    titles = _course_titles(payload)

    assert result.structuredContent["result_code"] == "course_ready"
    assert result.isError is False
    assert labels[0] == "지도 보기"
    assert len(labels) == 3
    assert "댕댕런" in json.dumps(payload, ensure_ascii=False)
    assert all("댕댕런" in title for title in titles)
    assert len(set(_urls(payload)[1:])) == 2


def test_case2_nearby_course_names_the_real_start_and_still_offers_three():
    result = server.create_seoul_running_course(
        course_type="whale", location="서울대입구역")
    payload = _card(result)
    lead = _lead(result)

    assert result.structuredContent["result_code"] == "nearby_course_ready"
    assert result.isError is False
    assert 1 <= len(_labels(payload)) <= 3
    # The card names the actual start directly in its leading title.
    assert "서울대입구역" in lead
    start_text = _course_titles(payload)[0]
    actual_start = result.structuredContent["course_selection"]["primary"]["start"]
    assert actual_start != "서울대입구역"
    assert actual_start in lead
    assert "다른 추천 코스를 준비했어요" in lead
    assert actual_start in json.dumps(payload, ensure_ascii=False)


def test_no_animal_within_two_km_reports_shortage_without_plain_substitution():
    result = server.create_seoul_running_course(
        course_type="dog", location="도봉산역")
    assert result.isError
    assert result.structuredContent["result_code"] == "insufficient_courses"
    assert not result.structuredContent["repeat_tool_call"]
    assert "widget" not in result.content[0].text


def test_every_choice_url_resolves_to_a_real_course_page():
    result = server.create_seoul_running_course(
        course_type="whale", location="서울대입구역")
    payload = _card(result)

    for url in _urls(payload):
        course_id = url.rsplit("/c/", 1)[1].removesuffix(".gpx")
        assert server._cached_course(course_id) is not None, url


def test_plain_course_option_is_dropped_rather_than_blowing_the_budget(monkeypatch):
    """A slow plain course must cost the runner the option, not the answer."""
    def too_slow(*args, **kwargs):
        raise server._GenerationTimeout

    monkeypatch.setattr(server, "_get_course", too_slow)
    result = server.create_seoul_running_course(
        course_type="whale", location="서울대입구역")
    payload = _card(result)

    assert result.structuredContent["result_code"] in {"nearby_course_ready", "course_ready"}
    assert 2 <= len(_labels(payload)) <= 3
    assert "고래런" in json.dumps(payload, ensure_ascii=False)


def test_widget_failure_still_returns_the_markdown_answer(monkeypatch):
    monkeypatch.setattr(server, "KAKAO_WIDGETS_ENABLED", False)
    result = server.create_seoul_running_course(
        course_type="whale", location="서울대입구역")

    assert result.structuredContent["result_code"] == "nearby_course_ready"
    assert 1 <= result.structuredContent["course_selection"]["returned_count"] <= 3
    assert "다른 코스도 있어요" in result.content[0].text


# ---------------------------------------------------------------------------
# A course request always answers with a card.
#
# "40분 정도 뛸 코스" is one of the most common ways to ask, and it used to
# come back as isError with no card at all: the duration note leads with a
# clock emoji, and the result classifier read that prefix as a timeout. The
# course itself was fine and sat right underneath the note.
# ---------------------------------------------------------------------------

DURATION_STARTS = ["시청", "강남역", "서울숲", "여의도한강공원"]


@pytest.mark.parametrize("location", DURATION_STARTS)
def test_a_plain_course_asked_for_by_time_still_answers_with_a_card(location):
    result = server.create_seoul_running_course(
        course_type="standard", location=location, duration_min=40)

    if result.structuredContent["result_code"] == "insufficient_courses":
        assert result.structuredContent["available_count"] == 0
        assert result.isError and not result.content[0].text.startswith("{")
        return
    assert result.isError is False, result.content[0].text[:200]
    assert result.structuredContent["result_code"] == "course_ready"
    payload = _card(result)
    assert _labels(payload)[0] == "지도 보기"


@pytest.mark.parametrize("location", ["강남역", "서울숲"])
def test_an_animal_course_asked_for_by_time_still_answers_with_a_card(location):
    result = server.create_seoul_running_course(
        course_type="dog", location=location, duration_min=40)

    assert result.isError is False, result.content[0].text[:200]
    assert result.structuredContent["result_code"] in {
        "course_ready", "nearby_course_ready"}
    _card(result)


def test_the_duration_note_is_not_dressed_as_a_failure():
    """Failure prefixes belong to failures. A reading of what the user meant
    is not one, and using the timeout prefix for it hid a working course."""
    text = server.generate_running_course(location="시청", duration_min=40)

    assert not text.startswith(("⏱️", "⚠️"))
    assert "6.2km" in text


@pytest.mark.parametrize("kind", ["standard", "dog", "best_animal"])
def test_primary_card_honors_duration_and_shape_within_local_radius(kind):
    from runart.geo import haversine_m
    result = server.create_seoul_running_course(
        course_type=kind, location="시청", duration_min=40)
    cid = _urls(_card(result))[0].rsplit("/c/", 1)[1]
    course = server._cached_course(cid)
    lat, lon, _ = server.resolve_location("시청", None, None)
    assert haversine_m(lat, lon, course.params.lat, course.params.lon) <= (150 if kind == "standard" else 2000)
    if kind == "best_animal":
        assert course.params.shape
    else:
        assert course.params.shape == (None if kind == "standard" else "dog")
    assert abs(course.length_km - 6.2) / 6.2 <= .10


def test_explicit_preferences_are_never_relaxed_to_fill_a_slot(monkeypatch):
    from runart.models import CourseParams
    from runart.course import Course, CourseError
    probe = CourseParams(lat=37.5665, lon=126.978, distance_km=6.2,
                         need_facilities=["park"], include_hills=True)
    calls = []
    def get(params, **kwargs):
        calls.append(params)
        if params.need_facilities:
            raise CourseError("공원 코스 없음")
        return Course(params=params, path=[], length_m=6200)
    monkeypatch.setattr(server, "_get_course", get)
    result = server._plain_course_here(probe, 6.2, 2)
    assert result is None and len(calls) == 1
    assert all(p.lat == probe.lat and p.lon == probe.lon and p.distance_km == 6.2 for p in calls)
    assert calls[-1].need_facilities == ["park"] and calls[-1].include_hills


def test_legacy_animal_duration_reaches_the_shared_priority_plan(monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "generate_animal_course", lambda *a, **k: "text")
    def result(text, **kwargs):
        captured.update(kwargs)
        return server._recommendation_shortage(0)
    monkeypatch.setattr(server, "_course_tool_result", result)
    server._legacy_generate_animal_course(shape="dog", location="시청", duration_min=40)
    assert captured["request"]["duration_min"] == 40


def test_animal_card_generation_reserves_time_and_resets_deadline(monkeypatch):
    deadlines = []
    def generate(**kwargs):
        deadlines.append(server._ANIMAL_CARD_DEADLINE.get())
        return "⏱️ 동물 탐색 시간 초과"
    monkeypatch.setattr(server, "generate_animal_course", generate)
    now = server.time.monotonic()
    server._animal_text_for_cards(shape="dog", location="시청")
    assert 0 < deadlines[0] - now < server.MCP_OUTER_RESPONSE_BUDGET_S - server.PLAIN_OPTION_MIN_BUDGET_S
    assert server._ANIMAL_CARD_DEADLINE.get() is None


def test_animal_timeout_never_substitutes_a_plain_course(monkeypatch):
    from runart.course import Course
    from runart.models import CourseParams
    course = Course(params=CourseParams(lat=37.5665, lon=126.978,
                    location_name="시청", distance_km=6.2), path=[], length_m=6200)
    budgets = []
    def plain(probe, distance, timeout):
        budgets.append(timeout)
        assert distance == 6.2
        return course
    monkeypatch.setattr(server, "_plain_course_here", plain)
    monkeypatch.setattr(server, "_any_animal_matches", lambda *args: [])
    monkeypatch.setattr(server, "_cache_put", lambda *args: None)
    result = server._planned_course_result("⏱️ 동물 탐색 시간 초과", course_type="dog",
        request={"location": "시청", "duration_min": 40}, timeout_s=.9)
    assert not budgets and result is None


def test_night_animal_never_reads_daytime_presets(monkeypatch):
    probes = []
    def candidates(probe, radius):
        probes.append(probe)
        return []
    monkeypatch.setattr(server, "_any_animal_matches", candidates)
    monkeypatch.setattr(server, "_plain_course_here", lambda *args: None)
    server._animal_course_plan({"location": "시청", "include_hills": True,
        "night_mode": True, "need_facilities": ["park"]}, "dog", "", .9)
    assert probes == []


# Every way of asking for a course that a runner can act on immediately.
COURSE_REQUESTS = [
    dict(course_type="standard", location="시청", distance_km=6),
    dict(course_type="standard", location="시청", duration_min=40),
    dict(course_type="standard", location="시청"),
    dict(course_type="dog", location="강남역"),
    dict(course_type="dog", location="강남역", duration_min=40),
    dict(course_type="cat", location="시청"),
    dict(course_type="whale", location="서울대입구역"),
    dict(course_type="best_animal", location="석촌호수"),
]


@pytest.mark.parametrize(
    "request_kwargs", COURSE_REQUESTS,
    ids=[",".join(f"{k}={v}" for k, v in r.items()) for r in COURSE_REQUESTS])
def test_every_answerable_course_request_comes_back_as_a_card(request_kwargs):
    """The card is the answer for a course request; Markdown is for the rest.

    Follow-up questions (facilities, status, relays) stay Markdown by design —
    they are read, not acted on with a route.
    """
    result = server.create_seoul_running_course(**request_kwargs)

    assert result.isError is False, result.content[0].text[:200]
    payload = _card(result)
    assert _labels(payload)[0] == "지도 보기"


def test_a_follow_up_question_stays_markdown():
    course = server.create_seoul_running_course(
        course_type="standard", location="시청", distance_km=6)
    course_id = _urls(_card(course))[0].rsplit("/c/", 1)[1]

    facilities = server.find_facilities_near_course(course_id=course_id)
    status = server.get_course_status(course_id=course_id)

    for answer in (facilities, status):
        assert isinstance(answer, str)
        assert not answer.lstrip().startswith("{")


def test_single_standard_call_returns_three_distinct_routes_with_restorable_ids():
    from runart.courseplan import route_signature
    from runart.models import decode_course_id

    result = server.create_seoul_running_course(course_type="standard", location="강남역")
    payload = _card(result)
    selection = result.structuredContent["course_selection"]
    assert selection["returned_count"] == selection["requested_count"] == 3
    assert len(_urls(payload)) == len(set(_urls(payload))) == 3
    courses = [server._cached_course(url.rsplit("/", 1)[1]) for url in _urls(payload)]
    assert len({route_signature(c) for c in courses}) == 3
    for url, course in zip(_urls(payload), courses):
        restored = decode_course_id(url.rsplit("/", 1)[1])
        assert restored.canonical() == course.params.canonical()
        assert course.params.location_name == "강남역"
    assert courses[0].params.shape is None and courses[0].params.distance_km == 5
    # No explicit distance: 2/3 can reuse a nearby animal's natural length.
    assert all(server.haversine_m(courses[0].params.lat, courses[0].params.lon,
                                c.params.lat, c.params.lon) <= 2000 for c in courses[1:])


def test_park_request_without_start_samples_three_of_five_without_route_calls(monkeypatch):
    from runart import park_presets

    draws = []
    def sample(population, count):
        draws.append((len(population), count))
        return population[-count:]
    def unexpected(*args, **kwargs):
        pytest.fail("Registered park recommendations must not generate another route")
    monkeypatch.setattr(park_presets.random, "sample", sample)
    monkeypatch.setattr(server, "generate_running_course", unexpected)
    monkeypatch.setattr(server, "_animal_text_for_cards", unexpected)
    result = server.create_seoul_running_course(course_type="standard", need_facilities=["park"])

    assert draws == [(5, 3)]
    assert result.structuredContent["course_selection"]["returned_count"] == 3
    assert result.structuredContent["park_selection"]["origin"] is None
    assert result.structuredContent["park_selection"]["mode"] == "random"
    assert len(set(_urls(_card(result)))) == 3
    assert len(result.model_dump_json().encode()) < 24_000
    assert _lead(result) == "각 공원·강변에서 출발하는 코스를 추천해요."


@pytest.mark.parametrize("origin", ["강남역", "홍대", "서울숲"])
def test_park_request_with_start_orders_by_distance_to_real_course_start(origin):
    from runart.park_presets import park_courses
    from runart.geo import haversine_m

    lat, lon, _ = server.resolve_location(origin, None, None)
    expected = sorted(park_courses(), key=lambda item: (
        haversine_m(lat, lon, item[1].params.lat, item[1].params.lon), item[0].id))[:3]
    result = server.create_seoul_running_course(
        course_type="standard", location=origin, need_facilities=["park"])
    selection = result.structuredContent["park_selection"]
    assert selection["mode"] == "nearest"
    assert [d["id"] for d in selection["destinations"]] == [s.id for s, _ in expected]
    assert "직선거리" not in _lead(result)
    assert "이동 경로" not in result.content[0].text
    facts = result.structuredContent["course_selection"]
    assert [c["start"] for c in [facts["primary"], *facts["alternatives"]]] == [s.name for s, _ in expected]
    assert len(_urls(_card(result))) == 3


def test_park_recommendations_accept_coordinates_and_preserve_night_lighting():
    from runart.rfs import has_sufficient_night_lighting

    result = server.create_seoul_running_course(
        course_type="standard", lat=37.4986, lon=127.0281,
        need_facilities=["park"], night_mode=True)
    assert result.structuredContent["park_selection"]["mode"] == "nearest"
    courses = [server._cached_course(url.rsplit("/", 1)[1]) for url in _urls(_card(result))]
    assert len(courses) == 3
    assert all(c.params.night_mode and has_sufficient_night_lighting(c.rfs) for c in courses)
    # Yangjae (.34) is below .4; Yeouido (.47) no longer needs the old .6 cutoff.
    assert "양재천" not in {c.params.location_name for c in courses}
    assert all(c.rfs["components"]["lighting"] >= .4 for c in courses)
    assert "야간 조명이 많은" in _lead(result)
    assert "야간 조명 많음" in result.content[0].text


@pytest.mark.parametrize("widget_fails", [False, True])
def test_park_response_uses_actual_starts_not_the_requested_district(monkeypatch, widget_fails):
    monkeypatch.setattr(server, "resolve_location", lambda *a, **k: pytest.fail("district geocoded"))
    if widget_fails:
        monkeypatch.setattr(server, "_plan_widget", lambda *a: None)
    result = server.create_seoul_running_course(
        course_type="standard", location="성동구", need_facilities=["park"])
    metadata = result.structuredContent
    selection = metadata["course_selection"]
    facts = [selection["primary"], *selection["alternatives"]]
    assert selection["actual_start_names"] == [c["start"] for c in facts]
    assert {c["start"] for c in facts} == {"서울숲"}
    spoken = "\n".join(c.text for c in result.content)
    for forbidden in ("강남구", "서울선릉과정릉", "직선거리", "등록된 5곳"):
        assert forbidden not in spoken
    assert metadata["park_selection"]["requested_location"] == "성동구"
    assert metadata["park_selection"]["mode"] == "district"
    assert metadata["park_selection"]["origin_role"] == "search_reference_only"
    final = metadata["assistant_final_text"]
    for fact in facts:
        assert f"[{fact['start']}]({fact['map_url']})" in final
    assert final.endswith(server.COURSE_EDIT_NOTICE)
    assert metadata["assistant_final_text_verbatim"] is True
    assert metadata["assistant_final_text_position"] == "after_widget"
    assert metadata["assistant_final_text_is_complete"] is True
    if not widget_fails:
        payload = _card(result)
        assert payload["widget"]["children"][0]["value"] == "추천 코스"
        assert not _values(payload, "Markdown")
        assert _course_titles(payload) == [c["title"] for c in facts]
    else:
        assert result.content[0].text.endswith(server.COURSE_EDIT_NOTICE)


@pytest.mark.parametrize("eligible_count", [0, 1, 2, 3, 5])
@pytest.mark.parametrize("location", [None, "강남역"])
def test_park_catalogue_returns_up_to_three_without_filling_with_dark_routes(monkeypatch, eligible_count, location):
    from copy import deepcopy
    from runart import park_presets

    courses = deepcopy(park_presets.park_courses())
    for i, (_, course) in enumerate(courses):
        course.rfs["components"]["lighting"] = .4 if i < eligible_count else .39
    monkeypatch.setattr(server, "park_courses", lambda: courses)
    result = server.create_seoul_running_course(
        course_type="standard", need_facilities=["park"], night_mode=True, location=location)
    if not eligible_count:
        assert result.isError
        assert result.structuredContent["available_count"] == 0
        assert result.structuredContent["result_code"] == "insufficient_courses"
        assert not result.content[0].text.startswith("{")
        assert "3개" not in result.content[0].text
        return
    assert not result.isError
    count = min(eligible_count, 3)
    selection = result.structuredContent["course_selection"]
    assert selection["returned_count"] == count
    assert len(set(_urls(_card(result)))) == count
    assert set(selection["actual_start_names"]) <= {s.name for s, _ in courses[:eligible_count]}
    assert ("다른 코스도 있어요" in result.content[0].text) is (count > 1)
    assert result.content[-1].text.endswith(server.COURSE_EDIT_NOTICE)


@pytest.mark.parametrize("kwargs", [{"lat": 37.5}, {"location": "부산역"}, {"distance_km": -1}])
def test_invalid_park_input_never_silently_becomes_random_recommendations(kwargs):
    result = server.create_seoul_running_course(course_type="standard", need_facilities=["park"], **kwargs)
    assert result.isError
    assert "park_selection" not in result.structuredContent


def test_park_widget_fallback_preserves_only_matching_distance_destinations(monkeypatch):
    monkeypatch.setattr(server, "KAKAO_WIDGETS_ENABLED", False)
    result = server.create_seoul_running_course(
        course_type="standard", location="강남역", distance_km=5, need_facilities=["park"])
    assert not result.isError
    assert "다를 수" not in result.content[0].text
    selection = result.structuredContent["course_selection"]
    for course in [selection["primary"], *selection["alternatives"]]:
        assert course["course_type"] == "standard"
        assert 4.5 <= course["distance_km"] <= 5.5
        assert course["map_url"] in result.content[0].text
        assert course["start"] in result.content[0].text


@pytest.mark.parametrize("legacy", [server._legacy_generate_running_course, server._legacy_generate_animal_course])
def test_legacy_park_calls_also_return_one_three_destination_widget(legacy):
    result = legacy(need_facilities=["park"])
    assert len(_urls(_card(result))) == 3
    assert result.structuredContent["park_selection"]["mode"] == "random"


def test_drinking_water_does_not_dispatch_to_the_park_catalogue(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Drinking water is a facility request, not waterside scenery")
    monkeypatch.setattr(server, "_park_course_result", unexpected)
    result = server.create_seoul_running_course(course_type="standard", need_facilities=["water"])
    assert result.isError and result.structuredContent["result_code"] == "missing_start"
    assert "park_selection" not in result.structuredContent


def test_empty_recommendation_does_not_request_repeat_calls():
    result = server._mcp_result("코스 없음", code="course_ready", course_selection={"returned_count": 0})
    result = server._complete_recommendation(result, night_mode=True)
    assert result.structuredContent["result_code"] == "insufficient_courses"
    assert result.structuredContent["repeat_tool_call"] is False
    assert result.structuredContent["retryable"] is False
    assert result.isError
    assert "야간 최소 기준" in result.content[0].text
    assert '"widget"' not in result.content[0].text


@pytest.mark.parametrize("available_count", [1, 2, 5])
@pytest.mark.parametrize("course_type", ["standard", "dog", "best_animal"])
@pytest.mark.parametrize("night_mode", [False, True])
@pytest.mark.parametrize("widget_enabled", [False, True])
def test_partial_recommendations_survive_all_course_modes(
        monkeypatch, available_count, course_type, night_mode, widget_enabled):
    from copy import deepcopy
    from runart.park_presets import park_courses
    from runart.animal_presets import PresetMatch

    # Supply a selected plan; this is the response/widget cardinality test.
    # Candidate eligibility and no-base early exit have separate regressions.
    courses = [deepcopy(c) for _, c in park_courses()[:available_count]]
    for course in courses:
        course.params.night_mode = night_mode
        course.rfs["components"]["lighting"] = .4
    monkeypatch.setattr(server, "_course_cache", {})
    monkeypatch.setattr(server, "KAKAO_WIDGETS_ENABLED", widget_enabled)
    monkeypatch.setattr(server, "generate_running_course", lambda **kw: "")
    monkeypatch.setattr(server, "_animal_text_for_cards", lambda **kw: "")
    choices = [server.CourseChoice(c, server.encode_course_id(c.params), "standard") for c in courses[:3]]
    plan = server.CoursePlan("exact", "조건을 만족하는 코스예요.", choices[0], tuple(choices[1:]))
    monkeypatch.setattr(server, "_animal_course_plan", lambda *a: plan)
    result = server.create_seoul_running_course(
        course_type=course_type, location="강남역", night_mode=night_mode)

    assert not result.isError
    selection = result.structuredContent["course_selection"]
    count = min(available_count, 3)
    assert selection["returned_count"] == count
    assert len(selection["alternatives"]) == count - 1
    assert ("다른 코스도 있어요" in result.content[0].text) is (count > 1)
    assert result.content[-1].text.endswith(server.COURSE_EDIT_NOTICE)
    if widget_enabled:
        assert len(set(_urls(_card(result)))) == count
        if night_mode:
            assert result.content[0].text.count("야간 조명 많음") >= count
    else:
        assert result.content[0].text.endswith(server.COURSE_EDIT_NOTICE)
    if count < 3:
        assert "3개" not in result.content[-1].text


def test_night_preferences_are_never_relaxed_to_daytime(monkeypatch):
    from runart.models import CourseParams
    from runart.course import CourseError

    calls = []
    def unavailable(params, **kwargs):
        calls.append(params)
        raise CourseError("조명 정보 부족")
    monkeypatch.setattr(server, "_get_course", unavailable)
    probe = CourseParams(lat=37.4986, lon=127.0281, night_mode=True, need_facilities=["park"])
    assert server._plain_course_here(probe, 5, 2) is None
    assert len(calls) == 1
    assert all(p.night_mode for p in calls)


@pytest.mark.parametrize("lighting", [None, .3, .32, .33, .39, .4, .8])
def test_night_request_returns_three_lit_routes_or_no_recommendation(monkeypatch, lighting):
    from runart import course as course_module
    from runart.courseplan import route_signature

    original_summary = course_module.route_rfs_summary

    def measured_summary(*args, **kwargs):
        summary = original_summary(*args, **kwargs)
        summary["components"].pop("lighting", None)
        if lighting is not None:
            summary["components"]["lighting"] = lighting
        return summary

    # Controlled measurements exercise the real router and MCP boundary;
    # they must never enter the shared cache used by real-data tests.
    monkeypatch.setattr(server, "_course_cache", {})
    monkeypatch.setattr(server, "_get_pool", lambda: None)
    monkeypatch.setattr(course_module, "route_rfs_summary", measured_summary)
    result = server.create_seoul_running_course(
        course_type="standard", location="강남역", night_mode=True)

    if lighting is None or lighting < .4:
        assert result.isError
        assert "조명" in result.content[0].text
        assert '"widget"' not in result.content[0].text
        return

    courses = [server._cached_course(url.rsplit("/", 1)[1]) for url in _urls(_card(result))]
    assert len(courses) == 3
    assert len({route_signature(c) for c in courses}) == 3
    assert all(c.params.night_mode and c.rfs["components"]["lighting"] == lighting for c in courses)
    assert result.structuredContent["course_selection"]["returned_count"] == 3


def test_night_refinement_rejects_cached_course_with_insufficient_lighting(monkeypatch):
    from runart.course import generate_course
    from runart.models import CourseParams, encode_course_id

    course = generate_course(CourseParams(lat=37.4986, lon=127.0281))
    course.rfs["components"]["lighting"] = .3
    monkeypatch.setattr(server, "_get_course", lambda *args, **kwargs: course)
    text = server.refine_course(encode_course_id(course.params), night_mode=True)
    assert text.startswith("⚠️")
    assert "야간 코스로 추천할 수 없어요" in text
    assert "/c/" not in text


# ---------------------------------------------------------------------------
# A refusal answers in the unit the runner used.
# ---------------------------------------------------------------------------

def test_a_time_request_with_no_matching_course_is_refused_in_minutes(monkeypatch):
    """Someone who said "60분" never mentioned kilometres.

    Telling them "목표 9.2km에 맞는 코스를 찾지 못했어요" answers a question
    they did not ask, with a number they never gave, and leaves them to work
    out which duration to try instead.
    """
    from runart.course import DistanceMissError

    # Verify unit conversion against a fixed router outcome. The bounded
    # search may find a different nearest loop depending on machine load.
    def no_matching_course(params, **kwargs):
        raise DistanceMissError(params.distance_km, 11.2)

    monkeypatch.setattr(server, "_get_course", no_matching_course)
    text = server.generate_running_course(
        location="여의도한강공원", duration_min=60)

    assert text.startswith("⚠️"), text
    assert "60분" in text
    assert "9.2km" not in text
    # The nearest loop restated as time, so the next ask is obvious.
    assert "73분" in text


def test_a_distance_request_is_still_refused_in_kilometres():
    text = server.generate_running_course(location="잠실", distance_km=3)

    assert text.startswith("⚠️"), text
    assert "3km" in text
    assert "4.1km" in text
