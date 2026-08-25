"""The three-case answer matrix for a named animal request, at the MCP boundary.

A chatbot answer is only as good as what the runner can act on, so every one
of the three cases must come back as a widget with the same three choices:
the animal they asked for, another animal, and a plain course — reordered by
which one is actually available where they asked.
"""

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
    assert result.structuredContent["assistant_text_position"] == "before_widget"
    assert result.structuredContent["assistant_text_verbatim"] is True
    return lead


ANIMAL_RUNS = ("댕댕런", "야옹런", "깡총런", "고래런")
# The card opens with a "추천 코스" heading; course names start after it.
CARD_HEADING = "추천 코스"


def _course_titles(payload) -> list[str]:
    return [value for value in _values(payload, "Title") if value != CARD_HEADING]


def test_case1_exact_course_here_also_offers_another_animal_and_a_plain_course():
    result = server.create_seoul_running_course(course_type="dog", location="강남역")
    payload = _card(result)
    labels = _labels(payload)
    titles = _course_titles(payload)

    assert result.structuredContent["result_code"] == "course_ready"
    assert result.isError is False
    assert labels[0] == "코스 보기"
    assert len(labels) == 3
    assert "댕댕런" in json.dumps(payload, ensure_ascii=False)
    # (b) a different animal, (c) a plain course — both at the requested start.
    assert any(run in titles[1] for run in ANIMAL_RUNS if run != "댕댕런")
    assert not any(run in titles[2] for run in ANIMAL_RUNS)
    assert len(set(_urls(payload)[1:])) == 2


def test_case2_nearby_course_names_the_real_start_and_still_offers_three():
    result = server.create_seoul_running_course(
        course_type="whale", location="서울대입구역")
    payload = _card(result)
    lead = _lead(result)

    assert result.structuredContent["result_code"] == "nearby_course_ready"
    assert result.isError is False
    assert len(_labels(payload)) == 3
    # The model must not be able to claim the requested start as the departure.
    assert "서울대입구역" in lead
    start_text = next(
        value for value in _values(payload, "Caption") if " 출발" in value
    )
    actual_start = start_text.split(" 출발")[0]
    assert actual_start != "서울대입구역"
    assert actual_start in lead
    assert "고래" in lead
    assert actual_start in json.dumps(payload, ensure_ascii=False)


def test_case3_no_animal_within_two_km_leads_with_the_plain_course_here():
    result = server.create_seoul_running_course(
        course_type="dog", location="도봉산역")
    payload = _card(result)
    lead = _lead(result)
    labels = _labels(payload)
    titles = _course_titles(payload)

    assert "도봉산역" in lead
    assert "1~2km" in lead
    assert "강아지" in lead
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

    assert result.structuredContent["result_code"] == "nearby_course_ready"
    assert 2 <= len(_labels(payload)) <= 3
    assert "고래런" in json.dumps(payload, ensure_ascii=False)


def test_widget_failure_still_returns_the_markdown_answer(monkeypatch):
    monkeypatch.setattr(server, "KAKAO_WIDGETS_ENABLED", False)
    result = server.create_seoul_running_course(
        course_type="whale", location="서울대입구역")

    assert result.structuredContent["result_code"] == "nearby_course_ready"
    assert result.content[0].text.startswith("🔎")
