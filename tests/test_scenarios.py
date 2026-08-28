"""Scenario acceptance tests — PRD §6 personas P1~P4 expressed as tool calls.

Each case asserts the '3-step completion' contract: the first response must
already contain a runnable course (stats + map + GPX) or actionable guidance.
"""

import re

import pytest

from runart import server

# A runnable course always exposes both the detail page and GPX. Presentation
# metrics may differ between the compact Kakao widget and Markdown fallback.
RESULT_MARKERS = ("/c/", ".gpx")


def _is_course(out: str) -> bool:
    return all(m in out for m in RESULT_MARKERS)


# P1 입문자: 시간 기반, 평지, 화장실
def test_p1_beginner_duration_flat_restroom():
    out = server.generate_running_course(
        location="여의도한강공원", duration_min=30, include_hills=False,
        need_facilities=["restroom"])
    assert _is_course(out)
    assert "6:30/km" in out  # conversion explained


# P2 기록 지향: 10km 오르막 훈련
def test_p2_trainer_hills():
    out = server.generate_running_course(location="남산", distance_km=6,
                                         include_hills=True)
    assert _is_course(out) or out.startswith("⚠️")
    assert "Traceback" not in out


# P3 재미/SNS: 동물 모양 + 공유
def test_p3_gps_art_share():
    out = server.generate_animal_course(shape="whale", location="강남역")
    assert _is_course(out) or out.startswith("⏱️")
    if _is_course(out):
        # Sharing travels via the course detail page's share button.
        assert "/c/" in out
    else:
        assert "3초 안에" in out and "한 번 더" in out
    assert "모양 완성도" not in out


def test_failed_shape_does_not_poison_another_animal_or_standard_course():
    rabbit = server.create_seoul_running_course(
        course_type="rabbit", location="경복궁역", allow_nearby_start=True)
    dog = server.create_seoul_running_course(
        course_type="dog", location="경복궁역", allow_nearby_start=True)
    standard = server.create_seoul_running_course(
        course_type="standard", location="성신여대역")
    rabbit_text = rabbit.content[0].text
    dog_text = dog.content[0].text
    standard_text = standard.content[0].text
    rabbit_guidance = rabbit.structuredContent["assistant_text"]
    standard_guidance = standard.structuredContent.get("assistant_text", "")
    # Each named animal stays that animal, even when its real start moves.
    assert "토끼" in rabbit_guidance and "일반 코스만 가능" not in rabbit_guidance
    assert '"widget"' in rabbit_text and "/c/" in rabbit_text
    assert "깡총런" in rabbit_text
    assert rabbit.structuredContent["result_code"] == "nearby_course_ready"
    assert '"widget"' in dog_text and "/c/" in dog_text and "댕댕런" in dog_text
    if standard.structuredContent["result_code"] == "insufficient_courses":
        assert standard.structuredContent["available_count"] == 0
        assert "코스를 찾지 못했어요" in standard_text
    else:
        assert '"widget"' in standard_text and "/c/" in standard_text
        assert "기본 5km" in standard_guidance


# P4 야간 러너: 야간 안전 모드
def test_p4_night_runner():
    out = server.generate_running_course(location="홍대", distance_km=5,
                                         night_mode=True)
    if _is_course(out):
        cid = server._extract_single_course_id(out)
        course = server._cached_course(cid)
        from runart.rfs import has_sufficient_night_lighting
        assert has_sufficient_night_lighting(course.rfs)
    else:
        assert out.startswith("⚠️") and "가로등" in out


# 모호/무리 입력 — 거절이 아니라 안내여야 한다
@pytest.mark.parametrize("kwargs,expect", [
    (dict(location=None), "출발 위치"),                      # 위치 없음
    (dict(location="아무데나요"), "구 이름만"),               # 범위는 지오코딩하지 않음
    (dict(location="시청", shape="dragon"), "추후 업데이트 예정"),  # 미지원 모양
])
def test_failures_guide_next_action(kwargs, expect):
    if "shape" in kwargs:
        out = server.generate_animal_course(**kwargs)
    else:
        out = server.generate_running_course(**kwargs)
    assert out.startswith("⚠️")
    assert expect in out
    assert "Traceback" not in out


# 거리 정확도 계약: 실거리 표기가 목표 ±10% 이내 (실그래프 ±5% 목표)
def test_distance_accuracy_stated_honestly():
    out = server.generate_running_course(location="여의도한강공원", distance_km=5)
    # Read the explicit distance line, not the title: the course name is a
    # product decision ("5.1km 한강공원런") and should be free to change.
    got = float(re.search(r"거리 \*\*([\d.]+)km\*\*", out).group(1))
    assert abs(got - 5.0) / 5.0 <= 0.10
