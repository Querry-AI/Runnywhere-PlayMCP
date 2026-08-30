"""What a start place may be, and what the tools say it may be.

All four forms have resolved for a while, but nothing told an assistant so:
the schema described "a Seoul place" and the failure message named only
stations and addresses, so a shop name was never tried.
"""

import pytest

from runart import server
from runart.course import CourseError
from runart.geocode import resolve_location


def test_every_tool_names_the_four_forms_a_start_place_can_take():
    for text in (server.LOCATION_FIELD_KO, server.LOCATION_FIELD_EN):
        assert text
    for keyword in ("지하철역", "상호명", "도로명", "지번"):
        assert keyword in server.LOCATION_FIELD_KO
    for keyword in ("station", "shop", "road-name", "lot-number"):
        assert keyword in server.LOCATION_FIELD_EN
    # And both forbid quietly swapping a shop name for the nearest station.
    assert "그대로 넘기세요" in server.LOCATION_FIELD_KO
    assert "verbatim" in server.LOCATION_FIELD_EN


def test_the_location_description_is_shared_by_every_tool_that_takes_one():
    """Three copies of this text drifted apart once already."""
    import inspect

    sources = inspect.getsource(server)
    assert sources.count('Field(description=LOCATION_FIELD_KO)') >= 1
    assert sources.count('LOCATION_FIELD_EN') >= 3   # incl. refine_course
    # No tool may reintroduce a hand-written variant.
    assert "Exact Seoul start place stated by the user. Never infer" not in sources


def test_a_failed_lookup_tells_the_runner_a_shop_name_would_work():
    with pytest.raises(CourseError) as caught:
        resolve_location("존재하지않는장소이름12345", None, None, timeout_s=0.2)
    message = str(caught.value)

    for hint in ("상호명", "도로명", "지번"):
        assert hint in message
    assert "스타벅스" in message      # a concrete shop-name example


def test_a_missing_location_asks_for_any_of_the_forms():
    with pytest.raises(CourseError) as caught:
        resolve_location(None, None, None)
    assert "상호명" in str(caught.value)


@pytest.mark.parametrize("station", ["강남역", "시청", "서울숲"])
def test_offline_places_still_resolve_without_the_network(station):
    lat, lon, name = resolve_location(station, None, None, timeout_s=0.2)
    assert 37.4 <= lat <= 37.72 and 126.76 <= lon <= 127.19
    assert name


def test_a_seoul_hit_that_answers_another_regions_query_is_refused():
    """The rect confines results to Seoul, so a Jeju ask came back as Gimpo."""
    from runart.geocode import _names_the_same_place

    assert not _names_the_same_place("제주공항", "김포국제공항 국내선")
    assert not _names_the_same_place("제주도", "제주갈비 강남점")
    assert not _names_the_same_place("제주 함덕해수욕장", "한강공원 뚝섬")

    assert _names_the_same_place("김포공항", "김포국제공항 국내선")
    assert _names_the_same_place("스타벅스 서울숲점", "스타벅스 서울숲점")
    assert _names_the_same_place("상암동 1601", "서울 마포구 상암동 1601")
    # A bare place type identifies nothing, so the hit stands as before.
    assert _names_the_same_place("공원", "북서울꿈의숲")


def test_a_start_outside_seoul_is_refused_before_the_seoul_bounded_lookup():
    """A rect-limited search answers with a Seoul business, never "no match"."""
    import pytest

    from runart.course import CourseError
    from runart.geocode import resolve_location

    for query in ("제주공항", "인천공항", "대구", "해운대", "제주도", "경기도"):
        with pytest.raises(CourseError, match="서울 밖"):
            resolve_location(query, None, None)

    # Seoul places resolve offline, before the gate, so they are unaffected --
    # 김포공항 carries a Gyeonggi city name and still reaches its own station.
    assert resolve_location("김포공항", None, None)[2] == "김포공항역"
    assert resolve_location("강남역", None, None)[2] == "강남역"
    assert resolve_location("여의도한강공원", None, None)[2] == "여의도한강공원"


def test_every_resolve_failure_keeps_its_own_message():
    """The ⚠️ classifier matches phrases, so a new one can fall through it."""
    from runart import server
    from runart.course import CourseError
    from runart.geocode import resolve_location

    for query in ("제주공항", "대구", "해운대", "없는가게이름12345"):
        try:
            resolve_location(query, None, None)
        except CourseError as exc:
            reason = str(exc)
        else:  # pragma: no cover - none of these is a Seoul start
            raise AssertionError(f"{query} unexpectedly resolved")

        result = server.create_seoul_running_course(
            course_type="standard", location=query, distance_km=5)
        structured = result.structuredContent
        assert structured["result_code"] == "location_not_found", query
        # The runner is told why, not handed generic shortage copy.
        assert reason[:12] in structured["assistant_final_text"], query
