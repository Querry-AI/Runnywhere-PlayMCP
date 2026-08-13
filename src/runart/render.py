"""Course result formatting (PRD §5.2: refined markdown, minimal size), the
preview web page with RFS heatmap + elevation profile (PRD §5.6), and the
SVG share card (PRD §2.2 — the spread loop). All user-visible free text from
data sources is escaped before rendering (PRD §8)."""

import html
import json
import math
import os

from . import graph as graphmod
from .course import Course, smooth_series
from .facilities import LABELS_KO
from .geo import haversine_m, to_xy
from .infrastructure import pedestrian_signals_crossed
from .models import encode_course_id
from .rfs import COMPONENT_LABELS_KO, edge_rfs
from .shapes import SHAPES

PREVIEW_FACILITY_TYPES = {"convenience_store", "restroom"}
FACILITY_CHIP_LIMIT = 10




def markdown_text(value: str) -> str:
    """Escape untrusted labels embedded in MCP Markdown responses."""
    value = "".join(ch for ch in value if ch >= " " and ch != "\x7f")[:120]
    for char in "\\`*_{}[]()<>#+-.!|":
        value = value.replace(char, "\\" + char)
    return value


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
        f"## {title}",
        f"📍 **출발·도착:** {markdown_text(p.location_name) if p.location_name else '지정한 출발점'}",
        "",
        "**한눈에 보기**",
        f"- 거리 **{course.length_km:.1f}km** · 예상 **{lo}~{hi}분** (6:30/km 기준)",
        f"- 누적 오르막 **{course.ascent_m:.0f}m** · {course.grade_label}",
        f"- 러닝 친화도 **{course.rfs['score']}/100** (서울 전체 상위 {course.rfs.get('top_percent', 50)}%)"
        + (" — " + " · ".join(course.rfs["highlights"]) if course.rfs["highlights"] else ""),
    ]
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
            lines.append(f"- 요청 시설 중 {', '.join(missing)}은 코스 10m 반경에서 찾지 못했어요")
    if p.night_mode:
        lines.append("- 🌙 야간 안전 모드: 조명·안심 CCTV가 좋은 길 위주예요")
    lines.extend([
        "",
        "**바로 시작하기**",
        f"- 🗺️ 지도·러닝 가이드: {base_url}/c/{cid}",
        f"- ⬇️ GPX 다운로드: {base_url}/c/{cid}.gpx",
    ])
    lines.append("지도에서 통행·공사·날씨를 확인한 뒤 **러닝 시작**을 누르세요. 코스는 참고용이에요.")
    return "\n".join(lines)


# ---------- preview page data ----------

def route_points(course: Course) -> list[tuple[float, float]]:
    """The course polyline following real street geometry (OSM way shapes),
    so the drawn route stays on pedestrian roads instead of cutting straight
    chords through blocks/buildings between graph nodes."""
    return graphmod.path_points(graphmod.get_graph(), course.path)


def _segments_with_rfs(course: Course) -> list:
    """[[lat1, lon1, lat2, lon2, rfs_0to1], ...] for the heatmap polyline.
    Each graph edge is expanded into its real street-geometry sub-segments."""
    g = graphmod.get_graph()
    p = course.params
    out = []
    for u, v in zip(course.path, course.path[1:]):
        score = round(edge_rfs(g.edges[u, v], p.night_mode, p.include_hills), 2)
        pts = graphmod.edge_points(g, u, v)
        for (alat, alon), (blat, blon) in zip(pts, pts[1:]):
            out.append([round(alat, 6), round(alon, 6),
                        round(blat, 6), round(blon, 6), score])
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


def _km_markers(points: list[tuple[float, float]]) -> list[dict]:
    markers = []
    target = 1.0
    cum = 0.0
    prev = None
    for lat, lon in points:
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


def _direction_markers(points: list[tuple[float, float]]) -> list[dict]:
    markers = []
    if len(points) < 2:
        return markers
    target = 0.35
    cum = 0.0
    prev = points[0]
    for lat, lon in points[1:]:
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
    """The clean view must be the same route as guide mode, just without UI."""
    return [[round(lat, 6), round(lon, 6)] for lat, lon in route_points(course)]


def editable_waypoints(course: Course) -> list[tuple[float, float]]:
    """Return persisted snapped handles or stable quarter-route handles."""
    if course.params.manual_waypoints:
        return [(p.lat, p.lon) for p in course.params.manual_waypoints]
    points = route_points(course)
    if len(points) < 4:
        return points[1:-1]
    distances = [0.0]
    for a, b in zip(points, points[1:]):
        distances.append(distances[-1] + haversine_m(a[0], a[1], b[0], b[1]))
    total = distances[-1] or 1.0
    handles = []
    for fraction in (0.25, 0.5, 0.75):
        target = total * fraction
        index = next((i for i, value in enumerate(distances) if value >= target), len(points) - 2)
        handles.append(points[min(index, len(points) - 2)])
    return handles


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
        '<details class="panel"><summary>러닝 친화도 근거</summary>'
        + "".join(rows) + "</details>"
    )


def _course_fact_html(course: Course, facilities: list[dict],
                      detailed_points: list[tuple[float, float]]) -> str:
    g = graphmod.get_graph()
    signals = pedestrian_signals_crossed(g, course.path)
    preview_facilities = [f for f in facilities if f["type"] in PREVIEW_FACILITY_TYPES]
    restroom_count = sum(1 for f in preview_facilities if f["type"] == "restroom")
    convenience_count = sum(1 for f in preview_facilities if f["type"] == "convenience_store")
    items = [
        ("보행 신호", f"{signals}개"),
        ("편의점", f"{convenience_count}개"),
        ("화장실", f"{restroom_count}개"),
    ]
    cells = "".join(
        f'<div class="fact"><b>{value}</b><span>{label}</span></div>'
        for label, value in items
    )
    return (
        '<details class="panel"><summary>러너 체크포인트</summary>'
        f'<div class="facts">{cells}</div>'
        '<p class="hint">신호 횡단 수와 코스 10m 안의 편의시설입니다.</p>'
        '</details>'
    )


_RFS_GRADES_KO = (
    (75, "아주 좋아요"),
    (60, "좋아요"),
    (45, "무난해요"),
)


def _rfs_grade_ko(score: int) -> str:
    """Plain-Korean reading of the RFS number — 58/100 means nothing on its own."""
    for floor, label in _RFS_GRADES_KO:
        if score >= floor:
            return label
    return "주의해서 달리세요"


def preview_html(course: Course, facilities: list[dict], base_url: str,
                 kakao_javascript_key: str = "") -> str:
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
    detailed = route_points(course)
    segments = json.dumps(_segments_with_rfs(course))
    shape_route = json.dumps(_shape_only_route(course))
    km_markers = json.dumps(_km_markers(detailed))
    dir_markers = json.dumps(_direction_markers(detailed))
    profile_svg = _profile_svg(_elevation_profile(course))
    score_breakdown = _score_breakdown_html(course)
    course_facts = _course_fact_html(course, facilities, detailed)
    markers = json.dumps([
        {"lat": f["lat"], "lon": f["lon"], "type": f["type"],
         "name": html.escape(f.get("name") or LABELS_KO[f["type"]]),
         "label": html.escape(f"{LABELS_KO[f['type']]} · {f['at_km']:g}km 지점")}
        for f in facilities
    ])
    highlights = html.escape(" · ".join(course.rfs.get("highlights", [])))
    shape_view_label = "동물 실루엣" if shape else "코스 라인"
    rfs_grade = html.escape(_rfs_grade_ko(course.rfs["score"]))
    where = html.escape(p.location_name)
    where_html = f'<p class="course-where">{where} 출발</p>' if where else ""
    # Name the empty categories too: "편의점·화장실" with only restrooms listed
    # reads as if the convenience-store lookup silently failed.
    facility_tally = " · ".join(
        f"{LABELS_KO[t]} {sum(1 for f in facilities if f['type'] == t)}곳"
        for t in sorted(PREVIEW_FACILITY_TYPES)
    )
    if len(facilities) > FACILITY_CHIP_LIMIT:
        facility_tally += f" · 가까운 {FACILITY_CHIP_LIMIT}곳 표시"
    edit_enabled = os.environ.get("RUNART_ROUTE_EDIT", "1") == "1"
    edit_notice = (
        '<p class="edit-notice">원본 동물 코스는 유지되고, 저장하면 새 <b>직접 편집한 코스</b>가 만들어져요.</p>'
        if shape else ""
    )
    edit_waypoints = json.dumps(editable_waypoints(course))
    kakao_key = html.escape(kakao_javascript_key, quote=True)
    map_sdk = (
        f'<script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey={kakao_key}'
        '&autoload=false&libraries=services"></script>'
        if kakao_key else ""
    )
    return f"""<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>러니웨어 — {title}</title>
<meta property="og:title" content="러니웨어 — {title}">
<meta property="og:description" content="{og_desc}">
<meta property="og:image" content="{base_url}/c/{cid}/card.svg">
<meta property="og:type" content="website">
{map_sdk}
<style>
 @font-face{{font-family:'Pretendard Variable';font-style:normal;font-weight:45 920;font-display:swap;src:url('/assets/PretendardVariable.woff2') format('woff2-variations')}}
 *{{box-sizing:border-box}}
 body{{margin:0;font-family:'Pretendard Variable',Pretendard,-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',sans-serif;color:#17201b;background:#f4f7f4;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}}
 .brand{{height:64px;display:flex;align-items:center;justify-content:space-between;padding:0 22px;
      background:#fff;border-bottom:1px solid #e2e7df;box-sizing:border-box}}
 .brand strong{{font-size:18px;color:#142018;letter-spacing:-.03em}}
 .brand span{{font-size:13px;color:#66726a}}
 #map{{height:62vh;min-height:460px;background:#e8ece5}}
 .local-course-editor{{height:100%;position:relative;overflow:hidden;background:#e8ece5}}
 .local-course-editor svg{{width:100%;height:100%;display:block;touch-action:none;background:linear-gradient(135deg,#f5f8f2 25%,#e9f0e7 25%,#e9f0e7 50%,#f5f8f2 50%,#f5f8f2 75%,#e9f0e7 75%);background-size:42px 42px}}
 .local-course-hint{{position:absolute;z-index:2;left:14px;right:14px;top:14px;padding:10px 12px;border-radius:12px;background:rgba(255,255,255,.94);box-shadow:0 4px 18px rgba(0,0,0,.1);font-size:13px;font-weight:700;line-height:1.4;color:#243028}}
 .local-course-hint strong{{display:block;color:#087b59;margin-bottom:2px}}
 .local-editor-actions{{position:absolute;z-index:3;left:14px;right:14px;bottom:14px;display:flex;gap:8px;flex-wrap:wrap}}
 .local-editor-actions button{{min-height:46px;padding:0 13px;border:0;border-radius:12px;background:#fff;color:#142018;box-shadow:0 4px 18px rgba(0,0,0,.14);font:700 14px inherit}}
 .local-editor-actions .local-primary{{background:#087b59;color:#fff}}
 .local-editor-actions .local-edit-tools{{display:none;gap:8px;flex-wrap:wrap;width:100%}}
 .local-course-editor.editing .local-edit-tools{{display:flex}}
 .local-course-editor.editing #localEditRoute{{display:none}}
 .map-error{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:24px;
      box-sizing:border-box;text-align:center;color:#44514a;font-size:14px;line-height:1.5;background:#eef2ec}}
 .map-error strong{{font-size:16px;color:#142018}}
 .map-error button{{min-height:44px;padding:0 18px;border:1px solid #c3cec6;border-radius:12px;
      background:#fff;color:#142018;font-size:14px;font-weight:700;font-family:inherit;cursor:pointer}}
 .map-hud{{position:absolute;z-index:500;left:14px;right:14px;top:14px;display:flex;gap:8px;flex-wrap:wrap;pointer-events:none}}
 .pill{{background:rgba(255,255,255,.94);border:1px solid rgba(20,35,25,.08);border-radius:10px;
      padding:9px 11px;font-size:13px;font-weight:720;box-shadow:0 4px 18px rgba(0,0,0,.08);backdrop-filter:blur(8px)}}
 .run-panel{{position:absolute;z-index:520;left:14px;right:14px;bottom:16px;display:flex;gap:8px;align-items:center;pointer-events:none}}
 .run-panel button,.run-status{{pointer-events:auto;border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.14)}}
 .run-panel button{{min-height:48px;border:0;background:#142018;color:#fff;padding:0 16px;font-size:14px;font-weight:800;font-family:inherit}}
 .run-panel button:disabled{{background:#59635c;cursor:not-allowed}}
 .run-panel button.on{{background:#0a7d43}}
 .run-status{{background:rgba(255,255,255,.96);border:1px solid rgba(20,35,25,.08);padding:10px 12px;
      color:#243028;font-size:13px;font-weight:700;line-height:1.35;min-width:128px}}
 .view-toggle{{position:absolute;z-index:530;right:14px;top:66px;display:flex;background:rgba(255,255,255,.96);
      border:1px solid rgba(20,35,25,.1);border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.1);overflow:hidden}}
 .view-toggle button{{min-height:48px;border:0;background:transparent;color:#4b5a50;padding:0 13px;font-size:13px;font-weight:800;font-family:inherit}}
 .view-toggle button.active{{background:#142018;color:#fff}}
 body.shape-only .map-hud,body.shape-only .run-panel{{display:none}}
 .wrap{{padding:22px;max-width:1040px;margin:0 auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
 .card,.panel{{background:#fff;border:1px solid #dfe7e1;border-radius:18px;padding:22px;margin:0;box-shadow:0 12px 34px rgba(20,45,30,.045)}}
 .course-summary{{grid-column:1/-1}}
 h1{{margin:0 0 8px;font-size:26px;line-height:1.28;letter-spacing:-.035em}}
 h2,h3{{margin:0 0 12px;font-size:17px;letter-spacing:-.02em}}
 .stat{{color:#3d473f;line-height:1.65;font-size:15px}}
 .course-metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:14px 0}}
 .course-metrics>div{{min-width:0;padding:12px;border:1px solid #e1e7dd;background:#f7faf5;border-radius:12px}}
 /* dt/dd carry a 40px UA margin-inline-start that squeezes the value out of the cell. */
 .course-metrics dt,.course-metrics dd{{margin:0}}
 .metric-value{{display:block;font-size:21px;font-weight:800;color:#142018;white-space:nowrap}}
 .metric-label{{display:block;font-size:13px;color:#55605a;margin-top:3px;word-break:keep-all}}
 .metric-note{{display:block;font-size:12px;font-weight:700;color:#17613e;margin-top:2px}}
 .course-where{{margin:-6px 0 12px;font-size:15px;font-weight:700;color:#44514a;word-break:keep-all}}
 .highlight-tags{{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 10px}}
 .tag{{padding:6px 9px;border-radius:999px;background:#edf5f0;color:#17613e;font-size:13px;font-weight:700;word-break:keep-all}}
 .supporting-copy{{font-size:13px;line-height:1.45;color:#55605a;margin:0}}
 .score{{font-size:1.35em;font-weight:800;color:#0a7d43}}
 .legend{{font-size:13px;color:#55605a;margin:10px 0 0}}
 .btn{{display:inline-flex;align-items:center;justify-content:center;min-height:48px;margin:6px 8px 0 0;padding:0 16px;border-radius:12px;
      background:#142018;color:#fff;text-decoration:none;font-size:14px;font-weight:700}}
 button.btn{{border:0;cursor:pointer;font-family:inherit;background:#0a7d43}}
 .actions{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:16px}}
 .actions .btn{{margin:0}}
 /* Padding (not flex) keeps the native disclosure marker while reaching a 44px target. */
 details.panel summary{{cursor:pointer;font-size:15px;font-weight:800;color:#344238;padding:13px 0;min-height:44px}}
 details.panel[open] summary{{margin-bottom:10px}}
 details.more-actions{{margin-top:10px}}details.more-actions summary{{cursor:pointer;font-size:13px;color:#4a554e;font-weight:700;padding:14px 0;min-height:44px}}
 .metric{{margin:10px 0}}
 .metric-top{{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:#445048;margin-bottom:5px}}
 .bar{{height:8px;background:#e8ede6;border-radius:999px;overflow:hidden}}
 .bar i{{display:block;height:100%;background:#2da85f;border-radius:999px}}
 .facts{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}}
 .fact{{border:1px solid #e1e7dd;background:#f7faf5;border-radius:12px;padding:12px;min-width:0}}
 .fact b{{display:block;font-size:18px;color:#142018;margin-bottom:3px;word-break:keep-all}}
 .fact span{{font-size:13px;color:#55605a}}
 .hint{{font-size:13px;color:#55605a;margin:10px 0 0}}
 .steps{{margin:8px 0 0;padding-left:20px;color:#3d473f;line-height:1.65;font-size:14px}}
 .facility-list{{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}}
 .chip{{border:1px solid #dce3d8;background:#f7faf5;border-radius:999px;padding:7px 10px;font-size:13px;color:#344238}}
 .km-marker{{background:#fff;border:2px solid #111;border-radius:999px;width:24px;height:24px;line-height:20px;
      text-align:center;font-size:11px;font-weight:800;box-shadow:0 2px 8px rgba(0,0,0,.2)}}
 .dir-marker span{{display:block;color:#142018;font-size:20px;text-shadow:0 0 3px #fff,0 1px 4px rgba(0,0,0,.2)}}
 .start-marker{{background:#142018;color:#fff;border:2px solid #fff;border-radius:999px;padding:6px 9px;
      font-size:12px;font-weight:800;box-shadow:0 3px 12px rgba(0,0,0,.28);white-space:nowrap}}
 .user-dot{{width:18px;height:18px;background:#e5322e;border:3px solid #fff;border-radius:999px;
      box-shadow:0 0 0 8px rgba(229,50,46,.18),0 2px 10px rgba(0,0,0,.25)}}
 .facility-marker{{position:relative;width:12px;height:12px;border:2px solid #fff;border-radius:999px;
      box-shadow:0 2px 8px rgba(0,0,0,.24);cursor:pointer}}
 .facility-marker.convenience_store{{background:#2563eb}}
 .facility-marker.restroom{{background:#0a9d4f}}
 .poi-pop{{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);background:#fff;
      border:1px solid rgba(20,35,25,.14);border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.18);
      padding:8px 10px;min-width:150px;max-width:220px;z-index:900;text-align:left;pointer-events:none}}
 .poi-pop b{{display:block;font-size:13px;color:#142018;margin-bottom:2px;white-space:nowrap;
      overflow:hidden;text-overflow:ellipsis}}
 .poi-pop span{{font-size:13px;color:#5c675e;line-height:1.4;word-break:keep-all}}
 .mobile-dock{{display:none}}
 .edit-bar{{display:none;position:absolute;z-index:940;left:14px;right:14px;bottom:14px;gap:8px;align-items:center;padding:12px;background:rgba(255,255,255,.97);border:1px solid rgba(20,35,25,.1);border-radius:16px;box-shadow:0 10px 30px rgba(10,28,19,.24);flex-wrap:wrap}}
 .edit-bar button{{min-height:46px;border:0;border-radius:11px;padding:0 12px;background:#edf5f0;color:#142018;font-family:inherit;font-size:13px;font-weight:700}}
 #editRoute{{position:absolute;z-index:540;left:14px;top:66px;min-height:46px;border:0;border-radius:12px;padding:0 14px;background:#fff;color:#142018;font-family:inherit;font-size:13px;font-weight:700;box-shadow:0 4px 18px rgba(0,0,0,.12)}}
 .edit-disabled{{position:absolute;z-index:540;left:14px;top:66px;padding:10px 12px;border-radius:10px;background:rgba(255,255,255,.94);color:#66726a;font-size:13px}}
 .edit-bar button.primary{{background:#087b59;color:#fff}}
 .edit-status{{font-size:13px;line-height:1.35;color:#344238;flex:1;min-width:180px}}
 .edit-notice{{width:100%;margin:0;padding:8px 10px;border-radius:10px;background:#fff5d6;color:#594600;font-size:13px;line-height:1.4}}
 .edit-points{{display:flex;gap:6px;width:100%;overflow:auto;padding:0;margin:0;list-style:none}}
 .edit-points button{{min-width:44px;padding:0 10px}}
 .edit-points button[aria-current="true"]{{background:#142018;color:#fff}}
 body.editing .edit-bar{{display:flex}}body.editing .mobile-dock{{display:none!important}}
 footer{{color:#55605a;font-size:13px;padding:8px 20px 28px;text-align:center;line-height:1.6}}
 footer a{{display:inline-block;padding:8px 4px;color:inherit}}
 @media (max-width:760px){{.brand{{height:48px;padding:0 16px}}.brand span{{font-size:13px}} .facts{{grid-template-columns:repeat(2,1fr)}}
      #map{{height:clamp(280px,42svh,380px);min-height:0}}.map-hud{{left:10px;right:10px;top:10px;gap:6px}}.pill{{font-size:13px;padding:8px 9px}}
      .view-toggle{{right:10px;top:60px}}.run-panel{{left:10px;right:10px;bottom:12px}}
      .wrap{{display:block;padding:0 16px 96px}}.card,.panel{{padding:18px;margin-bottom:12px;border-radius:16px}}h1{{font-size:22px;line-height:1.25;word-break:keep-all}}
      .actions{{grid-template-columns:1fr 1fr}}.actions .btn{{padding:0 8px;text-align:center}}
      .mobile-dock{{position:fixed;z-index:950;display:none;grid-template-columns:1fr;gap:8px;left:10px;right:10px;bottom:calc(8px + env(safe-area-inset-bottom));padding:8px;background:rgba(255,255,255,.94);border:1px solid rgba(20,35,25,.1);border-radius:18px;box-shadow:0 16px 45px rgba(10,28,19,.24);backdrop-filter:blur(14px)}}
      .mobile-dock a,.mobile-dock button{{display:flex;min-height:50px;align-items:center;justify-content:center;border:0;border-radius:12px;font-size:14px;font-weight:750;font-family:inherit;text-decoration:none}}.mobile-dock a{{background:#edf5f0;color:#142018}}.mobile-dock button{{background:#087b59;color:#fff}}.mobile-dock button:disabled{{background:#59635c}}
      footer{{padding-bottom:96px}}.course-metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.edit-bar{{left:10px;right:10px;bottom:calc(8px + env(safe-area-inset-bottom));}}
      .metric-value{{font-size:20px;line-height:1.2;font-variant-numeric:tabular-nums;white-space:nowrap}}.metric-label{{font-size:13px;line-height:1.35}}.supporting-copy{{font-size:13px;line-height:1.45;word-break:normal;line-break:strict}}
      footer{{padding-bottom:96px}}}}
 /* Below 480px the header must show the service name only — the tagline halves it. */
 @media (max-width:480px){{.brand span{{display:none}}}}
 @media (orientation:landscape) and (max-width:900px){{#map{{height:280px}}.wrap{{padding-bottom:72px}}}}
 @media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important;animation-duration:.001ms!important;transition-duration:.001ms!important}}}}
</style></head><body>
<header class="brand"><strong>Runnywhere · 러니웨어</strong><span>어디서든 러닝 코스 짜기!</span></header>
<div id="map"><div class="map-hud">
 <span class="pill">{course.length_km:.2f}km</span>
 <span class="pill">오르막 {course.ascent_m:.0f}m</span>
 <span class="pill">RFS {course.rfs["score"]}/100</span>
 <span class="pill">{course.grade_label}</span>
</div><div class="run-panel">
 <button id="runStart" type="button">내 위치 보기</button>
 <div id="runStatus" class="run-status" role="status" aria-live="polite">GPS 안내 대기</div>
</div><div class="view-toggle" aria-label="지도 보기 전환">
 <button id="shapeView" type="button">{shape_view_label}</button>
 <button id="guideView" type="button" class="active">러닝 안내</button>
 </div>{'<button id="editRoute" type="button">경로 수정</button>' if edit_enabled else ''}<div id="editBar" class="edit-bar" aria-label="경로 수정 도구" aria-busy="false">{edit_notice}<span id="editStatus" class="edit-status" role="status" aria-live="polite">지점을 선택하거나 추가 모드를 켜세요.</span><ol id="editPointList" class="edit-points" aria-label="경유점 순서"></ol><button id="editAdd" type="button" aria-pressed="false">＋ 지점 추가</button><button id="editMove" type="button" aria-pressed="false">선택 이동</button><button id="editUp" type="button" aria-label="선택한 지점을 앞 순서로 이동">순서 ↑</button><button id="editDown" type="button" aria-label="선택한 지점을 뒤 순서로 이동">순서 ↓</button><button id="editUndo" type="button" aria-label="마지막 편집 취소">되돌리기</button><button id="editRedo" type="button" aria-label="취소한 편집 다시 실행">다시 실행</button><button id="editDelete" type="button">선택 삭제</button><button id="editCancel" type="button">전체 취소</button><button id="editSave" class="primary" type="button">이 경로로 다시 계산</button></div></div>
<div class="wrap">
<div class="card course-summary">
 <h1>{title}</h1>
 {where_html}
 <dl class="course-metrics">
  <div><dt class="metric-label">실거리</dt><dd class="metric-value">{course.length_km:.1f}km</dd></div>
  <div><dt class="metric-label">예상 시간</dt><dd class="metric-value">{course.duration_range_min[0]}~{course.duration_range_min[1]}분</dd></div>
  <div><dt class="metric-label">오르막</dt><dd class="metric-value">{course.ascent_m:.0f}m</dd></div>
  <div><dt class="metric-label">러닝 친화도</dt><dd class="metric-value">{course.rfs["score"]}/100<span class="metric-note">{rfs_grade}</span></dd></div>
 </dl>
 <div class="highlight-tags">{''.join(f'<span class="tag">{h}</span>' for h in course.rfs.get("highlights", [])[:2])}</div>
 <p class="supporting-copy">실제 통행·공사 상황을 확인하고 안전하게 달려 주세요.</p>
 {profile_svg}
 <div class="actions primary-actions">
  <button class="btn" id="inlineRunStart" type="button">내 위치 추적 시작</button>
  <a class="btn" href="{base_url}/c/{cid}.gpx">GPX 파일 받기</a>
 </div>
 <details class="more-actions"><summary>더보기</summary>
  <div class="actions">
   <a class="btn" href="{base_url}/c/{cid}/card.svg">코스 카드</a>
   <button class="btn" id="shareCourse" type="button">친구에게 공유하기</button>
   <a class="btn" href="{base_url}/animals">서울 동물 지도</a>
  </div>
 </details>
</div>
<details class="panel"><summary>카카오맵 GPX 불러오기 방법</summary>
 <ol class="steps">
  <li>GPX 다운로드를 눌러 코스 파일을 저장합니다.</li>
  <li>카카오맵 앱 실행 후 우측 상단 길찾기 버튼을 누르세요.</li>
  <li>이동수단을 자전거로 선택한 뒤, 도착지 입력 화면 우측 하단의 GPX를 선택하세요.</li>
  <li>저장해 둔 GPX 파일을 찾아 선택하고 완료 후, 화면 우측 하단의 주행 시작을 눌러 안내를 받으세요.</li>
 </ol>
</details>
{course_facts}
{score_breakdown}
<section class="panel"><h2>코스 주변 편의시설</h2>
 <p class="supporting-copy">코스 10m 안 · {facility_tally}</p>
 <div class="facility-list">
  {''.join(f'<span class="chip">{LABELS_KO[f["type"]]} · {f["at_km"]:g}km</span>' for f in facilities[:FACILITY_CHIP_LIMIT]) or '<span class="chip">코스 10m 반경 편의점·화장실 없음</span>'}
 </div>
</section>
</div>
<div class="mobile-dock"><button id="mobileRunStart" type="button">내 위치 추적 시작</button></div>
<footer>러니웨어 · 배경 지도: Kakao Maps · 경로 데이터
<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors · ODbL</a> · NASA SRTM · 서울시 공공데이터<br>
GPS는 러니웨어 서버에 저장되지 않습니다 · <a href="/terms">이용·안전</a> · <a href="/privacy">개인정보</a> · <a href="/data-licenses">데이터 출처</a></footer>
<script>
 const segs = {segments};
 const shapeRoute = {shape_route};
 const kms = {km_markers};
 const dirs = {dir_markers};
 const initialEditWaypoints = {edit_waypoints};
 const editEnabled = {str(edit_enabled).lower()};
 const editEndpoint = '{base_url}/c/{cid}/edit';
 const inlineStartBtn = document.getElementById('inlineRunStart');
 const mobileDock = document.querySelector('.mobile-dock');
 const shareBtn = document.getElementById('shareCourse');
 if (shareBtn) shareBtn.addEventListener('click', () => {{
   const url = '{base_url}/c/{cid}';
   const done = () => {{
     shareBtn.textContent = '링크가 복사됐어요!';
     setTimeout(() => shareBtn.textContent = '친구에게 공유하기', 2200);
   }};
   if (navigator.clipboard && navigator.clipboard.writeText)
     navigator.clipboard.writeText(url).then(done)
       .catch(() => window.prompt('아래 링크를 복사하세요', url));
   else window.prompt('아래 링크를 복사하세요', url);
 }});
 const mobileStartBtn = document.getElementById('mobileRunStart');
 const editButton = document.getElementById('editRoute');
 const editCancel = document.getElementById('editCancel');
 const editDelete = document.getElementById('editDelete');
 const editSave = document.getElementById('editSave');
 const editStatus = document.getElementById('editStatus');
 const editAdd = document.getElementById('editAdd');
 const editMove = document.getElementById('editMove');
 const editUp = document.getElementById('editUp');
 const editDown = document.getElementById('editDown');
 const editUndo = document.getElementById('editUndo');
 const editRedo = document.getElementById('editRedo');
 const editPointList = document.getElementById('editPointList');
 const editBar = document.getElementById('editBar');
 const editN = document.getElementById('editN');
 const editS = document.getElementById('editS');
 const editW = document.getElementById('editW');
 const editE = document.getElementById('editE');
 const mapNode = document.getElementById('map');
 const initLocalCourseEditor = () => {{
   const source = segs.length ? [
     [segs[0][0], segs[0][1]],
     ...segs.map(segment => [segment[2], segment[3]])
   ] : [[{p.lat}, {p.lon}]];
   const all = [...source, ...initialEditWaypoints];
   const latMin=Math.min(...all.map(point=>point[0])), latMax=Math.max(...all.map(point=>point[0]));
   const lonMin=Math.min(...all.map(point=>point[1])), lonMax=Math.max(...all.map(point=>point[1]));
   const latSpan=Math.max(latMax-latMin,.003), lonSpan=Math.max(lonMax-lonMin,.003);
   const toSvg=([lat,lon]) => [70+(lon-lonMin)/lonSpan*860, 650-(lat-latMin)/latSpan*580];
   const fromSvg=(x,y) => [latMin+(650-y)/580*latSpan, lonMin+(x-70)/860*lonSpan];
   const pathFor=points => points.map((point,index) => `${{index?'L':'M'}} ${{toSvg(point).map(value=>value.toFixed(1)).join(' ')}}`).join(' ');
   mapNode.innerHTML='<div class="local-course-editor"><div class="local-course-hint"><strong>지도를 불러오지 못했어요 · 로컬 코스 편집 체험</strong><span id="localCourseHint">경로 수정을 눌러 지점을 선택하세요.</span></div><svg id="localCourseCanvas" viewBox="0 0 1000 720" role="application" aria-label="로컬 코스 편집 캔버스"></svg><div class="local-editor-actions"><button id="localEditRoute" class="local-primary" type="button">경로 수정</button><div class="local-edit-tools" role="toolbar" aria-label="로컬 경로 수정 도구"><ol id="localPointList" class="edit-points" aria-label="경유점 순서"></ol><button id="localEditAdd" type="button" aria-pressed="false">＋ 지점 추가</button><button id="localEditUndo" type="button">되돌리기</button><button id="localEditRedo" type="button">다시 실행</button><button id="localEditN" type="button" aria-label="선택한 경유점을 북쪽으로 이동">↑</button><button id="localEditS" type="button" aria-label="선택한 경유점을 남쪽으로 이동">↓</button><button id="localEditW" type="button" aria-label="선택한 경유점을 서쪽으로 이동">←</button><button id="localEditE" type="button" aria-label="선택한 경유점을 동쪽으로 이동">→</button><button id="localEditCancel" type="button">전체 취소</button><button id="localEditDelete" type="button">선택 삭제</button><button id="localEditSave" class="local-primary" type="button">이 경로로 다시 계산</button></div></div></div>';
   const canvas=document.getElementById('localCourseCanvas');
   const hint=document.getElementById('localCourseHint');
   const localShell=mapNode.querySelector('.local-course-editor');
   const localEditButton=document.getElementById('localEditRoute');
   const localCancel=document.getElementById('localEditCancel');
   const localDelete=document.getElementById('localEditDelete');
   const localSave=document.getElementById('localEditSave');
   const localAdd=document.getElementById('localEditAdd'), localUndo=document.getElementById('localEditUndo'), localRedo=document.getElementById('localEditRedo'), localPointList=document.getElementById('localPointList');
   let localEditing=false, selected=-1, localPoints=initialEditWaypoints.map(point=>[...point]), dragIndex=-1, localMode='select', localUndoStack=[], localRedoStack=[];
   const announce=text => {{ hint.textContent=text; if (editStatus) editStatus.textContent=text; }};
   const localSnapshot=()=>localPoints.map(point=>[...point]);
   const localRemember=()=>{{localUndoStack.push(localSnapshot());localRedoStack=[];}};
   const renderLocal=() => {{
     const controlPath=localPoints.length ? pathFor([[{p.lat},{p.lon}],...localPoints,[{p.lat},{p.lon}]]) : '';
     canvas.innerHTML=`<path d="${{pathFor(source)}}" fill="none" stroke="#78867c" stroke-width="10" stroke-linecap="round" stroke-linejoin="round" opacity=".5"/><path d="${{controlPath}}" fill="none" stroke="#087b59" stroke-width="6" stroke-dasharray="12 10" stroke-linecap="round" stroke-linejoin="round" opacity="${{localEditing?1:.75}}"/>${{localPoints.map((point,index)=>{{const [x,y]=toSvg(point);return `<circle data-index="${{index}}" cx="${{x}}" cy="${{y}}" r="${{selected===index?22:18}}" fill="${{selected===index?'#087b59':'#fff'}}" stroke="#142018" stroke-width="4"/><text x="${{x}}" y="${{y+6}}" text-anchor="middle" pointer-events="none" font-family="Arial" font-size="16" font-weight="700" fill="${{selected===index?'#fff':'#142018'}}">${{index+1}}</text>`;}}).join('')}}`;
     localPointList.replaceChildren(...localPoints.map((point,index)=>{{const li=document.createElement('li'),button=document.createElement('button');button.type='button';button.textContent=`${{index+1}}번`;button.setAttribute('aria-current',String(index===selected));button.onclick=()=>{{selected=index;renderLocal();}};li.appendChild(button);return li;}}));
     localUndo.disabled=!localUndoStack.length;localRedo.disabled=!localRedoStack.length;
   }};
   const localPointEvent=event => {{
     const rect=canvas.getBoundingClientRect(); const SVGPoint=canvas.createSVGPoint();
     SVGPoint.x=(event.clientX-rect.left)*1000/rect.width; SVGPoint.y=(event.clientY-rect.top)*720/rect.height;
     return fromSvg(SVGPoint.x,SVGPoint.y);
   }};
   const setLocalEditing=value => {{
     localEditing=value; localShell.classList.toggle('editing',value); renderLocal();
     announce(value ? '지점을 선택하세요. 추가하려면 지점 추가를 먼저 누르세요.' : '로컬 코스 편집을 마쳤어요.');
   }};
   canvas.addEventListener('pointerdown',event => {{
     if (!localEditing) return; const marker=event.target.closest('[data-index]');
     if (marker) {{ selected=Number(marker.dataset.index); localRemember(); dragIndex=selected; canvas.setPointerCapture(event.pointerId); renderLocal(); announce(`${{selected+1}}번 경유점을 이동 중이에요.`); return; }}
     if (localMode!=='add') return;
     if (localPoints.length>=6) return announce('경유점은 최대 6개까지 추가할 수 있어요.');
     localRemember(); localPoints.push(localPointEvent(event)); selected=localPoints.length-1; localMode='select'; localAdd.setAttribute('aria-pressed','false'); renderLocal(); announce(`${{selected+1}}번 경유점을 추가했어요.`);
   }});
   canvas.addEventListener('pointermove',event => {{ if (dragIndex<0) return; localPoints[dragIndex]=localPointEvent(event); renderLocal(); }});
   canvas.addEventListener('pointerup',event => {{ if (dragIndex>=0) {{ dragIndex=-1; announce(`${{selected+1}}번 경유점을 옮겼어요. 다시 계산해 결과를 확인하세요.`); }} }});
   localEditButton.addEventListener('click', () => setLocalEditing(true));
   localAdd.addEventListener('click',()=>{{localMode=localMode==='add'?'select':'add';localAdd.setAttribute('aria-pressed',String(localMode==='add'));announce(localMode==='add'?'캔버스에서 추가할 위치를 누르세요.':'지점 추가를 취소했어요.');}});
   localUndo.addEventListener('click',()=>{{if(!localUndoStack.length)return;localRedoStack.push(localSnapshot());localPoints=localUndoStack.pop();selected=-1;renderLocal();announce('마지막 편집을 되돌렸어요.');}});
   localRedo.addEventListener('click',()=>{{if(!localRedoStack.length)return;localUndoStack.push(localSnapshot());localPoints=localRedoStack.pop();selected=-1;renderLocal();announce('편집을 다시 실행했어요.');}});
   localCancel.addEventListener('click', () => {{ localPoints=initialEditWaypoints.map(point=>[...point]); selected=-1; setLocalEditing(false); }});
   localDelete.addEventListener('click', () => {{
     if (selected<0) return announce('먼저 삭제할 경유점을 선택하세요.');
     if (localPoints.length<=2) return announce('경유점은 두 곳 이상 필요해요.');
     localRemember(); localPoints.splice(selected,1); selected=Math.min(selected,localPoints.length-1); renderLocal(); announce('선택한 경유점을 삭제했어요.');
   }});
   const localNudge=(dlat,dlon) => {{ if (selected<0) return announce('먼저 이동할 경유점을 선택하세요.'); localRemember(); localPoints[selected][0]+=dlat; localPoints[selected][1]+=dlon; renderLocal(); announce('선택한 경유점을 이동했어요.'); }};
   document.getElementById('localEditN').addEventListener('click', () => localNudge(.00045,0)); document.getElementById('localEditS').addEventListener('click', () => localNudge(-.00045,0)); document.getElementById('localEditW').addEventListener('click', () => localNudge(0,-.00055)); document.getElementById('localEditE').addEventListener('click', () => localNudge(0,.00055));
   localSave.addEventListener('click', () => {{ if (!localEditing) return; setLocalEditing(false); announce('로컬 체험에서 경로를 다시 계산했어요. 실제 지도에서는 도로망 기준 거리·안전 점수가 갱신됩니다.'); }});
   renderLocal();
 }};
 if (inlineStartBtn && mobileDock && 'IntersectionObserver' in window) {{
   new IntersectionObserver((entries) => {{
     mobileDock.style.display = entries[0].isIntersecting ? 'none' : 'grid';
   }}, {{threshold:0.1}}).observe(inlineStartBtn);
 }}
 if (!window.kakao || !kakao.maps) {{
   initLocalCourseEditor();
   mobileStartBtn.disabled = true;
   mobileStartBtn.textContent = '지도 연결 필요';
   if (inlineStartBtn) {{ inlineStartBtn.disabled = true; inlineStartBtn.textContent = '지도 연결 필요'; }}
 }} else kakao.maps.load(() => {{
 const startPos = segs.length
   ? new kakao.maps.LatLng(segs[0][0], segs[0][1])
   : new kakao.maps.LatLng({p.lat}, {p.lon});
 const map = new kakao.maps.Map(mapNode, {{center:startPos, level:6}});
 map.addControl(new kakao.maps.ZoomControl(), kakao.maps.ControlPosition.LEFT);
 const color = s => s >= .62 ? '#18a558' : (s >= .48 ? '#f0a202' : '#dc3d2a');
 const routeLayers = [];
 const shapeLayers = [];
 const guideLayers = [];
 const addPolyline = (path, options, bucket, visible=true) => {{
   const line = new kakao.maps.Polyline({{path, ...options}});
   if (visible) line.setMap(map);
   bucket.push(line);
   return line;
 }};
 const addOverlay = (position, content, bucket) => {{
   const overlay = new kakao.maps.CustomOverlay({{
     position, content, xAnchor:.5, yAnchor:.5, zIndex:5
   }});
   overlay.setMap(map);
   bucket.push(overlay);
   return overlay;
 }};
 const setLayers = (layers, visible) => layers.forEach(layer => layer.setMap(visible ? map : null));
 const route = segs.map(s => [s[0], s[1]]);
 if (segs.length) route.push([segs[segs.length - 1][2], segs[segs.length - 1][3]]);
 const routePath = route.map(([lat, lon]) => new kakao.maps.LatLng(lat, lon));
 addPolyline(routePath, {{strokeColor:'#ffffff',strokeWeight:9,strokeOpacity:.95}}, routeLayers);
 for (const [a, b, c, d, s] of segs)
   addPolyline([new kakao.maps.LatLng(a,b),new kakao.maps.LatLng(c,d)],
     {{strokeColor:color(s),strokeWeight:5,strokeOpacity:.92}},routeLayers);
 const shapePath = shapeRoute.map(([lat, lon]) => new kakao.maps.LatLng(lat, lon));
 const shapeHalo = addPolyline(shapePath, {{strokeColor:'#ffffff',strokeWeight:13,strokeOpacity:.72}}, shapeLayers, false);
 const shapeLine = addPolyline(shapePath, {{strokeColor:'#18a558',strokeWeight:8,strokeOpacity:.92}}, shapeLayers, false);
 const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
 const animateShape = () => {{
   if (reduceMotion || shapePath.length < 24) {{ shapeHalo.setPath(shapePath); shapeLine.setPath(shapePath); return; }}
   let shown = 2;
   shapeHalo.setPath(shapePath.slice(0, shown));
   shapeLine.setPath(shapePath.slice(0, shown));
   const step = () => {{
     shown = Math.min(shapePath.length, shown + Math.max(2, Math.ceil(shapePath.length / 52)));
     const partial = shapePath.slice(0, shown);
     shapeHalo.setPath(partial); shapeLine.setPath(partial);
     if (shown < shapePath.length) requestAnimationFrame(step);
   }};
   requestAnimationFrame(step);
 }};
 const bounds = new kakao.maps.LatLngBounds();
 routePath.forEach(pos => bounds.extend(pos));
 if (routePath.length) map.setBounds(bounds, 42, 42, 42, 42);
 if (segs.length) addOverlay(startPos,
   '<div class="start-marker" title="출발·도착 지점">출발·도착</div>', guideLayers);
 for (const m of dirs) addOverlay(new kakao.maps.LatLng(m.lat,m.lon),
   '<div class="dir-marker" title="진행 방향"><span style="transform:rotate('+m.angle+'deg)">➤</span></div>',guideLayers);
 for (const k of kms) addOverlay(new kakao.maps.LatLng(k.lat,k.lon),
   '<div class="km-marker" title="'+k.km+'km 지점">'+k.km+'</div>',guideLayers);
 let editing = false;
 let selectedEdit = -1;
 let editPoints = initialEditWaypoints.map(([lat, lon]) => [lat, lon]);
 let editMarkers = [];
 let editMode = 'select';
 let undoStack = [], redoStack = [];
 let draftLine = null;
 let lastGestureAt = 0;
 const editImage = (index, selected=false) => new kakao.maps.MarkerImage(
   'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
     '<svg xmlns="http://www.w3.org/2000/svg" width="44" height="44"><circle cx="22" cy="22" r="17" fill="'+(selected?'%23087b59':'%23ffffff')+'" stroke="%23142018" stroke-width="3"/><text x="22" y="28" text-anchor="middle" font-family="Arial" font-size="16" font-weight="700" fill="'+(selected?'%23ffffff':'%23142018')+'">'+(index+1)+'</text></svg>'
   ), new kakao.maps.Size(44,44), {{offset:new kakao.maps.Point(22,22)}}
 );
 const setEditStatus = text => {{ if (editStatus) editStatus.textContent = text; }};
 const snapshot = () => editPoints.map(point => [...point]);
 const remember = () => {{ undoStack.push(snapshot()); if (undoStack.length > 30) undoStack.shift(); redoStack=[]; }};
 const refreshPointList = () => {{
   if (!editPointList) return;
   editPointList.replaceChildren(...editPoints.map((point,index) => {{
     const item=document.createElement('li'); const button=document.createElement('button');
     button.type='button'; button.textContent=`${{index+1}}번`;
     button.setAttribute('aria-current',String(index===selectedEdit));
     button.onclick=()=>{{selectedEdit=index;refreshEditMarkers();}};
     item.appendChild(button); return item;
   }}));
   editUndo.disabled=!undoStack.length; editRedo.disabled=!redoStack.length;
 }};
 const refreshEditMarkers = () => {{
   editMarkers.forEach(marker => marker.setMap(null));
   editMarkers = editPoints.map(([lat, lon], index) => {{
     const marker = new kakao.maps.Marker({{
       position:new kakao.maps.LatLng(lat, lon), draggable:editing,
       image:editImage(index, index === selectedEdit), zIndex:20
     }});
     marker.setMap(editing ? map : null);
     kakao.maps.event.addListener(marker, 'click', () => {{ selectedEdit=index; refreshEditMarkers(); setEditStatus(`${{index+1}}번 지점을 선택했어요.`); }});
     kakao.maps.event.addListener(marker, 'dragstart', () => {{ lastGestureAt=Date.now(); remember(); }});
     kakao.maps.event.addListener(marker, 'dragend', () => {{
       lastGestureAt=Date.now(); const p=marker.getPosition(); editPoints[index]=[p.getLat(),p.getLng()]; selectedEdit=index; refreshEditMarkers();
     }});
     return marker;
   }});
   if (draftLine) draftLine.setMap(null);
   if (editing) {{
     const draft=[[{p.lat},{p.lon}],...editPoints,[{p.lat},{p.lon}]].map(([lat,lon])=>new kakao.maps.LatLng(lat,lon));
     draftLine=new kakao.maps.Polyline({{map,path:draft,strokeColor:'#087b59',strokeWeight:6,strokeOpacity:.9,strokeStyle:'dash'}});
   }}
   refreshPointList();
 }};
 const setEditing = value => {{
   editing=value; document.body.classList.toggle('editing', editing);
   if (!editing) {{ selectedEdit=-1; refreshEditMarkers(); }}
   else {{ refreshEditMarkers(); setEditStatus('지점을 선택하세요. 추가하려면 지점 추가를 먼저 누르세요.'); }}
 }};
 const nudge = (dlat, dlon) => {{
   if (selectedEdit < 0) {{ setEditStatus('먼저 이동할 경유점을 선택하세요.'); return; }}
   editPoints[selectedEdit][0] += dlat; editPoints[selectedEdit][1] += dlon;
   refreshEditMarkers(); setEditStatus('선택한 경유점을 이동했어요.');
 }};
 if (editEnabled && editButton) editButton.addEventListener('click', () => setEditing(true));
 if (editCancel) editCancel.addEventListener('click', () => {{ editPoints=initialEditWaypoints.map(p=>[...p]); setEditing(false); }});
 if (editDelete) editDelete.addEventListener('click', () => {{
   if (selectedEdit < 0) return setEditStatus('먼저 삭제할 경유점을 선택하세요.');
   if (editPoints.length <= 2) return setEditStatus('경유점은 두 곳 이상 필요해요.');
   remember(); editPoints.splice(selectedEdit,1); selectedEdit=Math.min(selectedEdit, editPoints.length-1); refreshEditMarkers();
 }});
 if (editAdd) editAdd.addEventListener('click', () => {{ editMode=editMode==='add'?'select':'add'; editAdd.setAttribute('aria-pressed',String(editMode==='add')); setEditStatus(editMode==='add'?'지도에서 추가할 위치를 누르세요.':'지점 추가를 취소했어요.'); }});
 if (editMove) editMove.addEventListener('click', () => {{
   if (selectedEdit<0) return setEditStatus('먼저 이동할 지점을 선택하세요.');
   editMode=editMode==='move'?'select':'move'; editMove.setAttribute('aria-pressed',String(editMode==='move'));
   setEditStatus(editMode==='move'?'지도에서 새 위치를 누르세요.':'지점 이동을 취소했어요.');
 }});
 const reorder = delta => {{
   if (selectedEdit<0) return setEditStatus('먼저 순서를 바꿀 지점을 선택하세요.');
   const next=selectedEdit+delta; if (next<0 || next>=editPoints.length) return setEditStatus('더 이동할 수 없는 순서예요.');
   remember(); [editPoints[selectedEdit],editPoints[next]]=[editPoints[next],editPoints[selectedEdit]]; selectedEdit=next; refreshEditMarkers(); setEditStatus('경유점 순서를 바꿨어요.');
 }};
 if (editUp) editUp.addEventListener('click',()=>reorder(-1));
 if (editDown) editDown.addEventListener('click',()=>reorder(1));
 if (editUndo) editUndo.addEventListener('click', () => {{ if (!undoStack.length) return; redoStack.push(snapshot()); editPoints=undoStack.pop(); selectedEdit=-1; refreshEditMarkers(); setEditStatus('마지막 편집을 되돌렸어요.'); }});
 if (editRedo) editRedo.addEventListener('click', () => {{ if (!redoStack.length) return; undoStack.push(snapshot()); editPoints=redoStack.pop(); selectedEdit=-1; refreshEditMarkers(); setEditStatus('편집을 다시 실행했어요.'); }});
 if (editN) editN.addEventListener('click', () => nudge(.00045,0));
 if (editS) editS.addEventListener('click', () => nudge(-.00045,0));
 if (editW) editW.addEventListener('click', () => nudge(0,-.00055));
 if (editE) editE.addEventListener('click', () => nudge(0,.00055));
 if (editSave) editSave.addEventListener('click', async () => {{
   if (!editing) return;
   editSave.disabled=true; editBar.setAttribute('aria-busy','true'); setEditStatus('경로 다시 계산 중…');
   const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),3500);
   try {{
     const response=await fetch(editEndpoint, {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{waypoints:editPoints.map(([lat,lon])=>({{lat,lon}}))}}),signal:controller.signal}});
     const payload=await response.json();
     if (!response.ok) throw new Error(payload.error || '경로를 다시 계산하지 못했어요.');
     location.assign(payload.preview_url);
   }} catch (error) {{
     setEditStatus(error.name === 'AbortError' ? '시간이 초과됐어요. 다시 시도해 주세요.' : (error.message || '연결이 불안정해 다시 계산하지 못했어요.'));
     editSave.disabled=false; editCancel.disabled=false;
   }} finally {{ clearTimeout(timer); editBar.setAttribute('aria-busy','false'); }}
 }});
 kakao.maps.event.addListener(map, 'dragstart', () => {{ lastGestureAt=Date.now(); }});
 kakao.maps.event.addListener(map, 'dragend', () => {{ lastGestureAt=Date.now(); }});
 kakao.maps.event.addListener(map, 'zoom_changed', () => {{ lastGestureAt=Date.now(); }});
 kakao.maps.event.addListener(map, 'click', event => {{
   if (!editing || !['add','move'].includes(editMode) || Date.now()-lastGestureAt < 250) return;
   const p=event.latLng; remember();
   if (editMode==='move') {{ editPoints[selectedEdit]=[p.getLat(),p.getLng()]; editMove.setAttribute('aria-pressed','false'); setEditStatus(`${{selectedEdit+1}}번 지점을 옮겼어요.`); }}
   else {{ if (editPoints.length >= 6) {{ undoStack.pop(); return setEditStatus('경유점은 최대 6개까지 추가할 수 있어요.'); }} editPoints.push([p.getLat(),p.getLng()]); selectedEdit=editPoints.length-1; editAdd.setAttribute('aria-pressed','false'); setEditStatus(`${{selectedEdit+1}}번 지점을 추가했어요.`); }}
   editMode='select'; refreshEditMarkers();
 }});
 const geocoder = (kakao.maps.services && kakao.maps.services.Geocoder)
   ? new kakao.maps.services.Geocoder() : null;
 let openPop = null;
 const closePop = () => {{ if (openPop) {{ openPop.style.display = 'none'; openPop = null; }} }};
 kakao.maps.event.addListener(map, 'click', closePop);
 const addFacility = m => {{
   const el = document.createElement('div');
   el.className = 'facility-marker ' + m.type;
   el.title = m.label;
   const pop = document.createElement('div');
   pop.className = 'poi-pop';
   pop.style.display = 'none';
   const nameEl = document.createElement('b');
   nameEl.textContent = m.name;
   const addrEl = document.createElement('span');
   addrEl.textContent = m.label;
   pop.appendChild(nameEl);
   pop.appendChild(addrEl);
   el.appendChild(pop);
   let addressAsked = false;
   const show = () => {{
     if (openPop && openPop !== pop) openPop.style.display = 'none';
     pop.style.display = 'block';
     openPop = pop;
     if (!addressAsked && geocoder) {{
       addressAsked = true;
       addrEl.textContent = '주소 확인 중…';
       geocoder.coord2Address(m.lon, m.lat, (res, status) => {{
         const ok = status === kakao.maps.services.Status.OK && res && res[0];
         const addr = ok ? (res[0].road_address
           ? res[0].road_address.address_name : res[0].address.address_name) : '';
         addrEl.textContent = addr || m.label;
       }});
     }}
   }};
   const hide = () => {{ pop.style.display = 'none'; if (openPop === pop) openPop = null; }};
   el.addEventListener('mouseenter', show);
   el.addEventListener('mouseleave', hide);
   el.addEventListener('click', ev => {{
     ev.stopPropagation();
     pop.style.display === 'none' ? show() : hide();
   }});
   const overlay = new kakao.maps.CustomOverlay({{
     position: new kakao.maps.LatLng(m.lat, m.lon), content: el,
     xAnchor:.5, yAnchor:.5, zIndex:6
   }});
   overlay.setMap(map);
   guideLayers.push(overlay);
 }};
 for (const m of {markers}) addFacility(m);
 const shapeView = document.getElementById('shapeView');
 const guideView = document.getElementById('guideView');
 const setMapMode = mode => {{
   const shapeOnly = mode === 'shape';
   document.body.classList.toggle('shape-only', shapeOnly);
   shapeView.classList.toggle('active', shapeOnly);
   guideView.classList.toggle('active', !shapeOnly);
   if (shapeOnly) {{
     setLayers(routeLayers, false);
     setLayers(guideLayers, false);
     setLayers(shapeLayers, true);
     animateShape();
   }} else {{
     setLayers(shapeLayers, false);
     setLayers(routeLayers, true);
     setLayers(guideLayers, true);
   }}
 }};
 shapeView.addEventListener('click', () => setMapMode('shape'));
 guideView.addEventListener('click', () => setMapMode('guide'));
 const startBtn = document.getElementById('runStart');
 const runStatus = document.getElementById('runStatus');
 let watchId = null;
 let userMarker = null;
 let accuracyCircle = null;
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
 const syncStartButtons = () => {{
   const label = startBtn.textContent === '추적 중지' ? '추적 중지' : '내 위치 추적 시작';
   mobileStartBtn.textContent = label;
   if (inlineStartBtn) inlineStartBtn.textContent = label;
   mobileStartBtn.disabled = startBtn.disabled;
   if (inlineStartBtn) inlineStartBtn.disabled = startBtn.disabled;
 }};
 mobileStartBtn.addEventListener('click', () => startBtn.click());
 if (inlineStartBtn) inlineStartBtn.addEventListener('click', () => startBtn.click());
 const updatePosition = pos => {{
   const lat = pos.coords.latitude;
   const lon = pos.coords.longitude;
   const acc = Math.round(pos.coords.accuracy || 0);
   const off = Math.round(nearestRouteM(lat, lon));
   const posLatLng = new kakao.maps.LatLng(lat, lon);
   if (!userMarker) {{
     userMarker = addOverlay(posLatLng, '<div class="user-dot" title="현재 위치"></div>', guideLayers);
     accuracyCircle = new kakao.maps.Circle({{
       center:posLatLng,radius:acc,strokeWeight:1,strokeColor:'#e5322e',
       strokeOpacity:.8,fillColor:'#e5322e',fillOpacity:.06
     }});
     accuracyCircle.setMap(map);
     guideLayers.push(accuracyCircle);
   }} else {{
     userMarker.setPosition(posLatLng);
     accuracyCircle.setPosition(posLatLng);
     accuracyCircle.setRadius(acc);
   }}
   const guide = off > 80 ? `코스에서 약 ${{off}}m 벗어남` : `코스 위를 달리는 중 · 오차 ${{acc}}m`;
   setStatus(guide);
 }};
 const locationError = err => {{
   const msg = err.code === 1 ? '브라우저 설정에서 위치 권한을 허용해 주세요'
     : err.code === 2 ? 'GPS 신호가 약해 위치를 찾지 못했어요'
     : '위치 확인 시간이 초과됐어요';
   setStatus(msg);
   startBtn.classList.remove('on');
   startBtn.textContent = '다시 시도';
   syncStartButtons();
   watchId = null;
 }};
 startBtn.addEventListener('click', () => {{
   if (!window.isSecureContext) {{
     setStatus('위치 기능은 HTTPS에서만 사용할 수 있어요');
     return;
   }}
   if (!navigator.geolocation) {{
     setStatus('이 브라우저는 위치 기능을 지원하지 않아요');
     return;
   }}
   if (watchId !== null) {{
     navigator.geolocation.clearWatch(watchId);
     watchId = null;
     startBtn.classList.remove('on');
     startBtn.textContent = '내 위치 보기';
     syncStartButtons();
     setStatus('GPS 안내 중지');
     return;
   }}
   setStatus('GPS 위치 확인 중');
   startBtn.classList.add('on');
   startBtn.textContent = '추적 중지';
   syncStartButtons();
   watchId = navigator.geolocation.watchPosition(updatePosition, locationError, {{
     enableHighAccuracy:true, maximumAge:3000, timeout:12000
   }});
 }});
 if (!window.isSecureContext || !navigator.geolocation) {{
   startBtn.disabled = true;
   setStatus(!window.isSecureContext ? 'HTTPS 연결에서 위치 기능을 사용할 수 있어요' : '이 브라우저는 위치 기능을 지원하지 않아요');
 }}
 syncStartButtons();
 }});
</script></body></html>"""


# ---------- share card (SVG, no image deps) ----------

def card_svg(course: Course) -> str:
    p = course.params
    shape = SHAPES.get(p.shape) if p.shape else None
    title = (f"{shape.emoji} {shape.name_ko} 모양" if shape else "🏃 러닝 코스")
    w, h = 800, 418
    card_points = route_points(course)
    lats = [pt[0] for pt in card_points]
    lons = [pt[1] for pt in card_points]
    lat_c = (max(lats) + min(lats)) / 2
    span_lat = max(max(lats) - min(lats), 1e-6)
    span_lon = max(max(lons) - min(lons), 1e-6) * 0.79  # rough lon shrink at 37.5N
    box = 300.0
    scale = box / max(span_lat, span_lon)
    cx, cy = 545, h / 2
    pts = " ".join(
        f"{cx + (lon - (max(lons) + min(lons)) / 2) * scale * 0.79:.1f},"
        f"{cy - (lat - lat_c) * scale:.1f}"
        for lat, lon in card_points
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
   font-family="-apple-system,sans-serif">Runnywhere(러니웨어) — 어디서든 러닝 코스 짜기!</text>
 <text x="40" y="{h - 18}" font-size="12" fill="#666"
   font-family="-apple-system,sans-serif">경로 데이터 © OpenStreetMap contributors · ODbL · 코스는 참고용</text>
 <polyline points="{pts}" fill="none" stroke="#e0533d" stroke-width="5"
   stroke-linejoin="round" stroke-linecap="round"/>
</svg>"""
