"""Course result formatting (PRD §5.2: refined markdown, minimal size), the
preview web page with RFS heatmap + elevation profile (PRD §5.6), and the
SVG share card (PRD §2.2 — the spread loop). All user-visible free text from
data sources is escaped before rendering (PRD §8)."""

import html
import json

from . import graph as graphmod
from .course import Course, smooth_series
from .facilities import LABELS_KO
from .geo import haversine_m
from .models import encode_course_id, encode_shape_token
from .rfs import edge_rfs
from .shapes import SHAPES


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
        lines.append(f"- {shape.emoji} 친구도 자기 동네에서 이 모양 뛰기: {base_url}/s/{token}")
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


def preview_html(course: Course, facilities: list[dict], base_url: str) -> str:
    p = course.params
    cid = encode_course_id(p)
    shape = SHAPES.get(p.shape) if p.shape else None
    title = html.escape(
        (f"{shape.name_ko} 모양 " if shape else "") + f"{course.length_km:.1f}km 러닝 코스"
    )
    og_desc = html.escape(
        f"러닝 친화도 {course.rfs['score']}/100 · 누적 오르막 {course.ascent_m:.0f}m"
        f" · {p.location_name or '서울'} — RunArt(런아트)"
    )
    segments = json.dumps(_segments_with_rfs(course))
    profile_svg = _profile_svg(_elevation_profile(course))
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
    share = (
        f'<a class="btn" href="{base_url}/s/{encode_shape_token(p.shape, p.distance_km)}">'
        f"🐾 내 동네에서 이 모양 뛰기</a>" if shape else ""
    )
    return f"""<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RunArt — {title}</title>
<meta property="og:title" content="RunArt — {title}">
<meta property="og:description" content="{og_desc}">
<meta property="og:image" content="{base_url}/c/{cid}/card.svg">
<meta property="og:type" content="website">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 body{{margin:0;font-family:-apple-system,'Apple SD Gothic Neo',sans-serif}}
 #map{{height:52vh}}
 .card{{padding:16px 20px;max-width:640px;margin:0 auto}}
 .stat{{color:#444;line-height:1.7}}
 .score{{font-size:1.4em;font-weight:700;color:#0a7d43}}
 .legend{{font-size:.8em;color:#777}}
 .btn{{display:inline-block;margin:6px 8px 0 0;padding:10px 14px;border-radius:10px;
      background:#111;color:#fff;text-decoration:none;font-size:.95em}}
 footer{{color:#999;font-size:.8em;padding:12px 20px;text-align:center}}
</style></head><body>
<div id="map"></div>
<div class="card">
 <h2>{title}</h2>
 <div class="stat">
  <span class="score">러닝 친화도 {course.rfs["score"]}/100</span>
  · 서울 전체 상위 {course.rfs.get("top_percent", 50)}%{sim}<br>
  {highlights}<br>
  실거리 {course.length_km:.2f}km · 누적 오르막 {course.ascent_m:.0f}m ({course.grade_label}) ·
  예상 {course.duration_range_min[0]}~{course.duration_range_min[1]}분
 </div>
 <p class="legend">지도 색상 = 구간별 러닝 친화도 (초록: 좋음 · 빨강: 주의)</p>
 {profile_svg}
 <div>
  <a class="btn" href="{base_url}/c/{cid}.gpx">⬇️ GPX 다운로드</a>
  <a class="btn" href="{base_url}/c/{cid}/card.svg">🖼️ 공유 카드</a>{share}
 </div>
</div>
<footer>RunArt(런아트) · 데이터: OpenStreetMap, SRTM, 서울열린데이터광장 (스냅숏 기준) ·
위치 정보는 저장되지 않습니다</footer>
<script>
 const segs = {segments};
 const map = L.map('map');
 L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
   {{attribution:'&copy; OpenStreetMap'}}).addTo(map);
 const color = s => `hsl(${{Math.round(120 * s)}},72%,42%)`;
 const group = L.featureGroup();
 for (const [a, b, c, d, s] of segs)
   L.polyline([[a, b], [c, d]], {{color: color(s), weight: 5, opacity: .9}}).addTo(group);
 group.addTo(map);
 map.fitBounds(group.getBounds(), {{padding: [24, 24]}});
 if (segs.length) L.marker([segs[0][0], segs[0][1]]).addTo(map).bindPopup('출발/도착');
 for (const m of {markers}) L.circleMarker([m.lat, m.lon], {{radius: 6, color: '#2563eb'}})
   .addTo(map).bindPopup(m.label);
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
