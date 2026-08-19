# Kakao 코스 위젯 TDD 증빙

작성일: 2026-08-19

## 입력 계획

- `design-plans/2026-08-18-kakao-tools-widget-implementation-plan.md`
- Kakao Tools 개발가이드의 `Card` / `Text` / `Button`, `copy_text`, `status` 금지 계약

## 사용자 여정

1. 사용자가 새 일반·동물 코스를 요청하면 코스를 한 번만 생성하고 첫 MCP 응답에서 바로 카드로 본다.
2. 선택지·대안·오류이거나 카드 생성이 안전하지 않으면 기존 한국어 Markdown 안내를 그대로 받는다.
3. 상세페이지에서 경로를 수동 저장하면 원본은 유지되고 수정본은 새 `course_id`로 즉시 사용할 수 있다.
4. 운영자는 `RUNART_KAKAO_WIDGETS=0`으로 생성 기능을 건드리지 않고 카드만 비활성화할 수 있다.

## RED → GREEN 기록

| 단계 | 실행 | 결과 | 보장 |
|---|---|---|---|
| RED | `.venv/bin/python -m pytest -q tests/test_widget.py tests/test_course_edit.py` | `ModuleNotFoundError: runart.widget` | 구현 전에 새 PDF 계약 테스트가 실제 실행되고 의도한 누락으로 실패함 |
| GREEN | `.venv/bin/python -m pytest -q tests/test_widget.py tests/test_course_edit.py` | `39 passed` | 순수 빌더 계약, 폴백, 편집본 캐시가 통과함 |
| 경계 회귀 | `.venv/bin/python -m pytest -q tests/test_tools.py tests/test_scenarios.py tests/test_widget.py tests/test_course_edit.py` | `89 passed` | 기존 생성·실패 시나리오와 위젯 경계가 함께 통과함 |
| 전체 회귀 | `.venv/bin/python -m pytest -q` | `172 passed in 13.12s` | 전체 저장소 회귀 테스트가 통과함 |

RED 체크포인트는 commit `d84e20c`이며, GREEN 체크포인트는 이 문서를 포함하는 구현 commit이다.

## 계약별 증빙

| # | 보장 | 테스트/검증 | 종류 | 결과 |
|---|---|---|---|---|
| 1 | 최상위 `{widget, copy_text, name}`이며 `widget.type`은 `Card` | `tests/test_widget.py::test_course_widget_matches_kakao_card_contract_and_is_deterministic` | 단위 | PASS |
| 2 | 어느 깊이에도 `status`가 없고 버튼 URL 경로가 PDF와 일치함 | 같은 테스트 | 단위 | PASS |
| 3 | 한글 compact JSON, 제어문자 제한, 12KB 내부 상한을 지킴 | `tests/test_widget.py::test_course_widget_bounds_untrusted_place_copy_and_rejects_bad_urls` | 단위 | PASS |
| 4 | 새 코스는 한 번 생성된 뒤 같은 첫 응답에서 위젯이 됨 | `tests/test_widget.py::test_new_course_is_cached_and_widgeted_in_its_first_tool_response` | 통합 | PASS |
| 4-1 | `dog`, `cat`, `rabbit`, `whale`의 단일 확정 코스가 모두 카드 대상임 | `tests/test_widget.py::test_each_confirmed_animal_course_type_is_widget_eligible` | 통합 | PASS |
| 5 | 캐시 미스·기능 비활성화·비대상·오류는 Markdown으로 복귀함 | `tests/test_widget.py::test_mcp_widget_falls_back_to_original_markdown` | 통합 | PASS |
| 6 | 빌더 예외가 MCP 오류로 새지 않고 기존 `result_code`를 보존함 | `tests/test_widget.py::test_mcp_widget_build_error_preserves_markdown_and_result_code` | 통합 | PASS |
| 7 | 수동 저장은 새 ID를 캐시하고 원본 캐시를 덮어쓰지 않음 | `tests/test_course_edit.py::test_edit_endpoint_caches_new_version_without_overwriting_original` | 통합 | PASS |
| 8 | raw MCP 응답에서 카드 JSON은 `result.content[0].text`에 위치함 | 로컬 `tools/call` | E2E 경계 | PASS |

## 성능·크기

- 로컬 raw `tools/call` 전체 JSON-RPC 응답: 1,875바이트
- 대표 위젯 문자열: 1,655바이트
- 빌더 1,000회: p50 0.0332ms, p99 0.069ms, max 0.2332ms
- `tools/list`: 기존 7개 도구와 description 길이 유지

## 남은 검증

- 개발 환경에 `coverage` 모듈이 없어 수치형 코드 커버리지는 측정하지 못했다. 위 표의 신규 분기와 전체 회귀는 모두 실행했다.
- Kakao Tools Preview의 실제 카드 렌더링·버튼·공유 결과는 로그인된 Preview에서 확인해야 한다.
- KC 배포본 평균·p99·wire size는 배포 승인 후 별도로 측정해야 한다.
- 이번 구현은 배포하지 않았다.
