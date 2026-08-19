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
from .facilities import LABELS_KO, facilities_along
from .geo import haversine_m, to_xy
from .infrastructure import pedestrian_signals_crossed
from .models import encode_course_id
from .naming import course_badges, course_title
from .pace import DEFAULT_PACE_S, PACE_MODEL, PACE_TIERS, effort
from .rfs import COMPONENT_LABELS_KO, edge_rfs
from .shapes import SHAPES

PREVIEW_FACILITY_TYPES = {"convenience_store", "restroom"}
FACILITY_CHIP_LIMIT = 10




def script_json(value) -> str:
    """JSON safe to embed inside a <script> element.

    `<` must never reach the HTML parser literally: a `</script>` inside any
    string -- a facility name, a place name -- would end the element early.
    Escaping `<` alone also neutralises `<!--`.
    """
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def markdown_text(value: str) -> str:
    """Escape untrusted labels embedded in MCP Markdown responses."""
    value = "".join(ch for ch in value if ch >= " " and ch != "\x7f")[:120]
    for char in "\\`*_{}[]()<>#+-.!|":
        value = value.replace(char, "\\" + char)
    return value


def course_markdown(course: Course, base_url: str, facilities: list[dict]) -> str:
    p = course.params
    cid = encode_course_id(p)
    badges = course_badges(course)
    title = "".join(b["emoji"] for b in badges) + " " + course_title(course)
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


def _elevation_range(profile: list) -> tuple[int, int] | None:
    """(low, high) metres over the course, or None when the graph has no
    elevation for it. Distinct from cumulative ascent: a flat riverside loop
    can climb 30m in total while never leaving a 4m band."""
    if not profile:
        return None
    elevations = [e for _, e in profile]
    return round(min(elevations)), round(max(elevations))


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
    ids = ("factSignals", "factStores", "factRestrooms")
    cells = "".join(
        f'<div class="fact"><b id="{el_id}">{value}</b><span>{label}</span></div>'
        for (label, value), el_id in zip(items, ids)
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


def course_edit_summary(course: Course) -> dict:
    """The live numbers the detail panels show, recomputed for an edited course.

    Returned by the snap endpoint so the page below the map stops describing
    the course the user just changed. Mirrors what preview_html() renders on a
    full page load, so the two never disagree.
    """
    g = graphmod.get_graph()
    points = route_points(course)
    facilities = [f for f in facilities_along(points, sorted(PREVIEW_FACILITY_TYPES), limit=80)
                  if f["type"] in PREVIEW_FACILITY_TYPES]
    elev_range = _elevation_range(_elevation_profile(course))
    lo, hi = course.duration_range_min
    counts = {t: sum(1 for f in facilities if f["type"] == t)
              for t in sorted(PREVIEW_FACILITY_TYPES)}
    return {
        "length_km": round(course.length_km, 2),
        "ascent_m": round(course.ascent_m),
        "elev_range": list(elev_range) if elev_range else None,
        "rfs": course.rfs["score"],
        "rfs_grade": _rfs_grade_ko(course.rfs["score"]),
        "grade_label": course.grade_label,
        "duration_min": [lo, hi],
        "signals": pedestrian_signals_crossed(g, course.path),
        "highlights": course.rfs.get("highlights", [])[:2],
        "facility_counts": counts,
        "facility_tally": " · ".join(f"{LABELS_KO[t]} {counts[t]}곳" for t in counts),
        "facility_chips": [f"{LABELS_KO[f['type']]} · {f['at_km']:g}km"
                           for f in facilities[:FACILITY_CHIP_LIMIT]],
        "badges": [{"emoji": b["emoji"], "label": b["label"]} for b in course_badges(course)],
        "title": course_title(course),
    }


def preview_html(course: Course, facilities: list[dict], base_url: str,
                 kakao_javascript_key: str = "") -> str:
    facilities = [f for f in facilities if f["type"] in PREVIEW_FACILITY_TYPES]
    p = course.params
    cid = encode_course_id(p)
    shape = SHAPES.get(p.shape) if p.shape else None
    title = html.escape(course_title(course))
    badges = course_badges(course)
    badge_html = "".join(
        f'<span class="badge" role="img" aria-label="{html.escape(b["label"])}"'
        f' title="{html.escape(b["label"])}">{b["emoji"]}</span>'
        for b in badges
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
    profile = _elevation_profile(course)
    profile_svg = _profile_svg(profile)
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
    # Surfaced as a toast when editing starts. Saving an edited animal course
    # drops the silhouette, so this expectation has to reach sighted users --
    # it used to live in an sr-only node where nobody could see it.
    edit_notice = (
        "원본 동물 코스는 유지되고, 저장하면 새 직접 편집한 코스가 만들어져요."
        if shape else ""
    )
    g = graphmod.get_graph()
    edit_path = json.dumps([
        [node, round(g.nodes[node]["lat"], 6), round(g.nodes[node]["lon"], 6)]
        for node in course.path
    ])
    elev_range = _elevation_range(profile)
    elev_text = (f"{elev_range[0]}~{elev_range[1]}<i>m</i>" if elev_range else "정보 없음")
    initial_effort = effort(course.length_km, DEFAULT_PACE_S)
    # Quick jumps to each named band, at the slowest pace that still belongs
    # to it, so tapping a chip lands on a round, recognisable number.
    pace_chips = "".join(
        f'<button type="button" class="pace-chip" data-pace="{floor_s or 240}">{name}</button>'
        for floor_s, name, _ in PACE_TIERS
    )
    pace_model = script_json(PACE_MODEL)
    initial_summary = script_json(course_edit_summary(course))
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
 .brand strong{{font-size:clamp(14px,4.2vw,18px);color:#142018;letter-spacing:-.03em;word-break:keep-all}}
 .brand-tagline{{color:#66726a;font-weight:600}}
 /* The Kakao SDK sets position:relative on its container at runtime; declaring
    it here stops every absolutely positioned map control (HUD, toolbar, toast)
    from depending on that side effect. */
 #map{{position:relative;height:62vh;min-height:460px;background:#e8ece5}}
 .local-course-editor{{height:100%;position:relative;overflow:hidden;background:#e8ece5}}
 .local-course-editor svg{{width:100%;height:100%;display:block;touch-action:none;background:linear-gradient(135deg,#f5f8f2 25%,#e9f0e7 25%,#e9f0e7 50%,#f5f8f2 50%,#f5f8f2 75%,#e9f0e7 75%);background-size:42px 42px}}
 .local-course-hint{{position:absolute;z-index:2;left:14px;right:14px;top:14px;padding:10px 12px;border-radius:12px;background:rgba(255,255,255,.94);box-shadow:0 4px 18px rgba(0,0,0,.1);font-size:13px;font-weight:700;line-height:1.4;color:#243028}}
 .local-course-hint strong{{display:block;color:#087b59;margin-bottom:2px}}
 .local-editor-actions{{position:absolute;z-index:3;inset:0;pointer-events:none}}
 .local-editor-actions button{{min-height:46px;padding:0 13px;border:0;border-radius:12px;background:#fff;color:#142018;box-shadow:0 4px 18px rgba(0,0,0,.14);font:700 14px inherit}}
 .local-editor-actions .local-primary{{background:#087b59;color:#fff}}
 #localEditRoute{{position:absolute;left:14px;bottom:14px;pointer-events:auto}}
 .local-editor-actions .local-edit-tools{{display:none;position:absolute;left:10px;top:10px;gap:6px;pointer-events:auto}}
 .local-course-editor.editing .local-edit-tools{{display:flex}}
 .local-course-editor.editing #localEditRoute{{display:none}}
 .local-course-editor.editing .local-course-hint{{top:60px;left:10px;right:auto;max-width:calc(100% - 20px);padding:7px 9px;box-shadow:none}}
 .map-error{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:24px;
      box-sizing:border-box;text-align:center;color:#44514a;font-size:14px;line-height:1.5;background:#eef2ec}}
 .map-error strong{{font-size:16px;color:#142018}}
 .map-error button{{min-height:44px;padding:0 18px;border:1px solid #c3cec6;border-radius:12px;
      background:#fff;color:#142018;font-size:14px;font-weight:700;font-family:inherit;cursor:pointer}}
 /* The two map entry points sit in the top corners; the metric pills sit under
    them so nothing competes for the corner a thumb reaches for. */
 .map-hud{{position:absolute;z-index:500;left:14px;right:14px;top:64px;display:flex;gap:8px;flex-wrap:wrap;pointer-events:none}}
 .pill{{background:rgba(255,255,255,.94);border:1px solid rgba(20,35,25,.08);border-radius:10px;
      padding:9px 11px;font-size:13px;font-weight:720;box-shadow:0 4px 18px rgba(0,0,0,.08);backdrop-filter:blur(8px)}}
 .run-panel{{position:absolute;z-index:520;left:14px;right:14px;bottom:16px;display:flex;gap:8px;align-items:center;pointer-events:none}}
 .run-panel button,.run-status{{pointer-events:auto;border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.14)}}
 .run-panel button{{min-height:48px;border:0;background:#142018;color:#fff;padding:0 16px;font-size:14px;font-weight:800;font-family:inherit}}
 .run-panel button:disabled{{background:#59635c;cursor:not-allowed}}
 .run-panel button.on{{background:#0a7d43}}
 .run-status{{background:rgba(255,255,255,.96);border:1px solid rgba(20,35,25,.08);padding:10px 12px;
      color:#243028;font-size:13px;font-weight:700;line-height:1.35;min-width:128px}}
 .view-toggle{{position:absolute;z-index:530;right:14px;top:14px;display:flex;background:rgba(255,255,255,.96);
      border:1px solid rgba(20,35,25,.1);border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.1);overflow:hidden}}
 .view-toggle button{{min-height:48px;border:0;background:transparent;color:#4b5a50;padding:0 13px;font-size:13px;font-weight:800;font-family:inherit}}
 .view-toggle button.active{{background:#142018;color:#fff}}
 body.shape-only .map-hud,body.shape-only .run-panel{{display:none}}
 .wrap{{padding:22px;max-width:1040px;margin:0 auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
 .card,.panel{{background:#fff;border:1px solid #dfe7e1;border-radius:18px;padding:22px;margin:0;box-shadow:0 12px 34px rgba(20,45,30,.045)}}
 .course-summary{{grid-column:1/-1}}
 /* Title and badges share a baseline row; badges never push the name to wrap. */
 .course-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:8px}}
 .course-badges{{display:flex;flex-shrink:0;gap:4px;padding-top:2px}}
 .badge{{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
      border-radius:999px;background:#f2f6f0;border:1px solid #e1e7dd;font-size:16px;line-height:1}}
 h1{{margin:0;font-size:26px;line-height:1.28;letter-spacing:-.035em;word-break:keep-all}}
 h2,h3{{margin:0 0 12px;font-size:17px;letter-spacing:-.02em}}
 .stat{{color:#3d473f;line-height:1.65;font-size:15px}}
 /* Distance is a fact about the course; time is a consequence of the pace
    the runner picks. They sit on one line so the causal pair reads together. */
 .effort-line{{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin:2px 0 14px}}
 .effort-distance{{font-size:34px;font-weight:800;color:#0a7d43;letter-spacing:-.04em;line-height:1}}
 .effort-duration{{font-size:34px;font-weight:800;color:#142018;letter-spacing:-.04em;line-height:1}}
 .effort-line i{{font-style:normal;font-size:17px;font-weight:700;margin-left:2px;color:#55605a}}
 .pace-picker{{border:1px solid #e1e7dd;background:#f7faf5;border-radius:14px;padding:13px 14px 11px;margin-bottom:14px}}
 .pace-head{{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}}
 .pace-caption{{font-size:13px;font-weight:700;color:#55605a}}
 .pace-read{{margin-left:auto;font-size:20px;font-weight:800;color:#142018;letter-spacing:-.02em}}
 .pace-read i{{font-style:normal;font-size:13px;font-weight:700;color:#55605a;margin-left:1px}}
 .pace-tier{{padding:4px 9px;border-radius:999px;background:#0a7d43;color:#fff;font-size:12px;font-weight:800}}
 /* Thumb is 28px but the input owns 44px of vertical space, so the tap
    target clears the minimum without a fat visible track. */
 .pace-range{{-webkit-appearance:none;appearance:none;width:100%;height:44px;margin:2px 0 0;background:transparent;display:block}}
 .pace-range:focus-visible{{outline:3px solid #8ee0bb;outline-offset:3px;border-radius:8px}}
 .pace-range::-webkit-slider-runnable-track{{height:6px;border-radius:999px;
      background:linear-gradient(90deg,#0a7d43,#8ee0bb)}}
 .pace-range::-moz-range-track{{height:6px;border-radius:999px;
      background:linear-gradient(90deg,#0a7d43,#8ee0bb)}}
 .pace-range::-webkit-slider-thumb{{-webkit-appearance:none;appearance:none;width:28px;height:28px;
      margin-top:-11px;border-radius:50%;background:#fff;border:3px solid #0a7d43;
      box-shadow:0 2px 8px rgba(10,28,19,.24);cursor:grab}}
 .pace-range::-moz-range-thumb{{width:28px;height:28px;border-radius:50%;background:#fff;
      border:3px solid #0a7d43;box-shadow:0 2px 8px rgba(10,28,19,.24);cursor:grab}}
 .pace-range:active::-webkit-slider-thumb{{cursor:grabbing;transform:scale(.94)}}
 .pace-chips{{display:flex;gap:6px;margin-top:2px}}
 .pace-chip{{flex:1;min-height:34px;border:1px solid #dce3d8;border-radius:9px;background:#fff;
      color:#44514a;font-family:inherit;font-size:12px;font-weight:700;cursor:pointer;padding:0}}
 .pace-chip[aria-pressed="true"]{{background:#142018;border-color:#142018;color:#fff}}
 .pace-feel{{margin:9px 0 0;font-size:12px;color:#55605a;line-height:1.4;word-break:keep-all}}
 .metric-wide{{grid-column:1/-1}}
 .metric-value i{{font-style:normal;font-size:14px;font-weight:700;color:#55605a;margin-left:2px}}
 .metric-note-inline{{margin:8px 0 0;font-size:12px;color:#55605a}}
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
 /* Route decorations are labels, not controls: a drag that happens to start on
    a km bubble or a direction arrow must still pan the map. */
 .km-marker,.dir-marker,.start-marker{{pointer-events:none}}
 .km-marker{{background:#fff;border:2px solid #111;border-radius:999px;width:24px;height:24px;line-height:20px;
      text-align:center;font-size:11px;font-weight:800;box-shadow:0 2px 8px rgba(0,0,0,.2)}}
 .dir-marker svg{{display:block;width:11px;height:11px;fill:none;stroke:#fff;stroke-width:2.4;
      stroke-linecap:round;stroke-linejoin:round;transform-origin:50% 50%;
      filter:drop-shadow(0 1px 1px rgba(0,0,0,.42))}}
 .start-marker{{background:#142018;color:#fff;border:2px solid #fff;border-radius:999px;padding:6px 9px;
      font-size:12px;font-weight:800;box-shadow:0 3px 12px rgba(0,0,0,.28);white-space:nowrap}}
 .user-dot{{width:18px;height:18px;background:#e5322e;border:3px solid #fff;border-radius:999px;
      box-shadow:0 0 0 8px rgba(229,50,46,.18),0 2px 10px rgba(0,0,0,.25)}}
 .facility-marker{{position:relative;width:12px;height:12px;border:2px solid #fff;border-radius:999px;
      box-shadow:0 2px 8px rgba(0,0,0,.24);cursor:pointer}}
 /* A 12px dot is unhittable with a finger. Widen the tap area to 44px on touch
    only -- on a mouse the dot is already precise, and enlarging it there would
    swallow map drags that start near a marker. */
 @media (pointer:coarse){{.facility-marker::before{{content:"";position:absolute;top:-16px;right:-16px;
      bottom:-16px;left:-16px;border-radius:50%}}}}
 .facility-marker.convenience_store{{background:#2563eb}}
 .facility-marker.restroom{{background:#0a9d4f}}
 .poi-pop{{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);background:#fff;
      border:1px solid rgba(20,35,25,.14);border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.18);
      padding:8px 10px;min-width:150px;max-width:220px;z-index:900;text-align:left;pointer-events:none}}
 .poi-pop b{{display:block;font-size:13px;color:#142018;margin-bottom:2px;white-space:nowrap;
      overflow:hidden;text-overflow:ellipsis}}
 .poi-pop span{{font-size:13px;color:#5c675e;line-height:1.4;word-break:keep-all}}
 .mobile-dock{{display:none}}
 .sr-only{{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}}
 #editRoute{{position:absolute;z-index:540;left:14px;top:14px;min-height:46px;border:0;border-radius:12px;padding:0 14px;background:#fff;color:#142018;font-family:inherit;font-size:13px;font-weight:700;box-shadow:0 4px 18px rgba(0,0,0,.12)}}
 .edit-tools{{position:absolute;z-index:950;left:10px;top:10px;display:none;gap:6px}}
 .edit-tool-circle{{position:relative;display:inline-flex;align-items:center;justify-content:center;width:40px;height:40px;min-height:40px!important;padding:0!important;border:0;border-radius:50%!important;background:#fff!important;color:#142018!important;box-shadow:0 2px 8px rgba(10,28,19,.18)!important;cursor:pointer}}
 /* 44x44 tap target without growing the 40px button: the icons stay small by
    product requirement, so the touch area is widened invisibly instead. The
    6px gap leaves 2px clear between neighbouring hit areas. */
 .edit-tool-circle::before{{content:"";position:absolute;top:-2px;right:-2px;bottom:-2px;left:-2px;border-radius:50%}}
 .edit-tool-circle svg{{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}}
 .edit-tool-circle:disabled{{opacity:.42;cursor:not-allowed}}
 /* Extra room before the confirming action so a mis-tap does not discard work. */
 .edit-tool-circle.save{{margin-left:8px;background:#087b59!important;color:#fff!important}}
 .edit-tool-circle[aria-pressed="true"]{{background:#142018!important;color:#fff!important;outline:3px solid #8ee0bb;outline-offset:2px}}
 /* display:none when not drawing. pointer-events:none alone was enough in
    theory, but a full-bleed touch-action:none layer over the map is exactly
    the kind of thing that eats a drag on mobile -- keep it out of the tree
    unless a drawing tool is actually selected. */
 .edit-overlay{{position:absolute;z-index:930;inset:0;width:100%;height:100%;touch-action:none;pointer-events:none;display:none}}
 body.editing.tool-active .edit-overlay{{display:block}}
 /* Non-blocking edit feedback: one line pinned to the map's bottom edge so the
    drawing area stays clear (the large overlay panel was removed in f69e246). */
 .edit-toast{{position:absolute;z-index:960;left:10px;right:10px;bottom:calc(10px + env(safe-area-inset-bottom));
      display:flex;align-items:center;gap:9px;min-height:44px;padding:9px 12px;border-radius:12px;
      background:rgba(20,32,24,.95);color:#fff;font-size:14px;font-weight:700;line-height:1.35;
      box-shadow:0 6px 22px rgba(0,0,0,.3);backdrop-filter:blur(8px);word-break:keep-all;
      opacity:0;transform:translateY(8px);transition:opacity .18s ease-out,transform .18s ease-out}}
 .edit-toast[hidden]{{display:none}}
 .edit-toast[data-open]{{opacity:1;transform:none}}
 .edit-toast[data-tone="error"]{{background:#a32b1e}}
 .edit-toast[data-tone="blocked"]{{background:#7a4e00}}
 .edit-toast-text{{flex:1;min-width:0}}
 .edit-toast-action{{flex:0 0 auto;min-height:36px;padding:0 12px;border:0;border-radius:9px;
      background:rgba(255,255,255,.2);color:#fff;font-family:inherit;font-size:13px;font-weight:800;cursor:pointer}}
 .edit-toast-spin{{flex:0 0 auto;width:16px;height:16px;border:2px solid rgba(255,255,255,.35);
      border-top-color:#fff;border-radius:50%;display:none}}
 .edit-toast[data-tone="busy"] .edit-toast-spin{{display:block;animation:editspin .8s linear infinite}}
 @keyframes editspin{{to{{transform:rotate(360deg)}}}}
 /* The distance the user is editing toward — the whole point of the product.
    On its own row under the toolbar: six 40px tools make the toolbar 278px
    wide, which collides with a top-right chip at 375px. The pill row that
    normally occupies this line is hidden while editing. */
 .edit-distance{{position:absolute;z-index:950;right:10px;top:58px;display:none;align-items:center;
      min-height:40px;padding:0 13px;border-radius:999px;background:rgba(255,255,255,.96);
      color:#142018;font-size:14px;font-weight:800;box-shadow:0 2px 8px rgba(10,28,19,.18)}}
 body.editing .edit-distance{{display:flex}}
 .edit-tools[aria-busy="true"] .edit-tool-circle{{opacity:.45}}
 body.editing .edit-tools{{display:flex}}body.editing .mobile-dock{{display:none!important}}
 body.editing .map-hud,body.editing .run-panel,body.editing .view-toggle,body.editing #editRoute{{display:none!important}}
 body.editing.tool-active .facility-marker{{pointer-events:none;opacity:.2}}
 body.editing.tool-active .edit-overlay{{pointer-events:auto}}
 footer{{color:#55605a;font-size:13px;padding:8px 20px 28px;text-align:center;line-height:1.6}}
 footer a{{display:inline-block;padding:8px 4px;color:inherit}}
 @media (max-width:760px){{.brand{{height:48px;padding:0 16px}} .facts{{grid-template-columns:repeat(2,1fr)}}
      #map{{height:clamp(280px,42svh,380px);min-height:0}}.map-hud{{left:10px;right:10px;top:58px;gap:6px}}.pill{{font-size:13px;padding:8px 9px}}
      .view-toggle{{right:10px;top:10px}}#editRoute{{left:10px;top:10px}}.run-panel{{left:10px;right:10px;bottom:12px}}
      .course-head{{gap:8px}}.badge{{width:28px;height:28px;font-size:15px}}
      .wrap{{display:block;padding:0 16px 96px}}.card,.panel{{padding:18px;margin-bottom:12px;border-radius:16px}}h1{{font-size:22px;line-height:1.25;word-break:keep-all}}
      .actions{{grid-template-columns:1fr 1fr}}.actions .btn{{padding:0 8px;text-align:center}}
      .mobile-dock{{position:fixed;z-index:950;display:none;grid-template-columns:1fr;gap:8px;left:10px;right:10px;bottom:calc(8px + env(safe-area-inset-bottom));padding:8px;background:rgba(255,255,255,.94);border:1px solid rgba(20,35,25,.1);border-radius:18px;box-shadow:0 16px 45px rgba(10,28,19,.24);backdrop-filter:blur(14px)}}
      .mobile-dock a,.mobile-dock button{{display:flex;min-height:50px;align-items:center;justify-content:center;border:0;border-radius:12px;font-size:14px;font-weight:750;font-family:inherit;text-decoration:none}}.mobile-dock a{{background:#edf5f0;color:#142018}}.mobile-dock button{{background:#087b59;color:#fff}}.mobile-dock button:disabled{{background:#59635c}}
      footer{{padding-bottom:96px}}.course-metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.edit-bar{{left:10px;right:10px;bottom:calc(8px + env(safe-area-inset-bottom));}}
      .metric-value{{font-size:20px;line-height:1.2;font-variant-numeric:tabular-nums;white-space:nowrap}}.metric-label{{font-size:13px;line-height:1.35}}.supporting-copy{{font-size:13px;line-height:1.45;word-break:normal;line-break:strict}}
      footer{{padding-bottom:96px}}}}
 @media (orientation:landscape) and (max-width:900px){{#map{{height:280px}}.wrap{{padding-bottom:72px}}}}
 @media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important;animation-duration:.001ms!important;transition-duration:.001ms!important}}}}
</style></head><body>
<header class="brand"><strong>러니웨어<span class="brand-tagline">: 어디서든 러닝 코스 짜기!</span></strong></header>
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
 </div>{'<button id="editRoute" type="button">코스 편집</button>' if edit_enabled else ''}<svg id="editOverlay" class="edit-overlay" aria-hidden="true"></svg><div class="edit-tools" role="toolbar" aria-label="코스 편집 도구"><button id="panTool" class="edit-tool-circle" type="button" aria-label="지도 이동" title="지도 이동" aria-pressed="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v18M3 12h18"/><path d="m9 6 3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/></svg></button><button id="drawTool" class="edit-tool-circle" type="button" aria-label="펜으로 코스 선 그리기" title="펜" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 20 4.3-1 10.4-10.4a2.1 2.1 0 0 0-3-3L5.3 16Z"/><path d="m14.5 6.8 3 3"/></svg></button><button id="eraseTool" class="edit-tool-circle" type="button" aria-label="직선 지우개로 코스 구간 지우기" title="지우개" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4.7 14.7 8.6-8.6a2 2 0 0 1 2.8 0l2 2a2 2 0 0 1 0 2.8l-7.4 7.4a2 2 0 0 1-2.8 0Z"/><path d="m11 18 7 0M8.3 11.1l4.6 4.6"/></svg></button><button id="editUndo" class="edit-tool-circle" type="button" aria-label="마지막 선 수정 되돌리기" title="되돌리기"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8v5h5"/><path d="M5.5 12a7 7 0 1 0 2-5"/></svg></button><button id="editCancel" class="edit-tool-circle" type="button" aria-label="원본 코스로 복구" title="원본으로"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg></button><button id="editSave" class="edit-tool-circle save" type="button" aria-label="수정한 코스를 새 코스로 저장" title="저장"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h12l2 2v16H5Z"/><path d="M8 3v6h8V3M8 21v-7h8v7"/></svg></button></div><div id="editDistance" class="edit-distance" aria-label="수정 중인 코스 거리"></div><div id="editToast" class="edit-toast" role="status" aria-live="polite" data-tone="info" hidden><span class="edit-toast-spin" aria-hidden="true"></span><span id="editToastText" class="edit-toast-text"></span><button id="editToastAction" class="edit-toast-action" type="button" hidden></button></div></div>
<div class="wrap">
<div class="card course-summary">
 <div class="course-head"><h1 id="courseTitle">{title}</h1><div class="course-badges" id="courseBadges">{badge_html}</div></div>
 {where_html}
 <div class="effort-line">
  <span class="effort-distance" id="mLength">{course.length_km:.1f}<i>km</i></span>
  <span class="effort-duration"><b id="mDuration">{initial_effort["duration_min"]}</b><i>분</i></span>
 </div>
 <div class="pace-picker">
  <div class="pace-head">
   <span class="pace-caption">내 페이스</span>
   <span class="pace-read"><b id="paceValue">{initial_effort["pace_label"]}</b><i>/km</i></span>
   <span class="pace-tier" id="paceTier">{initial_effort["tier"]}</span>
  </div>
  <input id="paceRange" class="pace-range" type="range" min="{PACE_MODEL["fastest_s"]}"
   max="{PACE_MODEL["slowest_s"]}" step="{PACE_MODEL["step_s"]}" value="{DEFAULT_PACE_S}"
   aria-label="1km당 목표 페이스" aria-describedby="paceFeel"
   aria-valuetext="{initial_effort["pace_label"]} 퍼 킬로미터, {initial_effort["tier"]}">
  <div class="pace-chips" role="group" aria-label="페이스 빠르게 고르기">{pace_chips}</div>
  <p class="pace-feel" id="paceFeel">{initial_effort["tier_feel"]} · 아래 숫자가 이 페이스에 맞춰 바뀌어요</p>
 </div>
 <dl class="course-metrics">
  <div><dt class="metric-label">걸음 수</dt><dd class="metric-value" id="mSteps">{initial_effort["steps"]:,}<i>걸음</i></dd></div>
  <div><dt class="metric-label">칼로리</dt><dd class="metric-value" id="mKcal">{initial_effort["kcal"]}<i>kcal</i></dd></div>
  <div><dt class="metric-label">고도 범위</dt><dd class="metric-value" id="mElev">{elev_text}</dd></div>
  <div><dt class="metric-label">총 오르막</dt><dd class="metric-value" id="mAscent">{course.ascent_m:.0f}<i>m</i></dd></div>
  <div class="metric-wide"><dt class="metric-label">러닝 친화도</dt><dd class="metric-value" id="mRfs">{course.rfs["score"]}/100<span class="metric-note" id="mRfsGrade">{rfs_grade}</span></dd></div>
 </dl>
 <p class="metric-note-inline">걸음·칼로리는 성인 {PACE_MODEL["weight_kg"]:.0f}kg 기준 추정치예요.</p>
 <div class="highlight-tags" id="courseHighlights">{''.join(f'<span class="tag">{h}</span>' for h in course.rfs.get("highlights", [])[:2])}</div>
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
 <p class="supporting-copy">코스 10m 안 · <span id="facilityTally">{facility_tally}</span></p>
 <div class="facility-list" id="facilityList">
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
 const initialEditPath = {edit_path};
 const editEnabled = {str(edit_enabled).lower()};
 const editNotice = {json.dumps(edit_notice, ensure_ascii=False)};
 const initialLengthKm = {course.length_km:.2f};
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
 const editSave = document.getElementById('editSave');
 const panTool = document.getElementById('panTool');
 const drawTool = document.getElementById('drawTool');
 const eraseTool = document.getElementById('eraseTool');
 const editUndo = document.getElementById('editUndo');
 const editTools = document.querySelector('.edit-tools');
 const editToast = document.getElementById('editToast');
 const editToastText = document.getElementById('editToastText');
 const editToastAction = document.getElementById('editToastAction');
 const editDistance = document.getElementById('editDistance');
 const mapNode = document.getElementById('map');
 // Single visible + announced feedback channel. `role="status"` on the toast
 // keeps the screen-reader behaviour the sr-only bar used to provide, without
 // duplicating announcements across two live regions.
 let toastTimer = null;
 let toastAction = null;
 const AUTO_DISMISS_MS = {{info:3600, success:3600, busy:0, error:0, blocked:0}};
 const hideEditToast = () => {{
   if (!editToast) return;
   clearTimeout(toastTimer); toastTimer = null; toastAction = null;
   delete editToast.dataset.open;
   editToast.hidden = true;
   editToastAction.hidden = true;
 }};
 const showEditToast = (text, tone, action) => {{
   if (!editToast) {{ return; }}
   clearTimeout(toastTimer);
   editToast.dataset.tone = tone || 'info';
   editToastText.textContent = text;
   if (action) {{
     editToastAction.textContent = action.label;
     editToastAction.hidden = false;
     toastAction = action.run;
   }} else {{
     editToastAction.hidden = true;
     toastAction = null;
   }}
   editToast.hidden = false;
   void editToast.offsetWidth;  // let the enter transition run from hidden
   editToast.dataset.open = '';
   const ms = action ? 6000 : AUTO_DISMISS_MS[tone || 'info'];
   if (ms) toastTimer = setTimeout(hideEditToast, ms);
 }};
 if (editToastAction) editToastAction.addEventListener('click', () => {{
   const run = toastAction; hideEditToast(); if (run) run();
 }});
 const initLocalCourseEditor = () => {{
   const source = initialEditPath.map(([,lat,lon])=>[lat,lon]);
   const all = source;
   const latMin=Math.min(...all.map(point=>point[0])), latMax=Math.max(...all.map(point=>point[0]));
   const lonMin=Math.min(...all.map(point=>point[1])), lonMax=Math.max(...all.map(point=>point[1]));
   const latSpan=Math.max(latMax-latMin,.003), lonSpan=Math.max(lonMax-lonMin,.003);
   const toSvg=([lat,lon]) => [70+(lon-lonMin)/lonSpan*860, 650-(lat-latMin)/latSpan*580];
   const pathFor=points => points.map((point,index) => `${{index?'L':'M'}} ${{toSvg(point).map(value=>value.toFixed(1)).join(' ')}}`).join(' ');
   mapNode.innerHTML='<div class="local-course-editor"><div class="local-course-hint"><strong>지도를 불러오지 못했어요 · 로컬 코스 편집 체험</strong><span id="localCourseHint" role="status" aria-live="polite">펜이나 지우개를 선택하세요.</span></div><svg id="localCourseCanvas" viewBox="0 0 1000 720" role="application" aria-label="로컬 코스 선 편집 캔버스"></svg><div class="local-editor-actions"><button id="localEditRoute" class="local-primary" type="button">코스 편집</button><div class="local-edit-tools" role="toolbar" aria-label="로컬 코스 편집 도구"><button id="localDraw" class="edit-tool-circle" type="button" aria-label="펜으로 그리기" title="펜" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 20 4.3-1 10.4-10.4a2.1 2.1 0 0 0-3-3L5.3 16Z"/><path d="m14.5 6.8 3 3"/></svg></button><button id="localErase" class="edit-tool-circle" type="button" aria-label="직선 지우개" title="지우개" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4.7 14.7 8.6-8.6a2 2 0 0 1 2.8 0l2 2a2 2 0 0 1 0 2.8l-7.4 7.4a2 2 0 0 1-2.8 0Z"/><path d="m11 18 7 0M8.3 11.1l4.6 4.6"/></svg></button><button id="localEditUndo" class="edit-tool-circle" type="button" aria-label="마지막 선 수정 되돌리기" title="되돌리기"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8v5h5"/><path d="M5.5 12a7 7 0 1 0 2-5"/></svg></button><button id="localEditCancel" class="edit-tool-circle" type="button" aria-label="원본 코스로 복구" title="원본으로"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg></button><button id="localEditSave" class="edit-tool-circle save" type="button" aria-label="수정한 코스를 새 코스로 저장" title="저장"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h12l2 2v16H5Z"/><path d="M8 3v6h8V3M8 21v-7h8v7"/></svg></button></div></div></div>';
   const canvas=document.getElementById('localCourseCanvas');
   const hint=document.getElementById('localCourseHint');
   const localShell=mapNode.querySelector('.local-course-editor');
   const localEditButton=document.getElementById('localEditRoute');
   const localCancel=document.getElementById('localEditCancel');
   const localSave=document.getElementById('localEditSave');
   const localDraw=document.getElementById('localDraw'), localErase=document.getElementById('localErase');
   const localUndo=document.getElementById('localEditUndo');
   let localEditing=false, localMode=null, raw=[], gap=null, localUndoStack=[];
   const announce=text => {{ hint.textContent=text; }};
   const localSnapshot=()=>({{gap:gap?[...gap]:null}});
   const localRemember=()=>{{localUndoStack.push(localSnapshot());}};
   const renderLocal=() => {{
     const parts=gap?[source.slice(0,gap[0]+1),source.slice(gap[1])]:[source];
     canvas.innerHTML=parts.map(points=>`<path d="${{pathFor(points)}}" fill="none" stroke="#087b59" stroke-width="9" stroke-linecap="round" stroke-linejoin="round"/>`).join('')+(raw.length?`<path d="M ${{raw.map(point=>point.join(' ')).join(' L ')}}" fill="none" stroke="${{localMode==='erase'?'#e05252':'#142018'}}" stroke-width="12" stroke-linecap="round" opacity=".75"/>`:'');
     localUndo.disabled=!localUndoStack.length;
   }};
   const localPointEvent=event => {{const rect=canvas.getBoundingClientRect();const SVGPoint=canvas.createSVGPoint();SVGPoint.x=(event.clientX-rect.left)*1000/rect.width;SVGPoint.y=(event.clientY-rect.top)*720/rect.height;return [SVGPoint.x,SVGPoint.y];}};
   const nearestIndex=point=>source.reduce((best,p,index)=>{{const q=toSvg(p),d=(q[0]-point[0])**2+(q[1]-point[1])**2;return d<best.d?{{index,d}}:best;}},{{index:0,d:Infinity}}).index;
   const setMode=mode=>{{localMode=localMode===mode?null:mode;localDraw.setAttribute('aria-pressed',String(localMode==='draw'));localErase.setAttribute('aria-pressed',String(localMode==='erase'));announce(localMode==='draw'?'기존 코스 선에서 시작해 바꾸려는 길을 따라 그리세요.':localMode==='erase'?'지울 구간의 시작과 끝을 직선으로 그으세요.':'펜이나 지우개를 선택하세요.');}};
   const setLocalEditing=value => {{
     localEditing=value; localShell.classList.toggle('editing',value); renderLocal();
     announce(value ? '펜이나 지우개를 선택하세요.' : '로컬 코스 편집을 마쳤어요.');
   }};
   canvas.addEventListener('pointerdown',event=>{{if(!localEditing||!localMode)return;raw=[localPointEvent(event)];canvas.setPointerCapture(event.pointerId);renderLocal();}});
   canvas.addEventListener('pointermove',event=>{{if(!raw.length)return;raw.push(localPointEvent(event));renderLocal();}});
   canvas.addEventListener('pointerup',()=>{{if(raw.length<2)return;localRemember();let a=nearestIndex(raw[0]),b=nearestIndex(raw[raw.length-1]);gap=[Math.min(a,b),Math.max(a,b)];raw=[];renderLocal();announce(localMode==='erase'?'선택한 구간을 지웠어요. 펜으로 새 길을 그려 연결하세요.':'로컬 체험에서는 그린 선을 표시했어요. 실제 지도에서는 보행로로 보정됩니다.');}});
   localEditButton.addEventListener('click', () => setLocalEditing(true));
   localDraw.addEventListener('click',()=>setMode('draw'));localErase.addEventListener('click',()=>setMode('erase'));
   localUndo.addEventListener('click',()=>{{if(!localUndoStack.length)return;gap=localUndoStack.pop().gap;renderLocal();announce('마지막 수정을 되돌렸어요.');}});
   localCancel.addEventListener('click', () => {{gap=null;localUndoStack=[];setLocalEditing(false);}});
   localSave.addEventListener('click', () => announce(gap?'지운 구간을 펜으로 연결한 뒤 저장할 수 있어요.':'카카오 지도가 연결되면 보행로 보정 결과를 저장할 수 있어요.'));
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
 // Kept on a reference so editing can remove it: setZoomable(false) only stops
 // wheel/pinch, and a zoom press mid-gesture would move the ground under the
 // screen-space stroke the drawing code is collecting.
 const zoomControl = new kakao.maps.ZoomControl();
 let zoomControlShown = false;
 const showZoomControl = value => {{
   if (value === zoomControlShown) return;
   if (value) map.addControl(zoomControl, kakao.maps.ControlPosition.LEFT);
   else map.removeControl(zoomControl);
   zoomControlShown = value;
 }};
 showZoomControl(true);
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
 addPolyline(routePath, {{strokeColor:'#ffffff',strokeWeight:11,strokeOpacity:.95}}, routeLayers);
 for (const [a, b, c, d, s] of segs)
   addPolyline([new kakao.maps.LatLng(a,b),new kakao.maps.LatLng(c,d)],
     {{strokeColor:color(s),strokeWeight:7,strokeOpacity:.92}},routeLayers);
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
   '<div class="dir-marker" title="진행 방향"><svg viewBox="0 0 12 12" style="transform:rotate('+m.angle+'deg)" aria-hidden="true"><path d="M2 2.5 9 6 2 9.5"/></svg></div>',guideLayers);
 for (const k of kms) addOverlay(new kakao.maps.LatLng(k.lat,k.lon),
   '<div class="km-marker" title="'+k.km+'km 지점">'+k.km+'</div>',guideLayers);
 let editing = false;
 let editMode = null;
 let editNodes = initialEditPath.map(point => [...point]);
 let erasedGap = null;
 let rawStroke = [];
 let undoStack = [];
 let draftLines = [];
 let gesturePointer = null;
 let editBusy = false;
 let editLengthKm = initialLengthKm;
 const setEditStatus = (text, tone, action) => showEditToast(text, tone, action);
 const setEditDistance = km => {{
   if (typeof km === 'number' && isFinite(km)) editLengthKm = km;
   if (editDistance) editDistance.textContent = editLengthKm.toFixed(2) + 'km';
 }};
 // The panels under the map describe the course; once the course changes they
 // must follow, or the page shows one route and describes another.
 const initialSummary = {initial_summary};
 // Sets the number while leaving the trailing unit <i> in place.
 const setValue = (id, value, unit) => {{
   const node = document.getElementById(id);
   if (!node) return;
   node.textContent = value;
   if (unit) {{
     const u = document.createElement('i');
     u.textContent = unit;
     node.appendChild(u);
   }}
 }};
 const setText = (id, value) => {{
   const node = document.getElementById(id);
   if (node && value !== undefined && value !== null) node.textContent = value;
 }};
 const setChips = (id, labels, empty, className) => {{
   const host = document.getElementById(id);
   if (!host) return;
   host.replaceChildren();
   const items = labels && labels.length ? labels : (empty ? [empty] : []);
   for (const label of items) {{
     const chip = document.createElement('span');
     chip.className = className;
     chip.textContent = label;      // textContent: facility names are external data
     host.appendChild(chip);
   }}
 }};
 // Pace model constants come from src/runart/pace.py so the browser and the
 // server cannot drift into two different answers for the same course.
 const PACE = {pace_model};
 let paceSeconds = {DEFAULT_PACE_S};
 let effortKm = {course.length_km:.4f};
 const paceRange = document.getElementById('paceRange');
 const paceValueEl = document.getElementById('paceValue');
 const paceTierEl = document.getElementById('paceTier');
 const paceFeelEl = document.getElementById('paceFeel');
 const paceChips = [...document.querySelectorAll('.pace-chip')];
 const fmtPace = s => Math.floor(s / 60) + "'" + String(s % 60).padStart(2, '0') + '"';
 const paceTierOf = s => PACE.tiers.find(t => s >= t.min_s) || PACE.tiers[PACE.tiers.length - 1];
 const speedKmh = s => 3600 / s;
 const cadenceSpm = s => Math.min(PACE.cadence.max, Math.max(PACE.cadence.min,
   PACE.cadence.base + PACE.cadence.per_kmh * (speedKmh(s) - PACE.cadence.ref_kmh)));
 const metOf = s => PACE.met.per_kmh * speedKmh(s) + PACE.met.intercept;
 const effortFor = (km, s) => {{
   const minutes = km * s / 60;
   return {{
     minutes: Math.round(minutes),
     steps: Math.round(cadenceSpm(s) * minutes),
     kcal: Math.round(metOf(s) * 3.5 * PACE.weight_kg / 200 * minutes),
   }};
 }};
 const renderEffort = () => {{
   const e = effortFor(effortKm, paceSeconds);
   const tier = paceTierOf(paceSeconds);
   setText('mDuration', e.minutes);
   setText('mSteps', e.steps.toLocaleString('ko-KR'));
   setText('mKcal', e.kcal);
   if (paceValueEl) paceValueEl.textContent = fmtPace(paceSeconds);
   if (paceTierEl) paceTierEl.textContent = tier.name;
   if (paceFeelEl) paceFeelEl.textContent = tier.feel + ' · 아래 숫자가 이 페이스에 맞춰 바뀌어요';
   if (paceRange) paceRange.setAttribute('aria-valuetext',
     fmtPace(paceSeconds) + ' 퍼 킬로미터, ' + tier.name);
   for (const chip of paceChips)
     chip.setAttribute('aria-pressed', String(paceTierOf(Number(chip.dataset.pace)).name === tier.name));
 }};
 const setPace = seconds => {{
   paceSeconds = Math.min(PACE.slowest_s, Math.max(PACE.fastest_s,
     Math.round(seconds / PACE.step_s) * PACE.step_s));
   if (paceRange) paceRange.value = String(paceSeconds);
   renderEffort();
 }};
 if (paceRange) paceRange.addEventListener('input', ev => setPace(Number(ev.target.value)));
 for (const chip of paceChips)
   chip.addEventListener('click', () => setPace(Number(chip.dataset.pace)));
 renderEffort();
 let currentSummary = initialSummary;
 const applySummary = summary => {{
   if (!summary) return;
   currentSummary = summary;
   setText('courseTitle', summary.title);
   // Value and unit are separate nodes; textContent would eat the <i>.
   setValue('mLength', summary.length_km.toFixed(1), 'km');
   setValue('mAscent', String(Math.round(summary.ascent_m)), 'm');
   setValue('mElev', summary.elev_range
     ? summary.elev_range[0] + '~' + summary.elev_range[1] : '정보 없음',
     summary.elev_range ? 'm' : '');
   // Distance changed, so time / steps / kcal must follow at the current pace.
   effortKm = summary.length_km;
   renderEffort();
   setText('mRfsGrade', summary.rfs_grade);
   const rfs = document.getElementById('mRfs');
   if (rfs && rfs.firstChild) rfs.firstChild.nodeValue = summary.rfs + '/100';
   setText('factSignals', summary.signals + '개');
   setText('factStores', (summary.facility_counts.convenience_store || 0) + '개');
   setText('factRestrooms', (summary.facility_counts.restroom || 0) + '개');
   setText('facilityTally', summary.facility_tally);
   setChips('courseHighlights', summary.highlights, null, 'tag');
   setChips('facilityList', summary.facility_chips, '코스 10m 반경 편의점·화장실 없음', 'chip');
   const badges = document.getElementById('courseBadges');
   if (badges && summary.badges) {{
     badges.replaceChildren();
     for (const badge of summary.badges) {{
       const el = document.createElement('span');
       el.className = 'badge'; el.setAttribute('role','img');
       el.setAttribute('aria-label', badge.label); el.title = badge.label;
       el.textContent = badge.emoji;
       badges.appendChild(el);
     }}
   }}
 }};
 // One in-flight edit request at a time: the server admits a single concurrent
 // route edit, and a second tap would otherwise queue a doomed request while
 // the first is still snapping.
 const setEditBusy = value => {{
   editBusy = value;
   if (editTools) editTools.setAttribute('aria-busy', String(value));
   for (const button of [panTool, drawTool, eraseTool, editUndo, editCancel, editSave]) {{
     if (!button) continue;
     if (value) button.disabled = true;
     else if (button === editUndo) button.disabled = !undoStack.length;
     else if (button === editSave) button.disabled = Boolean(erasedGap);
     else button.disabled = false;
   }}
 }};
 // km rides along in the snapshot: only the server knows the real edge-geometry
 // length, so undo restores the value that came back with that path.
 const snapshot = () => ({{nodes:editNodes.map(point=>[...point]),gap:erasedGap?[...erasedGap]:null,km:editLengthKm,summary:currentSummary}});
 const restore = state => {{editNodes=state.nodes.map(point=>[...point]);erasedGap=state.gap?[...state.gap]:null;setEditDistance(state.km);applySummary(state.summary);renderDraft();}};
 const remember = () => {{undoStack.push(snapshot());if(undoStack.length>40)undoStack.shift();}};
 const pointPath = points => points.map(([,lat,lon])=>new kakao.maps.LatLng(lat,lon));
 const renderDraft = () => {{
   draftLines.forEach(line=>line.setMap(null));draftLines=[];
   if (editing) {{
     const parts=erasedGap?[editNodes.slice(0,erasedGap[0]+1),editNodes.slice(erasedGap[1])]:[editNodes];
     for (const part of parts) if(part.length>1) draftLines.push(new kakao.maps.Polyline({{map,path:pointPath(part),strokeColor:'#087b59',strokeWeight:7,strokeOpacity:.96,strokeStyle:'solid'}}));
   }}
   if(editUndo)editUndo.disabled=editBusy||!undoStack.length;
   if(editSave)editSave.disabled=editBusy||Boolean(erasedGap);
 }};
 // 'pan' is the no-drawing-tool state, but it is a real button rather than an
 // empty toolbar: two-finger panning exists (below) yet nothing on screen says
 // so, and one-finger drag is the gesture people actually reach for.
 const syncToolPressed = () => {{
   if(panTool)panTool.setAttribute('aria-pressed',String(!editMode));
   if(drawTool)drawTool.setAttribute('aria-pressed',String(editMode==='draw'));
   if(eraseTool)eraseTool.setAttribute('aria-pressed',String(editMode==='erase'));
 }};
 const applyMode = () => {{
   syncToolPressed();
   document.body.classList.toggle('tool-active',Boolean(editMode));
   // One finger belongs to the active drawing tool. Two-finger movement is
   // forwarded to map.panBy() by the overlay handlers below.
   map.setDraggable(!editMode);map.setZoomable(!editMode);
 }};
 const setMode = mode => {{
   if(editBusy)return;
   editMode=(mode==='pan'||editMode===mode)?null:mode;
   applyMode();
   // A cleared gap blocks saving; that reason has to stay on screen until it
   // is resolved, so it outranks the per-tool hint.
   if(erasedGap&&!editMode)return setEditStatus('지운 구간을 펜으로 연결해야 저장할 수 있어요.','blocked');
   setEditStatus(editMode==='draw'?'기존 선에서 시작해 원하는 길을 따라 그리세요. 손을 떼면 보행로로 보정됩니다.':editMode==='erase'?'지울 구간의 시작과 끝을 직선으로 그으세요.':'지도 이동 모드예요. 끌어서 움직이고 편의시설을 확인하세요.','info');
 }};
 // A finished gesture always releases the active tool so the map can be panned
 // again. setMode() is not reused for this: it toggles and emits its own hint,
 // which would overwrite the gesture's result toast.
 const releaseTool = () => {{
   editMode=null;
   applyMode();
 }};
 const setEditing = value => {{
   editing=value;document.body.classList.toggle('editing',value);
   setLayers(routeLayers,!value);setLayers(shapeLayers,false);setLayers(guideLayers,!value);
   showZoomControl(!value);
   if(!value){{editMode=null;document.body.classList.remove('tool-active');map.setDraggable(true);map.setZoomable(true);}}
   renderDraft();setEditDistance();
   if(!value)return hideEditToast();
   setEditStatus(editNotice||'펜이나 지우개를 선택하세요.','info');
 }};
 const projection=map.getProjection();
 const screenPoint = node => projection.containerPointFromCoords(new kakao.maps.LatLng(node[1],node[2]));
 const nearestIndex = point => editNodes.reduce((best,node,index)=>{{const p=screenPoint(node),d=(p.x-point.x)**2+(p.y-point.y)**2;return d<best.d?{{index,d}}:best;}},{{index:0,d:Infinity}}).index;
 const overlayPoint = event => {{const rect=editOverlay.getBoundingClientRect();return {{x:event.clientX-rect.left,y:event.clientY-rect.top}};}};
 const toCoord = point => {{const p=projection.coordsFromContainerPoint(new kakao.maps.Point(point.x,point.y));return {{lat:p.getLat(),lon:p.getLng()}};}};
 const paintGesture = () => {{
   if(!editOverlay)return;
   editOverlay.setAttribute('viewBox',`0 0 ${{editOverlay.clientWidth||1}} ${{editOverlay.clientHeight||1}}`);
   editOverlay.innerHTML=rawStroke.length?`<path d="M ${{rawStroke.map(point=>`${{point.x}} ${{point.y}}`).join(' L ')}}" fill="none" stroke="${{editMode==='erase'?'#e05252':'#142018'}}" stroke-width="10" stroke-linecap="round" stroke-linejoin="round" opacity=".78"/>`:'';
 }};
 const clearGesture = () => {{rawStroke=[];gesturePointer=null;if(editOverlay)editOverlay.replaceChildren();}};
 const postEdit = async body => {{
   const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),3500);
   try{{const response=await fetch(editEndpoint,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body),signal:controller.signal}});const payload=await response.json();if(!response.ok)throw new Error(payload.error||'코스 선을 처리하지 못했어요.');return payload;}}
   finally{{clearTimeout(timer);}}
 }};
 const finishGesture = async () => {{
   if(editBusy){{clearGesture();return;}}
   if(rawStroke.length<2){{clearGesture();return;}}
   const start=nearestIndex(rawStroke[0]),end=nearestIndex(rawStroke[rawStroke.length-1]);
   let lo=Math.min(start,end),hi=Math.max(start,end);
   if(hi-lo<2){{clearGesture();releaseTool();setEditStatus('조금 더 긴 구간을 선택해 주세요.','error');return;}}
   if(editMode==='erase'){{
     if(erasedGap){{clearGesture();releaseTool();setEditStatus('지운 구간을 먼저 펜으로 연결해 주세요.','blocked');return;}}
     remember();erasedGap=[lo,hi];clearGesture();renderDraft();releaseTool();setEditStatus('선택한 구간을 지웠어요. 펜으로 새 길을 그려 연결하세요.','blocked');return;
   }}
   if(editMode==='draw'){{
     if(erasedGap){{
       const connectsGap=(Math.abs(start-erasedGap[0])<=2&&Math.abs(end-erasedGap[1])<=2)||(Math.abs(end-erasedGap[0])<=2&&Math.abs(start-erasedGap[1])<=2);
       if(!connectsGap){{clearGesture();releaseTool();setEditStatus('펜 선을 지운 구간의 양 끝에 이어 주세요.','error');return;}}
       lo=erasedGap[0];hi=erasedGap[1];
     }}
     const before=snapshot();
     setEditBusy(true);setEditStatus('그린 선을 보행 가능한 길로 보정 중…','busy');
     try{{
       const directed=start<=end?rawStroke:[...rawStroke].reverse();
       const sample=directed.filter((_,index)=>index===0||index===directed.length-1||index%Math.max(1,Math.floor(directed.length/40))===0).map(toCoord);
       const payload=await postEdit({{action:'snap',path:editNodes.map(point=>point[0]),from_index:lo,to_index:hi,stroke:sample}});
       undoStack.push(before);if(undoStack.length>40)undoStack.shift();editNodes=payload.path.map(point=>[...point]);erasedGap=null;
       setEditDistance(payload.length_km);
       applySummary(payload.summary);
       setEditStatus('보행로에 맞췄어요 · ' + editLengthKm.toFixed(2) + 'km','success');
     }}catch(error){{
       // editNodes is untouched on failure -- say so, and keep the message up
       // until the user dismisses it.
       setEditStatus(error.name==='AbortError'?'보정 시간이 초과됐어요. 그리던 선은 그대로 남아 있어요.':(error.message||'보행로 보정에 실패했어요.'),'error',{{label:'닫기',run:()=>{{}}}});
     }}
     finally{{setEditBusy(false);clearGesture();renderDraft();releaseTool();}}
   }}
 }};
 if(editEnabled&&editButton)editButton.addEventListener('click',()=>setEditing(true));
 if(panTool)panTool.addEventListener('click',()=>setMode('pan'));
 if(drawTool)drawTool.addEventListener('click',()=>setMode('draw'));
 if(eraseTool)eraseTool.addEventListener('click',()=>setMode('erase'));
 if(editOverlay){{
   const editPointers=new Map();
   let twoFingerPan=false,panCenter=null;
   const centerOfPointers=()=>{{const points=[...editPointers.values()];return {{x:points.reduce((sum,p)=>sum+p.x,0)/points.length,y:points.reduce((sum,p)=>sum+p.y,0)/points.length}};}};
   const endOverlayPointer=event=>{{
     if(!editPointers.has(event.pointerId))return;
     editPointers.delete(event.pointerId);
     if(twoFingerPan){{
       if(editPointers.size===0){{twoFingerPan=false;panCenter=null;clearGesture();}}
       else panCenter=centerOfPointers();
       return;
     }}
     if(event.pointerId===gesturePointer)finishGesture();
   }};
   editOverlay.addEventListener('pointerdown',event=>{{
     if(!editing||!editMode||editBusy)return;
     const point=overlayPoint(event);editPointers.set(event.pointerId,point);
     editOverlay.setPointerCapture(event.pointerId);
     if(event.pointerType==='touch'&&editPointers.size>=2){{
       twoFingerPan=true;panCenter=centerOfPointers();clearGesture();return;
     }}
     if(twoFingerPan)return;
     gesturePointer=event.pointerId;rawStroke=[point];paintGesture();
   }});
   editOverlay.addEventListener('pointermove',event=>{{
     if(!editPointers.has(event.pointerId))return;
     const point=overlayPoint(event);editPointers.set(event.pointerId,point);
     if(twoFingerPan){{
       if(editPointers.size<2)return;
       const next=centerOfPointers();
       if(panCenter)map.panBy(panCenter.x-next.x,panCenter.y-next.y);
       panCenter=next;return;
     }}
     if(event.pointerId!==gesturePointer)return;
     const last=rawStroke[rawStroke.length-1];if(!last||Math.hypot(point.x-last.x,point.y-last.y)>4)rawStroke.push(point);paintGesture();
   }});
   editOverlay.addEventListener('pointerup',endOverlayPointer);
   editOverlay.addEventListener('pointercancel',endOverlayPointer);
 }}
 if(editUndo)editUndo.addEventListener('click',()=>{{if(editBusy||!undoStack.length)return;restore(undoStack.pop());setEditStatus('마지막 선 수정을 되돌렸어요.','info');}});
 // Reverting is the only irreversible action in the editor, so it is made
 // reversible instead of guarded by a modal: a confirm sheet would cover the
 // map and add a step, while an undo offer costs nothing when it is not needed.
 // The distance is restored with the path -- the badge would otherwise keep
 // showing the length of the discarded edit.
 if(editCancel)editCancel.addEventListener('click',()=>{{
   if(editBusy)return;
   const hadEdits=undoStack.length>0||Boolean(erasedGap);
   const discarded={{...snapshot(),stack:undoStack.slice()}};
   editNodes=initialEditPath.map(point=>[...point]);erasedGap=null;undoStack=[];
   setEditDistance(initialLengthKm);applySummary(initialSummary);setEditing(false);
   if(!hadEdits)return;
   showEditToast('원본 코스로 되돌렸어요.','info',{{label:'실행 취소',run:()=>{{
     editNodes=discarded.nodes.map(point=>[...point]);
     erasedGap=discarded.gap?[...discarded.gap]:null;
     undoStack=discarded.stack;
     setEditDistance(discarded.km);
     applySummary(discarded.summary);
     setEditing(true);
   }}}});
 }});
 if(editSave)editSave.addEventListener('click',async()=>{{
   if(!editing||editBusy)return;
   if(erasedGap)return setEditStatus('지운 구간을 펜으로 연결한 뒤 저장해 주세요.','blocked');
   setEditBusy(true);setEditStatus('수정한 코스를 저장 중…','busy');
   try{{
     const payload=await postEdit({{action:'save',path:editNodes.map(point=>point[0])}});
     setEditStatus('저장했어요. 새 코스로 이동합니다…','success');
     location.assign(payload.preview_url);
   }}
   catch(error){{
     setEditStatus(error.name==='AbortError'?'저장 시간이 초과됐어요. 수정한 선은 그대로 남아 있어요.':(error.message||'저장하지 못했어요. 다시 시도해 주세요.'),'error',{{label:'닫기',run:()=>{{}}}});
     setEditBusy(false);
   }}
 }});
 const geocoder = (kakao.maps.services && kakao.maps.services.Geocoder)
   ? new kakao.maps.services.Geocoder() : null;
 let openPop = null;
 const closePop = () => {{ if (openPop) {{ openPop.style.display = 'none'; openPop = null; }} }};
 kakao.maps.event.addListener(map, 'click', closePop);
 const preventKakaoMap = () => {{if(kakao.maps.event.preventMap)kakao.maps.event.preventMap();}};
 const canHover = window.matchMedia&&window.matchMedia('(hover: hover)').matches;
 const FACILITY_TAP_SLOP = 8;
 const addFacility = m => {{
   const el = document.createElement('div');
   el.className = 'facility-marker ' + m.type;
   el.title = m.label;
   el.tabIndex = 0;
   el.setAttribute('role','button');
   el.setAttribute('aria-label',m.name+' · '+m.label);
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
   const toggle=()=>{{pop.style.display==='none'?show():hide();}};
   let facilityPointer=null,suppressClickUntil=0;
   if(canHover){{el.addEventListener('mouseenter',show);el.addEventListener('mouseleave',hide);}}
   el.addEventListener('pointerdown',ev=>{{
     facilityPointer={{id:ev.pointerId,startX:ev.clientX,startY:ev.clientY,lastX:ev.clientX,lastY:ev.clientY,moved:false}};
     el.setPointerCapture(ev.pointerId);preventKakaoMap();
   }});
   el.addEventListener('pointermove',ev=>{{
     if(!facilityPointer||facilityPointer.id!==ev.pointerId)return;
     if(Math.hypot(ev.clientX-facilityPointer.startX,ev.clientY-facilityPointer.startY)>FACILITY_TAP_SLOP)facilityPointer.moved=true;
     if(facilityPointer.moved){{
       closePop();
       map.panBy(facilityPointer.lastX-ev.clientX,facilityPointer.lastY-ev.clientY);
     }}
     facilityPointer.lastX=ev.clientX;facilityPointer.lastY=ev.clientY;preventKakaoMap();
   }});
   el.addEventListener('pointerup',ev=>{{
     if(!facilityPointer||facilityPointer.id!==ev.pointerId)return;
     preventKakaoMap();suppressClickUntil=performance.now()+600;
     if(!facilityPointer.moved)toggle();
     facilityPointer=null;
   }});
   el.addEventListener('pointercancel',()=>{{facilityPointer=null;}});
   el.addEventListener('click',ev=>{{
     ev.stopPropagation();preventKakaoMap();
     if(performance.now()<suppressClickUntil)return;
     toggle();
   }});
   el.addEventListener('keydown',ev=>{{if(ev.key==='Enter'||ev.key===' '){{ev.preventDefault();preventKakaoMap();toggle();}}}});
   const overlay = new kakao.maps.CustomOverlay({{
     position: new kakao.maps.LatLng(m.lat, m.lon), content: el,
     xAnchor:.5, yAnchor:.5, zIndex:6, clickable:true
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
   map.setCenter(posLatLng);
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
   setMapMode('guide');
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
