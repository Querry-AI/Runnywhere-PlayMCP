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
