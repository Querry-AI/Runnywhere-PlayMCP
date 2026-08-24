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
from .rfs import edge_rfs
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
    # Small repeated chevrons read as one continuous direction cue.  Keep them
    # close on normal city loops, but cap the total so a marathon overview does
    # not become a solid white ribbon.
    total_km = sum(
        haversine_m(a[0], a[1], b[0], b[1]) / 1000.0
        for a, b in zip(points, points[1:])
    )
    spacing_km = max(0.12, total_km / 80.0)
    target = spacing_km * 0.65
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
            target += spacing_km
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
        '<section class="panel"><h2>러너 체크포인트</h2>'
        f'<div class="facts">{cells}</div>'
        '<p class="hint">신호 횡단 수와 코스 10m 안의 편의시설입니다.</p>'
        '</section>'
    )


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
        "grade_label": course.grade_label,
        "duration_min": [lo, hi],
        "signals": pedestrian_signals_crossed(g, course.path),
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
        f"거리 {course.length_km:.1f}km · 누적 오르막 {course.ascent_m:.0f}m"
        f" · {p.location_name or '서울'} — 러니웨어"
    )
    detailed = route_points(course)
    segments = json.dumps(_segments_with_rfs(course))
    shape_route = json.dumps(_shape_only_route(course))
    km_markers = json.dumps(_km_markers(detailed))
    dir_markers = json.dumps(_direction_markers(detailed))
    profile = _elevation_profile(course)
    profile_svg = _profile_svg(profile)
    course_facts = _course_fact_html(course, facilities, detailed)
    markers = json.dumps([
        {"lat": f["lat"], "lon": f["lon"], "type": f["type"],
         "name": html.escape(f.get("name") or LABELS_KO[f["type"]]),
         "label": html.escape(f"{LABELS_KO[f['type']]} · {f['at_km']:g}km 지점")}
        for f in facilities
    ])
    shape_view_label = "동물 실루엣" if shape else "코스 라인"
    where = html.escape(p.location_name)
    where_html = f'<p class="course-where">{where} 출발·도착</p>' if where else ""
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
 .run-locate{{position:absolute;z-index:520;left:50%;bottom:16px;transform:translateX(-50%);
      display:inline-flex;align-items:center;justify-content:center;width:60px;height:60px;padding:0;border:0;
      border-radius:50%;background:#142018;color:#fff;box-shadow:0 7px 24px rgba(10,28,19,.28);
      cursor:pointer}}
 .run-locate svg{{width:29px;height:29px;fill:none;stroke:currentColor;stroke-width:2;
      stroke-linecap:round;stroke-linejoin:round;pointer-events:none}}
 .run-locate:focus-visible{{outline:3px solid #8ee0bb;outline-offset:3px}}
 .run-locate:disabled{{background:#727b75;cursor:not-allowed}}
 .run-locate.on{{background:#0a7d43}}
 .view-toggle{{position:absolute;z-index:530;right:14px;top:14px;display:flex;background:rgba(255,255,255,.96);
      border:1px solid rgba(20,35,25,.1);border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.1);overflow:hidden}}
 .view-toggle button{{min-height:48px;border:0;background:transparent;color:#4b5a50;padding:0 13px;font-size:13px;font-weight:800;font-family:inherit}}
 .view-toggle button.active{{background:#142018;color:#fff}}
 body.shape-only .run-locate{{display:none}}
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
 .metric-value i{{font-style:normal;font-size:14px;font-weight:700;color:#55605a;margin-left:2px}}
 /* Secondary actions must not compete with the two primary CTAs above them. */
 .btn.ghost{{background:#f2f6f0;color:#2b3630;border:1px solid #dce3d8;font-weight:700}}
 .actions.secondary-actions{{grid-template-columns:repeat(3,1fr);margin-top:8px}}
 .actions.secondary-actions .btn{{min-height:44px;padding:0 6px;font-size:13px}}
 .metric-note-inline{{margin:8px 0 0;font-size:12px;color:#55605a}}
 .course-metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:14px 0}}
 .course-metrics>div{{min-width:0;padding:12px;border:1px solid #e1e7dd;background:#f7faf5;border-radius:12px}}
 /* dt/dd carry a 40px UA margin-inline-start that squeezes the value out of the cell. */
 .course-metrics dt,.course-metrics dd{{margin:0}}
 .metric-value{{display:block;font-size:21px;font-weight:800;color:#142018;white-space:nowrap}}
 .metric-label{{display:block;font-size:13px;color:#55605a;margin-top:3px;word-break:keep-all}}
 .metric-note{{display:block;font-size:12px;font-weight:700;color:#17613e;margin-top:2px}}
 .course-where{{margin:-6px 0 12px;font-size:15px;font-weight:700;color:#44514a;word-break:keep-all}}
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
 .km-marker,.dir-marker,.start-marker,.finish-marker{{pointer-events:none}}
 .km-marker{{background:#fff;border:2px solid #111;border-radius:999px;width:24px;height:24px;line-height:20px;
      text-align:center;font-size:11px;font-weight:800;box-shadow:0 2px 8px rgba(0,0,0,.2)}}
 .dir-marker svg{{display:block;width:9px;height:9px;fill:#fff;stroke:rgba(0,70,42,.58);stroke-width:.7;
      stroke-linecap:round;stroke-linejoin:round;transform-origin:50% 50%;
      filter:drop-shadow(0 1px 1px rgba(0,0,0,.36))}}
 .start-marker{{background:#142018;color:#fff;border:2px solid #fff;border-radius:999px;padding:6px 9px;
      font-size:12px;font-weight:800;box-shadow:0 3px 12px rgba(0,0,0,.28);white-space:nowrap}}
 .finish-marker{{background:#087b59;color:#fff;border:2px solid #fff;border-radius:999px;padding:6px 9px;
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
 .edit-anchor{{width:12px;height:12px;border:3px solid #fff;border-radius:50%;background:#e0522d;
      box-shadow:0 2px 7px rgba(0,0,0,.3);pointer-events:none}}
 /* display:none when not selecting. pointer-events:none alone was enough in
    theory, but a full-bleed touch-action:none layer over the map is exactly
    the kind of thing that eats a drag on mobile -- keep it out of the tree
    unless the segment tool is actually selected. */
 .edit-overlay{{position:absolute;z-index:930;inset:0;width:100%;height:100%;touch-action:none;pointer-events:none;display:none}}
 body.editing.tool-active .edit-overlay{{display:block}}
 /* Non-blocking edit feedback: one line pinned to the map's bottom edge so the
    route area stays clear (the large overlay panel was removed in f69e246). */
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
 body.editing .edit-tools{{display:flex}}
 body.editing .run-locate,body.editing .view-toggle,body.editing #editRoute{{display:none!important}}
 body.editing.tool-active .facility-marker{{pointer-events:none;opacity:.2}}
 body.editing.tool-active .edit-overlay{{pointer-events:auto}}
 footer{{color:#55605a;font-size:13px;padding:8px 20px 28px;text-align:center;line-height:1.6}}
 footer a{{display:inline-block;padding:8px 4px;color:inherit}}
 @media (max-width:760px){{.brand{{height:48px;padding:0 16px}} .facts{{grid-template-columns:repeat(2,1fr)}}
      #map{{height:clamp(280px,42svh,380px);min-height:0}}
      .view-toggle{{right:10px;top:10px}}#editRoute{{left:10px;top:10px}}
      .run-locate{{position:fixed;bottom:calc(14px + env(safe-area-inset-bottom));width:58px;height:58px}}
      .course-head{{gap:8px}}.badge{{width:28px;height:28px;font-size:15px}}
      .wrap{{display:block;padding:0 16px 96px}}.card,.panel{{padding:18px;margin-bottom:12px;border-radius:16px}}h1{{font-size:22px;line-height:1.25;word-break:keep-all}}
      .actions{{grid-template-columns:1fr 1fr}}.actions .btn{{padding:0 8px;text-align:center}}
      footer{{padding-bottom:96px}}.course-metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.edit-bar{{left:10px;right:10px;bottom:calc(8px + env(safe-area-inset-bottom));}}
      .metric-value{{font-size:20px;line-height:1.2;font-variant-numeric:tabular-nums;white-space:nowrap}}.metric-label{{font-size:13px;line-height:1.35}}.supporting-copy{{font-size:13px;line-height:1.45;word-break:normal;line-break:strict}}
      footer{{padding-bottom:96px}}}}
 @media (orientation:landscape) and (max-width:900px){{#map{{height:280px}}.wrap{{padding-bottom:72px}}}}
 @media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important;animation-duration:.001ms!important;transition-duration:.001ms!important}}}}
</style></head><body>
<header class="brand"><strong>러니웨어<span class="brand-tagline">: 어디서든 러닝 코스 짜기!</span></strong></header>
<div id="map"><button id="runStart" class="run-locate" type="button"
 aria-label="내 위치 추적 시작" title="내 위치 추적 시작"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="16" cy="4" r="2"/><path d="m14.5 7-3 4 3.2 2.2 2.1-3.1 3.2 2M11.5 11 7 13.5M14.7 13.2 10 21M14.7 13.2 20 19"/></svg><span class="sr-only">내 위치 추적 시작</span></button><span id="runStatus" class="sr-only" role="status" aria-live="polite"></span><div class="view-toggle" aria-label="지도 보기 전환">
 <button id="shapeView" type="button">{shape_view_label}</button>
 <button id="guideView" type="button" class="active">러닝 안내</button>
 </div>{'<button id="editRoute" type="button">코스 편집</button>' if edit_enabled else ''}<svg id="editOverlay" class="edit-overlay" aria-hidden="true"></svg><div class="edit-tools" role="toolbar" aria-label="코스 편집 도구"><button id="panTool" class="edit-tool-circle" type="button" aria-label="지도 이동" title="지도 이동" aria-pressed="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v18M3 12h18"/><path d="m9 6 3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/></svg></button><button id="segmentTool" class="edit-tool-circle" type="button" aria-label="바꿀 코스 구간 선택" title="구간 선택" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h5M14 7h5M10 7h4"/><circle cx="5" cy="7" r="2"/><circle cx="19" cy="7" r="2"/><path d="M5 17h14" stroke-dasharray="2.5 2.5"/></svg></button><button id="editUndo" class="edit-tool-circle" type="button" aria-label="마지막 수정 실행 취소" title="한 번 되돌리기"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 7H4V2"/><path d="M4 7c2.2-2.4 5-3.4 8-3 4.6.6 8 4.5 8 9"/></svg></button><button id="editCancel" class="edit-tool-circle" type="button" aria-label="모든 수정 초기화" title="전체 초기화"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13"/><path d="M10 11v5M14 11v5"/></svg></button><button id="editSave" class="edit-tool-circle save" type="button" aria-label="수정한 코스를 새 코스로 저장" title="저장"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h12l2 2v16H5Z"/><path d="M8 3v6h8V3M8 21v-7h8v7"/></svg></button></div><div id="editDistance" class="edit-distance" aria-label="수정 중인 코스 거리"></div><div id="editToast" class="edit-toast" role="status" aria-live="polite" data-tone="info" hidden><span class="edit-toast-spin" aria-hidden="true"></span><span id="editToastText" class="edit-toast-text"></span><button id="editToastAction" class="edit-toast-action" type="button" hidden></button></div></div>
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
 </dl>
 <p class="metric-note-inline">걸음·칼로리는 성인 {PACE_MODEL["weight_kg"]:.0f}kg 기준 추정치예요.</p>
 <p class="supporting-copy">실제 통행·공사 상황을 확인하고 안전하게 달려 주세요.</p>
 {profile_svg}
 <div class="actions primary-actions">
  <a class="btn" href="{base_url}/c/{cid}.gpx">GPX 파일 받기</a>
 </div>
 <div class="actions secondary-actions">
  <a class="btn ghost" href="{base_url}/c/{cid}/card.svg">코스 카드</a>
  <button class="btn ghost" id="shareCourse" type="button">공유하기</button>
  <a class="btn ghost" href="{base_url}/animals">동물 지도</a>
 </div>
</div>
{course_facts}
<section class="panel"><h2>코스 주변 편의시설</h2>
 <p class="supporting-copy">코스 10m 안 · <span id="facilityTally">{facility_tally}</span></p>
 <div class="facility-list" id="facilityList">
  {''.join(f'<span class="chip">{LABELS_KO[f["type"]]} · {f["at_km"]:g}km</span>' for f in facilities[:FACILITY_CHIP_LIMIT]) or '<span class="chip">코스 10m 반경 편의점·화장실 없음</span>'}
 </div>
</section>
<section class="panel"><h2>카카오맵으로 따라 달리기</h2>
 <ol class="steps">
  <li>GPX 파일 받기를 눌러 코스를 저장합니다.</li>
  <li>카카오맵 앱에서 우측 상단 길찾기를 누르세요.</li>
  <li>이동수단을 자전거로 고른 뒤, 도착지 입력 화면 우측 하단의 GPX를 선택하세요.</li>
  <li>저장한 파일을 고르고 완료한 뒤, 우측 하단 주행 시작을 누르면 안내가 시작됩니다.</li>
 </ol>
</section>
</div>
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
 const startBtn = document.getElementById('runStart');
 const runStatus = document.getElementById('runStatus');
 const setRunButtonLabel = label => {{
   startBtn.setAttribute('aria-label', label);
   startBtn.title = label;
   const hiddenLabel = startBtn.querySelector('.sr-only');
   if (hiddenLabel) hiddenLabel.textContent = label;
 }};
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
 const editButton = document.getElementById('editRoute');
 const editCancel = document.getElementById('editCancel');
 const editSave = document.getElementById('editSave');
 const panTool = document.getElementById('panTool');
 const segmentTool = document.getElementById('segmentTool');
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
   const ms = action ? (action.persist ? 0 : 6000) : AUTO_DISMISS_MS[tone || 'info'];
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
   const arrowsFor=points=>{{let markup='',target=24,cum=0,prev=toSvg(points[0]);for(const point of points.slice(1)){{const next=toSvg(point),dx=next[0]-prev[0],dy=next[1]-prev[1],length=Math.hypot(dx,dy);while(length&&cum+length>=target){{const t=(target-cum)/length,x=prev[0]+dx*t,y=prev[1]+dy*t,angle=Math.atan2(dy,dx)*180/Math.PI;markup+=`<path d="M -6 -3.7 5.2 0 -6 3.7 -2.4 0Z" transform="translate(${{x.toFixed(1)}} ${{y.toFixed(1)}}) rotate(${{angle.toFixed(1)}})" fill="#fff" stroke="#064f38" stroke-width="1" stroke-linejoin="round"/>`;target+=36;}}cum+=length;prev=next;}}return markup;}};
   mapNode.innerHTML='<div class="local-course-editor"><div class="local-course-hint"><strong>지도를 불러오지 못했어요 · 로컬 코스 편집 체험</strong><span id="localCourseHint" role="status" aria-live="polite">구간 선택을 누른 뒤 바꿀 코스 선을 탭하세요.</span></div><svg id="localCourseCanvas" viewBox="0 0 1000 720" role="application" aria-label="로컬 코스 구간 선택 캔버스"></svg><div class="local-editor-actions"><button id="localEditRoute" class="local-primary" type="button">코스 편집</button><div class="local-edit-tools" role="toolbar" aria-label="로컬 코스 편집 도구"><button id="localSegment" class="edit-tool-circle" type="button" aria-label="바꿀 코스 구간 선택" title="구간 선택" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h5M14 7h5M10 7h4"/><circle cx="5" cy="7" r="2"/><circle cx="19" cy="7" r="2"/><path d="M5 17h14" stroke-dasharray="2.5 2.5"/></svg></button><button id="localEditUndo" class="edit-tool-circle" type="button" aria-label="마지막 수정 실행 취소" title="한 번 되돌리기"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 7H4V2"/><path d="M4 7c2.2-2.4 5-3.4 8-3 4.6.6 8 4.5 8 9"/></svg></button><button id="localEditCancel" class="edit-tool-circle" type="button" aria-label="모든 수정 초기화" title="전체 초기화"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13"/><path d="M10 11v5M14 11v5"/></svg></button><button id="localEditSave" class="edit-tool-circle save" type="button" aria-label="수정한 코스를 새 코스로 저장" title="저장"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h12l2 2v16H5Z"/><path d="M8 3v6h8V3M8 21v-7h8v7"/></svg></button></div></div></div>';
   const canvas=document.getElementById('localCourseCanvas');
   const hint=document.getElementById('localCourseHint');
   const localShell=mapNode.querySelector('.local-course-editor');
   const localEditButton=document.getElementById('localEditRoute');
   const localCancel=document.getElementById('localEditCancel');
   const localSave=document.getElementById('localEditSave');
   const localSegment=document.getElementById('localSegment');
   const localUndo=document.getElementById('localEditUndo');
   let localEditing=false, localMode=null, selected=null, localUndoStack=[];
   const announce=text => {{ hint.textContent=text; }};
   const renderLocal=() => {{
     let markup=`<path d="${{pathFor(source)}}" fill="none" stroke="#087b59" stroke-width="9" stroke-linecap="round" stroke-linejoin="round"/>${{arrowsFor(source)}}`;
     if(selected){{const piece=source.slice(selected[0],selected[1]+1);const [a,b]=piece.map(toSvg);markup+=`<path d="${{pathFor(piece)}}" fill="none" stroke="#fff" stroke-width="16" stroke-linecap="round"/><path d="${{pathFor(piece)}}" fill="none" stroke="#e0522d" stroke-width="11" stroke-linecap="round"/><circle cx="${{a[0]}}" cy="${{a[1]}}" r="7" fill="#e0522d" stroke="#fff" stroke-width="3"/><circle cx="${{b[0]}}" cy="${{b[1]}}" r="7" fill="#e0522d" stroke="#fff" stroke-width="3"/>`;}}
     canvas.innerHTML=markup;
     localUndo.disabled=!localUndoStack.length;
   }};
   const localPointEvent=event => {{const rect=canvas.getBoundingClientRect();const SVGPoint=canvas.createSVGPoint();SVGPoint.x=(event.clientX-rect.left)*1000/rect.width;SVGPoint.y=(event.clientY-rect.top)*720/rect.height;return [SVGPoint.x,SVGPoint.y];}};
   const segmentDistance=(p,a,b)=>{{const dx=b[0]-a[0],dy=b[1]-a[1],l=dx*dx+dy*dy||1,t=Math.max(0,Math.min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/l));return Math.hypot(p[0]-a[0]-dx*t,p[1]-a[1]-dy*t);}};
   const nearestSegment=point=>{{let best={{index:0,d:Infinity}};for(let i=0;i<source.length-1;i++){{const d=segmentDistance(point,toSvg(source[i]),toSvg(source[i+1]));if(d<best.d)best={{index:i,d}};}}return best;}};
   const setMode=()=>{{localMode=localMode?null:'segment';localSegment.setAttribute('aria-pressed',String(Boolean(localMode)));announce(localMode?'바꾸려는 코스 선을 탭하세요.':'구간 선택을 누른 뒤 바꿀 코스 선을 탭하세요.');}};
   const setLocalEditing=value => {{
     localEditing=value; localShell.classList.toggle('editing',value); renderLocal();
     announce(value ? '구간 선택을 누른 뒤 바꿀 코스 선을 탭하세요.' : '로컬 코스 편집을 마쳤어요.');
   }};
   canvas.addEventListener('pointerup',event=>{{if(!localEditing||localMode!=='segment')return;const hit=nearestSegment(localPointEvent(event));if(hit.d>28)return announce('코스 선 가까이를 탭해 주세요.');localUndoStack.push(selected?[...selected]:null);selected=[hit.index,hit.index+1];localMode=null;localSegment.setAttribute('aria-pressed','false');renderLocal();announce('구간을 선택했어요. 실제 지도에서는 다른 보행로로 자동 연결됩니다.');}});
   localEditButton.addEventListener('click', () => setLocalEditing(true));
   localSegment.addEventListener('click',setMode);
   localUndo.addEventListener('click',()=>{{if(!localUndoStack.length)return;selected=localUndoStack.pop();renderLocal();announce('마지막 선택을 한 번 되돌렸어요.');}});
   localCancel.addEventListener('click', () => {{selected=null;localUndoStack=[];setLocalEditing(false);}});
   localSave.addEventListener('click', () => announce('카카오 지도가 연결되면 대체 보행로 결과를 저장할 수 있어요.'));
   renderLocal();
 }};
 if (!window.kakao || !kakao.maps) {{
   initLocalCourseEditor();
   startBtn.disabled = true;
   setRunButtonLabel('지도 연결 필요');
 }} else kakao.maps.load(() => {{
 const startPos = segs.length
   ? new kakao.maps.LatLng(segs[0][0], segs[0][1])
   : new kakao.maps.LatLng({p.lat}, {p.lon});
 const map = new kakao.maps.Map(mapNode, {{center:startPos, level:6}});
 // Kept on a reference so editing can remove it: setZoomable(false) only stops
 // wheel/pinch, and a zoom press mid-selection would move the ground under the
 // screen-space edge hit test.
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
 // Kakao can reset interaction flags while applying bounds. Keep the default
 // course view explicitly draggable; segment selection opts out through applyMode().
 const syncMapInteraction = () => {{
   const selecting = editing && editMode === 'segment';
   map.setDraggable(!selecting);
   map.setZoomable(!selecting);
 }};
 if (route.length) {{
   const routeEnd = route[route.length - 1];
   const sameEndpoint = Math.abs(route[0][0] - routeEnd[0]) < .00002
     && Math.abs(route[0][1] - routeEnd[1]) < .00002;
   addOverlay(startPos, sameEndpoint
     ? '<div class="start-marker" title="출발·도착 지점">출발·도착</div>'
     : '<div class="start-marker" title="출발 지점">출발</div>', guideLayers);
   if (!sameEndpoint) addOverlay(routePath[routePath.length - 1],
     '<div class="finish-marker" title="도착 지점">도착</div>', guideLayers);
 }}
 for (const m of dirs) addOverlay(new kakao.maps.LatLng(m.lat,m.lon),
   '<div class="dir-marker" title="진행 방향"><svg viewBox="0 0 10 10" style="transform:rotate('+m.angle+'deg)" aria-hidden="true"><path d="M1.4 2.1 7.5 5 1.4 7.9 3.2 5Z"/></svg></div>',guideLayers);
 for (const k of kms) addOverlay(new kakao.maps.LatLng(k.lat,k.lon),
   '<div class="km-marker" title="'+k.km+'km 지점">'+k.km+'</div>',guideLayers);
 let editing = false;
 let editMode = null;
 let editNodes = initialEditPath.map(point => [...point]);
 let selectedRange = null;
 let undoStack = [];
 let draftLines = [];
 let selectPointer = null;
 let editBusy = false;
 let editLengthKm = initialLengthKm;
 syncMapInteraction();
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
   setText('factSignals', summary.signals + '개');
   setText('factStores', (summary.facility_counts.convenience_store || 0) + '개');
   setText('factRestrooms', (summary.facility_counts.restroom || 0) + '개');
   setText('facilityTally', summary.facility_tally);
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
 // One in-flight edit request at a time: a second replacement must not race
 // the first and apply its node indexes to a route that has already changed.
 const setEditBusy = value => {{
   editBusy = value;
   if (editTools) editTools.setAttribute('aria-busy', String(value));
   for (const button of [panTool, segmentTool, editUndo, editCancel, editSave]) {{
     if (!button) continue;
     if (value) button.disabled = true;
     else if (button === editUndo) button.disabled = !undoStack.length;
     else button.disabled = false;
   }}
 }};
 // km rides along in the snapshot: only the server knows the real edge-geometry
 // length, so undo restores the value that came back with that path.
 const snapshot = () => ({{nodes:editNodes.map(point=>[...point]),km:editLengthKm,summary:currentSummary}});
 const restore = state => {{editNodes=state.nodes.map(point=>[...point]);selectedRange=null;setEditDistance(state.km);applySummary(state.summary);renderDraft();}};
 const pointPath = points => points.map(([,lat,lon])=>new kakao.maps.LatLng(lat,lon));
 const renderDraft = () => {{
   draftLines.forEach(line=>line.setMap(null));draftLines=[];
   if (editing) {{
     draftLines.push(new kakao.maps.Polyline({{map,path:pointPath(editNodes),strokeColor:'#087b59',strokeWeight:7,strokeOpacity:.96,strokeStyle:'solid'}}));
     if(selectedRange){{
       const selected=editNodes.slice(selectedRange[0],selectedRange[1]+1);
       draftLines.push(new kakao.maps.Polyline({{map,path:pointPath(selected),strokeColor:'#fff',strokeWeight:13,strokeOpacity:.96,strokeStyle:'solid'}}));
       draftLines.push(new kakao.maps.Polyline({{map,path:pointPath(selected),strokeColor:'#e0522d',strokeWeight:8,strokeOpacity:1,strokeStyle:'solid'}}));
       for(const endpoint of [selected[0],selected[selected.length-1]]){{
         const marker=new kakao.maps.CustomOverlay({{map,position:new kakao.maps.LatLng(endpoint[1],endpoint[2]),content:'<span class="edit-anchor" aria-hidden="true"></span>',xAnchor:.5,yAnchor:.5,zIndex:8}});
         draftLines.push(marker);
       }}
     }}
   }}
   if(editUndo)editUndo.disabled=editBusy||!undoStack.length;
   if(editSave)editSave.disabled=editBusy;
 }};
 // 'pan' is the no-selection state and remains the default so one-finger map
 // movement works normally until the user explicitly asks to pick a segment.
 const syncToolPressed = () => {{
   if(panTool)panTool.setAttribute('aria-pressed',String(!editMode));
   if(segmentTool)segmentTool.setAttribute('aria-pressed',String(editMode==='segment'));
 }};
 const applyMode = () => {{
   syncToolPressed();
   document.body.classList.toggle('tool-active',Boolean(editMode));
   // One finger selects a line. Two-finger movement is still forwarded to the
   // map so a user does not need to leave selection mode just to inspect it.
   syncMapInteraction();
 }};
 const setMode = mode => {{
   if(editBusy)return;
   editMode=(mode==='pan'||editMode===mode)?null:mode;
   applyMode();
   setEditStatus(editMode==='segment'?'바꾸려는 코스 선을 탭하세요.':'지도 이동 모드예요. 끌어서 코스를 살펴보세요.','info');
 }};
 // A selected line stays highlighted, but the tool releases so the map can be
 // moved again while the user decides whether to replace it.
 const releaseTool = () => {{
   editMode=null;
   applyMode();
 }};
 const setEditing = value => {{
   editing=value;document.body.classList.toggle('editing',value);
   setLayers(routeLayers,!value);setLayers(shapeLayers,false);setLayers(guideLayers,!value);
   showZoomControl(!value);
   if(!value){{editMode=null;selectedRange=null;document.body.classList.remove('tool-active');}}
   syncMapInteraction();
   renderDraft();setEditDistance();
   if(!value)return hideEditToast();
   setEditStatus(editNotice||'구간 선택을 누른 뒤 바꿀 코스 선을 탭하세요.','info');
 }};
 const projection=map.getProjection();
 const screenPoint = node => projection.containerPointFromCoords(new kakao.maps.LatLng(node[1],node[2]));
 const overlayPoint = event => {{const rect=editOverlay.getBoundingClientRect();return {{x:event.clientX-rect.left,y:event.clientY-rect.top}};}};
 const distanceToSegment = (point,a,b) => {{
   const dx=b.x-a.x,dy=b.y-a.y,len2=dx*dx+dy*dy||1;
   const t=Math.max(0,Math.min(1,((point.x-a.x)*dx+(point.y-a.y)*dy)/len2));
   return Math.hypot(point.x-(a.x+dx*t),point.y-(a.y+dy*t));
 }};
 const nearestSegment = point => {{
   let best={{index:0,d:Infinity}};
   for(let index=0;index<editNodes.length-1;index++){{
     const d=distanceToSegment(point,screenPoint(editNodes[index]),screenPoint(editNodes[index+1]));
     if(d<best.d)best={{index,d}};
   }}
   return best;
 }};
 const postEdit = async body => {{
   const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),3500);
   try{{const response=await fetch(editEndpoint,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body),signal:controller.signal}});const payload=await response.json();if(!response.ok)throw new Error(payload.error||'코스 선을 처리하지 못했어요.');return payload;}}
   finally{{clearTimeout(timer);}}
 }};
 const replaceSelected = async () => {{
   if(editBusy||!selectedRange)return;
   const before=snapshot(),range=[...selectedRange];
   setEditBusy(true);setEditStatus('다른 보행로를 찾는 중…','busy');
   try{{
     const payload=await postEdit({{action:'reroute',path:editNodes.map(point=>point[0]),from_index:range[0],to_index:range[1]}});
     undoStack.push(before);if(undoStack.length>40)undoStack.shift();
     editNodes=payload.path.map(point=>[...point]);selectedRange=null;
     setEditDistance(payload.length_km);applySummary(payload.summary);
     setEditStatus('다른 보행로로 연결했어요 · '+editLengthKm.toFixed(2)+'km','success');
   }}catch(error){{
     setEditStatus(error.name==='AbortError'?'대체 경로 검색 시간이 초과됐어요. 선택한 선은 그대로예요.':(error.message||'다른 보행로를 찾지 못했어요.'),'error',{{label:'닫기',run:()=>{{}}}});
   }}finally{{setEditBusy(false);renderDraft();releaseTool();}}
 }};
 const selectSegmentAt = point => {{
   const hit=nearestSegment(point);
   if(hit.d>28){{releaseTool();setEditStatus('코스 선 가까이를 탭해 주세요.','error',{{label:'닫기',run:()=>{{}}}});return;}}
   selectedRange=[hit.index,hit.index+1];renderDraft();releaseTool();
   setEditStatus('선택한 구간을 다른 보행로로 바꿀까요?','info',{{label:'다른 길로',persist:true,run:replaceSelected}});
 }};
 if(editEnabled&&editButton)editButton.addEventListener('click',()=>setEditing(true));
 if(panTool)panTool.addEventListener('click',()=>setMode('pan'));
 if(segmentTool)segmentTool.addEventListener('click',()=>setMode('segment'));
 if(editOverlay){{
   const editPointers=new Map();
   let twoFingerPan=false,panCenter=null;
   const centerOfPointers=()=>{{const points=[...editPointers.values()].map(value=>value.current);return {{x:points.reduce((sum,p)=>sum+p.x,0)/points.length,y:points.reduce((sum,p)=>sum+p.y,0)/points.length}};}};
   const endOverlayPointer=event=>{{
     if(!editPointers.has(event.pointerId))return;
     const info=editPointers.get(event.pointerId);
     editPointers.delete(event.pointerId);
     if(twoFingerPan){{
       if(editPointers.size===0){{twoFingerPan=false;panCenter=null;selectPointer=null;}}
       else panCenter=centerOfPointers();
       return;
     }}
     if(event.pointerId===selectPointer){{
       selectPointer=null;
       if(Math.hypot(info.current.x-info.start.x,info.current.y-info.start.y)<=10)selectSegmentAt(info.current);
     }}
   }};
   editOverlay.addEventListener('pointerdown',event=>{{
     if(!editing||editMode!=='segment'||editBusy)return;
     const point=overlayPoint(event);editPointers.set(event.pointerId,{{start:point,current:point}});
     editOverlay.setPointerCapture(event.pointerId);
     if(event.pointerType==='touch'&&editPointers.size>=2){{
       twoFingerPan=true;panCenter=centerOfPointers();selectPointer=null;return;
     }}
     if(twoFingerPan)return;
     selectPointer=event.pointerId;
   }});
   editOverlay.addEventListener('pointermove',event=>{{
     if(!editPointers.has(event.pointerId))return;
     const point=overlayPoint(event),info=editPointers.get(event.pointerId);info.current=point;
     if(twoFingerPan){{
       if(editPointers.size<2)return;
       const next=centerOfPointers();
       if(panCenter)map.panBy(panCenter.x-next.x,panCenter.y-next.y);
       panCenter=next;return;
     }}
   }});
   editOverlay.addEventListener('pointerup',endOverlayPointer);
   editOverlay.addEventListener('pointercancel',event=>{{editPointers.delete(event.pointerId);if(event.pointerId===selectPointer)selectPointer=null;}});
 }}
 if(editUndo)editUndo.addEventListener('click',()=>{{if(editBusy||!undoStack.length)return;restore(undoStack.pop());setEditStatus('마지막 수정을 한 번 되돌렸어요.','info');}});
 // Reverting is the only irreversible action in the editor, so it is made
 // reversible instead of guarded by a modal: a confirm sheet would cover the
 // map and add a step, while an undo offer costs nothing when it is not needed.
 // The distance is restored with the path -- the badge would otherwise keep
 // showing the length of the discarded edit.
 if(editCancel)editCancel.addEventListener('click',()=>{{
   if(editBusy)return;
   const hadEdits=undoStack.length>0;
   const discarded={{...snapshot(),stack:undoStack.slice()}};
   editNodes=initialEditPath.map(point=>[...point]);selectedRange=null;undoStack=[];
   setEditDistance(initialLengthKm);applySummary(initialSummary);setEditing(false);
   if(!hadEdits)return;
   showEditToast('원본 코스로 되돌렸어요.','info',{{label:'실행 취소',run:()=>{{
     editNodes=discarded.nodes.map(point=>[...point]);
     undoStack=discarded.stack;
     setEditDistance(discarded.km);
     applySummary(discarded.summary);
     setEditing(true);
   }}}});
 }});
 if(editSave)editSave.addEventListener('click',async()=>{{
   if(!editing||editBusy)return;
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
   setRunButtonLabel('내 위치 다시 시도');
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
     setRunButtonLabel('내 위치 추적 시작');
     setStatus('GPS 안내 중지');
     return;
   }}
   setStatus('GPS 위치 확인 중');
   setMapMode('guide');
   startBtn.classList.add('on');
   setRunButtonLabel('위치 추적 중지');
   watchId = navigator.geolocation.watchPosition(updatePosition, locationError, {{
     enableHighAccuracy:true, maximumAge:3000, timeout:12000
   }});
 }});
 if (!window.isSecureContext || !navigator.geolocation) {{
   startBtn.disabled = true;
   setRunButtonLabel('위치 추적 사용 불가');
   setStatus(!window.isSecureContext ? 'HTTPS 연결에서 위치 기능을 사용할 수 있어요' : '이 브라우저는 위치 기능을 지원하지 않아요');
 }}
 }});
</script></body></html>"""


# ---------- share card (SVG, no image deps) ----------

def course_thumbnail_svg(course: Course) -> str:
    """Square, text-free route artwork over the real bundled OSM street map."""
    points = route_points(course)
    lats = [point[0] for point in points]
    lons = [point[1] for point in points]
    lat_c = (max(lats) + min(lats)) / 2
    lon_c = (max(lons) + min(lons)) / 2
    span_lat = max(max(lats) - min(lats), 1e-6)
    span_lon = max(max(lons) - min(lons), 1e-6) * 0.79
    size = 320
    scale = 252 / max(span_lat, span_lon)
    route = " ".join(
        f"{size / 2 + (lon - lon_c) * scale * 0.79:.1f},"
        f"{size / 2 - (lat - lat_c) * scale:.1f}"
        for lat, lon in points
    )
    first_x = size / 2 + (points[0][1] - lon_c) * scale * 0.79
    first_y = size / 2 - (points[0][0] - lat_c) * scale

    # Draw the actual walk-network under the course instead of a decorative
    # grid.  The image stays self-contained (important for Kakao's image
    # proxy), while every background line corresponds to a real OSM edge.
    pad_lat = max(span_lat * 0.18, 0.0007)
    pad_lon = max((max(lons) - min(lons)) * 0.18, 0.0007)
    lat_lo, lat_hi = min(lats) - pad_lat, max(lats) + pad_lat
    lon_lo, lon_hi = min(lons) - pad_lon, max(lons) + pad_lon
    radius_m = max(
        haversine_m(lat_c, lon_c, lat, lon)
        for lat, lon in ((lat_lo, lon_lo), (lat_lo, lon_hi),
                         (lat_hi, lon_lo), (lat_hi, lon_hi))
    )
    streets = graphmod.subgraph_around(lat_c, lon_c, radius_m)
    major_types = {"primary", "secondary", "tertiary"}
    major: list[tuple[float, object, object]] = []
    minor: list[tuple[float, object, object]] = []
    for u, v, attrs in streets.edges(data=True):
        a, b = streets.nodes[u], streets.nodes[v]
        if not (
            lat_lo <= a["lat"] <= lat_hi and lon_lo <= a["lon"] <= lon_hi
            or lat_lo <= b["lat"] <= lat_hi and lon_lo <= b["lon"] <= lon_hi
        ):
            continue
        highway = attrs.get("highway")
        if isinstance(highway, (list, tuple)):
            highway = highway[0] if highway else ""
        target = major if str(highway) in major_types else minor
        target.append((float(attrs.get("length", 0.0)), u, v))
    major.sort(reverse=True, key=lambda item: item[0])
    minor.sort(reverse=True, key=lambda item: item[0])

    def project(point: tuple[float, float]) -> tuple[float, float]:
        lat, lon = point
        return (
            size / 2 + (lon - lon_c) * scale * 0.79,
            size / 2 - (lat - lat_c) * scale,
        )

    def street_path(edges: list[tuple[float, object, object]]) -> str:
        commands: list[str] = []
        for _, u, v in edges:
            geometry = graphmod.edge_points(streets, u, v)
            if len(geometry) > 8:
                step = max(1, math.ceil(len(geometry) / 8))
                geometry = geometry[::step] + [geometry[-1]]
            xy = [project(point) for point in geometry]
            commands.append("M" + " L".join(
                f"{x:.1f},{y:.1f}" for x, y in xy
            ))
        return " ".join(commands)

    major_roads = street_path(major[:180])
    minor_roads = street_path(minor[:360])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">
 <defs><clipPath id="map-clip"><rect width="{size}" height="{size}" rx="32"/></clipPath></defs>
 <g clip-path="url(#map-clip)">
  <rect width="{size}" height="{size}" fill="#edf2ed"/>
  <path d="{minor_roads}" fill="none" stroke="#d8ded9" stroke-width="4"
    stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{minor_roads}" fill="none" stroke="#fff" stroke-width="2.2"
    stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{major_roads}" fill="none" stroke="#ccd4ce" stroke-width="7"
    stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{major_roads}" fill="none" stroke="#fff" stroke-width="4.5"
    stroke-linecap="round" stroke-linejoin="round"/>
 <polyline points="{route}" fill="none" stroke="#fff" stroke-width="15"
   stroke-linejoin="round" stroke-linecap="round"/>
 <polyline points="{route}" fill="none" stroke="#087b59" stroke-width="8"
   stroke-linejoin="round" stroke-linecap="round"/>
 <circle cx="{first_x:.1f}" cy="{first_y:.1f}" r="8" fill="#fff"
   stroke="#087b59" stroke-width="5"/>
 </g>
</svg>"""


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
   font-family="-apple-system,sans-serif">누적 오르막 {course.ascent_m:.0f}m</text>
 <text x="40" y="{h - 44}" font-size="18" fill="#666"
   font-family="-apple-system,sans-serif">Runnywhere(러니웨어) — 어디서든 러닝 코스 짜기!</text>
 <text x="40" y="{h - 18}" font-size="12" fill="#666"
   font-family="-apple-system,sans-serif">경로 데이터 © OpenStreetMap contributors · ODbL · 코스는 참고용</text>
 <polyline points="{pts}" fill="none" stroke="#e0533d" stroke-width="5"
   stroke-linejoin="round" stroke-linecap="round"/>
</svg>"""
