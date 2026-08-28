# 실제 출발지·대안 추천 안내 TDD 검증

2026-08-29 · 기준 커밋 `655b7c3`

## 사용자 여정과 변경 범위

사용자가 “왕십리역에서 강아지 코스 그려줘”라고 요청했으나 카드에는 신당역·청구역 코스가 표시되고, 카드 밖 설명은 왕십리역 출발이라고 주장한 사례에서 출발했다.

- 출발지가 바뀐 추천이면 위젯과 마지막 설명에서 실제 출발지와 대안임을 함께 확인한다.
- 원래 출발지 코스와 이동이 필요한 대안이 섞이면 두 종류를 구분한다.
- 위젯 비활성화·크기 초과 시에도 텍스트 안내를 유지한다.
- 코스 선정·경로·course_id·150m 역 출구 허용 범위는 변경하지 않는다.

`CoursePlan`에 비교 기준 출발지를 보존하고, 선택된 코스의 실측 출발지 차이로 하나의 안내 문장을 만든다. 이 문장을 위젯 상단 Text, 공유 문구, 최종 설명의 첫 문장, Markdown 폴백에서 재사용한다. 구/공원 목록처럼 특정 출발점이 없는 계획에는 출발지 변경을 추정하지 않는다.

## RED → GREEN

- RED 체크포인트: `04a9bd0` — `tests/test_course_start_disclosure.py`를 실행해 **12 failed (6.68s)** 확인. 출발지 대안 안내와 관련 응답 필드 부재가 원인이다.
- GREEN 체크포인트: `cf9b5a5` — 같은 테스트 **12 passed (6.31s)**.
- 첫 전체 회귀에서 기존 위젯 실패 mock 2개의 새 키워드 인자 미지원과 FastMCP의 일회용 lifespan 재사용 문제가 발견됐다. mock이 키워드 인자를 받고 HTTP 테스트가 자신의 session manager를 사용하도록 테스트 격리를 보완했다.
- 보완 후 `.venv/bin/pytest -q --tb=short` → **707 passed (32.39s)**.
- 커밋 대상 Git 인덱스를 임시 디렉터리에 복원하고 해당 `src`를 `PYTHONPATH`로 지정한 독립 검증도 **707 passed (32.63s)**. 제외한 로컬 실험 변경 없이 통과했다.
- `compileall`, `git diff --check` 통과.

## 보장하는 동작

| 보장 | 검증 | 결과 |
| --- | --- | --- |
| 인근 대안의 실제 출발지 안내가 위젯·공유·최종 설명에 동일하게 포함됨 | `test_nearby_notice_is_visible_in_widget_final_text_and_share_copy` | PASS |
| 원래 출발지 코스가 함께 있으면 그 존재를 부정하지 않음 | `test_exact_primary_with_nearby_alternatives_does_not_deny_the_exact_start` | PASS |
| 0m·149.9m·150m 경계 및 같은 역 이름의 다른 출발 위치 구분 | 허용 범위 파라미터 테스트와 같은 역 이름 테스트 | PASS |
| 위젯 비활성화·크기 초과에도 동일 안내 유지 | `test_markdown_fallback_keeps_the_same_start_disclosure` | PASS |
| 긴 장소명·마크업을 정제하고 안내에 줄 수 제한을 두지 않음; 위젯 12,000바이트 미만 | `test_start_disclosure_is_bounded_plain_text_without_line_clamping` | PASS |
| 실제 왕십리역 강아지 요청이 통합·레거시 툴 모두에서 안내를 포함함 | `test_wangsimni_dog_reproduction_discloses_actual_starts` | PASS |
| MCP HTTP 직렬화 후에도 위젯과 일반 TextContent에 동일 안내가 있음 | `test_mcp_http_preserves_disclosure_in_both_text_and_widget` | PASS |

## 커버리지와 한계

별도 커버리지 의존성을 추가하지 않고 Python 표준 `trace.Trace(count=True, trace=False)`로 새 테스트 중 순수 응답 테스트 9개를 실행했다(`-k 'not wangsimni and not mcp_http'`). 기준 커밋 대비 변경된 실행 가능 행을 `trace._find_executable_linenos()`와 대조했다.

- `courseplan.py`: 3/3
- `server.py`: 35/35
- `widget.py`: 9/9
- 합계 **변경 실행 행 47/47, 100%**. 전체 저장소 커버리지나 분기 커버리지 수치는 아니다. 제외된 실데이터·HTTP 테스트 3개도 위 전체 테스트에서는 모두 실행·통과했다.

Kakao Preview에서의 실제 렌더링과 카드 밖 LLM 문장 재작성은 아직 검증하지 않았다. 자체 필드인 `assistant_final_text_verbatim`은 카카오의 강제 출력 옵션이 아니므로, 사용자에게 반드시 보여야 할 안내를 위젯 안에도 넣었다. KC 배포·PlayMCP 설정 변경은 수행하지 않는다.

기존 별도 작업인 `course.py`, `models.py`, `rfs.py`, 미연결 `discovery.py` 및 관련 없는 문서는 이번 커밋에서 제외한다.
