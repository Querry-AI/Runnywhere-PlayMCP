# Kakao Tools 위젯 세부 명세 및 코드 구현 계획

작성일: 2026-08-18
재검증: 2026-08-19
대상 기준: `de4c903` (`main`)
권위 있는 입력 문서:

- `/Users/theo/Downloads/[Agentic Player 10] Kakao Tools 개발가이드.pdf` v1.0.0, 2026-08-03
- `/Users/theo/Downloads/[Agentic Player 10] 공모전 본선 가이드 (2).pdf`

이 계획은 위 두 PDF와 현재 소스의 교집합만 확정 사항으로 취급한다. PDF에 필드 구조가 없는 ChatKit 확장 컴포넌트는 추측해서 구현하지 않는다.

재검증 결과 개발 가이드의 위젯 계약은 이전 PDF와 의미상 동일하다. 본선 가이드만 일정이 변경됐다. 신규 기능 완료일은 8월 31일, QA는 9월 1~11일, 코드 프리징은 9월 14일, 본선은 9월 16일~10월 12일이다. 8월 31일 이후에는 오류 수정만 한다.

구현 상태(2026-08-19): `src/runart/widget.py`의 순수 빌더, 생성 MCP 경계 연결, 기능 플래그, Markdown 폴백, 수동 수정본 새 ID 캐시까지 로컬 구현했다. 전체 테스트는 **172개 통과**, 로컬 raw `tools/call` 응답은 **1,875바이트**, 위젯 빌더 1,000회 측정 p99는 **0.069ms**였다. 실제 Kakao Tools Preview와 KC 성능 측정은 아직 남아 있다.

## 1. 결론

1차 위젯은 `create_seoul_running_course`가 **단일 확정 코스**를 성공적으로 반환할 때만 사용한다. 구성 요소는 PDF 예시에 구조가 명시된 `Card`, `Text`, `Button`으로 제한한다.

다음 응답은 계속 일반 Markdown으로 반환한다.

- `best_animal` 조사 및 선택지
- 인근 역 프리셋 이동 제안
- 요청 거리보다 긴 대안 제안
- 위치 오류, 잘못된 요청, 타임아웃, 내부 오류
- 시설 목록, 모양 목록, 완주 기록, 릴레이

이유는 단순하다. 위젯을 쓰면 ChatGPT가 답변을 가공하지 않는다. 선택과 설명이 필요한 응답까지 카드로 고정하면 오히려 대화 품질이 떨어진다.

## 2. PDF에서 확정된 응답 계약

### 2.1 전송 위치

카카오 위젯은 MCP Apps의 `ui://` 리소스나 `structuredContent` 전용 UI가 아니다. 위젯 envelope를 JSON 문자열로 직렬화해 `tools/call`의 `result.content[0].text`에 넣는다.

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"widget\":{...},\"copy_text\":\"...\",\"name\":\"runnywhere_course\"}"
      }
    ]
  }
}
```

현재 코드의 `structuredContent.result_code`, `retryable`, `isError`는 MCP 표준 필드이므로 보존한다. 단, 카카오 Preview에서 `structuredContent`와 위젯 문자열의 공존을 실제 확인한다. 공존 때문에 렌더링이 실패할 때만 제거 여부를 재검토한다.

### 2.2 실제 생성할 envelope

```json
{
  "widget": {
    "type": "Card",
    "children": [
      {
        "type": "Text",
        "value": "🐶 강남역 댕댕런"
      },
      {
        "type": "Text",
        "value": "9.0km · 누적 오르막 31m · 러닝 친화도 86/100"
      },
      {
        "type": "Text",
        "value": "출발·도착: 강남역"
      },
      {
        "type": "Button",
        "label": "코스 지도 보기",
        "onClickAction": {
          "payload": {
            "target": {
              "url": "https://runnywhere.playmcp-endpoint.kakaocloud.io/c/{course_id}",
              "pcUrl": "https://runnywhere.playmcp-endpoint.kakaocloud.io/c/{course_id}"
            }
          }
        }
      },
      {
        "type": "Button",
        "label": "GPX 다운로드",
        "onClickAction": {
          "payload": {
            "target": {
              "url": "https://runnywhere.playmcp-endpoint.kakaocloud.io/c/{course_id}.gpx",
              "pcUrl": "https://runnywhere.playmcp-endpoint.kakaocloud.io/c/{course_id}.gpx"
            }
          }
        }
      }
    ]
  },
  "copy_text": "**🐶 강남역 댕댕런**\n- 거리: 9.0km\n- 출발·도착: 강남역\n- 지도: https://runnywhere.playmcp-endpoint.kakaocloud.io/c/{course_id}",
  "name": "runnywhere_course"
}
```

### 2.3 불변 조건

- 최상위 위젯은 반드시 `widget`으로 한 번 감싼다.
- `status`는 최상위와 `widget` 내부 어느 곳에도 넣지 않는다. 카카오가 추가한다.
- `copy_text`는 간단한 Markdown만 사용한다. 1차 구현은 굵게와 순서 없는 목록만 쓴다.
- Markdown 링크 문법, 헤딩, 표, 인용, 코드 블록은 `copy_text`에 쓰지 않는다.
- 버튼 URL은 `onClickAction.payload.target.url`에 둔다.
- `pcUrl`은 같은 HTTPS 웹 URL로 명시한다.
- `name`은 PDF 별첨 전체 응답 예시에 포함돼 있으므로 `runnywhere_course`로 고정해 보낸다. 다만 본문이 필수 property로 별도 선언하지는 않으므로 문서상 **예시 기반 호환성 필드**로 분류한다.
- JSON은 `ensure_ascii=False`, compact separators로 한 번만 직렬화한다.
- 전체 UTF-8 크기가 12,000바이트를 넘으면 위젯을 포기하고 Markdown으로 폴백한다. 12KB는 PDF 규정이 아니라, 저장소에 이미 있는 PlayMCP 24KB 회귀 한도의 절반을 여유로 둔 **내부 운영 기준**이다.
- 좌표 배열, 전체 시설 목록, 내부 점수 원자료는 넣지 않는다.
- 예상 시간·걸음·칼로리는 사용자 페이스에 따라 달라지는 값이므로 1차 위젯에 고정하지 않는다. 상세 페이지의 페이스 선택기가 계산한다. 위젯에는 거리·출발지·누적 오르막·RFS처럼 코스 자체의 사실만 넣는다.

## 3. 현재 코드에서의 정확한 삽입 지점

현재 생성 흐름은 다음과 같다.

```text
create_seoul_running_course
  -> generate_running_course 또는 generate_animal_course
  -> 내부에서 Course 생성 및 _course_cache 저장
  -> course_markdown 문자열 반환
  -> _course_tool_result
  -> CallToolResult(content=[TextContent], structuredContent, isError)
```

위젯은 `_course_tool_result` 직전에 붙인다. 코스 탐색·프리셋·거리 보정 로직은 건드리지 않는다.

```text
기존 Markdown 결과
  -> 결과 분류
  -> 위젯 대상인가?
     -> 아니오: 기존 _mcp_result
     -> 예: Markdown 안의 단일 canonical course_id 확인
          -> 이미 캐시에 있는 Course만 조회
          -> widget.py 순수 빌더 실행
          -> 성공: content[0].text = widget JSON
          -> 실패/캐시 미스/크기 초과: 기존 Markdown
```

Markdown에서 course id를 찾는 방식은 장기적으로는 타입 결과 객체보다 덜 우아하다. 하지만 현재 `generate_animal_course`에 대안·조사·오류 반환 지점이 많아 전체 반환형을 지금 바꾸면 회귀 위험이 크다. 마감 전 1차 구현에서는 서버가 스스로 생성한 `BASE_URL/c/{id}`만 엄격하게 추출하고, 중복을 제거한 고유 id가 정확히 하나일 때만 카드화하는 것이 더 안전하다.

여기서 “캐시에 있는 코스만 카드화”는 과거 요청만 위젯으로 만든다는 뜻이 아니다. 첫 요청에서 코스를 한 번 생성한 뒤 `_get_course()` 또는 `_serve_course()`가 **같은 요청 안에서 즉시 캐시에 넣고**, 이어지는 표시 단계가 그 객체를 읽는다. 따라서 **기존 코스뿐 아니라 새로 호출해 방금 생성한 코스도 첫 응답부터 위젯이 나온다.** 캐시는 영구 저장소가 아니라 생성과 표시 사이의 임시 전달 계층이다. 진짜 원본은 자기완결형 `course_id`다.

다중 인스턴스 환경에서 캐시가 없는 경우에는 위젯 때문에 탐색을 다시 돌리지 않는다. 이미 완성된 Markdown을 그대로 반환한다. 이는 “카드 누락 가능성보다 p99 초과와 이중 계산을 더 위험하게 본다”는 의도적인 fail-open 정책이다.

### 3.1 상세 페이지 수기 수정의 버전 계약

수기 편집은 기존 코스를 덮어쓰지 않는다.

```text
원본 course_id + 원본 채팅 위젯
  -> 상세 페이지에서 경로 수정·저장
  -> manual_path를 포함한 새 course_id 발급
  -> 새 상세 페이지와 새 GPX는 수정본을 가리킴
```

- 기존 채팅 위젯과 원본 URL은 계속 원본 코스를 가리킨다.
- 수정본은 새 `course_id`의 독립 버전이다.
- `manual_path`가 새 id에 포함되므로 서버 재시작이나 다른 인스턴스에서도 같은 도로 노드 경로를 복원할 수 있다.
- 수기 수정 시 `shape=None`이 되므로 수정본을 계속 “검증된 강아지·고양이 모양”이라고 표시하지 않는다.
- 브라우저 편집은 이미 반환된 채팅 위젯을 실시간 갱신하지 않는다. 수정본 카드를 원하면 추후 새 id로 상태 조회가 호출돼야 한다.

현재 저장 API는 새 id를 반환하지만 즉시 `_course_cache`에 넣지 않는다. 위젯 작업과 함께 `new_id` 검증 직후 `_cache_put(new_id, course)`를 추가한다. 이는 경로를 다시 찾는 기능이 아니라 방금 검증·저장한 수정본을 같은 프로세스에서 재사용하는 보완이다.

## 4. 파일별 변경 계획

### 4.1 신규: `src/runart/widget.py`

역할은 **순수 표시 변환**뿐이다. 코스 생성, 캐시, 네트워크, 환경 변수 조회를 하지 않는다.

예정 API:

```python
WIDGET_NAME = "runnywhere_course"
WIDGET_MAX_BYTES = 12_000

class WidgetBuildError(ValueError):
    pass

def build_course_widget(
    course: Course,
    course_id: str,
    base_url: str,
) -> str:
    """Kakao widget envelope를 compact JSON 문자열로 반환한다."""
```

내부 헬퍼:

- `_plain_text(value, max_chars)` — 제어문자 제거, 길이 상한. Markdown 이스케이프는 하지 않는다.
- `_button(label, url)` — PDF의 `onClickAction.payload.target` 모양을 한 곳에서 생성한다.
- `_copy_text(course, title, preview_url)` — 허용된 Markdown 부분집합만 만든다.
- `_validate_envelope(payload)` — `status` 부재, 필수 키, URL, 크기를 직렬화 전에 검사한다.
- `_serialize(payload)` — `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`를 딱 한 번 호출한다.

표시 데이터는 기존 도메인 객체에서 직접 읽는다.

- 제목: `naming.course_title(course)`와 현재 배지/동물 emoji
- 거리: `course.length_km`
- 오르막: `course.ascent_m`
- 러닝 친화도: `course.rfs["score"]`
- 출발지: `course.params.location_name`
- 지도/GPX: 검증된 `BASE_URL`과 `course_id`

`render.course_markdown()`을 다시 파싱해 통계값을 얻지 않는다.

### 4.2 수정: `src/runart/server.py`

추가 함수:

```python
_COURSE_LINK_RE = ...  # BASE_URL을 escape하고 URL-safe token만 허용

def _extract_single_course_id(text: str) -> str | None:
    """서버가 만든 canonical /c/{id} 링크의 고유 id가 하나일 때만 반환."""

def _cached_course(course_id: str) -> Course | None:
    """캐시 조회만 수행. 없을 때 생성하지 않음."""

def _try_course_widget(text: str, course_type: str) -> str | None:
    """대상이 아니거나 빌드 실패면 None."""
```

`_course_tool_result` 시그니처 변경:

```python
def _course_tool_result(text: str, *, course_type: str) -> CallToolResult:
```

판정 순서:

1. 기존 로직으로 `result_code`와 `isError`를 먼저 확정한다.
2. `result_code == "course_ready"`인지 확인한다.
3. `course_type`이 `standard`, `dog`, `cat`, `rabbit`, `whale` 중 하나인지 확인한다.
4. canonical course id가 정확히 하나인지 확인한다.
5. 동일 id의 `Course`가 캐시에 있는지 확인한다. 없으면 **재생성하지 않는다**.
6. feature flag가 켜져 있는지 확인한다.
7. 빌더가 성공하면 widget JSON을 `content[0].text`로 사용한다.
8. 어느 단계든 실패하면 원래 Markdown을 사용한다.

`create_seoul_running_course`는 `_course_tool_result(..., course_type=course_type)`로 호출한다.

`edit_course_route`의 `save` 성공 경로에서는 길이 제한을 통과한 새 id를 반환하기 전에 수정본을 캐시한다.

```python
new_id = encode_course_id(course.params)
if len(new_id) > 4096:
    ...
_cache_put(new_id, course)
```

이 변경은 기존 id를 덮어쓰지 않으며, `manual_path`가 든 새 id와 방금 계산한 `Course`만 연결한다.

feature flag:

```python
KAKAO_WIDGETS_ENABLED = os.environ.get("RUNART_KAKAO_WIDGETS", "1") == "1"
```

운영에서 렌더링 장애가 생기면 코드 롤백 없이 `RUNART_KAKAO_WIDGETS=0`으로 기존 Markdown 경로를 복구할 수 있게 한다.

로그는 위치·course id·프롬프트를 남기지 않고 아래 값만 기록한다.

```text
mcp_widget tool=create_seoul_running_course state=emitted|ineligible|fallback reason=...
```

예상 reason은 `disabled`, `result_code`, `course_type`, `no_single_id`, `cache_miss`, `build_error`, `too_large`로 제한한다.

### 4.3 수정 없음: `src/runart/render.py`

기존 웹 상세 페이지와 Markdown 폴백을 그대로 보존한다. 위젯 때문에 상세 페이지 HTML, GPX, 카드 SVG, 코스 생성 로직을 바꾸지 않는다.

### 4.4 신규: `tests/test_widget.py`

순수 빌더 계약 테스트를 둔다.

- 최상위에 `widget`, `copy_text`, 예시 기반 호환성 필드 `name`이 포함되는지
- `widget.type == "Card"`인지
- 모든 `Text`가 `value`, 모든 `Button`이 `label`을 갖는지
- 버튼 경로가 정확히 `onClickAction.payload.target.url/pcUrl`인지
- `status`가 재귀적으로 존재하지 않는지
- 지도 URL과 GPX URL이 올바른 course id를 사용하는지
- 한글이 `\uXXXX`로 불필요하게 팽창하지 않는지
- `copy_text`에 헤딩·표·Markdown 링크·코드 블록이 없는지
- 제어문자와 비정상적으로 긴 위치명이 제한되는지
- 직렬화 결과가 12KB 미만인지
- 같은 입력이 byte-for-byte 같은 결과를 만드는지

### 4.5 수정: `tests/test_tools.py`

통합 경계 테스트를 추가한다.

- `standard` 성공 결과의 `content[0].text`가 `json.loads` 가능한 위젯인지
- `dog/cat/rabbit/whale` 단일 확정 성공도 위젯인지
- `best_animal`은 Markdown인지
- `nearby_course_ready`, `exact_shape_unavailable`, `generation_timeout`, `location_not_found`, `internal_error`는 Markdown인지
- 빌더를 강제로 예외 내도 기존 Markdown과 `result_code`가 보존되는지
- 캐시를 강제로 비워도 코스를 재생성하지 않고 Markdown으로 끝나는지
- `structuredContent.result_code`와 `isError`가 기존과 같은지
- `RUNART_KAKAO_WIDGETS=0`에서 전부 기존 Markdown인지
- MCP tool 목록·inputSchema·description이 위젯 작업으로 바뀌지 않는지
- raw `tools/call` JSON-RPC 응답에서도 위젯 문자열 위치가 `result.content[0].text`인지
- 수기 저장 성공 시 새 `course_id`가 캐시에 들어가고 원본 id의 캐시 항목은 그대로인지
- 수정본 id를 decode하면 `manual_path`가 보존되고 `shape is None`인지
- 기존 채팅 위젯 URL은 원본 id, 수정 후 `preview_url`은 새 id인지

기존 helper 함수 `generate_running_course`, `generate_animal_course`의 문자열 반환 테스트는 그대로 통과해야 한다. 위젯은 MCP 공개 진입점에만 적용한다.

## 5. 구현 순서와 중단 기준

### 단계 A — 기준선 (2026-08-19 재검증 완료)

1. 구현 전 전체 `pytest`: **161 passed in 15.91s**
2. 구현 후 전체 `pytest`: **172 passed in 13.12s**
3. `mcp.list_tools()`: 7개, 최장 description 464자, 필수 annotations 유지
4. 새 코스 raw JSON-RPC 응답 1,875바이트, 위젯 문자열 1,655바이트
5. 위젯 빌더 1,000회 로컬 측정: p50 0.0332ms, p99 0.069ms, max 0.2332ms
6. p50/p99 자료는 2026-07-11 로컬 80건뿐이므로 KC 서버 기준선은 아직 없음
7. 감사 시점 워크트리의 `src/runart/render.py`, `src/runart/pace.py`는 별도 상세 페이지 작업이다. 위젯 commit에 섞지 않고 명시적으로 파일을 stage한다.

중단: 기준선에서 기존 테스트가 실패하면 위젯 작업 전에 원인을 분리한다.

### 단계 B — 순수 빌더 TDD

1. `tests/test_widget.py`를 먼저 작성
2. `widget.py` 최소 구현
3. 공식 PDF의 Card/Text/Button 예시와 동일한 경로를 구조 테스트로 고정

중단: PDF에 없는 필드가 필요해지면 임의로 추가하지 않는다. 해당 표현은 텍스트로 남긴다.

### 단계 C — 생성 진입점 연결

1. 단일 course id 추출
2. 캐시 전용 조회
3. `course_type`·`result_code` eligibility gate
4. 예외·크기 초과 Markdown 폴백
5. feature flag와 비식별 로그

중단: 코스 생성/프리셋/라우팅 코드를 수정해야만 연결할 수 있다면 설계를 재검토한다.

### 단계 D — 회귀 및 성능

1. 전체 `pytest`
2. 대표 7개 툴 결과 크기 < 24KB
3. 위젯 빌드 단독 p99 목표 < 2ms
4. 기존 코스 생성 p99 3초 제한 불변
5. MCP Inspector로 KC 배포본의 `tools/list`, `tools/call` 점검

KC 부하테스트는 URL을 반드시 명시한다. `RUNART_LOADTEST_REPORT`만 지정하면 스크립트가 localhost를 사용하므로 실서버 증빙이 되지 않는다.

```bash
RUNART_LOADTEST_URL=https://<KC-ENDPOINT>/mcp \
RUNART_LOADTEST_REPORT=artifacts/playmcp-loadtest-kc.json \
.venv/bin/python scripts/loadtest.py 1000 10
```

중단: 위젯 도입으로 코스 생성 경로에 추가 탐색이나 외부 요청이 생기면 배포하지 않는다.

### 단계 E — Kakao Tools Preview

명시적 툴 이름은 **위젯 렌더링 검증용**으로만 사용한다.

1. PlayMCP 도구함에는 테스트 대상 MCP만 담는다.
2. `runnywhere-create_seoul_running_course`를 명시해 표준/동물 확정 코스 카드가 보이는지 확인한다.
3. 지도 버튼과 GPX 버튼이 새 창에서 올바른 URL을 여는지 확인한다.
4. 카카오톡 공유 결과가 `copy_text`와 일치하는지 확인한다.
5. 실패·대안 응답이 카드가 아니라 Markdown인지 확인한다.
6. 각 확인 뒤 **새 대화 시작**으로 문맥을 초기화한다.

그 다음 자연 발화 호출률을 별도로 측정한다. 위젯 렌더링 성공과 툴 선택 성공을 같은 실험으로 섞지 않는다.

description A/B를 할 때도 등록 서비스명 `Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!)`은 모든 변형에 그대로 둔다. 바꾸는 것은 첫 영문 기능 문장, 한국어 트리거, 부정 경계의 순서와 길이뿐이다.

권장 자연 발화 세트:

- `성신여대역 러닝 코스 그려줘`
- `강남역 3km 일반 러닝 코스`
- `강남역 강아지 코스 그려줘`
- `경복궁역 토끼 코스`
- `성신여대역 동물 코스 추천해줘`
- `테헤란로8길 8에서 5km 야간 코스`

각 발화를 새 대화에서 5회 반복하고 다음을 분리 기록한다.

- 기대 툴 호출 여부
- `course_type` 정확도
- 위치·거리 파라미터 정확도
- 위젯 대상이면 렌더링 여부
- 텍스트 대상이면 대안 설명 정확도

내부 출시 기준은 핵심 생성 발화 30회 중 27회 이상 기대 툴 호출, 호출된 건의 파라미터 정확도 100%, 위젯 대상 응답 렌더링 100%로 잡는다. LLM 호출 자체는 공식 가이드상 100% 보장되지 않으므로 이 수치는 회귀 감지 기준이지 보증 문구가 아니다.

### 일정 배치

| 날짜 | 작업 |
|---|---|
| 8/19 | PDF·코드·기준선 재검증 완료 |
| 8/20~8/22 | 빌더·계약 테스트·생성 진입점 연결 |
| 8/23~8/24 | 전체 회귀·수기 수정본 캐시·raw JSON-RPC 검증 |
| 8/25 | 첫 KC 재배포 및 MCP Inspector |
| 8/26~8/30 | Preview 렌더링·자연 발화 호출률·description A/B |
| **8/31** | 신규 기능 종료, release candidate 확정 |
| 9/1~9/11 | 카카오 QA 오류 대응만 |
| 9/12~9/13 | 최종 회귀·롤백 점검 |
| **9/14** | 코드 프리징 |

## 6. 배포 및 롤백 순서

사용자가 배포를 승인한 뒤에만 수행한다.

1. 로컬 전체 테스트
2. commit 및 `main` 반영
3. PlayMCP in KC에서 기존 본선 서버 **재배포**
4. `Redeploying`이 `Active`가 될 때까지 대기
5. `/healthz`의 `release_sha`가 기대 commit인지 확인
6. MCP `tools/list`가 소스와 같은 7개인지 확인
7. Preview에서 명시적 위젯 테스트
8. Preview에서 자연 발화 호출률 테스트

롤백 우선순위:

1. 위젯만 문제: `RUNART_KAKAO_WIDGETS=0`
2. 새 release 전체 문제: 마지막 정상 commit 재배포
3. 응답 지연 문제: 위젯 로그의 `state/reason` 확인 후 카드 경로 비활성화

## 7. 명시적으로 하지 않을 것

- `structuredContent`에만 위젯을 넣지 않는다.
- MCP Apps, `ui://`, iframe 리소스를 도입하지 않는다.
- `status`를 직접 넣지 않는다.
- 1차에 `ListView`, Image, 지도 좌표, 커스텀 HTML을 넣지 않는다.
- 모든 성공·실패 응답을 무조건 카드화하지 않는다.
- 위젯을 만들기 위해 코스를 재생성하지 않는다.
- 기존 상세 페이지 UI나 프리셋 데이터를 함께 수정하지 않는다.
- 위젯 작업과 description A/B 변경을 같은 commit에 섞지 않는다.

## 8. 완료 정의

다음 조건을 모두 만족해야 위젯 구현 완료로 본다.

- 공식 PDF의 top-level wrapper, `copy_text`, `status` 금지, 버튼 payload 계약을 테스트로 고정
- 단일 확정 코스만 위젯, 선택·대안·오류는 Markdown
- 위젯 빌드 실패가 사용자 오류로 노출되지 않고 기존 Markdown으로 복귀
- 코스 재탐색·외부 호출·DB·세션 추가 없음
- 수기 수정은 새 `course_id`로 버전되며, 새 코스는 즉시 캐시되고 기존 채팅 위젯은 원본 id를 유지
- 시간·걸음·칼로리처럼 페이스 의존적인 값은 위젯에 고정하지 않음
- 전체 테스트 및 MCP Inspector 통과
- 응답 24KB 제한과 p99 3초 제한 유지
- KC의 `release_sha`와 source commit 일치
- Kakao Tools Preview에서 카드, 두 버튼, 공유 텍스트를 실제 확인
- 자연 발화 호출률 실험을 위젯 렌더링 실험과 분리해 기록
