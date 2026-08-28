# 지운 구간 끝점·여러 획 연결 TDD 증거

2026-08-29. 별도 계획 문서 없이 사용자 재현에서 도출했다.

## 사용자 여정

- 지운 구간의 붉은 끝점 근처에 손가락으로 선을 그리면, 같은 보행망 끝점이라는 근거가 있을 때 도보 경로 미리보기를 만든다.
- 한 경로를 여러 번 손을 떼며 이어 그려도 순서·방향과 무관하게 하나의 연결된 초안으로 확인하고 저장한다.
- 가까워 보이더라도 다른 층·계단·긴 우회 간선·모호한 3방향 접점은 자동 연결하지 않고, 지우지 않은 원래 코스는 보존한다.

## 원인과 구현

기존 연결 판정은 실제 좌표 교차 오차가 0.1mm였고, 지운 구간은 한 획이 양 끝을 모두 교차해야 했다. 실제 API 재현에서 0.5m·5m 끝점 오차와 정확히 맞닿는 여러 획이 422를 반환했다.

상호작용 전용 허용 범위를 12m로 분리했다. 이 범위는 지운 구간 끝과 획의 끝에만 적용한다. 지운 끝점은 가장 가까운 노드가 해당 끝점이거나 24m 이하의 직접 연결된 보행 가능 간선일 때만 붙인다. 여러 획은 양 끝이 같은 보행 노드로 확인되고 접점이 모호하지 않을 때만 합친다. 일반적인 선 교차의 엄격한 기하·보행망 판정은 변경하지 않았다.

## RED → GREEN

- `db10747`: 최초 `.venv/bin/pytest -q tests/test_editor_stroke_join.py` → **6 failed, 1 passed**. 끝점 오차와 여러 획 연결 실패를 재현했다.
- `bbbb529`: `.venv/bin/pytest -q tests/test_editor_stroke_join.py tests/test_course_edit.py` → **98 passed**.
- Superpowers 코드 리뷰에서 동일 좌표·다른 노드가 합쳐질 수 있다는 중요 문제를 발견했다.
- `5a5b0f7`: 해당 회귀 테스트 → **1 failed**.
- `561215a`: 안전 보완 후 편집 테스트 → **99 passed**.
- `78987ef`: 정확히 겹친 접점에만 중복 노드 조회를 수행하도록 리팩터한 뒤 동일 편집 테스트 **99 passed**.

## 검증

| 보장 | 테스트/명령 | 결과 |
| --- | --- | --- |
| 0.5m·5m 손가락 오차를 같은 보행 끝점으로 연결 | `test_finger_miss_at_erased_endpoints_can_be_previewed` | PASS |
| 2~3개 획, 역방향·무순서·근접 접점을 연결 | `test_connected_pen_lifts_*`, `test_mixed_direction_unordered_strokes_join` | PASS |
| 연결된 여러 획을 미리보기 후 실제 새 코스로 저장 | `test_connected_multistroke_preview_can_be_saved` | PASS |
| 다른 층·계단·긴 간선·12m 밖·모호한 분기는 연결하지 않음 | `tests/test_editor_stroke_join.py` 안전 경계 테스트 | PASS |
| 지우지 않은 초록 경로의 방향별 간선 보존 | `test_connected_pen_lifts_replace_gap_without_deleting_green_edges` | PASS |
| 커밋된 파일만 복원한 독립 전체 회귀 | `PYTHONPATH=/tmp/runart-stroke-release.tAeVsR/src ... pytest -q --tb=short` | **738 passed (33.02s)** |
| 운영 편집 스크립트 DOM/포인터 시나리오 | `node tests/browser/run_scenarios.js ...` | **59/59 passed** |
| 모바일 터치·핀치·도구 전환 | `node tests/browser/mobile_gestures.js ...` | **390px PASS, 320px PASS** |
| 공백 오류 검사 | `git diff --check` | PASS |

표준 라이브러리 `trace.Trace`로 새 테스트 20개를 실행해 두 새 헬퍼의 실행 가능 행 **68/71 (95.8%)**를 실행했다. 이는 전체 저장소 또는 분기 커버리지가 아니다.

## 한계

브라우저 테스트는 카카오 SDK 투영과 서버 fetch를 대역하므로, 실제 카카오 지도 타일 위의 손가락 체감은 KC 배포 후 실기기 확인이 필요하다. 서버 API·실제 서울 그래프 테스트는 대역하지 않았다. 외부 코드 리뷰는 한 차례 사용량 제한으로 실패했고, 저비용 재시도에서 발견된 중요 안전 문제를 위 RED/GREEN 사이클로 수정했다.

기존 로컬 변경인 `course.py`의 지형/가중치 부분, `models.py`, `rfs.py`, `discovery.py`와 관련 없는 파일은 이번 작업 커밋에서 제외했다.
