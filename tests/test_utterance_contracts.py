"""Contracts every reply owes the runner, checked across real utterances.

These are the shapes that broke in Preview rather than in a unit test: a
reply that ends with nothing to act on, an option referred to by number that
was never listed, an escaped or truncated sentence, a course announced for a
start the runner never named. Each case here is a request a runner actually
typed during the Preview sweep, reduced to the arguments the host sent.
"""

import re

import pytest

from runart import server

# (label, arguments) — the host's own reading of a real Korean utterance.
UTTERANCES = [
    ("강남역에서 5km", dict(course_type="standard", location="강남역", distance_km=5)),
    ("여의도에서 뛸 데", dict(course_type="standard", location="여의도")),
    ("남산 5km", dict(course_type="standard", location="남산", distance_km=5)),
    ("어린이대공원 5km", dict(course_type="standard", location="어린이대공원", distance_km=5)),
    ("망원한강공원 10km", dict(course_type="standard", location="망원한강공원", distance_km=10)),
    ("선유도공원 4km", dict(course_type="standard", location="선유도공원", distance_km=4)),
    ("한강 따라 뛰고 싶어", dict(course_type="standard", need_facilities=["park"])),
    ("왕십리 화장실 있는 코스", dict(course_type="standard", location="왕십리역",
                            need_facilities=["restroom"])),
    ("강남구에서 5km", dict(course_type="standard", location="강남구", distance_km=5)),
    ("잠실역 강아지 모양", dict(course_type="dog", location="잠실역")),
    ("정확히 4km 강남역", dict(course_type="standard", location="강남역",
                          distance_km=4, strict_distance=True)),
    ("제주도 5km", dict(course_type="standard", location="제주도", distance_km=5)),
]

# Backslash escapes belong to markdown_text() and untrusted labels. Reaching
# the runner means a whole sentence went through it: "찾지 못했어요\.".
ESCAPE = re.compile(r"\\[.\-*_()\[\]!|#+]")
NUMBERED = re.compile(r"(?:^|\n)\s*(\d)\.\s+\S")


def _replies():
    for label, arguments in UTTERANCES:
        result = server.create_seoul_running_course(**arguments)
        yield label, arguments, result, (result.structuredContent or {})


@pytest.fixture(scope="module")
def replies():
    return list(_replies())


def test_every_reply_says_something_the_runner_can_read(replies):
    for label, _, _, structured in replies:
        spoken = structured.get("assistant_final_text")
        assert spoken and spoken.strip(), f"{label}: no closing prose"
        assert not ESCAPE.search(spoken), f"{label}: escaped punctuation in {spoken[:60]}"


def test_a_numbered_option_the_reply_mentions_is_actually_offered(replies):
    """Preview said "1번으로 진행할 수 있어요" with no 1 anywhere on screen."""
    for label, _, _, structured in replies:
        spoken = structured.get("assistant_final_text") or ""
        mentioned = {int(m.group(1)) for m in NUMBERED.finditer(spoken)}
        if not mentioned:
            continue
        offered = {option["choice"] for option in
                   (structured.get("confirmation_options")
                    or structured.get("relaxation_options") or [])}
        assert mentioned <= offered, (
            f"{label}: text lists {sorted(mentioned)} but options are {sorted(offered)}")


def test_a_delivered_course_names_the_start_it_actually_used(replies):
    for label, _, _, structured in replies:
        if structured.get("result_code") not in {"course_ready", "nearby_course_ready"}:
            continue
        selection = structured.get("course_selection") or {}
        assert selection.get("actual_start_names"), f"{label}: course with no start named"


def test_a_start_outside_seoul_is_refused_rather_than_answered(replies):
    for label, arguments, _, structured in replies:
        if arguments.get("location") != "제주도":
            continue
        assert structured["result_code"] == "location_not_found", label
        assert "서울 밖" in structured["assistant_final_text"], label


@pytest.mark.parametrize("landmark", [
    "여의도", "남산", "남산타워", "어린이대공원", "망원한강공원", "안양천",
    "서울숲", "여의도한강공원", "반포한강공원", "올림픽공원",
])
def test_a_bundled_landmark_can_start_the_default_course(landmark):
    """Ten of these built nothing at any distance and served the stored refusal."""
    result = server.create_seoul_running_course(
        course_type="standard", location=landmark, distance_km=5)
    structured = result.structuredContent or {}
    assert structured["result_code"] in {"course_ready", "nearby_course_ready"}, (
        f"{landmark}: {structured.get('assistant_final_text', '')[:80]}")
