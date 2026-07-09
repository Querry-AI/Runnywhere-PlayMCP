"""Course result formatting (PRD §5.2: refined markdown, minimal size), the
preview web page with RFS heatmap + elevation profile (PRD §5.6), and the
SVG share card (PRD §2.2 — the spread loop). All user-visible free text from
data sources is escaped before rendering (PRD §8)."""

import html
import json
import math

from . import graph as graphmod
from .course import Course, smooth_series
from .facilities import LABELS_KO
from .geo import haversine_m, to_xy
from .infrastructure import infra_count_along
from .models import encode_course_id, encode_shape_token
from .rfs import COMPONENT_LABELS_KO, edge_rfs
from .shapes import SHAPES

PREVIEW_FACILITY_TYPES = {"convenience_store", "restroom"}


def course_markdown(course: Course, base_url: str, facilities: list[dict]) -> str:
    p = course.params
    cid = encode_course_id(p)
    shape = SHAPES.get(p.shape) if p.shape else None
    title = (
        f"{shape.emoji} {shape.name_ko} 모양 {course.length_km:.1f}km 코스"
        if shape else f"🏃 {course.length_km:.1f}km 러닝 코스"
    )
    where = f" ({p.location_name} 출발·도착)" if p.location_name else ""
    lo, hi = course.duration_range_min
    lines = [
        f"{title}{where}",
        f"- 누적 오르막 {course.ascent_m:.0f}m ({course.grade_label}) · 예상 {lo}~{hi}분 (6:30/km 기준)",
        f"- 러닝 친화도 {course.rfs['score']}/100 (서울 전체 상위 {course.rfs.get('top_percent', 50)}%)"
        + (" — " + " · ".join(course.rfs["highlights"]) if course.rfs["highlights"] else ""),
    ]
    if course.shape_similarity is not None:
        lines.append(f"- 모양 완성도 {course.shape_similarity:.0%}")
    if facilities:
        f_str = ", ".join(
            f"{LABELS_KO[f['type']]}({f['at_km']:g}km)" for f in facilities[:4]
        )
        lines.append(f"- 경유: {f_str}")
    if p.need_facilities:
        found_types = {f["type"] for f in facilities}
        missing = [LABELS_KO[t] for t in p.need_facilities
                   if t in LABELS_KO and t not in found_types]
        if missing:
            lines.append(f"- 요청 시설 중 {', '.join(missing)}은 100m 반경 후보에서 찾지 못했어요")
    if p.night_mode:
        lines.append("- 🌙 야간 안전 모드: 조명·안심 CCTV가 좋은 길 위주예요")
    lines.append(f"- 🗺️ 지도 보기: {base_url}/c/{cid}  ⬇️ GPX: {base_url}/c/{cid}.gpx")
    if shape:
        token = encode_shape_token(p.shape, p.distance_km)
        lines.append(f"- {shape.emoji} 모양 공유 링크: {base_url}/s/{token}")
    return "\n".join(lines)


# ---------- preview page data ----------

def _segments_with_rfs(course: Course) -> list:
    """[[lat1, lon1, lat2, lon2, rfs_0to1], ...] for the heatmap polyline."""
    g = graphmod.get_graph()
    p = course.params
    out = []
    for u, v in zip(course.path, course.path[1:]):
        a, b = g.nodes[u], g.nodes[v]
        score = edge_rfs(g.edges[u, v], p.night_mode, p.include_hills)
        out.append([round(a["lat"], 6), round(a["lon"], 6),
                    round(b["lat"], 6), round(b["lon"], 6), round(score, 2)])
    return out


def _elevation_profile(course: Course) -> list:
    """[(cumulative_km, elev_m), ...] — empty when the graph has no elevation."""
    g = graphmod.get_graph()
    out = []
    cum = 0.0
    prev = None
    for n in course.path:
        d = g.nodes[n]
        if prev is not None:
            cum += haversine_m(prev["lat"], prev["lon"], d["lat"], d["lon"]) / 1000.0
        if d.get("elev") is not None:
            out.append((round(cum, 3), d["elev"]))
        prev = d
    if len(out) < max(3, len(course.path) // 2):
        return []
    sm = smooth_series([e for _, e in out])
    return [(k, round(e, 1)) for (k, _), e in zip(out, sm)]


def _profile_svg(profile: list) -> str:
    if not profile:
        return ""
    w, h, pad = 640, 120, 8
    kms = [p[0] for p in profile]
    els = [p[1] for p in profile]
    kmax = kms[-1] or 1.0
    lo, hi = min(els), max(els)
    span = max(hi - lo, 6.0)  # keep a flat course visually flat
    pts = " ".join(
        f"{pad + (w - 2 * pad) * k / kmax:.1f},{h - pad - (h - 2 * pad) * (e - lo) / span:.1f}"
        for k, e in profile
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;background:#f7f7f7;'
        f'border-radius:10px" aria-label="고도 프로파일">'
        f'<polyline points="{pts}" fill="none" stroke="#0a7d43" stroke-width="2"/>'
        f'<text x="{pad}" y="14" font-size="11" fill="#666">고도 {lo:.0f}~{hi:.0f}m</text>'
        f"</svg>"
    )


def _km_markers(course: Course) -> list[dict]:
    markers = []
    target = 1.0
    cum = 0.0
    prev = None
    for lat, lon in course.points:
        if prev is None:
            prev = (lat, lon)
            continue
        seg_km = haversine_m(prev[0], prev[1], lat, lon) / 1000.0
        while seg_km > 0 and cum + seg_km >= target:
            t = (target - cum) / seg_km
            markers.append({
                "lat": round(prev[0] + (lat - prev[0]) * t, 6),
                "lon": round(prev[1] + (lon - prev[1]) * t, 6),
                "km": int(target),
            })
            target += 1.0
        cum += seg_km
        prev = (lat, lon)
    return markers


def _direction_markers(course: Course) -> list[dict]:
    markers = []
    if len(course.points) < 2:
        return markers
    target = 0.35
    cum = 0.0
    prev = course.points[0]
    for lat, lon in course.points[1:]:
        seg_km = haversine_m(prev[0], prev[1], lat, lon) / 1000.0
        while seg_km > 0 and cum + seg_km >= target:
            t = (target - cum) / seg_km
            mlat = prev[0] + (lat - prev[0]) * t
            mlon = prev[1] + (lon - prev[1]) * t
            markers.append({
                "lat": round(mlat, 6),
                "lon": round(mlon, 6),
                "angle": round(_screen_angle_deg(prev[0], prev[1], lat, lon), 1),
            })
            target += 0.45
        cum += seg_km
        prev = (lat, lon)
    return markers


def _screen_angle_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dx = (lon2 - lon1) * 111_320.0
    dy = (lat2 - lat1) * 111_320.0
    return math.degrees(math.atan2(-dy, dx))


def _point_line_distance(p, a, b) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def _rdp(points_xy: list[tuple[float, float]], keep: list[int], lo: int, hi: int, tolerance_m: float) -> None:
    if hi <= lo + 1:
        return
    best_i = lo
    best_d = 0.0
    for i in range(lo + 1, hi):
        d = _point_line_distance(points_xy[i], points_xy[lo], points_xy[hi])
        if d > best_d:
            best_i = i
            best_d = d
    if best_d >= tolerance_m:
        keep.append(best_i)
        _rdp(points_xy, keep, lo, best_i, tolerance_m)
        _rdp(points_xy, keep, best_i, hi, tolerance_m)


def _shape_only_route(course: Course) -> list[list[float]]:
    """Display-only simplification for the cute shape view.

    The exact road-following points remain in GPX and guide mode; this layer
    only removes tiny street wiggles that make GPS art look noisy at a glance.
    """
    points = course.points
    if len(points) <= 3:
        return [[round(lat, 6), round(lon, 6)] for lat, lon in points]
    lat0, lon0 = points[0]
    xy = [to_xy(lat, lon, lat0, lon0) for lat, lon in points]
    keep = [0, len(points) - 1]
    tolerance = 35.0 if course.shape_similarity is None else 55.0
    _rdp(xy, keep, 0, len(points) - 1, tolerance)
    keep = sorted(set(keep))
    # Keep the simplified visual cute, but not so sparse that the animal vanishes.
    if len(keep) < 12 and len(points) > 12:
        step = max(1, len(points) // 12)
        keep = sorted(set(keep + list(range(0, len(points), step)) + [len(points) - 1]))
    return [[round(points[i][0], 6), round(points[i][1], 6)] for i in keep]


def _score_breakdown_html(course: Course) -> str:
    comps = course.rfs.get("components", {})
    weights = course.rfs.get("weights", {})
    if not comps or not weights:
        return ""
    rows = []
    order = ("slope", "crossing", "lighting", "sidewalk", "cctv", "park")
    for key in order:
        value = float(comps.get(key, 0.5))
        weight = float(weights.get(key, 0.0))
        label = "훈련 언덕" if key == "slope" and course.params.include_hills else COMPONENT_LABELS_KO[key]
        rows.append(
            f'<div class="metric">'
            f'<div class="metric-top"><span>{label}</span>'
            f'<span>{round(value * 100)}점 · {round(weight * 100)}%</span></div>'
            f'<div class="bar"><i style="width:{max(3, round(value * 100))}%"></i></div>'
            f'</div>'
        )
    return (
        '<section class="panel"><h3>러닝 친화도 산정</h3>'
        '<p class="hint">경사가 낮고 보행 신호가 적을수록 점수가 높아집니다. '
        '야간 코스는 가로등과 안심 요소의 비중을 더 크게 봅니다.</p>'
        + "".join(rows) + "</section>"
    )


def _course_fact_html(course: Course, facilities: list[dict]) -> str:
    signals = infra_count_along(course.points, "pedestrian_signal")
    streetlights = infra_count_along(course.points, "streetlight")
    preview_facilities = [f for f in facilities if f["type"] in PREVIEW_FACILITY_TYPES]
    restroom_count = sum(1 for f in preview_facilities if f["type"] == "restroom")
    convenience_count = sum(1 for f in preview_facilities if f["type"] == "convenience_store")
    light_gap = round(course.length_m / streetlights) if streetlights else None
    light_text = f"약 {light_gap}m마다" if light_gap else "정보 없음"
    items = [
        ("보행 신호", f"{signals}개"),
        ("가로등 간격", light_text),
        ("편의점", f"{convenience_count}개"),
        ("화장실", f"{restroom_count}개"),
    ]
    cells = "".join(
        f'<div class="fact"><b>{value}</b><span>{label}</span></div>'
        for label, value in items
    )
    return (
        '<section class="panel"><h3>러너 체크포인트</h3>'
        f'<div class="facts">{cells}</div>'
        '<p class="hint">코스 100m 반경 기준입니다. 편의점과 화장실만 집계하며 음수대와 공원은 제외합니다.</p>'
        '</section>'
    )


def preview_html(course: Course, facilities: list[dict], base_url: str) -> str:
    facilities = [f for f in facilities if f["type"] in PREVIEW_FACILITY_TYPES]
    p = course.params
    cid = encode_course_id(p)
    shape = SHAPES.get(p.shape) if p.shape else None
    title = html.escape(
        (f"{shape.name_ko} 모양 " if shape else "") + f"{course.length_km:.1f}km 러닝 코스"
    )
    og_desc = html.escape(
        f"러닝 친화도 {course.rfs['score']}/100 · 누적 오르막 {course.ascent_m:.0f}m"
        f" · {p.location_name or '서울'} — 러니웨어"
    )
    segments = json.dumps(_segments_with_rfs(course))
    shape_route = json.dumps(_shape_only_route(course))
    km_markers = json.dumps(_km_markers(course))
    dir_markers = json.dumps(_direction_markers(course))
    profile_svg = _profile_svg(_elevation_profile(course))
    score_breakdown = _score_breakdown_html(course)
    course_facts = _course_fact_html(course, facilities)
    markers = json.dumps([
        {"lat": f["lat"], "lon": f["lon"],
         "label": html.escape(f"{LABELS_KO[f['type']]} · {f['at_km']:g}km 지점")}
        for f in facilities
    ])
    highlights = html.escape(" · ".join(course.rfs.get("highlights", [])))
    sim = (
        f' · 모양 완성도 {course.shape_similarity:.0%}'
        if course.shape_similarity is not None else ""
    )
    return f"""<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>러니웨어 — {title}</title>
<meta property="og:title" content="러니웨어 — {title}">
<meta property="og:description" content="{og_desc}">
<meta property="og:image" content="{base_url}/c/{cid}/card.svg">
<meta property="og:type" content="website">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 body{{margin:0;font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;color:#17201b;background:#f5f7f3}}
 #map{{height:58vh;min-height:380px;background:#e8ece5}}
 .leaflet-control-attribution{{font-size:10px}}
 .map-hud{{position:absolute;z-index:500;left:14px;right:14px;top:14px;display:flex;gap:8px;flex-wrap:wrap;pointer-events:none}}
 .pill{{background:rgba(255,255,255,.94);border:1px solid rgba(20,35,25,.08);border-radius:8px;
      padding:8px 10px;font-size:13px;font-weight:700;box-shadow:0 4px 18px rgba(0,0,0,.08)}}
 .run-panel{{position:absolute;z-index:520;left:14px;right:14px;bottom:16px;display:flex;gap:8px;align-items:center;pointer-events:none}}
 .run-panel button,.run-status{{pointer-events:auto;border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.14)}}
 .run-panel button{{border:0;background:#142018;color:#fff;padding:11px 14px;font-size:14px;font-weight:800}}
 .run-panel button.on{{background:#0a7d43}}
 .run-status{{background:rgba(255,255,255,.96);border:1px solid rgba(20,35,25,.08);padding:10px 12px;
      color:#243028;font-size:13px;font-weight:700;line-height:1.35;min-width:128px}}
 .view-toggle{{position:absolute;z-index:530;right:14px;top:62px;display:flex;background:rgba(255,255,255,.96);
      border:1px solid rgba(20,35,25,.1);border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.1);overflow:hidden}}
 .view-toggle button{{border:0;background:transparent;color:#4b5a50;padding:9px 11px;font-size:12px;font-weight:800}}
 .view-toggle button.active{{background:#142018;color:#fff}}
 body.shape-only .map-hud,body.shape-only .run-panel{{display:none}}
 .wrap{{padding:16px;max-width:760px;margin:0 auto}}
 .card,.panel{{background:#fff;border:1px solid #e2e7df;border-radius:8px;padding:16px;margin:0 0 12px}}
 h2{{margin:0 0 10px;font-size:22px;letter-spacing:0}}
 h3{{margin:0 0 12px;font-size:16px;letter-spacing:0}}
 .stat{{color:#3d473f;line-height:1.65;font-size:15px}}
 .score{{font-size:1.35em;font-weight:800;color:#0a7d43}}
 .legend{{font-size:12px;color:#6b746d;margin:10px 0 0}}
 .btn{{display:inline-block;margin:6px 8px 0 0;padding:10px 13px;border-radius:8px;
      background:#142018;color:#fff;text-decoration:none;font-size:14px;font-weight:700}}
 .metric{{margin:10px 0}}
 .metric-top{{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:#445048;margin-bottom:5px}}
 .bar{{height:8px;background:#e8ede6;border-radius:999px;overflow:hidden}}
 .bar i{{display:block;height:100%;background:#2da85f;border-radius:999px}}
 .facts{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}}
 .fact{{border:1px solid #e1e7dd;background:#f7faf5;border-radius:8px;padding:10px;min-width:0}}
 .fact b{{display:block;font-size:18px;color:#142018;margin-bottom:3px;word-break:keep-all}}
 .fact span{{font-size:12px;color:#66726a}}
 .hint{{font-size:12px;color:#7b857d;margin:10px 0 0}}
 .steps{{margin:8px 0 0;padding-left:20px;color:#3d473f;line-height:1.65;font-size:14px}}
 .facility-list{{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}}
 .chip{{border:1px solid #dce3d8;background:#f7faf5;border-radius:999px;padding:5px 8px;font-size:12px;color:#344238}}
 .km-marker{{background:#fff;border:2px solid #111;border-radius:999px;width:24px;height:24px;line-height:20px;
      text-align:center;font-size:11px;font-weight:800;box-shadow:0 2px 8px rgba(0,0,0,.2)}}
 .dir-marker span{{display:block;color:#142018;font-size:20px;text-shadow:0 0 3px #fff,0 1px 4px rgba(0,0,0,.2)}}
 .start-marker{{background:#142018;color:#fff;border:2px solid #fff;border-radius:999px;padding:6px 9px;
      font-size:12px;font-weight:800;box-shadow:0 3px 12px rgba(0,0,0,.28);white-space:nowrap}}
 .user-dot{{width:18px;height:18px;background:#1677ff;border:3px solid #fff;border-radius:999px;
      box-shadow:0 0 0 8px rgba(22,119,255,.18),0 2px 10px rgba(0,0,0,.25)}}
 footer{{color:#7b857d;font-size:12px;padding:8px 20px 20px;text-align:center}}
 @media (max-width:560px){{.facts{{grid-template-columns:repeat(2,1fr)}} #map{{height:54vh;min-height:320px}}}}
</style></head><body>
<div id="map"><div class="map-hud">
 <span class="pill">{course.length_km:.2f}km</span>
 <span class="pill">오르막 {course.ascent_m:.0f}m</span>
 <span class="pill">RFS {course.rfs["score"]}/100</span>
 <span class="pill">{course.grade_label}</span>
</div><div class="run-panel">
 <button id="runStart" type="button">러닝 시작</button>
 <div id="runStatus" class="run-status">위치 추적 대기</div>
</div><div class="view-toggle" aria-label="지도 보기 전환">
 <button id="shapeView" type="button">코스만</button>
 <button id="guideView" type="button" class="active">안내 포함</button>
</div></div>
<div class="wrap">
<div class="card">
 <h2>{title}</h2>
 <div class="stat">
  <span class="score">러닝 친화도 {course.rfs["score"]}/100</span>
  · 서울 전체 상위 {course.rfs.get("top_percent", 50)}%{sim}<br>
  {highlights}<br>
  실거리 {course.length_km:.2f}km · 누적 오르막 {course.ascent_m:.0f}m ({course.grade_label}) ·
  예상 {course.duration_range_min[0]}~{course.duration_range_min[1]}분
 </div>
 <p class="legend">초록 구간일수록 경사가 낮고 보행 신호가 적으며, 조명·보도·안심 요소가 좋은 길입니다.</p>
 {profile_svg}
 <div>
  <a class="btn" href="{base_url}/c/{cid}.gpx">⬇️ GPX 다운로드</a>
  <a class="btn" href="{base_url}/c/{cid}/card.svg">🖼️ 공유 카드</a>
 </div>
</div>
<section class="panel"><h3>GPX 사용 방법</h3>
 <ol class="steps">
  <li>GPX 다운로드를 눌러 코스 파일을 저장합니다.</li>
  <li>카카오맵에서 파일 열기/공유 대상으로 카카오맵이 보이면 선택합니다.</li>
  <li>카카오맵에 GPX 가져오기 메뉴가 보이지 않으면 직접 등록이 제한된 환경입니다.</li>
  <li>그때는 이 페이지의 러닝 시작을 쓰거나, GPX를 지원하는 러닝 앱에서 파일을 엽니다.</li>
 </ol>
</section>
{course_facts}
{score_breakdown}
<section class="panel"><h3>코스 주변 편의점·화장실</h3>
 <div class="facility-list">
  {''.join(f'<span class="chip">{LABELS_KO[f["type"]]} · {f["at_km"]:g}km</span>' for f in facilities[:10]) or '<span class="chip">100m 반경 편의점·화장실 없음</span>'}
 </div>
</section>
</div>
<footer>러니웨어 · 데이터: OpenStreetMap, SRTM, 서울열린데이터광장 (스냅숏 기준) ·
위치 정보는 저장되지 않습니다</footer>
<script>
 const segs = {segments};
 const shapeRoute = {shape_route};
 const kms = {km_markers};
 const dirs = {dir_markers};
 const map = L.map('map');
 L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png',
   {{attribution:'&copy; OpenStreetMap &copy; CARTO', subdomains:'abcd', maxZoom:20}}).addTo(map);
 const color = s => s >= .62 ? '#18a558' : (s >= .48 ? '#f0a202' : '#dc3d2a');
 const group = L.featureGroup();
 const shapeLayer = L.featureGroup();
 const guideLayer = L.layerGroup().addTo(map);
 const route = segs.map(s => [s[0], s[1]]);
 if (segs.length) route.push([segs[segs.length - 1][2], segs[segs.length - 1][3]]);
 L.polyline(route, {{color:'#ffffff', weight:9, opacity:.95, lineJoin:'round', lineCap:'round'}}).addTo(group);
 for (const [a, b, c, d, s] of segs)
   L.polyline([[a, b], [c, d]], {{color: color(s), weight: 5, opacity: .92, lineJoin:'round', lineCap:'round'}}).addTo(group);
 L.polyline(shapeRoute, {{color:'#ffffff', weight:13, opacity:.72, lineJoin:'round', lineCap:'round'}}).addTo(shapeLayer);
 L.polyline(shapeRoute, {{color:'#18a558', weight:8, opacity:.92, lineJoin:'round', lineCap:'round'}}).addTo(shapeLayer);
 group.addTo(map);
 map.fitBounds(group.getBounds(), {{padding: [42, 42]}});
 if (segs.length) L.marker([segs[0][0], segs[0][1]], {{icon:L.divIcon({{className:'start-marker', html:'출발·도착', iconAnchor:[28,16]}})}})
   .addTo(guideLayer).bindTooltip('출발·도착 지점', {{direction:'top'}}).bindPopup('출발·도착 지점');
 for (const m of dirs) L.marker([m.lat, m.lon], {{icon:L.divIcon({{className:'dir-marker', html:'<span style="transform:rotate('+m.angle+'deg)">➤</span>', iconSize:[22,22], iconAnchor:[11,11]}})}})
   .addTo(guideLayer).bindTooltip('진행 방향', {{direction:'top'}});
 for (const k of kms) L.marker([k.lat, k.lon], {{icon:L.divIcon({{className:'km-marker', html:k.km, iconSize:[24,24], iconAnchor:[12,12]}})}})
   .addTo(guideLayer).bindTooltip(`${{k.km}}km 지점`, {{direction:'top'}});
 for (const m of {markers}) L.circleMarker([m.lat, m.lon], {{radius: 6, color: '#2563eb'}})
   .addTo(guideLayer).bindTooltip(m.label, {{direction:'top'}}).bindPopup(m.label);
 const shapeView = document.getElementById('shapeView');
 const guideView = document.getElementById('guideView');
 const setMapMode = mode => {{
   const shapeOnly = mode === 'shape';
   document.body.classList.toggle('shape-only', shapeOnly);
   shapeView.classList.toggle('active', shapeOnly);
   guideView.classList.toggle('active', !shapeOnly);
   if (shapeOnly) {{
     map.removeLayer(group);
     map.removeLayer(guideLayer);
     if (!map.hasLayer(shapeLayer)) shapeLayer.addTo(map);
   }} else if (!map.hasLayer(guideLayer)) {{
     map.removeLayer(shapeLayer);
     if (!map.hasLayer(group)) group.addTo(map);
     guideLayer.addTo(map);
   }} else {{
     map.removeLayer(shapeLayer);
     if (!map.hasLayer(group)) group.addTo(map);
   }}
 }};
 shapeView.addEventListener('click', () => setMapMode('shape'));
 guideView.addEventListener('click', () => setMapMode('guide'));
 const startBtn = document.getElementById('runStart');
 const runStatus = document.getElementById('runStatus');
 let watchId = null;
 let userMarker = null;
 let accuracyCircle = null;
 const userIcon = L.divIcon({{className:'', html:'<div class="user-dot"></div>', iconSize:[24,24], iconAnchor:[12,12]}});
 const toRad = deg => deg * Math.PI / 180;
 const distM = (a, b, c, d) => {{
   const R = 6371000;
   const x = toRad(d - b) * Math.cos(toRad((a + c) / 2));
   const y = toRad(c - a);
   return Math.sqrt(x * x + y * y) * R;
 }};
 const nearestRouteM = (lat, lon) => {{
   let best = Infinity;
   for (const [a, b, c, d] of segs) {{
     const x = distM(a, b, a, lon);
     const y = distM(a, b, lat, b);
     const sx = distM(a, b, a, d) * (d >= b ? 1 : -1);
     const sy = distM(a, b, c, b) * (c >= a ? 1 : -1);
     const px = x * (lon >= b ? 1 : -1);
     const py = y * (lat >= a ? 1 : -1);
     const len2 = sx * sx + sy * sy || 1;
     const t = Math.max(0, Math.min(1, (px * sx + py * sy) / len2));
     const dx = px - sx * t;
     const dy = py - sy * t;
     best = Math.min(best, Math.sqrt(dx * dx + dy * dy));
   }}
   return best;
 }};
 const setStatus = text => runStatus.textContent = text;
 const updatePosition = pos => {{
   const lat = pos.coords.latitude;
   const lon = pos.coords.longitude;
   const acc = Math.round(pos.coords.accuracy || 0);
   const off = Math.round(nearestRouteM(lat, lon));
   if (!userMarker) {{
     userMarker = L.marker([lat, lon], {{icon:userIcon, zIndexOffset:1000}}).addTo(map);
     accuracyCircle = L.circle([lat, lon], {{radius: acc, color:'#1677ff', weight:1, fillOpacity:.06}}).addTo(map);
   }} else {{
     userMarker.setLatLng([lat, lon]);
     accuracyCircle.setLatLng([lat, lon]).setRadius(acc);
   }}
   const guide = off > 80 ? `코스에서 약 ${{off}}m 벗어남` : `코스 위를 달리는 중 · 오차 ${{acc}}m`;
   setStatus(guide);
 }};
 const locationError = err => {{
   const msg = err.code === 1 ? '위치 권한이 필요해요' : '현재 위치를 가져오지 못했어요';
   setStatus(msg);
   startBtn.classList.remove('on');
   startBtn.textContent = '러닝 시작';
 }};
 startBtn.addEventListener('click', () => {{
   if (!navigator.geolocation) {{
     setStatus('이 브라우저는 위치 추적을 지원하지 않아요');
     return;
   }}
   if (watchId !== null) {{
     navigator.geolocation.clearWatch(watchId);
     watchId = null;
     startBtn.classList.remove('on');
     startBtn.textContent = '러닝 시작';
     setStatus('위치 추적 일시정지');
     return;
   }}
   setStatus('위치 권한 확인 중');
   startBtn.classList.add('on');
   startBtn.textContent = '추적 중지';
   watchId = navigator.geolocation.watchPosition(updatePosition, locationError, {{
     enableHighAccuracy:true, maximumAge:3000, timeout:12000
   }});
 }});
</script></body></html>"""


# ---------- share card (SVG, no image deps) ----------

def card_svg(course: Course) -> str:
    p = course.params
    shape = SHAPES.get(p.shape) if p.shape else None
    title = (f"{shape.emoji} {shape.name_ko} 모양" if shape else "🏃 러닝 코스")
    w, h = 800, 418
    lats = [pt[0] for pt in course.points]
    lons = [pt[1] for pt in course.points]
    lat_c = (max(lats) + min(lats)) / 2
    span_lat = max(max(lats) - min(lats), 1e-6)
    span_lon = max(max(lons) - min(lons), 1e-6) * 0.79  # rough lon shrink at 37.5N
    box = 300.0
    scale = box / max(span_lat, span_lon)
    cx, cy = 545, h / 2
    pts = " ".join(
        f"{cx + (lon - (max(lons) + min(lons)) / 2) * scale * 0.79:.1f},"
        f"{cy - (lat - lat_c) * scale:.1f}"
        for lat, lon in course.points
    )
    where = html.escape(p.location_name or "서울")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">
 <rect width="{w}" height="{h}" rx="24" fill="#101418"/>
 <text x="40" y="76" font-size="40" fill="#fff"
   font-family="-apple-system,'Apple SD Gothic Neo',sans-serif" font-weight="700">{title}</text>
 <text x="40" y="130" font-size="26" fill="#9be49b"
   font-family="-apple-system,sans-serif">{course.length_km:.1f}km · {where}</text>
 <text x="40" y="180" font-size="20" fill="#aaa"
   font-family="-apple-system,sans-serif">러닝 친화도 {course.rfs['score']}/100 · 오르막 {course.ascent_m:.0f}m</text>
 <text x="40" y="{h - 44}" font-size="18" fill="#666"
   font-family="-apple-system,sans-serif">RunArt(런아트) — 대화로 그리는 러닝 코스</text>
 <polyline points="{pts}" fill="none" stroke="#e0533d" stroke-width="5"
   stroke-linejoin="round" stroke-linecap="round"/>
</svg>"""
