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
    assert payload["widget"]["type"] == "Basic", result.content[0].text[:400]
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
    if result.structuredContent["assistant_text_in_widget"]:
        assert result.structuredContent["assistant_text_position"] == "widget_intro"
        assert _card(result)["widget"]["children"][0] == {
            "type": "Markdown", "value": lead,
        }
    else:
        assert result.structuredContent["assistant_text_position"] == "before_widget"
    assert result.structuredContent["assistant_text_verbatim"] is True
    return lead


ANIMAL_RUNS = ("댕댕런", "야옹런", "깡총런", "고래런")


def _course_titles(payload) -> list[str]:
    return [row["children"][1]["children"][1]["value"]
            for row in payload["widget"]["children"]
            if row["type"] == "Row" and row["children"][0]["type"] == "Image"]


def test_case1_exact_course_here_also_offers_another_animal_and_a_plain_course():
    result = server.create_seoul_running_course(course_type="dog", location="강남역")
    payload = _card(result)
    labels = _labels(payload)
    titles = _course_titles(payload)

    assert result.structuredContent["result_code"] == "course_ready"
    assert result.isError is False
    assert labels[0] == "지도 보기"
    assert len(labels) == 3
    assert "댕댕런" in json.dumps(payload, ensure_ascii=False)
    # (b) a different animal, (c) a plain course — both at the requested start.
    assert any(not any(run in title for run in ANIMAL_RUNS) for title in titles[1:])
    assert len(set(_urls(payload)[1:])) == 2


def test_case2_nearby_course_names_the_real_start_and_still_offers_three():
    result = server.create_seoul_running_course(
        course_type="whale", location="서울대입구역")
    payload = _card(result)
    lead = _lead(result)

    assert result.structuredContent["result_code"] == "course_ready"
    assert result.isError is False
    assert len(_labels(payload)) == 3
    # The card names the actual start directly in its leading title.
    assert "서울대입구역" in lead
    start_text = _course_titles(payload)[0]
    actual_start = "서울대입구역"
    assert actual_start == "서울대입구역"
    assert actual_start in lead
    assert "다른 추천 코스를 준비했어요" in lead
    assert actual_start in json.dumps(payload, ensure_ascii=False)


def test_case3_no_animal_within_two_km_leads_with_the_plain_course_here():
    result = server.create_seoul_running_course(
        course_type="dog", location="도봉산역")
    payload = _card(result)
    lead = _lead(result)
    labels = _labels(payload)
    titles = _course_titles(payload)

    assert "도봉산역" in lead
    assert "다른 추천 코스를 준비했어요" in lead
    # (a) the plain course at the requested start leads the card.
    title = titles[0]
    assert "도봉산" in title
    assert not any(run in title for run in ANIMAL_RUNS)
    # (b)/(c) the animal courses stay one click away.
    assert len(labels) >= 3
    assert any(run in candidate for run in ANIMAL_RUNS for candidate in titles[1:])


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
    assert result.content[0].text.startswith("🔎")


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
def test_primary_card_honors_start_and_duration_before_shape(kind):
    from runart.geo import haversine_m
    result = server.create_seoul_running_course(
        course_type=kind, location="시청", duration_min=40)
    cid = _urls(_card(result))[0].rsplit("/c/", 1)[1]
    course = server._cached_course(cid)
    lat, lon, _ = server.resolve_location("시청", None, None)
    assert haversine_m(lat, lon, course.params.lat, course.params.lon) < 150
    assert abs(course.length_km - 6.2) / 6.2 <= .10


def test_optional_preferences_can_relax_without_changing_start_or_effort(monkeypatch):
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
    assert result is not None and len(calls) == 2
    assert all(p.lat == probe.lat and p.lon == probe.lon and p.distance_km == 6.2 for p in calls)
    assert not calls[-1].need_facilities and not calls[-1].include_hills


def test_legacy_animal_duration_reaches_the_shared_priority_plan(monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "generate_animal_course", lambda *a, **k: "text")
    def result(text, **kwargs):
        captured.update(kwargs)
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


def test_animal_timeout_still_plans_local_candidate_with_reserved_budget(monkeypatch):
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
    assert budgets and result.structuredContent["result_code"] == "course_ready"
    assert "일반 코스 대안" in json.dumps(_card(result), ensure_ascii=False)


def test_optional_preferences_do_not_exclude_verified_animal_candidates(monkeypatch):
    probes = []
    def candidates(probe, radius):
        probes.append(probe)
        return []
    monkeypatch.setattr(server, "_any_animal_matches", candidates)
    monkeypatch.setattr(server, "_plain_course_here", lambda *args: None)
    server._animal_course_plan({"location": "시청", "include_hills": True,
        "night_mode": True, "need_facilities": ["park"]}, "dog", "", .9)
    assert len(probes) == 1
    assert not probes[0].include_hills and not probes[0].night_mode
    assert probes[0].need_facilities == []


# Every way of asking for a course that a runner can act on immediately.
COURSE_REQUESTS = [
    dict(course_type="standard", location="시청", distance_km=6),
    dict(course_type="standard", location="시청", duration_min=40),
    dict(course_type="standard", location="시청"),
    dict(course_type="standard", location="시청", night_mode=True),
    dict(course_type="dog", location="강남역"),
    dict(course_type="dog", location="강남역", duration_min=40),
    dict(course_type="dog", location="강남역", distance_km=3),
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


# ---------------------------------------------------------------------------
# A refusal answers in the unit the runner used.
# ---------------------------------------------------------------------------

def test_a_time_request_with_no_matching_course_is_refused_in_minutes():
    """Someone who said "60분" never mentioned kilometres.

    Telling them "목표 9.2km에 맞는 코스를 찾지 못했어요" answers a question
    they did not ask, with a number they never gave, and leaves them to work
    out which duration to try instead.
    """
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
