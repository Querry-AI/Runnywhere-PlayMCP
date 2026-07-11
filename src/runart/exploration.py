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
@media(max-width:800px){{.wrap{{padding:0 20px 56px}}header{{height:64px;margin-bottom:40px}}nav{{gap:14px}}nav a:first-child{{display:none}}.product-hero{{grid-template-columns:1fr;gap:38px;margin-bottom:72px}}h1{{font-size:42px}}.feature-row{{grid-template-columns:38px 1fr;gap:10px 14px}}.feature-row p,.feature-row a{{grid-column:2}}}}
@media(max-width:480px){{.wrap{{padding:0 18px 48px}}header{{margin-bottom:34px}}.brand{{font-size:16px}}nav a{{font-size:13px}}h1{{font-size:36px;line-height:1.17}}.lead{{font-size:16px}}.demo{{padding:20px;border-radius:9px}}.route-preview{{height:140px}}.metric b{{font-size:19px}}.section-head h2{{font-size:26px}}}}</style></head><body><div class=\"wrap\">
<header><a class=\"brand\" href=\"/\" style=\"color:inherit;text-decoration:none\">Runnywhere · 러니웨어</a><nav><a href=\"/\">서비스 소개</a><a href=\"/animals\">동물 GPS 아트</a></nav></header>{body}</div></body></html>"""


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
    body = f"""<section class=\"hero\"><div class=\"eyebrow\">러니웨어 동물 GPS 아트</div><h1>검증된 동물 코스를<br>지도에서 찾아보세요.</h1>
<p>러니웨어의 맞춤 러닝 기능 중 GPS 아트를 더 쉽게 찾기 위한 탐험 지도입니다. 서울 역 주변에서 검증된 421개 코스를 동물별로 확인할 수 있어요.</p></section>
<style>#map{{height:58vh;min-height:430px;border:1px solid var(--line);border-radius:10px;overflow:hidden}}.filters{{display:flex;gap:8px;overflow:auto;margin:14px 0}}button{{border:1px solid var(--line);background:white;padding:9px 12px;border-radius:7px;font-weight:650;white-space:nowrap}}button.on{{background:var(--green);border-color:var(--green);color:white}}.dot{{font-size:23px;filter:drop-shadow(0 3px 4px #0004)}}</style>
<div class=\"filters\"><button class=\"on\" data-shape=\"all\">전체</button>{buttons}</div><div id=\"map\"><div class=\"card\" style=\"margin:20px;max-width:440px\"><h2>지도를 불러오는 중이에요</h2><p class=\"muted\">지도 연결이 어려우면 AI에게 현재 역을 말해 주세요. 가장 가까운 동물을 바로 추천해 드려요.</p></div></div>
<p class=\"muted\">마커를 누르면 거리와 출발역을 확인할 수 있어요. 완주 후 AI에게 course_id와 함께 “동물도감에 기록해줘”라고 말해보세요.</p>
<script src=\"//dapi.kakao.com/v2/maps/sdk.js?appkey={html.escape(kakao_key)}\"></script><script>
const items={data};let overlays=[];let map=null;
function draw(shape){{overlays.forEach(o=>o.setMap(null));overlays=[];items.filter(x=>shape==='all'||x.shape===shape).forEach(x=>{{const el=document.createElement('button');el.className='dot';el.textContent=x.emoji;el.title=x.name+' '+x.km+'km';el.onclick=()=>{{location.href='{base_url}/c/'+x.cid}};const o=new kakao.maps.CustomOverlay({{position:new kakao.maps.LatLng(x.lat,x.lon),content:el,yAnchor:.5}});o.setMap(map);overlays.push(o)}})}}
if(window.kakao?.maps){{map=new kakao.maps.Map(document.getElementById('map'),{{center:new kakao.maps.LatLng(37.5665,126.978),level:8}});document.querySelectorAll('[data-shape]').forEach(b=>b.onclick=()=>{{document.querySelectorAll('[data-shape]').forEach(x=>x.classList.remove('on'));b.classList.add('on');draw(b.dataset.shape)}});draw('all')}}else{{document.querySelectorAll('[data-shape]').forEach(b=>b.disabled=true)}}</script>"""
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
    body = f'<section class="hero"><div class="eyebrow">동물 GPS 아트 · Shape Relay {len(courses)}개 코스</div><h1>{spec.emoji} 서로 다른 동네가 그린<br>{spec.name_ko} 코스</h1><p>러니웨어에서 만든 같은 동물 코스를 나란히 비교하는 공유 기능입니다. 서로 다른 서울 도로망이 만든 모양의 차이를 확인해 보세요.</p></section><div class="card" style="margin-bottom:14px"><h2>겹쳐 보기</h2>{collective}<p class="muted">각 색은 한 동네의 코스입니다. 릴레이가 이어질수록 비교할 수 있는 코스가 늘어납니다.</p></div><div class="grid">{cards}</div><div class="card" style="margin-top:14px"><h2>다음 코스 연결하기</h2><p>이 링크를 공유한 뒤 친구가 자기 동네에서 같은 동물 코스를 만들고 AI에게 릴레이 연결을 요청하면 됩니다.</p><code>{base_url}/relay/{html.escape(token)}</code></div>'
    return _page(f"{spec.name_ko} Shape Relay", body)
