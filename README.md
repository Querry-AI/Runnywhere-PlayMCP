# Runnywhere(러니웨어) — 어디서든 러닝 코스 짜기!

카카오 PlayMCP **Agentic Player 10** 공모전 출품작. AI 채팅에 *"시청에서 5km, 오르막 없이, 고래 모양으로"* 라고 말하면 서울 보행 도로망 위에 뛸 수 있는 코스를 생성한다.
서울시 경사도(표고·등고선), 보행자 신호등, 가로등 위치 데이터를 러닝 친화도 점수(RFS)에 반영해 평지 우선·밤안심 코스를 제공한다. 서울시 공중화장실과 OSM 편의점 데이터도 반영해 "화장실/편의점 지나가게" 같은 요청을 코스 후보 선택에 활용한다.
PRD: [`../runart-mcp-prd/PRD.md`](../runart-mcp-prd/PRD.md)

## 실행

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m runart.server           # http://localhost:8000/mcp (Streamable HTTP, JSON)
pytest                            # 테스트 (시나리오 수용 테스트 포함)
python scripts/loadtest.py 1000 10 # 콜드 미스 포함 부하 테스트 (평균 100ms / p99 3s)
```

환경변수: `HOST`(로컬 기본 127.0.0.1, 컨테이너 0.0.0.0) · `PORT`(기본 8000) · `RUNART_BASE_URL`(미리보기 링크 도메인) · `KAKAO_JAVASCRIPT_KEY`(카카오맵 Web API, 필수) · `KAKAO_REST_API_KEY`(지오코딩, 선택) · `RUNART_TOKEN_SECRET`(32자 이상 도감·릴레이 토큰 서명키, 운영 환경 필수) · `WEB_CONCURRENCY`(웹 워커 수, 기본 1) · `RUNART_POOL_WORKERS`(코스 탐색 프로세스 수, 기본 2) · `RATE_LIMIT_RPS`(IP당, 기본 20) · `RUNART_ETL_LOCAL_ONLY=1`(기존 OSM 속성은 보존하고 로컬 경사도/신호등/가로등만 재반영)

실그래프(`data/seoul_graph.pkl`)가 없으면 **서울 시청 일대 데모 그리드**로 구동된다(전체 파이프라인 동작 확인용). 공모전 제출 전 반드시 ETL 실행:

```bash
pip install -e '.[etl]'
python etl/build_graph.py         # OSM 서울 전역 보행망 -> data/seoul_graph.pkl
python scripts/build_animal_presets.py --workers 2 --fresh  # 고유 역 좌표 × 동물 4종 품질 우선 사전 계산
RUNART_ETL_LOCAL_ONLY=1 python etl/build_rfs.py
# 로컬 서울시 경사도 + 보행자 신호등 + 가로등 위치 + 공중화장실을 반영
```

현재 스냅숏(`data/snapshot.json`, 2026-07-11): 서울 전역 보행 그래프 163,848 노드 / 232,006 엣지에 경사도 232,006개 엣지, 보행자 신호등 26,769개 포인트 기반 횡단 점수, 가로등 19,316개 포인트 기반 조명 점수를 반영했다. 편의시설은 편의점 6,693개, 화장실 4,985개, 공원 2,237개, 음수대 213개가 포함되어 있다.

pickle 형식의 그래프·시설·인프라 파일은 로드 전에 `src/runart/data_integrity.py`의 SHA-256과 대조한다. ETL로 이 세 파일을 다시 만들 때만 `RUNART_ALLOW_UNVERIFIED_DATA=1`로 검증을 임시 해제하고, 검수 후 새 체크섬을 코드에 반영해야 한다. 운영 환경에서는 검증 해제 변수를 설정하지 않는다.

동물 코스 프리셋은 `stations.py`의 289행을 동일 좌표 기준으로 합친 뒤 강아지·고양이·고래·토끼를 각각 11km까지 전 거리 탐색한다. 런타임의 3초 제한이나 조기 종료를 적용하지 않고 레퍼런스 실루엣 유사도를 최우선으로 선택하며, 유사도가 비슷할 때만 더 짧은 코스를 고른다. 결과는 `data/animal_station_presets.json.gz`에 저장되며, 적절한 코스가 없는 조합도 명시적으로 저장해 런타임 재탐색을 막는다. 그래프가 변경되면 fingerprint가 달라져 기존 프리셋은 자동 무효화된다.

런타임에는 요청 지점의 정확한 프리셋을 먼저 사용하고, 없으면 같은 동물의 검증 코스를 주변 2km에서 찾아 실제 출발역·이동 거리·도보 시간을 명시한다. 품질 게이트를 낮추지 않으면서 역×동물 즉시 추천 범위를 421개에서 905개 조합으로 넓힌다.

## 구조

| 경로 | 역할 (PRD 매핑) |
|---|---|
| `src/runart/server.py` | MCP 툴 6개 + 미리보기/GPX/공유 라우트 (§5.1, §5.6) |
| `src/runart/course.py` | RFS 가중 순환 코스 생성, ±5% 거리 허용 (§5.3) |
| `src/runart/shapes.py` | 동물 모양 템플릿·스냅핑·유사도 게이트 0.7 (§5.4) |
| `src/runart/rfs.py` | 러닝 친화도 점수 — 기본/야간 가중 프로파일 (§5.7) |
| `src/runart/facilities.py` | 코스 10m 반경 편의시설 (§5.5) |
| `src/runart/models.py` | 자기완결형 course_id — stateless (§5.1) |
| `etl/` | 오프라인 데이터 파이프라인 — 경로 생성 중 데이터 API 호출 없음 (§5.7) |

경로·안전·시설 데이터는 컨테이너에 미리 적재되어 런타임에 외부로 조회하지 않는다. 단, 사용자가 입력한 임의의 서울 주소를 좌표로 바꾸는 지오코딩은 `KAKAO_REST_API_KEY`가 설정된 경우 Kakao Local API를 선택적으로 사용하며, 지하철역 289개와 주요 지명은 네트워크 없이 해석한다.

## 툴 (9개, 모두 stateless·idempotent)

`generate_running_course` · `generate_animal_course` · `list_available_shapes` · `find_facilities_near_course` · `refine_course` · `get_course_status` · `explore_animal_collection` · `record_animal_completion` · `extend_shape_relay`

서울 동물지도(`/animals`)는 검증된 421개 GPS 아트를 한 화면에서 탐색하게 한다. 완주 기록은 서버 DB나 로그인 대신 자기완결형 `passport_token`으로 이어지며, 4종 도감·지역별 4종 배지·주간 최인접 미발견 동물을 제공한다. Shape Relay(`/relay/{token}`)도 최대 8개 동네의 같은 동물 course_id를 자기완결형 토큰에 담아 나란히 비교하고 공동 GPS 작품으로 겹쳐 보여준다. 따라서 PlayMCP 권장 stateless/no-session 구조를 유지한다.

## 배포 (PlayMCP in KC)

```bash
docker build -t runnywhere .
docker run -p 8000:8000 -e RUNART_BASE_URL=https://<kc-endpoint> runnywhere
```

MCP Endpoint: `https://<kc-endpoint>/mcp` — PlayMCP 등록 전 [MCP Inspector](https://github.com/modelcontextprotocol/inspector)로 검증할 것.

## 데이터 출처
OpenStreetMap, 서울 열린데이터광장(서울시 경사도 OA-22241, 서울시 가로등 위치 정보 OA-22205, 서울특별시 보행자 신호등 분포도, 서울시 공중화장실 위치정보), SRTM 30m 고도 데이터. 안심이 CCTV 포인트 데이터는 서비스 종료로 직접 사용하지 않고 OSM surveillance 태그를 폴백으로 사용한다. 개인정보는 수집·저장하지 않는다.
