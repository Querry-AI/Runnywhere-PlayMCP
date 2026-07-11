"""Stateless animal atlas, passport, and shape-relay presentation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import math
import os
import zlib
from datetime import date

from .animal_presets import all_verified_animal_presets
from .course import Course
from .geo import haversine_m
from .models import decode_course_id, encode_course_id
from .shapes import SHAPES

PASSPORT_VERSION = 1
RELAY_VERSION = 1
MAX_PASSPORT_RUNS = 40
MAX_RELAY_LEGS = 8


def _token_secret() -> bytes:
    secret = os.environ.get("RUNART_TOKEN_SECRET")
    if secret:
        if len(secret) < 32:
            raise RuntimeError("RUNART_TOKEN_SECRET must be at least 32 characters")
        return secret.encode()
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return b"runart-test-only-token-secret-32-bytes"
    raise RuntimeError("RUNART_TOKEN_SECRET is required for passport and relay tokens")


def _encode(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    payload = base64.urlsafe_b64encode(zlib.compress(raw.encode(), 9)).decode().rstrip("=")
    secret = _token_secret()
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _decode(token: str, version: int) -> dict:
    if not token:
        return {"v": version}
    if len(token) > 12_000:
        raise ValueError("token too large")
    payload, separator, signature = token.rpartition(".")
    if not separator:
        raise ValueError("unsigned token")
    secret = _token_secret()
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("token signature")
    padded = payload + "=" * (-len(payload) % 4)
    inflater = zlib.decompressobj()
    raw = inflater.decompress(base64.urlsafe_b64decode(padded), 64_001)
    if len(raw) > 64_000 or inflater.unconsumed_tail:
        raise ValueError("token too large")
    raw += inflater.flush(64_001 - len(raw))
    if len(raw) > 64_000:
        raise ValueError("token too large")
    data = json.loads(raw.decode())
    if data.get("v") != version:
        raise ValueError("token version")
    return data


def decode_passport(token: str | None) -> dict:
    if token and "/passport/" in token:
        token = token.rsplit("/passport/", 1)[1].split("?", 1)[0]
    data = _decode(token or "", PASSPORT_VERSION)
    runs = data.get("runs", [])
    if (not isinstance(runs, list) or len(runs) > MAX_PASSPORT_RUNS
            or any(not isinstance(run, dict) for run in runs)):
        raise ValueError("runs")
    data["runs"] = runs[-MAX_PASSPORT_RUNS:]
    return data


def record_run(course_id: str, passport_token: str | None) -> tuple[str, dict]:
    params = decode_course_id(course_id)
    if params.shape not in SHAPES:
        raise ValueError("animal course required")
    data = decode_passport(passport_token)
    runs = [run for run in data["runs"] if run.get("c") != course_id]
    runs.append({"c": course_id, "s": params.shape,
                 "n": params.location_name[:30], "d": round(params.distance_km, 1)})
    data = {"v": PASSPORT_VERSION, "runs": runs[-MAX_PASSPORT_RUNS:]}
    return _encode(data), passport_summary(data)


def passport_summary(data: dict) -> dict:
    runs = data.get("runs", [])
    shapes = {run.get("s") for run in runs if run.get("s") in SHAPES}
    by_place: dict[str, set] = {}
    for run in runs:
        if run.get("s") in SHAPES and run.get("n"):
            by_place.setdefault(run["n"], set()).add(run["s"])
    badges = [f"{name} 동물 마스터" for name, found in by_place.items()
              if len(found) == len(SHAPES)]
    if len(shapes) == len(SHAPES):
        badges.insert(0, "서울 동물도감 완성")
    return {"runs": len(runs), "shapes": sorted(shapes), "badges": badges}


def weekly_recommendation(lat: float, lon: float,
                          passport_token: str | None) -> Course | None:
    passport = decode_passport(passport_token)
    completed = {run.get("c") for run in passport["runs"]}
    discovered_shapes = {run.get("s") for run in passport["runs"]}
    candidates = [course for course in all_verified_animal_presets()
                  if encode_course_id(course.params) not in completed
                  and course.params.shape not in discovered_shapes]
    if not candidates:  # after all four species, recommend a new neighborhood
        candidates = [course for course in all_verified_animal_presets()
                      if encode_course_id(course.params) not in completed]
    if not candidates:
        return None
    week = date.today().isocalendar().week
    # Distance dominates; the small weekly offset rotates equally close finds.
    return min(candidates, key=lambda course: (
        round(haversine_m(lat, lon, course.params.lat, course.params.lon) / 250),
        (int(hashlib.sha256(encode_course_id(course.params).encode()).hexdigest()[:8], 16)
         + week) % 97,
        -float(course.shape_similarity or 0),
    ))


def create_relay(course_id: str, relay_token: str | None) -> tuple[str, dict]:
    params = decode_course_id(course_id)
    if params.shape not in SHAPES:
        raise ValueError("animal course required")
    if relay_token and "/relay/" in relay_token:
        relay_token = relay_token.rsplit("/relay/", 1)[1].split("?", 1)[0]
    data = _decode(relay_token or "", RELAY_VERSION)
    legs = data.get("legs", [])
    shape = data.get("shape") or params.shape
    if shape != params.shape:
        raise ValueError("same shape required")
    legs = [cid for cid in legs if cid != course_id]
    legs.append(course_id)
    data = {"v": RELAY_VERSION, "shape": shape, "legs": legs[-MAX_RELAY_LEGS:]}
    return _encode(data), data


def decode_relay(token: str) -> dict:
    if "/relay/" in token:
        token = token.rsplit("/relay/", 1)[1].split("?", 1)[0]
    data = _decode(token, RELAY_VERSION)
    legs = data.get("legs")
    if (data.get("shape") not in SHAPES or not isinstance(legs, list)
            or not 1 <= len(legs) <= MAX_RELAY_LEGS
            or any(not isinstance(cid, str) or len(cid) > 4096 for cid in legs)):
        raise ValueError("relay")
    return data


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{html.escape(title)}</title>
<style>:root{{--ink:#151b1e;--muted:#677176;--line:#e4e8e9;--paper:#fff;--card:#fff;--green:#08735a;--soft:#edf6f2;--navy:#17333d;--warm:#f6f5f1}}
*{{box-sizing:border-box}}html{{font-size:16px}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Pretendard Variable',Pretendard,'Noto Sans KR','Apple SD Gothic Neo',sans-serif;letter-spacing:-.012em;text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1160px;margin:auto;padding:0 32px 80px}}header{{height:76px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);margin-bottom:64px}}
.brand{{font-weight:760;letter-spacing:-.035em;font-size:18px}}.brand:before{{content:'';display:inline-block;width:10px;height:10px;border:3px solid var(--green);border-radius:50%;margin-right:9px;vertical-align:1px}}nav{{display:flex;gap:28px}}nav a{{color:#535d62;text-decoration:none;font-size:14px;font-weight:520}}nav a:hover{{color:var(--green)}}
.eyebrow{{color:var(--green);font-weight:680;font-size:13px;letter-spacing:.01em;margin-bottom:14px}}.pill{{display:inline-block;background:var(--soft);color:var(--green);padding:5px 9px;border-radius:5px;font-weight:650;font-size:12px}}
.hero{{background:white;border:0;border-radius:0;padding:0;margin-bottom:56px}}h1{{font-size:clamp(38px,5vw,60px);line-height:1.13;letter-spacing:-.055em;margin:0 0 24px;font-weight:720}}h2{{letter-spacing:-.035em;font-weight:680}}p{{color:inherit;font-size:16px;line-height:1.76;margin:9px 0}}.muted{{color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:24px}}
a.btn{{display:inline-block;background:var(--green);color:white;text-decoration:none;padding:11px 16px;border-radius:5px;font-weight:650;font-size:14px}}.emoji{{font-size:30px}}
.product-hero{{display:grid;grid-template-columns:minmax(0,1.04fr) minmax(360px,.96fr);gap:72px;align-items:center;margin-bottom:96px}}.lead{{font-size:18px;line-height:1.76;max-width:620px;color:#455056}}.data-note{{font-size:14px;color:#7a8488;max-width:610px}}
.demo{{background:var(--navy);color:white;border-radius:12px;padding:28px;box-shadow:0 22px 60px #17333d1a}}.demo-label{{font-size:12px;color:#a9c0c6;margin-bottom:9px}}.prompt{{background:white;color:var(--ink);border-radius:7px;padding:15px 16px;font-size:15px;font-weight:600}}.route-preview{{height:178px;margin:24px 0 18px}}.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;border-top:1px solid #ffffff26;padding-top:18px}}.metric b{{display:block;font-size:22px;letter-spacing:-.04em}}.metric span{{font-size:11px;color:#a9c0c6}}
.section-head{{display:flex;justify-content:space-between;align-items:end;margin:0 0 22px;padding-top:48px;border-top:1px solid var(--line)}}.section-head h2{{font-size:30px;margin:0}}.feature-list{{border-top:1px solid var(--line)}}.feature-row{{display:grid;grid-template-columns:56px 220px 1fr auto;gap:20px;align-items:center;padding:26px 0;border-bottom:1px solid var(--line)}}.feature-no{{font-size:12px;color:var(--green);font-weight:700}}.feature-row h3{{font-size:18px;margin:0}}.feature-row p{{font-size:15px;color:var(--muted);margin:0}}.text-link{{color:var(--green);font-weight:650;text-decoration:none;font-size:14px;white-space:nowrap}}
code{{overflow-wrap:anywhere;font-size:12px;color:var(--muted)}}
.legal-footer{{margin-top:72px;padding-top:22px;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 18px;color:var(--muted);font-size:12px}}.legal-footer a{{color:inherit;text-decoration:none}}.legal-footer a:hover{{color:var(--green)}}
.legal-copy{{max-width:760px}}.legal-copy h2{{margin-top:38px}}.legal-copy ul{{line-height:1.75;padding-left:20px}}.legal-copy a{{color:var(--green)}}
@media(max-width:800px){{.wrap{{padding:0 20px 56px}}header{{height:64px;margin-bottom:40px}}nav{{gap:14px}}nav a:first-child{{display:none}}.product-hero{{grid-template-columns:1fr;gap:38px;margin-bottom:72px}}h1{{font-size:42px}}.feature-row{{grid-template-columns:38px 1fr;gap:10px 14px}}.feature-row p,.feature-row a{{grid-column:2}}}}
@media(max-width:480px){{.wrap{{padding:0 18px 48px}}header{{margin-bottom:34px}}.brand{{font-size:16px}}nav a{{font-size:13px}}h1{{font-size:36px;line-height:1.17}}.lead{{font-size:16px}}.demo{{padding:20px;border-radius:9px}}.route-preview{{height:140px}}.metric b{{font-size:19px}}.section-head h2{{font-size:26px}}}}</style></head><body><div class=\"wrap\">
<header><a class=\"brand\" href=\"/\" style=\"color:inherit;text-decoration:none\">Runnywhere · 러니웨어</a><nav><a href=\"/\">서비스 소개</a><a href=\"/animals\">동물 GPS 아트</a></nav></header>{body}
<footer class=\"legal-footer\"><span>© 2026 Runnywhere</span><a href=\"/terms\">이용약관</a><a href=\"/privacy\">개인정보</a><a href=\"/data-licenses\">데이터·라이선스</a><a href=\"https://www.openstreetmap.org/copyright\">© OpenStreetMap contributors</a></footer>
</div></body></html>"""


def legal_html(kind: str, contact: str = "") -> str:
    """Compact legal pages linked from the global footer, outside MCP output."""
    contact_html = (f'<a href="mailto:{html.escape(contact, quote=True)}">'
                    f'{html.escape(contact)}</a>' if contact else
                    '서비스가 제공되는 채널의 운영자')
    if kind == "privacy":
        title = "개인정보 처리 안내"
        body = f"""<main class="legal-copy"><div class="eyebrow">PRIVACY</div><h1>{title}</h1>
<p>시행일: 2026-07-11</p><p>러니웨어는 계정과 서버 사이드 완주 데이터베이스를 운영하지 않습니다.</p>
<h2>처리되는 정보</h2><ul><li>입력한 장소·주소는 코스 생성에 사용됩니다. Kakao Local API가 활성화된 경우 주소 검색어가 Kakao에 전송됩니다.</li><li>지도 페이지를 열면 Kakao Maps가 IP, 브라우저 정보, referrer를 처리할 수 있습니다.</li><li>브라우저 GPS는 사용자 동의 후 현재 위치를 표시하는 데만 쓰이며 러니웨어 서버로 전송하지 않습니다.</li><li>IP는 남용 방지를 위한 메모리 내 rate limit에 일시 사용될 수 있으며, 호스팅 제공자의 보안 로그 정책은 별도로 적용됩니다.</li></ul>
<h2>자기완결형 토큰</h2><p>course_id, passport_token, relay_token은 링크 안에 코스 조건·출발지·완주 목록을 담을 수 있습니다. 서버에 계정 기록으로 저장되지 않지만 링크를 받은 사람은 내용을 볼 수 있으므로 공개 공유에 주의하세요.</p>
<h2>보유·삭제·문의</h2><p>서비스는 사용자 계정이나 위치 기록을 지속 보유하지 않습니다. 토큰 기록을 없애려면 보관한 링크를 삭제하세요. 문의: {contact_html}</p></main>"""
    elif kind == "terms":
        title = "이용약관·안전 안내"
        body = f"""<main class="legal-copy"><div class="eyebrow">TERMS & SAFETY</div><h1>{title}</h1><p>시행일: 2026-07-11</p>
<h2>서비스의 범위</h2><p>러니웨어는 공개·사전 계산 데이터를 기반으로 러닝 코스 후보를 제공하는 참고용 서비스입니다. 실시간 내비게이션, 응급, 의료 또는 안전 보증 서비스가 아닙니다.</p>
<h2>사용자 안전</h2><ul><li>출발 전 현장의 통행 가능 여부, 공사·통제·사유지·계단·날씨·조명을 직접 확인하세요.</li><li>교통법규와 시설 안내를 우선하고, 화면보다 주변 환경에 주의하세요.</li><li>건강 상태와 운동 경험에 맞게 이용하고, 위험하거나 이상 증상이 있으면 즉시 중단하세요. 긴급 상황에서는 112 또는 119를 이용하세요.</li></ul>
<h2>데이터·제3자 서비스</h2><p>경로와 점수는 불완전하거나 늦게 갱신될 수 있습니다. Kakao Maps·Local API는 Kakao 약관과 쿼터의 적용을 받습니다. 데이터 출처와 이용조건은 <a href="/data-licenses">데이터·라이선스</a>에서 확인할 수 있습니다.</p>
<h2>책임·변경</h2><p>법령이 허용하는 범위에서 서비스는 특정 목적 적합성과 코스의 절대적 안전을 보증하지 않습니다. 중요한 변경은 이 페이지의 시행일을 갱신합니다. 문의: {contact_html}</p></main>"""
    elif kind == "licenses":
        title = "데이터·라이선스"
        body = """<main class="legal-copy"><div class="eyebrow">OPEN DATA</div><h1>데이터·라이선스</h1>
<p>코드는 MIT License, OpenStreetMap 파생 데이터베이스는 ODbL 1.0, 해당 서울시 공공데이터는 공공누리 1유형 조건으로 사용합니다.</p>
<h2>주요 출처</h2><ul><li><a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors · ODbL 1.0</a></li><li><a href="https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do">서울시 경사도 OA-22241</a></li><li><a href="https://data.seoul.go.kr/dataList/OA-22205/F/1/datasetView.do">서울시 가로등 OA-22205</a></li><li><a href="https://data.seoul.go.kr/dataList/OA-22356/F/1/datasetView.do">서울시 보행자 신호등 OA-22356</a></li><li><a href="https://data.seoul.go.kr/dataList/OA-22586/S/1/datasetView.do">서울시 공중화장실 OA-22586</a></li><li><a href="https://www.data.go.kr/data/15044231/fileData.do">서울교통공사 역주소</a> · <a href="https://www.data.go.kr/data/15099316/fileData.do">1–8호선 역 좌표</a></li><li><a href="https://www.earthdata.nasa.gov/data/instruments/srtm">NASA/JPL-Caltech/NGA SRTM</a></li></ul>
<h2>가공 및 제공</h2><p>원천 데이터는 좌표 변환·필터링·결합·점수화되었으며 공식 실시간 자료가 아닙니다. OSM 파생 DB와 재생성 코드는 공개 소스 배포본의 <code>data/</code>, <code>etl/</code>, <code>scripts/build_animal_presets.py</code>에서 ODbL 조건으로 제공됩니다. 전체 고지는 배포본의 <code>DATA_LICENSES.md</code>를 참고하세요.</p><p class="muted">출처 표시는 각 기관의 후원이나 인증을 의미하지 않습니다.</p></main>"""
    else:
        raise ValueError("unknown legal page")
    return _page(title, body)


def home_html(base_url: str) -> str:
    body = f"""<section class=\"product-hero\"><div><div class=\"eyebrow\">서울 러닝 코스 서비스</div>
<h1>어디서든,<br>나에게 맞는<br>러닝 코스를.</h1>
<p class=\"lead\">러니웨어는 장소만 입력하면 거리별 맞춤 코스부터 동물 모양 GPS 아트 코스, 안전한 야간 러닝 코스까지 만들어주는 서울 러닝 코스 서비스입니다.</p>
<p class=\"data-note\">서울시 경사도·보행자 신호등·가로등·공중화장실·편의점 데이터를 바탕으로, 초보 러너도 더 쉽고 안전하게 달리기 시작할 수 있도록 돕습니다.</p></div>
<div class=\"demo\" aria-label=\"러니웨어 코스 생성 예시\"><div class=\"demo-label\">AI에게 장소와 원하는 조건을 말해보세요</div><div class=\"prompt\">시청에서 평지 위주 5km 코스 만들어줘</div>
<svg class=\"route-preview\" viewBox=\"0 0 420 190\" role=\"img\" aria-label=\"시청 주변 러닝 코스 예시\"><path d=\"M38 145 C67 122 84 76 122 69 S184 101 218 72 278 43 316 66 365 119 385 139\" fill=\"none\" stroke=\"#87e0bd\" stroke-width=\"8\" stroke-linecap=\"round\"/><circle cx=\"38\" cy=\"145\" r=\"9\" fill=\"#fff\"/><circle cx=\"38\" cy=\"145\" r=\"4\" fill=\"#08735a\"/><path d=\"M56 155 L115 166 M268 144 L352 151 M105 42 L177 30\" stroke=\"#ffffff20\" stroke-width=\"2\"/></svg>
<div class=\"metrics\"><div class=\"metric\"><b>5.1km</b><span>실거리</span></div><div class=\"metric\"><b>34분</b><span>예상 시간</span></div><div class=\"metric\"><b>82</b><span>러닝 친화도</span></div></div></div></section>
<section><div class=\"section-head\"><div><div class=\"eyebrow\">WHAT YOU CAN DO</div><h2>달리기 전에 필요한 것만.</h2></div></div><div class=\"feature-list\">
<article class=\"feature-row\"><span class=\"feature-no\">01</span><h3>거리별 맞춤 코스</h3><p>출발 장소와 거리만 말하면 실제 보행 도로를 따라 순환 코스를 만듭니다.</p></article>
<article class=\"feature-row\"><span class=\"feature-no\">02</span><h3>안전한 야간 러닝</h3><p>가로등과 보행 환경을 반영해 밤에도 더 안심되는 길을 우선합니다.</p></article>
<article class=\"feature-row\"><span class=\"feature-no\">03</span><h3>동물 GPS 아트</h3><p>강아지·고양이·토끼·고래 모양을 서울 도로 위에 또렷하게 그립니다.</p><a class=\"text-link\" href=\"{base_url}/animals\">검증 코스 보기 →</a></article>
</div></section>"""
    return _page("러니웨어 · 서울 러닝 코스", body)


def atlas_html(base_url: str, kakao_key: str) -> str:
    courses = all_verified_animal_presets()
    items = [{"lat": c.params.lat, "lon": c.params.lon, "shape": c.params.shape,
              "emoji": SHAPES[c.params.shape].emoji, "name": c.params.location_name,
              "km": round(c.length_km, 1), "cid": encode_course_id(c.params)} for c in courses]
    data = json.dumps(items, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    buttons = "".join(f'<button data-shape="{k}">{s.emoji} {s.name_ko}</button>' for k, s in SHAPES.items())
    js_base = json.dumps(base_url, ensure_ascii=False)
    safe_key = html.escape(kakao_key, quote=True)
    map_sdk = (
        f'<script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey={safe_key}&autoload=false"></script>'
        if safe_key else ""
    )
    stations = sorted({c.params.location_name for c in courses if c.params.location_name})
    station_options = "".join(f'<option value="{html.escape(name)}">' for name in stations)
    body = f"""<section class=\"hero\"><div class=\"eyebrow\">러니웨어 동물 GPS 아트</div><h1>검증된 동물 코스를<br>지도에서 찾아보세요.</h1>
<p>러니웨어의 맞춤 러닝 기능 중 GPS 아트를 더 쉽게 찾기 위한 탐험 지도입니다. 서울 역 주변에서 검증된 {len(courses)}개 코스를 동물별·역별로 확인할 수 있어요. 마커를 누르면 코스 모양이 지도 위에 그려집니다.</p></section>
<style>#map{{height:58vh;min-height:430px;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#edf1ec}}.map-message{{height:100%;display:flex;align-items:center;justify-content:center;padding:22px}}.map-message .card{{max-width:460px;margin:0}}.filters{{display:flex;gap:8px;overflow:auto;margin:14px 0 8px}}button{{border:1px solid var(--line);background:white;padding:9px 12px;border-radius:7px;font-weight:650;white-space:nowrap}}button.on{{background:var(--green);border-color:var(--green);color:white}}button:disabled{{opacity:.45}}.dot{{font-size:23px;filter:drop-shadow(0 3px 4px #0004);cursor:pointer;background:none;border:0;padding:0}}
.station-filter{{display:flex;gap:8px;margin:0 0 14px}}.station-filter input{{flex:1;max-width:340px;border:1px solid var(--line);border-radius:7px;padding:9px 12px;font-size:14px;font-family:inherit}}
.atlas-pop{{background:#fff;border:1px solid var(--line);border-radius:9px;box-shadow:0 8px 26px #0003;padding:10px 12px;min-width:190px}}.atlas-pop b{{display:block;font-size:14px;margin-bottom:3px}}.atlas-pop span{{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}}.atlas-pop a{{display:block;color:var(--green);font-weight:650;font-size:13px;text-decoration:none;margin-bottom:7px}}.atlas-pop .share-btn{{width:100%;background:var(--green);border-color:var(--green);color:#fff;font-size:13px;padding:8px 10px;cursor:pointer}}</style>
<div class=\"filters\"><button class=\"on\" data-shape=\"all\">전체</button>{buttons}</div>
<div class=\"station-filter\"><input id=\"stationInput\" list=\"stationList\" placeholder=\"역 이름으로 찾기 (예: 강남역)\" autocomplete=\"off\"><datalist id=\"stationList\">{station_options}</datalist><button id=\"stationClear\" type=\"button\">지우기</button></div>
<div id=\"map\"><div class=\"map-message\"><div class=\"card\"><h2>지도를 불러오는 중이에요</h2><p class=\"muted\">지도 연결이 어려우면 AI에게 현재 역을 말해 주세요. 가장 가까운 동물을 바로 추천해 드려요.</p></div></div></div>
<p class=\"muted\">마커를 누르면 코스 모양과 거리·출발역이 지도에 표시돼요. 완주 후 AI에게 course_id와 함께 “동물도감에 기록해줘”라고 말해보세요. · <a href=\"https://www.openstreetmap.org/copyright\">경로 데이터 © OpenStreetMap contributors · ODbL</a></p>
{map_sdk}<script>
const items={data};const baseUrl={js_base};let overlays=[];let map=null;let routeLines=[];let infoOv=null;
let shapeFilter='all';let stationFilter='';
const mapNode=document.getElementById('map');
function showMapError(message){{mapNode.innerHTML='<div class="map-message"><div class="card"><h2>카카오맵을 불러오지 못했습니다</h2><p class="muted">'+message+'</p></div></div>';document.querySelectorAll('[data-shape]').forEach(b=>b.disabled=true)}}
function clearCourse(){{routeLines.forEach(l=>l.setMap(null));routeLines=[];if(infoOv){{infoOv.setMap(null);infoOv=null}}}}
const ROUTE_COLORS=['#08735a','#f06d3b','#5677d8','#9c5cc8'];
function coursePopup(x){{
 const el=document.createElement('div');el.className='atlas-pop';
 const t=document.createElement('b');t.textContent=x.emoji+' '+x.name;
 const s=document.createElement('span');s.textContent=x.km+'km 검증 코스';
 const a=document.createElement('a');a.href=baseUrl+'/c/'+x.cid;a.textContent='코스 상세·GPX 보기 →';
 const btn=document.createElement('button');btn.className='share-btn';btn.type='button';btn.textContent='친구에게 공유하기';
 btn.onclick=ev=>{{ev.stopPropagation();const url=baseUrl+'/c/'+x.cid;
  const done=()=>{{btn.textContent='링크가 복사됐어요!';setTimeout(()=>btn.textContent='친구에게 공유하기',2200)}};
  if(navigator.clipboard&&navigator.clipboard.writeText){{navigator.clipboard.writeText(url).then(done).catch(()=>window.prompt('아래 링크를 복사하세요',url))}}
  else{{window.prompt('아래 링크를 복사하세요',url)}}
 }};
 el.append(t,s,a,btn);
 infoOv=new kakao.maps.CustomOverlay({{position:new kakao.maps.LatLng(x.lat,x.lon),content:el,yAnchor:1.25,zIndex:40}});
 infoOv.setMap(map);
}}
function showCourses(list){{clearCourse();
 const picks=list.slice(0,4);
 Promise.all(picks.map(x=>fetch(baseUrl+'/c/'+encodeURIComponent(x.cid)+'/route.json')
   .then(r=>r.ok?r.json():null).catch(()=>null).then(d=>({{x,d}}))))
 .then(results=>{{
  const bounds=new kakao.maps.LatLngBounds();let drawn=null;
  results.forEach((r,i)=>{{if(!r.d)return;
   const path=r.d.points.map(p=>new kakao.maps.LatLng(p[0],p[1]));
   routeLines.push(new kakao.maps.Polyline({{map,path,strokeColor:'#ffffff',strokeWeight:11,strokeOpacity:.9}}));
   routeLines.push(new kakao.maps.Polyline({{map,path,strokeColor:ROUTE_COLORS[i%ROUTE_COLORS.length],strokeWeight:6,strokeOpacity:.95}}));
   path.forEach(p=>bounds.extend(p));if(!drawn)drawn=r.x;
  }});
  if(drawn){{map.setBounds(bounds,70,70,70,70);coursePopup(drawn)}}
  else if(picks.length===1){{location.href=baseUrl+'/c/'+picks[0].cid}}
 }});
}}
function visibleItems(){{return items.filter(x=>(shapeFilter==='all'||x.shape===shapeFilter)&&(!stationFilter||x.name.includes(stationFilter)))}}
function draw(){{overlays.forEach(o=>o.setMap(null));overlays=[];clearCourse();
 const vis=visibleItems();
 vis.forEach(x=>{{const el=document.createElement('button');el.className='dot';el.textContent=x.emoji;el.title=x.name+' '+x.km+'km';el.onclick=()=>showCourses([x]);const o=new kakao.maps.CustomOverlay({{position:new kakao.maps.LatLng(x.lat,x.lon),content:el,yAnchor:.5}});o.setMap(map);overlays.push(o)}});
 if(stationFilter&&vis.length){{showCourses(vis)}}
}}
function bootAtlasMap(){{map=new kakao.maps.Map(mapNode,{{center:new kakao.maps.LatLng(37.5665,126.978),level:8}});map.addControl(new kakao.maps.ZoomControl(),kakao.maps.ControlPosition.LEFT);
 document.querySelectorAll('[data-shape]').forEach(b=>b.onclick=()=>{{document.querySelectorAll('[data-shape]').forEach(x=>x.classList.remove('on'));b.classList.add('on');shapeFilter=b.dataset.shape;draw()}});
 const input=document.getElementById('stationInput');
 let timer=null;
 input.addEventListener('input',()=>{{clearTimeout(timer);timer=setTimeout(()=>{{stationFilter=input.value.trim();draw()}},250)}});
 document.getElementById('stationClear').onclick=()=>{{input.value='';stationFilter='';draw();map.setCenter(new kakao.maps.LatLng(37.5665,126.978));map.setLevel(8)}};
 draw()}}
if(!"{safe_key}"){{showMapError('운영 환경의 KAKAO_JAVASCRIPT_KEY가 설정되어야 합니다.')}}else if(!window.kakao?.maps){{showMapError('KAKAO_JAVASCRIPT_KEY와 카카오 개발자 콘솔의 등록 도메인을 확인해 주세요.')}}else{{kakao.maps.load(bootAtlasMap)}}</script>"""
    return _page("서울 동물지도", body)


def passport_html(token: str, base_url: str) -> str:
    data = decode_passport(token)
    summary = passport_summary(data)
    found = set(summary["shapes"])
    cards = "".join(f'<div class="card"><div class="emoji">{s.emoji}</div><b>{s.name_ko}</b><p class="muted">{"발견 완료" if k in found else "아직 미발견"}</p></div>' for k, s in SHAPES.items())
    badges = "".join(f'<span class="pill">🏅 {html.escape(b)}</span> ' for b in summary["badges"]) or '<span class="muted">4종을 완주하면 첫 배지가 열려요.</span>'
    body = f'<section class="hero"><div class="eyebrow">동물 GPS 아트 · 완주 기록</div><h1>나의 동물 코스 기록</h1><p>러니웨어로 달린 동물 코스를 한곳에서 확인하는 보조 기록장입니다. 기록은 서버에 저장되지 않고 변조 감지 서명과 함께 링크 안에 담겨요.</p><p class="muted">이 링크를 가진 사람은 완주한 코스와 출발역을 볼 수 있으니 공개 공유에 주의하세요.</p></section><div class="grid">{cards}</div><div class="card" style="margin-top:14px"><h2>완주 배지</h2>{badges}<p><a class="btn" href="{base_url}/animals">다른 동물 코스 찾기</a></p></div>'
    return _page("나의 동물도감", body)


def _route_svg(course: Course, color: str = "#176b45") -> str:
    points = course.points
    if len(points) < 2:
        return ""
    lats = [p[0] for p in points]; lons = [p[1] for p in points]
    lat_span = max(max(lats) - min(lats), 1e-6)
    lon_span = max(max(lons) - min(lons), 1e-6)
    coords = " ".join(
        f"{12 + (lon-min(lons))/lon_span*216:.1f},{12 + (max(lats)-lat)/lat_span*136:.1f}"
        for lat, lon in points[::max(1, len(points)//300)])
    return (f'<svg viewBox="0 0 240 160" role="img" aria-label="동물 코스 실루엣" '
            f'style="width:100%;background:#f5f7f2;border-radius:15px">'
            f'<polyline points="{coords}" fill="none" stroke="{color}" '
            'stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def relay_html(token: str, courses: list[Course], base_url: str) -> str:
    data = decode_relay(token); spec = SHAPES[data["shape"]]
    colors = ("#176b45", "#f06d3b", "#5677d8", "#9c5cc8", "#d59a12", "#178b8b", "#d34b72", "#65702d")
    cards = "".join(f'<div class="card"><div class="emoji">{spec.emoji}</div><h2>{idx+1}. {html.escape(c.params.location_name)}</h2>{_route_svg(c, colors[idx % len(colors)])}<p>{c.length_km:.1f}km · 모양 유사도 {float(c.shape_similarity or 0):.2f}</p><a class="btn" href="{base_url}/c/{encode_course_id(c.params)}">코스 보기</a></div>' for idx, c in enumerate(courses))
    layers = "".join(_route_svg(c, colors[idx % len(colors)]).replace('<svg viewBox="0 0 240 160" role="img" aria-label="동물 코스 실루엣" style="width:100%;background:#f5f7f2;border-radius:15px">', '').replace('</svg>', '') for idx, c in enumerate(courses))
    collective = f'<svg viewBox="0 0 240 160" style="width:100%;max-height:360px;background:#17211b;border-radius:18px">{layers}</svg>'
    body = f'<section class="hero"><div class="eyebrow">동물 GPS 아트 · Shape Relay {len(courses)}개 코스</div><h1>{spec.emoji} 서로 다른 동네가 그린<br>{spec.name_ko} 코스</h1><p>러니웨어에서 만든 같은 동물 코스를 나란히 비교하는 공유 기능입니다. 서로 다른 서울 도로망이 만든 모양의 차이를 확인해 보세요.</p></section><div class="card" style="margin-bottom:14px"><h2>겹쳐 보기</h2>{collective}<p class="muted">각 색은 한 동네의 코스입니다. 릴레이가 이어질수록 비교할 수 있는 코스가 늘어납니다. · <a href="https://www.openstreetmap.org/copyright">경로 데이터 © OpenStreetMap contributors · ODbL</a></p></div><div class="grid">{cards}</div><div class="card" style="margin-top:14px"><h2>다음 코스 연결하기</h2><p>이 링크를 공유한 뒤 친구가 자기 동네에서 같은 동물 코스를 만들고 AI에게 릴레이 연결을 요청하면 됩니다.</p><code>{base_url}/relay/{html.escape(token)}</code></div>'
    return _page(f"{spec.name_ko} Shape Relay", body)
