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
python scripts/loadtest.py 200 10 # 부하 테스트 (규격: 평균 100ms / p99 3s)
```

환경변수: `PORT`(기본 8000) · `RUNART_BASE_URL`(미리보기 링크 도메인) · `WEB_CONCURRENCY`(웹 워커 수, 기본 1) · `RUNART_POOL_WORKERS`(코스 탐색 프로세스 수, 기본 2) · `RATE_LIMIT_RPS`(IP당, 기본 20) · `KAKAO_REST_API_KEY`(지오코딩, 선택) · `RUNART_ETL_LOCAL_ONLY=1`(기존 OSM 속성은 보존하고 로컬 경사도/신호등/가로등만 재반영)

실그래프(`data/seoul_graph.pkl`)가 없으면 **서울 시청 일대 데모 그리드**로 구동된다(전체 파이프라인 동작 확인용). 공모전 제출 전 반드시 ETL 실행:

```bash
pip install -e '.[etl]'
python etl/build_graph.py         # OSM 서울 전역 보행망 -> data/seoul_graph.pkl
RUNART_ETL_LOCAL_ONLY=1 python etl/build_rfs.py
# 로컬 서울시 경사도 + 보행자 신호등 + 가로등 위치 + 공중화장실을 반영
```

현재 스냅숏(`data/snapshot.json`, 2026-07-09): 서울 전역 보행 그래프 163,848 노드 / 232,006 엣지에 경사도 232,006개 엣지, 보행자 신호등 26,769개 포인트 기반 횡단 점수, 가로등 19,316개 포인트 기반 조명 점수를 반영했다. 편의시설은 편의점 6,693개, 화장실 4,985개, 공원 2,237개, 음수대 213개가 포함되어 있다.

## 구조

| 경로 | 역할 (PRD 매핑) |
|---|---|
| `src/runart/server.py` | MCP 툴 6개 + 미리보기/GPX/공유 라우트 (§5.1, §5.6) |
| `src/runart/course.py` | RFS 가중 순환 코스 생성, ±5% 거리 허용 (§5.3) |
| `src/runart/shapes.py` | 동물 모양 템플릿·스냅핑·유사도 게이트 0.7 (§5.4) |
| `src/runart/rfs.py` | 러닝 친화도 점수 — 기본/야간 가중 프로파일 (§5.7) |
| `src/runart/facilities.py` | 코스 100m 반경 편의시설 (§5.5) |
| `src/runart/models.py` | 자기완결형 course_id — stateless (§5.1) |
| `etl/` | 오프라인 데이터 파이프라인 — 런타임 외부 API 호출 없음 (§5.7) |

## 툴 (6개, 모두 read-only·idempotent)

`generate_running_course` · `generate_animal_course` · `list_available_shapes` · `find_facilities_near_course` · `refine_course` · `get_course_status`

## 배포 (PlayMCP in KC)

```bash
docker build -t runnywhere .
docker run -p 8000:8000 -e RUNART_BASE_URL=https://<kc-endpoint> runnywhere
```

MCP Endpoint: `https://<kc-endpoint>/mcp` — PlayMCP 등록 전 [MCP Inspector](https://github.com/modelcontextprotocol/inspector)로 검증할 것.

## 데이터 출처
OpenStreetMap, 서울 열린데이터광장(서울시 경사도 OA-22241, 서울시 가로등 위치 정보 OA-22205, 서울특별시 보행자 신호등 분포도, 서울시 공중화장실 위치정보), SRTM 30m 고도 데이터. 안심이 CCTV 포인트 데이터는 서비스 종료로 직접 사용하지 않고 OSM surveillance 태그를 폴백으로 사용한다. 개인정보는 수집·저장하지 않는다.
