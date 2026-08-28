# 출발지 대안 선택 후 위젯 제공

2026-08-29. 사용자 요청에서 도출한 여정이며 별도 계획 문서 변경은 없다.

## 계약

요청한 출발지의 동물 코스가 없고 인근 대안만 있으면 먼저 텍스트로 묻는다.
선택 1은 원래 모양의 가까운 출발지, 선택 2는 원래 출발지의 일반 러닝 코스다.
선택 전에는 위젯·지도 URL·course_id·course_selection을 반환하지 않는다.
선택 후 통합 툴에 원래 조건과 선택을 전달한다. 가까운 출발지 허용은 기본 false다.
원래 출발지 코스가 있으면 해당 카드만 반환하며 인근 카드로 빈자리를 채우지 않는다.
구/공원 목적지 카탈로그, 기존 150m 미만 역 출구 허용 범위는 그대로다.

## TDD 증거

- RED `cb229e0`: `.venv/bin/pytest -q tests/test_course_confirmation.py` → 4 failed, 2 passed. 질문 대신 위젯 반환, 선택 인자 부재, 미동의 인근 카드 노출을 재현했다.
- GREEN `0176c72`: 동일 명령 → 6 passed.
- 이후 실제 데이터·HTTP 두 턴 테스트, 좌표·조건 보존·기본값·부족한 후보 테스트를 추가했다.
- 기존 즉시 대안 제공 테스트는 명시적 동의 인자를 갖는 선택 후 검증으로 전환했다. 정확한 출발지 카드 수는 미동의 인근 카드를 제외하도록 변경했다.
- 전체 `.venv/bin/pytest -q` → 718 passed (36.12s).
- 스테이징된 파일을 `git checkout-index`로 새 임시 디렉터리에 복원하고 해당 src를 PYTHONPATH로 지정한 독립 테스트 → 718 passed (38.10s). 제외한 로컬 변경 없이 통과했다.
- `git diff --cached --check` 통과.

## 커버리지

Python 표준 `trace.Trace(count=True, trace=False)`로 `pytest.main(['-q', 'tests/test_course_confirmation.py'])`를 실행했다(11 passed). `git show 556b369:src/runart/server.py` 대비 추가/수정 행을 `difflib.SequenceMatcher`로 구한 뒤 `trace._find_executable_linenos`와 대조했다. 변경 실행 행 **34/35 (97.1%)**가 실행됐다. 미실행 한 행은 성공 결과의 조건 충족 메타데이터 분기이며 별도 실데이터 HTTP 테스트에서 검사했다. 이 수치는 전체 저장소나 분기 커버리지가 아니다.

| 보장 | 테스트 | 종류 |
| --- | --- | --- |
| 위젯 켬/끔 모두 동의 전 질문만 제공 | test_course_confirmation.py | 단위 |
| 거리·시간·평지·야간·시설·좌표 유지 | test_course_confirmation.py | 단위 |
| 동의 후에도 조건 불충족 코스는 미제공 | test_confirmation_does_not_relax_hard_constraints | 단위 |
| 통합/레거시 최초 요청은 질문, 다음 요청은 실제 출발지 위젯 | test_wangsimni_dog_reproduction_discloses_actual_starts | 실데이터 통합 |
| MCP HTTP로 질문 → 인근 강아지 / 원래 역 일반 코스 선택 검증 | test_mcp_http_preserves_disclosure_in_both_text_and_widget | HTTP 통합 |
| 동의 기본 false, 두 선택지에 모호한 답은 재확인하도록 툴 설명 제공 | test_public_schema_defaults_to_no_consent | 스키마 |

## 운영 검증 한계

서버는 stateless이므로 카카오 LLM이 사용자 답변을 올바른 인자로 변환하는지는 서버 단위 테스트로 보장할 수 없다. `allow_nearby_start=true`의 실제 사용자 동의 여부는 호스트가 판단한다. 질문/STOP/다음 턴 호출 규칙을 툴 설명에 명시했다. KC 배포 후 최신 툴 스키마가 전달되는지, Preview에서 최초 질문 → “1번”/“2번”/모호한 “네” 흐름을 별도로 확인해야 한다.

로컬의 별도 변경 course.py/models.py/rfs.py 및 미연결 discovery.py와 관련 없는 파일은 이번 커밋에서 제외한다. 이 작업에서 푸시나 KC 배포는 수행하지 않았다.
