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
from .insights import CourseFacts, course_facts, is_loop
from .models import encode_course_id
from .naming import COURSE_EDIT_NOTICE, course_badges, course_name_placeholder, course_title
from .pace import DEFAULT_PACE_S, PACE_MODEL, effort
from .rfs import edge_rfs, has_sufficient_night_lighting, night_lighting_label
from .shapes import SHAPES

PREVIEW_FACILITY_TYPES = {"convenience_store", "restroom"}
FACILITY_CHIP_LIMIT = 10
FACILITY_EMOJI = {"convenience_store": "🏪", "restroom": "🚻"}
# Matches the server's own guard: past this an edited route cannot be a link.
COURSE_ID_MAX_CHARS = 4096
# One course, three jobs. Reading about a route, running it, and redrawing it
# want different things on screen -- and on one page the map ended up carrying
# an edit toolbar, a tracking control and a page of prose at the same time.
COURSE_PAGES = ("info", "run", "edit")
TAB_LABELS = {"info": "코스 정보", "run": "달리기", "edit": "코스 편집"}
TAB_ICONS = {"info": "📋", "run": "🏃", "edit": "✏️"}
PAGE_PATHS = {"info": "", "run": "/run", "edit": "/editor"}


def script_json(value) -> str:
    """JSON safe to embed inside a <script> element.

    `<` must never reach the HTML parser literally: a `</script>` inside any
    string -- a facility name, a place name -- would end the element early.
    Escaping `<` alone also neutralises `<!--`.
    """
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


# Our own failure sentences run past 120 characters; the cap only keeps a
# pathological string out of the tool result (PlayMCP asks for minimal size).
ERROR_TEXT_LIMIT = 600


def markdown_text(value: str) -> str:
    """Escape untrusted labels embedded in MCP Markdown responses."""
    value = "".join(ch for ch in value if ch >= " " and ch != "\x7f")[:120]
    for char in "\\`*_{}[]()<>#+-.!|":
        value = value.replace(char, "\\" + char)
    return value


def error_text(value: str) -> str:
    """Pass one of our own error sentences through to the runner intact.

    markdown_text() is for an untrusted label dropped inside our Markdown; run
    over a whole sentence it escaped every '.' and '-' (the runner read
    "찾지 못했어요\\.") and cut the message at 120 characters, which truncated
    the location-not-found help mid-word. These strings are ours, and the one
    untrusted part -- the echoed query -- is escaped and length-capped where
    it is interpolated.
    """
    return "".join(ch for ch in value
                   if ch >= " " or ch == "\n")[:ERROR_TEXT_LIMIT].strip()


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
        if has_sufficient_night_lighting(course.rfs):
            lines.append(f"- 💡 {night_lighting_label(course.rfs)}: 가로등 데이터가 야간 추천 최소 기준을 통과했어요")
        else:
            lines.append("- ⚠️ 가로등이 충분한지 확인되지 않아 야간 러닝에는 추천하지 않아요")
    lines.extend([
        "",
        "**바로 시작하기**",
        f"- 🗺️ 지도·러닝 가이드: {base_url}/c/{cid}",
        f"- ⬇️ GPX 다운로드: {base_url}/c/{cid}.gpx",
    ])
    lines.append("지도에서 통행·공사·날씨를 확인한 뒤 **러닝 시작**을 누르세요. 코스는 참고용이에요.")
    lines.extend(["", COURSE_EDIT_NOTICE])
    return "\n".join(lines)


# ---------- preview page data ----------

def route_points(course: Course) -> list[tuple[float, float]]:
    """The course polyline following real street geometry (OSM way shapes),
    so the drawn route stays on pedestrian roads instead of cutting straight
    chords through blocks/buildings between graph nodes."""
    return graphmod.path_points(graphmod.get_graph(), course.path)


def edit_path_nodes(path: list) -> list:
    """[[node_id, lat, lon], ...] -- the editor's handle on the route."""
    g = graphmod.get_graph()
    return [[node, round(g.nodes[node]["lat"], 6), round(g.nodes[node]["lon"], 6)]
            for node in path]


def edit_path_geometry(path: list) -> list:
    """Per-edge intermediate shape points, parallel to the edges of ``path``.

    The editor used to draw a straight chord between consecutive graph nodes
    while the info and run pages drew the same route along its real OSM way
    geometry. Two pages, one course, two different lines -- and the editor's
    version cut corners through blocks the route never enters. ``null`` means
    the way really is straight there, which for 58% of the network it is.
    """
    g = graphmod.get_graph()
    out = []
    for u, v in zip(path, path[1:]):
        points = graphmod.edge_points(g, u, v)
        middle = [[round(lat, 6), round(lon, 6)] for lat, lon in points[1:-1]]
        out.append(middle or None)
    return out


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


def _facility_rows(facilities: list[dict], course: Course) -> list[dict]:
    """Course-ordered rows, bracketed by the start and the finish.

    One shape for both consumers is the only way the panel a runner sees after
    an edit stays identical to the one a full page load renders. The anchors
    are part of that list rather than separate markup for the same reason --
    komoot's waypoint list carries its Starting Point / End Point the same way,
    and a list that opens at 1.9km leaves the runner placing it by guesswork.
    """
    start_name = course.params.location_name or "출발점"
    rows = [{
        "at_km": "0km", "emoji": "🚩", "kind": "",
        "name": f"{start_name} 출발", "anchor": True,
    }]
    for f in facilities[:FACILITY_CHIP_LIMIT]:
        kind = LABELS_KO[f["type"]]
        name = f.get("name") or kind
        rows.append({
            "at_km": f"{f['at_km']:g}km",
            "emoji": FACILITY_EMOJI[f["type"]],
            # Unnamed POIs fall back to their category, and "화장실 · 화장실"
            # is a rendering bug the runner has to decode.
            "kind": "" if name == kind else kind,
            "name": name,
            "anchor": False,
        })
    rows.append({
        "at_km": f"{course.length_km:.1f}km", "emoji": "🏁", "kind": "",
        "name": f"{start_name} 도착" if is_loop(course) else "도착",
        "anchor": True,
    })
    return rows


def _badge_html(index: int, badge: dict) -> str:
    """One course badge and the tooltip that says what it means.

    A bare emoji next to the run name is a riddle -- 🏙️ and 🌳 differ only by
    hue at 30px, and `title=` never appears on a phone. This ships the label
    and the reason it was awarded in a popover that opens on hover, on focus
    and on tap.
    """
    tip_id = f"badgeTip{index}"
    label = html.escape(badge["label"])
    detail = html.escape(badge.get("detail", ""))
    return (
        f'<span class="badge-wrap">'
        f'<button class="badge" type="button" aria-label="{label}"'
        f' aria-describedby="{tip_id}" aria-expanded="false">'
        f'<span aria-hidden="true">{badge["emoji"]}</span></button>'
        f'<span class="badge-tip" id="{tip_id}" role="tooltip">'
        f'<b>{label}</b>{detail}</span></span>'
    )


def _trait_chips_html(traits) -> str:
    return "".join(
        f'<span class="trait"><i aria-hidden="true">{html.escape(t["emoji"])}</i>'
        f'{html.escape(t["label"])}</span>'
        for t in traits
    )


def _note_box_html(box_id: str, list_id: str, tone: str, heading: str,
                   notes) -> str:
    """A good-points or caveats box, hidden when the course has nothing to say."""
    items = "".join(f"<li>{html.escape(note)}</li>" for note in notes)
    hidden = "" if notes else " hidden"
    return (
        f'<div class="note-box {tone}" id="{box_id}"{hidden}>'
        f"<b>{heading}</b>"
        f'<ul id="{list_id}">{items}</ul></div>'
    )


def _character_panel_html(facts: CourseFacts) -> str:
    """What kind of run this is, before any number is discussed.

    Emoji badges next to the title identify the course; they cannot say that
    it is flat, dark or crossing-heavy. That belongs here, in words.
    """
    return (
        '<section class="panel" id="character"><h2>이 코스는 이런 코스예요</h2>'
        f'<div class="trait-chips" id="courseTraits">'
        f"{_trait_chips_html(facts.traits)}</div>"
        + _note_box_html("courseGoodBox", "courseGood", "good", "👍 좋은 점",
                         facts.highlights)
        + _note_box_html("courseCareBox", "courseCare", "care", "⚠️ 참고할 점",
                         facts.cautions)
        + "</section>"
    )


def _facility_rows_html(rows: list[dict]) -> str:
    items = "".join(
        f'<li class="facility-row{" anchor" if row["anchor"] else ""}">'
        f'<span class="facility-km">{html.escape(row["at_km"])}</span>'
        f'<span class="facility-icon" aria-hidden="true">{html.escape(row["emoji"])}</span>'
        f'<span class="facility-name">{html.escape(row["name"])}</span>'
        + (f'<span class="facility-kind">{html.escape(row["kind"])}</span>'
           if row["kind"] else "")
        + "</li>"
        for row in rows
    )
    empty = (
        '<p class="facility-empty" id="facilityEmpty">'
        "코스 10m 안에 편의점·화장실이 없어요. 출발 전에 준비해 주세요.</p>"
        if all(row["anchor"] for row in rows) else ""
    )
    return f'{empty}<ul class="facility-rows">{items}</ul>'


def _facility_panel_html(facts: CourseFacts, rows: list[dict]) -> str:
    """Crossings and on-route facilities in one panel.

    They used to be two panels showing the same three numbers twice. What a
    runner actually asks is "where, and how far in" -- so the counts summarise
    and the list answers.
    """
    counts = facts.facility_counts
    cells = "".join(
        f'<div class="fact"><b id="{el_id}">{value}개</b><span>{label}</span></div>'
        for label, value, el_id in (
            ("보행 신호", facts.signals, "factSignals"),
            ("편의점", counts["convenience_store"], "factStores"),
            ("화장실", counts["restroom"], "factRestrooms"),
        )
    )
    return (
        '<section class="panel" id="facilities"><h2>러닝 중 편의시설</h2>'
        f'<div class="facts">{cells}</div>'
        f'<div id="facilityList">{_facility_rows_html(rows)}</div>'
        "</section>"
    )


def course_edit_summary(course: Course) -> dict:
    """The live numbers the detail panels show, recomputed for an edited course.

    Returned by the snap endpoint so the page below the map stops describing
    the course the user just changed. Mirrors what preview_html() renders on a
    full page load, so the two never disagree.
    """
    points = route_points(course)
    facilities = [f for f in facilities_along(points, sorted(PREVIEW_FACILITY_TYPES), limit=80)
                  if f["type"] in PREVIEW_FACILITY_TYPES]
    facts = course_facts(course, facilities)
    elev_range = _elevation_range(_elevation_profile(course))
    lo, hi = course.duration_range_min
    counts = facts.facility_counts
    # The download, card and share links all address a course by id. Editing
    # rewrote every panel but left them pointing at the original route, so a
    # runner who edited and tapped GPX got the course they had just changed
    # away from. The id travels with the numbers it belongs to.
    course_id = encode_course_id(course.params)
    return {
        "course_id": course_id if len(course_id) <= COURSE_ID_MAX_CHARS else "",
        "length_km": round(course.length_km, 2),
        "ascent_m": round(course.ascent_m),
        "elev_range": list(elev_range) if elev_range else None,
        "grade_label": course.grade_label,
        "duration_min": [lo, hi],
        "signals": facts.signals,
        "facility_counts": counts,
        "facility_rows": _facility_rows(facilities, course),
        "traits": [dict(trait) for trait in facts.traits],
        "highlights": list(facts.highlights),
        "cautions": list(facts.cautions),
        "start_name": course.params.location_name or "지정한 출발점",
        "badges": [{"emoji": b["emoji"], "label": b["label"],
                    "detail": b.get("detail", "")}
                   for b in course_badges(course)],
        "title": course_title(course),
        # What the rename field shows in grey. It follows the edit: a course
        # that grew from 4.8km to 5.3km offers the 5.3km name, not a stale one.
        "name_placeholder": course_name_placeholder(course),
    }


def _edit_chrome_html() -> str:
    """The drawing overlay, the editing controls, the toast and the rename sheet.

    Laid out the way AllTrails lays out its route editor, because the previous
    card -- a titled panel with a status chip, a distance readout, three modes
    and four actions, pinned to the top of the map -- covered half the route it
    was editing. The map is the thing being worked on, so it keeps the screen:
    undo/redo/reset are icon-only buttons in the corner stack, the three modes
    are a segmented pill at the bottom, and there is exactly one primary button
    whose label and colour say what the editor will do next.
    """
    return """<div id="mapPanSurface" class="map-pan-surface" aria-hidden="true"></div><svg id="editOverlay" class="edit-overlay" aria-hidden="true"></svg><div class="edit-quick" role="group" aria-label="편집 되돌리기"><button id="editUndo" class="edit-quick-btn" type="button" aria-label="마지막 수정 실행 취소" title="한 번 되돌리기"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 7-4 4 4 4"/><path d="M5 11h8a6 6 0 0 1 6 6v1"/></svg></button><button id="editRedo" class="edit-quick-btn" type="button" aria-label="되돌린 수정 다시 실행" title="다시 실행"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m16 7 4 4-4 4"/><path d="M19 11h-8a6 6 0 0 0-6 6v1"/></svg></button><button id="editCancel" class="edit-quick-btn reset" type="button" aria-label="모든 수정 초기화" title="전체 초기화"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12a8 8 0 1 0 2.3-5.7L4 8.6"/><path d="M4 4v4.6h4.6"/></svg></button></div><div class="edit-tools" role="toolbar" aria-label="코스 편집 도구"><div class="edit-mode-group" role="group" aria-label="편집 방식"><button id="panTool" class="edit-tool mode" type="button" aria-label="지도 이동" aria-pressed="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.5 11V6.5a1.5 1.5 0 0 1 3 0V10 5.5a1.5 1.5 0 0 1 3 0V10 7a1.5 1.5 0 0 1 3 0v4-2a1.5 1.5 0 0 1 3 0v4.5c0 4.4-3 7.5-7.3 7.5H12c-2.4 0-4.1-1-5.5-2.8L3.8 15a1.7 1.7 0 0 1 2.6-2.2L8.5 15"/></svg><span>지도 이동</span></button><button id="eraserTool" class="edit-tool mode" type="button" aria-label="지우개 · 코스 선을 문질러 지우기" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15.5 4.5 4 4a2 2 0 0 1 0 2.8l-7.7 7.7H7.5l-3-3a2 2 0 0 1 0-2.8l8.2-8.7a2 2 0 0 1 2.8 0Z"/><path d="m9 9 6 6M12 19h8"/></svg><span>지우기</span></button><button id="drawTool" class="edit-tool mode" type="button" aria-label="그리기 · 코스 선 위에 새 길을 그리기" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14.5 5.5 4 4M4 20l3.8-.8L19 8a2.1 2.1 0 0 0-3-3L4.8 16.2 4 20Z"/><path d="M12.5 7.5l4 4"/></svg><span>그리기</span></button></div><button id="editSave" class="edit-primary" type="button" data-action="save"><span id="editSaveLabel">저장</span></button></div><div id="editToast" class="edit-toast" role="status" aria-live="polite" data-tone="info" hidden><span class="edit-toast-spin" aria-hidden="true"></span><span id="editToastText" class="edit-toast-text"></span><button id="editToastAction" class="edit-toast-action" type="button" hidden></button></div><div id="nameSheet" class="name-sheet" role="dialog" aria-modal="true" aria-labelledby="nameSheetTitle" hidden><div class="name-sheet-card"><h2 id="nameSheetTitle">코스 이름을 지어 주세요</h2><p id="nameSheetHint">비워 두면 지금 이름 그대로 저장돼요.</p><input id="nameSheetInput" class="name-sheet-input" type="text" maxlength="24" autocomplete="off" enterkeyhint="done" aria-labelledby="nameSheetTitle" aria-describedby="nameSheetHint"><div class="name-sheet-actions"><button id="nameSheetCancel" class="name-sheet-btn ghost" type="button">취소</button><button id="nameSheetSave" class="name-sheet-btn" type="button">저장</button></div></div></div>"""


def _tab_bar_html(base_url: str, cid: str, page: str) -> str:
    """The one control that is on every page.

    komoot, AllTrails, Strava and Slopes all keep a persistent bar and give
    the primary action extra weight (Strava's Record is a filled circle);
    running the course is this product's Record.
    """
    tabs = "".join(
        f'<a class="tab{" primary" if key == "run" else ""}'
        f'{" current" if key == page else ""}"'
        f' href="{base_url}/c/{cid}{PAGE_PATHS[key]}"'
        f' data-page="{PAGE_PATHS[key]}"'
        f'{" aria-current=\"page\"" if key == page else ""}>'
        f'<span class="tab-icon" aria-hidden="true">{TAB_ICONS[key]}</span>'
        f"<span>{TAB_LABELS[key]}</span></a>"
        for key in COURSE_PAGES
    )
    return f'<nav class="tab-bar" aria-label="코스 화면 전환">{tabs}</nav>'


# One confirmation surface for the whole page, on every page. The edit toast
# is positioned inside .map-wrap and only ships with the editor, so a control
# further down the page -- sharing sits at the very bottom of the run page --
# had nowhere on screen to answer from.
PAGE_TOAST_HTML = (
    '<div id="pageToast" class="page-toast" role="status" aria-live="polite"'
    ' data-tone="ok" hidden><span id="pageToastText" class="page-toast-text">'
    '</span></div>'
)


def _howto_panel_html(base_url: str, cid: str) -> str:
    """The ways to run this course somewhere other than here.

    Running it on this page with live location is the product, so it owns the
    bottom bar. These are for a runner who wants their own watch or app, and
    they belong below that, not in competition with it.

    Sharing is the third of those ways -- handing the course to someone else --
    so it reads as a row like its siblings rather than as the pale ghost pill
    it used to be, which looked disabled next to them and gave a 13px label a
    44px box on a phone.
    """
    return f"""<section class="panel" id="howto"><h2>다른 앱으로 달리기</h2>
 <a class="action-row" id="gpxLink" href="{base_url}/c/{cid}.gpx">
  <span class="action-icon" aria-hidden="true">📥</span>
  <span class="action-text"><b>GPX 파일 받기</b>
   <span>GPS 파일을 내려받아 쓰던 러닝 앱에서 바로 열어요.</span></span>
 </a>
 <div class="action-row action-guide">
  <div class="action-head">
   <span class="action-icon" aria-hidden="true">🗺️</span>
   <span class="action-text"><b>카카오맵에서 열기</b>
    <span>받은 GPX를 카카오맵에 불러오면 음성 안내로 달릴 수 있어요.</span></span>
  </div>
  <ol class="steps">
   <li>위 GPX 파일 받기를 눌러 코스를 저장합니다.</li>
   <li>카카오맵 앱에서 우측 상단 길찾기를 누르세요.</li>
   <li>이동수단을 자전거로 고른 뒤, 도착지 입력 화면 우측 하단의 GPX를 선택하세요.</li>
   <li>저장한 파일을 고르고 완료한 뒤, 우측 하단 주행 시작을 누르면 안내가 시작됩니다.</li>
  </ol>
 </div>
 <button class="action-row share-row" id="shareCourse" type="button">
  <span class="action-icon" aria-hidden="true">🔗</span>
  <span class="action-text"><b>링크 복사해 공유하기</b>
   <span>코스 링크를 복사해 보내면 친구도 같은 코스를 열어 볼 수 있어요.</span></span>
 </button>
</section>"""


def preview_html(course: Course, facilities: list[dict], base_url: str,
                 kakao_javascript_key: str = "", page: str = "info") -> str:
    """One generator, three pages.

    The shell -- head, styles, map, tab bar, scripts -- is identical on all
    three so a course keeps one identity as the runner moves between them.
    Only the sections under the map change. The script block is shared too:
    every feature already guards on its own DOM nodes, and one script that
    finds nothing to bind is far safer than three that drift apart.
    """
    if page not in COURSE_PAGES:
        raise ValueError(f"unknown course page: {page}")
    facilities = [f for f in facilities if f["type"] in PREVIEW_FACILITY_TYPES]
    p = course.params
    cid = encode_course_id(p)
    shape = SHAPES.get(p.shape) if p.shape else None
    title = html.escape(course_title(course))
    badges = course_badges(course)
    badge_html = "".join(
        _badge_html(index, badge) for index, badge in enumerate(badges))
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
    facts = course_facts(course, facilities)
    facility_rows = _facility_rows(facilities, course)
    markers = json.dumps([
        {"lat": f["lat"], "lon": f["lon"], "type": f["type"],
         "name": html.escape(f.get("name") or LABELS_KO[f["type"]]),
         "label": html.escape(f"{LABELS_KO[f['type']]} · {f['at_km']:g}km 지점")}
        for f in facilities
    ])
    shape_view_label = "러닝 코스"
    # An animal course exists to be looked at. Opening its info page on the
    # running-guide layer buried the one thing that makes it different, so the
    # silhouette leads there and the guide stays one tap away. The run and
    # editor pages still open on the guide: both are about the road ahead.
    opens_on_shape = bool(shape) and page == "info"
    where = html.escape(p.location_name)
    where_html = (
        f'<p class="course-where"><span aria-hidden="true">📍</span> '
        f"{where} 출발·도착</p>" if where else ""
    )
    edit_enabled = os.environ.get("RUNART_ROUTE_EDIT", "1") == "1"
    # Surfaced as a toast when editing starts. Saving an edited animal course
    # drops the silhouette, so this expectation has to reach sighted users --
    # it used to live in an sr-only node where nobody could see it.
    edit_notice = (
        "원본 동물 코스는 유지되고, 저장하면 새 직접 편집한 코스가 만들어져요."
        if shape else ""
    )
    edit_path = json.dumps(edit_path_nodes(course.path))
    edit_geometry = json.dumps(edit_path_geometry(course.path))
    elev_range = _elevation_range(profile)
    elev_text = (f"{elev_range[0]}~{elev_range[1]}<i>m</i>" if elev_range else "정보 없음")
    initial_effort = effort(course.length_km, DEFAULT_PACE_S)
    # The track runs fast-to-slow left-to-right; nothing on screen said so,
    # and nothing marked the value the page opens at.
    default_pace_label = effort(course.length_km, DEFAULT_PACE_S)["pace_label"]
    identity = f"""<div class="card course-summary">
 <div class="course-head"><h1 id="courseTitle">{title}</h1><div class="course-badges" id="courseBadges">{badge_html}</div></div>
 {where_html}
 <div class="headline-stats">
  <div class="lead"><b id="mLength">{course.length_km:.1f}<i>km</i></b><span>거리</span></div>
  <div><b><span id="mDuration">{initial_effort["duration_min"]}</span><i>분</i></b><span>예상 시간</span></div>
  <div><b id="mAscent">{course.ascent_m:.0f}<i>m</i></b><span>누적 오르막</span></div>
 </div>
</div>"""
    effort_card = f"""<div class="card course-summary">
 <div class="course-head"><h1 id="courseTitle">{title}</h1><div class="course-badges" id="courseBadges">{badge_html}</div></div>
 {where_html}
 <div class="headline-stats">
  <div class="lead"><b id="mLength">{course.length_km:.1f}<i>km</i></b><span>거리</span></div>
  <div><b><span id="mDuration">{initial_effort["duration_min"]}</span><i>분</i></b><span>예상 시간</span></div>
  <div><b id="mAscent">{course.ascent_m:.0f}<i>m</i></b><span>누적 오르막</span></div>
 </div>
 <div class="pace-picker">
  <div class="pace-head">
   <span class="pace-caption">내 페이스</span>
   <span class="pace-read"><b id="paceValue">{initial_effort["pace_label"]}</b><i>/km</i></span>
   <span class="pace-tier" id="paceTier">{initial_effort["tier"]}</span>
  </div>
  <input id="paceRange" class="pace-range" type="range" min="{PACE_MODEL["fastest_s"]}"
   max="{PACE_MODEL["slowest_s"]}" step="{PACE_MODEL["step_s"]}" value="{DEFAULT_PACE_S}"
   aria-label="1km당 목표 페이스"
   aria-valuetext="{initial_effort["pace_label"]} 퍼 킬로미터, {initial_effort["tier"]}">
  <div class="pace-scale" aria-hidden="true">
   <span>빠르게</span><span class="pace-default">기본 {default_pace_label}</span><span>느리게</span>
  </div>
 </div>
 <dl class="course-metrics">
  <div><dt class="metric-label">걸음 수</dt><dd class="metric-value" id="mSteps">{initial_effort["steps"]:,}<i>걸음</i></dd></div>
  <div><dt class="metric-label">칼로리</dt><dd class="metric-value" id="mKcal">{initial_effort["kcal"]}<i>kcal</i></dd></div>
  <div><dt class="metric-label">고도 범위</dt><dd class="metric-value" id="mElev">{elev_text}</dd></div>
 </dl>
 <p class="metric-note-inline">걸음·칼로리는 성인 {PACE_MODEL["weight_kg"]:.0f}kg 기준 추정치예요.</p>
</div>"""
    editor_card = f"""<div class="card course-summary">
 <div class="course-head"><h1 id="courseTitle">{title}</h1><div class="course-badges" id="courseBadges">{badge_html}</div></div>
 {where_html}
 <div class="headline-stats">
  <div class="lead"><b id="mLength">{course.length_km:.1f}<i>km</i></b><span>거리</span></div>
  <div><b><span id="mDuration">{initial_effort["duration_min"]}</span><i>분</i></b><span>예상 시간</span></div>
  <div><b id="mAscent">{course.ascent_m:.0f}<i>m</i></b><span>누적 오르막</span></div>
 </div>
 <ol class="steps edit-steps">
  <li><code>지우기</code>를 누른 뒤 바꾸고 싶은 코스 구간을 선택하고, <code>선택 구간 지우기</code>를 눌러주세요.</li>
  <li><code>그리기</code>를 누르고 새로운 코스를 지도 위에 그린 뒤, <code>도보 경로 확인</code>을 눌러주세요.</li>
  <li><code>저장</code>을 누르면 나만의 코스가 완성돼요.</li>
 </ol>
 <p class="metric-note-inline" id="editNoticeText">{edit_notice or "거리와 아래 정보는 수정하는 즉시 다시 계산돼요."}</p>
</div>"""
    character_panel = _character_panel_html(facts)
    facility_panel = _facility_panel_html(facts, facility_rows)
    howto_panel = _howto_panel_html(base_url, cid)
    tab_bar = _tab_bar_html(base_url, cid, page)
    run_float = (
        '<div class="run-float"><button class="run-start" id="runCta" type="button">'
        '<span aria-hidden="true">▶</span>달리기 시작</button></div>'
        if page == "run" else ""
    )
    edit_chrome = _edit_chrome_html() if page == "edit" and edit_enabled else ""
    page_sections = {
        "info": identity + character_panel + facility_panel,
        "run": effort_card + howto_panel,
        "edit": editor_card,
    }[page]
    shape_view_active = ' class="active"' if opens_on_shape else ""
    opens_on_shape_js = str(opens_on_shape).lower()
    guide_view_active = "" if opens_on_shape else ' class="active"'
    pace_model = script_json(PACE_MODEL)
    initial_summary = script_json(course_edit_summary(course))
    base_url_json = script_json(base_url)
    page_json = script_json(page)
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
 /* touch-action:none so a swipe that starts on the map pans the map instead
    of scrolling the page out from under it. */
 #map{{position:relative;height:62vh;min-height:460px;background:#e8ece5;touch-action:none}}
 .local-course-editor{{height:100%;position:relative;overflow:hidden;background:#e8ece5}}
 .local-course-editor svg{{width:100%;height:100%;display:block;touch-action:none;background:linear-gradient(135deg,#f5f8f2 25%,#e9f0e7 25%,#e9f0e7 50%,#f5f8f2 50%,#f5f8f2 75%,#e9f0e7 75%);background-size:42px 42px}}
 .local-course-hint{{position:absolute;z-index:2;left:14px;right:14px;top:14px;padding:10px 12px;border-radius:12px;background:rgba(255,255,255,.94);box-shadow:0 4px 18px rgba(0,0,0,.1);font-size:13px;font-weight:700;line-height:1.4;color:#243028}}
 .local-course-hint strong{{display:block;color:#087b59;margin-bottom:2px}}
 .local-editor-actions{{position:absolute;z-index:3;inset:0;pointer-events:none}}
 .local-editor-actions button{{min-height:46px;padding:0 13px;border:0;border-radius:12px;background:#fff;color:#142018;box-shadow:0 4px 18px rgba(0,0,0,.14);font:700 14px inherit}}
 .local-editor-actions .local-primary{{background:#087b59;color:#fff}}
 #localEditRoute{{position:absolute;left:14px;bottom:14px;pointer-events:auto}}
 .local-editor-actions .local-edit-tools{{display:none;position:absolute;left:12px;right:12px;bottom:12px;
      grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;padding:10px;border-radius:14px;
      border:1px solid rgba(20,35,25,.12);background:rgba(255,255,255,.97);
      box-shadow:0 8px 24px rgba(10,28,19,.16);pointer-events:auto}}
 .local-course-editor.editing .local-edit-tools{{display:grid}}
 .local-course-editor.editing #localEditRoute{{display:none}}
 .local-course-editor.editing .local-course-hint{{top:84px;left:12px;right:12px;padding:7px 9px;box-shadow:none}}
 .map-error{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:24px;
      box-sizing:border-box;text-align:center;color:#44514a;font-size:14px;line-height:1.5;background:#eef2ec}}
 .map-error strong{{font-size:16px;color:#142018}}
 .map-error button{{min-height:44px;padding:0 18px;border:1px solid #c3cec6;border-radius:12px;
      background:#fff;color:#142018;font-size:14px;font-weight:700;font-family:inherit;cursor:pointer}}
 .view-toggle{{position:absolute;z-index:530;right:14px;top:14px;display:flex;background:rgba(255,255,255,.96);
      border:1px solid rgba(20,35,25,.1);border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.1);overflow:hidden}}
 .view-toggle button{{min-height:48px;border:0;background:transparent;color:#4b5a50;padding:0 13px;font-size:13px;font-weight:800;font-family:inherit}}
 .view-toggle button.active{{background:#142018;color:#fff}}
 .wrap{{padding:22px;max-width:1040px;margin:0 auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
 .card,.panel{{background:#fff;border:1px solid #dfe7e1;border-radius:18px;padding:22px;margin:0;box-shadow:0 12px 34px rgba(20,45,30,.045)}}
 .course-summary{{grid-column:1/-1}}
 /* The run page reads top to bottom: its panels are the same column as the
    effort card above them, not a second column beside it. */
 body.page-run .panel{{grid-column:1/-1}}
 /* Title and badges share a baseline row; badges never push the name to wrap. */
 .course-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:8px}}
 .course-badges{{display:flex;flex-shrink:0;gap:4px;padding-top:2px}}
 .badge-wrap{{position:relative;display:inline-flex}}
 .badge{{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
      padding:0;border-radius:999px;background:#f2f6f0;border:1px solid #e1e7dd;font-size:16px;
      line-height:1;font-family:inherit;cursor:help}}
 .badge:focus-visible{{outline:3px solid #8ee0bb;outline-offset:2px}}
 /* Right-anchored: the badges sit at the end of the title row, so a
    centred bubble would hang off the screen edge on a phone. */
 .badge-tip{{position:absolute;z-index:940;top:calc(100% + 9px);right:-6px;width:max-content;
      max-width:min(252px,calc(100vw - 40px));padding:9px 11px;border-radius:11px;
      background:#142018;color:#fff;font-size:12.5px;font-weight:600;line-height:1.45;
      text-align:left;word-break:keep-all;box-shadow:0 8px 24px rgba(10,25,16,.28);
      opacity:0;visibility:hidden;transform:translateY(-4px);
      transition:opacity .14s ease-out,transform .14s ease-out,visibility .14s;
      pointer-events:none}}
 .badge-tip b{{display:block;font-size:13px;font-weight:800;margin-bottom:2px}}
 .badge-tip::before{{content:"";position:absolute;bottom:100%;right:16px;border:6px solid transparent;
      border-bottom-color:#142018}}
 .badge-wrap.open .badge-tip{{opacity:1;visibility:visible;transform:none}}
 @media (hover:hover){{.badge-wrap:hover .badge-tip,.badge-wrap:focus-within .badge-tip{{
      opacity:1;visibility:visible;transform:none}}}}
 h1{{margin:0;font-size:26px;line-height:1.28;letter-spacing:-.035em;word-break:keep-all}}
 h2,h3{{margin:0 0 12px;font-size:17px;letter-spacing:-.02em}}
 .stat{{color:#3d473f;line-height:1.65;font-size:15px}}
 .pace-picker{{border:0;border-top:1px solid #eef1ed;background:none;padding:16px 0 4px;margin:0 0 4px}}
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
 .metric-value i{{font-style:normal;font-size:13px;font-weight:700;color:#8a958d;margin-left:2px}}
 .metric-note-inline{{margin:14px 0 0;font-size:12px;line-height:1.55;color:#8a958d}}
 .course-metrics{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;
      margin:18px 0 0;padding-top:16px;border-top:1px solid #eef1ed}}
 .course-metrics>div{{min-width:0}}
 /* dt/dd carry a 40px UA margin-inline-start that squeezes the value out of the cell. */
 .course-metrics dt,.course-metrics dd{{margin:0}}
 .metric-label{{display:block;font-size:12px;color:#8a958d;letter-spacing:.01em;word-break:keep-all}}
 .metric-value{{display:block;margin-top:5px;font-size:20px;font-weight:800;color:#142018;
      letter-spacing:-.035em;white-space:nowrap;font-variant-numeric:tabular-nums}}
 .metric-note{{display:block;font-size:12px;font-weight:700;color:#17613e;margin-top:2px}}
 .course-where{{margin:-6px 0 12px;font-size:15px;font-weight:700;color:#44514a;word-break:keep-all}}
 .tag{{padding:6px 9px;border-radius:999px;background:#edf5f0;color:#17613e;font-size:13px;font-weight:700;word-break:keep-all}}
 .score{{font-size:1.35em;font-weight:800;color:#0a7d43}}
 .legend{{font-size:13px;color:#55605a;margin:10px 0 0}}
 /* Padding (not flex) keeps the native disclosure marker while reaching a 44px target. */
 details.panel summary{{cursor:pointer;font-size:15px;font-weight:800;color:#344238;padding:13px 0;min-height:44px}}
 details.panel[open] summary{{margin-bottom:10px}}
 details.more-actions{{margin-top:10px}}details.more-actions summary{{cursor:pointer;font-size:13px;color:#4a554e;font-weight:700;padding:14px 0;min-height:44px}}
 .metric{{margin:10px 0}}
 .metric-top{{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:#445048;margin-bottom:5px}}
 .bar{{height:8px;background:#e8ede6;border-radius:999px;overflow:hidden}}
 .bar i{{display:block;height:100%;background:#2da85f;border-radius:999px}}
 .facts{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:2px}}
 .fact{{min-width:0}}
 .fact b{{display:block;font-size:20px;font-weight:800;color:#142018;letter-spacing:-.035em;
      line-height:1.1;word-break:keep-all;font-variant-numeric:tabular-nums}}
 .fact span{{display:block;margin-top:5px;font-size:12px;color:#8a958d;letter-spacing:.01em}}
 .steps{{margin:8px 0 0;padding-left:20px;color:#3d473f;line-height:1.65;font-size:14px}}
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
 .facility-marker{{position:relative;display:flex;align-items:center;justify-content:center;
      width:26px;height:26px;font-size:14px;line-height:1;border:2px solid #fff;border-radius:999px;
      box-shadow:0 2px 8px rgba(0,0,0,.24);cursor:pointer}}
 /* A 12px dot is unhittable with a finger. Widen the tap area to 44px on touch
    only -- on a mouse the dot is already precise, and enlarging it there would
    swallow map drags that start near a marker. */
 /* 44px total, the accessible minimum -- not the 58px it used to be. With
    up to 80 markers on one screen the larger disc tiled the map, and every
    drag that began inside one was a marker gesture instead of a map gesture. */
 @media (pointer:coarse){{.facility-marker::before{{content:"";position:absolute;top:-9px;right:-9px;
      bottom:-9px;left:-9px;border-radius:50%}}}}
 .facility-marker.convenience_store{{background:#eaf1ff}}
 .facility-marker.restroom{{background:#e6f7ee}}
 .poi-pop{{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);background:#fff;
      border:1px solid rgba(20,35,25,.14);border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.18);
      padding:8px 10px;min-width:150px;max-width:220px;z-index:900;text-align:left;pointer-events:none}}
 .poi-pop b{{display:block;font-size:13px;color:#142018;margin-bottom:2px;white-space:nowrap;
      overflow:hidden;text-overflow:ellipsis}}
 .poi-pop span{{font-size:13px;color:#5c675e;line-height:1.4;word-break:keep-all}}
 .sr-only{{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}}
 /* Bottom-anchored so the route keeps the map. The old card sat at the top
    with a title row, a status chip and a distance chip, and covered half of
    what it was editing. */
 /* One row: the three modes and the single primary action. Two rows cost
    97px of a 354px map for chrome that is mostly whitespace. */
 .edit-tools{{position:absolute;z-index:950;left:10px;right:10px;
      bottom:calc(10px + env(safe-area-inset-bottom));display:none;gap:7px;
      flex-direction:row;align-items:center}}
 body.editing .edit-tools{{display:flex}}
 .edit-mode-group{{flex:1 1 auto;min-width:0;display:grid;
      grid-template-columns:repeat(3,minmax(0,1fr));gap:2px;
      padding:3px;border-radius:13px;background:rgba(255,255,255,.96);
      box-shadow:0 4px 18px rgba(10,28,19,.18);backdrop-filter:blur(8px)}}
 .edit-tool{{display:inline-flex;align-items:center;justify-content:center;gap:5px;min-width:0;
      min-height:40px;padding:0 6px;border:0;border-radius:10px;background:transparent;
      color:#4a5951;font-family:inherit;font-size:12.5px;font-weight:750;line-height:1.15;
      word-break:keep-all;cursor:pointer}}
 .edit-tool svg{{flex:0 0 auto;width:17px;height:17px;fill:none;stroke:currentColor;
      stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}}
 .edit-tool.mode[aria-pressed="true"]{{background:#142018;color:#fff}}
 .edit-tool:focus-visible{{outline:3px solid #8ee0bb;outline-offset:2px}}
 .edit-tool:disabled{{opacity:.42;cursor:not-allowed}}
 /* The only primary action on the screen. What it says is what it will do. */
 .edit-primary{{flex:0 0 auto;display:flex;align-items:center;justify-content:center;
      min-height:46px;padding:0 15px;border:0;border-radius:13px;background:#087b59;
      color:#fff;font-family:inherit;font-size:14px;font-weight:800;letter-spacing:-.03em;
      white-space:nowrap;cursor:pointer;box-shadow:0 6px 22px rgba(8,80,58,.3)}}
 .edit-primary[data-action="erase"]{{background:#c0392b;box-shadow:0 6px 22px rgba(120,30,20,.34)}}
 .edit-primary[data-action="verify"]{{background:#1668dc;box-shadow:0 6px 22px rgba(18,70,160,.3)}}
 .edit-primary:focus-visible{{outline:3px solid #8ee0bb;outline-offset:2px}}
 .edit-primary:disabled{{opacity:.5;cursor:not-allowed}}
 /* Corner stack, out of the route's way. */
 .edit-quick{{position:absolute;z-index:950;right:10px;top:10px;display:none;
      flex-direction:column;gap:6px}}
 body.editing .edit-quick{{display:flex}}
 .edit-quick-btn{{display:inline-flex;align-items:center;justify-content:center;
      width:40px;height:40px;padding:0;border:0;border-radius:50%;
      background:rgba(255,255,255,.96);color:#243028;cursor:pointer;
      box-shadow:0 3px 12px rgba(10,28,19,.2)}}
 .edit-quick-btn.reset{{color:#a23c31}}
 .edit-quick-btn svg{{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.9;
      stroke-linecap:round;stroke-linejoin:round;pointer-events:none}}
 .edit-quick-btn:focus-visible{{outline:3px solid #8ee0bb;outline-offset:2px}}
 .edit-quick-btn:disabled{{opacity:.4;cursor:not-allowed}}
 /* display:none when not selecting. pointer-events:none alone was enough in
    theory, but a full-bleed touch-action:none layer over the map is exactly
    the kind of thing that eats a drag on mobile -- keep it out of the tree
    unless the segment tool is actually selected. */
 .edit-overlay{{position:absolute;z-index:930;inset:0;width:100%;height:100%;touch-action:none;pointer-events:none;display:none}}
 body.editing.tool-active .edit-overlay{{display:block}}
 /* HTML touch surface for both pan and pinch, separate from the drawing SVG.
    Controls stay above it; it disappears completely while a tool is active. */
 .map-pan-surface{{display:none;position:absolute;z-index:930;inset:0;touch-action:none}}
 body.editing.mobile-edit-pan:not(.tool-active) .map-pan-surface{{display:block;pointer-events:auto}}
 /* Non-blocking edit feedback: one line pinned to the map's bottom edge so the
    route area stays clear (the large overlay panel was removed in f69e246). */
 .edit-toast{{position:absolute;z-index:960;left:10px;right:10px;bottom:calc(66px + env(safe-area-inset-bottom));
      display:flex;align-items:center;gap:9px;min-height:44px;padding:9px 12px;border-radius:12px;
      background:rgba(20,32,24,.95);color:#fff;font-size:14px;font-weight:700;line-height:1.35;
      box-shadow:0 6px 22px rgba(0,0,0,.3);backdrop-filter:blur(8px);word-break:keep-all;
      opacity:0;transform:translateY(8px);transition:opacity .18s ease-out,transform .18s ease-out}}
 .edit-toast{{cursor:pointer}}
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
 /* The selection's own controls, on the map beside it. */
 /* The two tools read as what they do, and the active one stays obvious
    while a finger is on the map. */
 #eraserTool[aria-pressed="true"]{{color:#9d3025}}
 #drawTool[aria-pressed="true"]{{color:#0c5fca}}
 /* The grip: where the finger currently has hold of the line. It leads the
    route, which catches up as the graph answers. */
 .via-dot{{display:block;width:14px;height:14px;border-radius:50%;border:3px solid #1668dc;
      background:#fff;box-sizing:border-box;box-shadow:0 2px 8px rgba(0,0,0,.25)}}
 /* Naming happens at the moment of saving, over the map that shows what is
    being named -- a sheet rather than a route to a separate screen. */
 .name-sheet{{position:fixed;z-index:1200;inset:0;display:flex;align-items:center;
      justify-content:center;padding:20px;background:rgba(12,20,15,.5)}}
 .name-sheet[hidden]{{display:none}}
 .name-sheet-card{{width:100%;max-width:360px;padding:20px;border-radius:18px;background:#fff;
      box-shadow:0 20px 60px rgba(8,20,13,.34)}}
 .name-sheet-card h2{{margin:0 0 6px;font-size:18px;letter-spacing:-.02em}}
 .name-sheet-card p{{margin:0 0 14px;color:#5c675e;font-size:13.5px;line-height:1.5;word-break:keep-all}}
 .name-sheet-input{{width:100%;min-height:50px;padding:0 14px;box-sizing:border-box;
      border:1.5px solid #dce3d8;border-radius:12px;background:#fff;color:#142018;
      font-family:inherit;font-size:16px;font-weight:700}}
 /* The name it would keep, shown in grey: saving without typing keeps exactly
    what is written here, so the placeholder is a preview, not a prompt. */
 .name-sheet-input::placeholder{{color:#98a49b;font-weight:600;opacity:1}}
 .name-sheet-input:focus-visible{{outline:none;border-color:#087b59;box-shadow:0 0 0 3px rgba(8,123,89,.18)}}
 .name-sheet-actions{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:14px}}
 .name-sheet-btn{{min-height:50px;border:0;border-radius:12px;background:#087b59;color:#fff;
      font-family:inherit;font-size:15px;font-weight:800;cursor:pointer}}
 .name-sheet-btn.ghost{{background:#f2f6f0;color:#2b3630;border:1px solid #dce3d8;font-weight:700}}
 .name-sheet-btn:disabled{{opacity:.5;cursor:not-allowed}}
 body.tool-active .edit-overlay{{touch-action:none}}
 /* The two open ends the pencil has to join. */
 .gap-end{{display:block;width:16px;height:16px;border-radius:50%;border:4px solid #c0392b;
      background:#fff;box-sizing:border-box;box-shadow:0 2px 8px rgba(0,0,0,.25)}}
 /* A 12px dot is a label; a 28px target is a handle you can actually grab. */
 .edit-anchor{{display:block;width:28px;height:28px;border-radius:50%;
      border:4px solid #e0522d;background:#fff;box-sizing:border-box;
      box-shadow:0 2px 10px rgba(0,0,0,.28);cursor:grab;touch-action:none}}
 .edit-anchor:focus-visible{{outline:3px solid #8ee0bb;outline-offset:2px}}
 body.handle-drag .edit-anchor{{cursor:grabbing}}
 body.handle-drag{{touch-action:none}}
 .edit-tools[aria-busy="true"] .edit-tool{{opacity:.45}}
 body.editing .view-toggle{{display:none!important}}
 body.editing.tool-active .facility-marker{{pointer-events:none;opacity:.2}}
 body.editing.tool-active .edit-overlay{{pointer-events:auto}}
 /* Distance, time and climb decide whether the run fits the day, so they
    share one row instead of being split across the page. */
 /* Runna, adidas and AllTrails all set run figures straight on the surface.
    A border around every number turns a page into a spreadsheet, and the
    borders were doing work that column alignment already does. */
 .headline-stats{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0;
      margin:16px 0 20px;padding:0}}
 .headline-stats>div{{min-width:0}}
 .headline-stats b{{display:block;font-size:30px;font-weight:800;letter-spacing:-.045em;
      color:#142018;line-height:1.05;white-space:nowrap;font-variant-numeric:tabular-nums}}
 .headline-stats .lead b{{color:#0a7d43}}
 .headline-stats i{{font-style:normal;font-size:14px;font-weight:700;color:#8a958d;margin-left:2px;
      letter-spacing:0}}
 .headline-stats>div>span{{display:block;margin-top:6px;font-size:12px;color:#8a958d;
      letter-spacing:.01em;word-break:keep-all}}
 .trait-chips{{display:flex;flex-wrap:wrap;gap:8px}}
 .trait{{display:inline-flex;align-items:center;gap:7px;min-height:34px;padding:4px 10px 4px 5px;
      border:1px solid #dfe6e0;border-radius:10px;background:#f7f9f7;color:#344238;
      font-size:13px;font-weight:700;word-break:keep-all}}
 .trait i{{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;
      border-radius:7px;background:#fff;font-style:normal;font-size:14px;line-height:1;
      box-shadow:0 1px 2px rgba(10,28,19,.1)}}
 /* komoot tints its route alerts and skips the outline: with a fill already
    separating the group, the border is a second boundary doing the same job. */
 .note-box{{border:0;border-radius:14px;padding:14px 16px;margin-top:14px}}
 .note-box b{{display:block;font-size:13.5px;font-weight:800;letter-spacing:-.01em;margin-bottom:7px}}
 .note-box ul{{margin:0;padding-left:16px;font-size:14px;line-height:1.7;color:#3d473f;
      word-break:keep-all;letter-spacing:-.005em}}
 .note-box li+li{{margin-top:3px}}
 .note-box.good{{background:#eef7f1}}
 .note-box.good b{{color:#17613e}}
 .note-box.care{{background:#fdf6ec}}
 .note-box.care b{{color:#8a5a12}}
 .facility-rows{{list-style:none;margin:6px 0 0;padding:0}}
 .facility-row{{display:flex;align-items:center;gap:10px;padding:11px 2px;border-top:1px solid #eef1ed}}
 .facility-row:first-child{{border-top:0}}
 .facility-km{{flex:0 0 54px;font-size:13px;font-weight:800;color:#0a7d43}}
 .facility-icon{{font-size:15px;line-height:1}}
 .facility-name{{flex:1;min-width:0;font-size:14px;color:#213028;overflow:hidden;
      text-overflow:ellipsis;white-space:nowrap}}
 .facility-kind{{flex-shrink:0;font-size:12px;color:#55605a}}
 .facility-empty{{margin:10px 0 0;font-size:13px;line-height:1.5;color:#55605a}}
 /* Starting the run is a choice between two tools, not a wall of steps. */
 .action-row{{display:flex;align-items:center;gap:12px;padding:15px 16px;
      min-height:44px;text-decoration:none;color:inherit;cursor:pointer}}
 .action-row{{margin-top:8px;border:0;border-radius:14px;background:#f5f8f4}}
 .action-icon{{display:inline-flex;align-items:center;justify-content:center;flex:0 0 38px;height:38px;
      border-radius:11px;background:#fff;font-size:17px;line-height:1}}
 .action-text{{flex:1;min-width:0}}
 .action-text b{{display:block;font-size:15px;letter-spacing:-.02em;color:#142018}}
 .action-text>span{{display:block;margin-top:2px;font-size:12.5px;line-height:1.45;color:#55605a;word-break:keep-all}}
 .action-guide{{display:block;padding:15px 16px 14px;cursor:default;background:#f5f8f4}}
 /* .action-row is worn by an <a>, a <div> and a <button>; only the button
    needs the UA font, width and alignment resets to sit flush with the rest. */
 button.action-row{{width:100%;font-family:inherit;font-size:inherit;text-align:left}}
 button.action-row:active{{background:#e3eee7}}
 .action-row:focus-visible{{outline:3px solid #8ee0bb;outline-offset:2px}}
 /* Sharing hands the course to a person rather than to another app, so it
    carries the brand tint the two app rows do not. */
 .action-row.share-row{{background:#eef7f1}}
 .action-head{{display:flex;align-items:center;gap:12px}}
 .action-guide .steps{{margin:10px 0 0;padding-left:19px;font-size:13.5px;line-height:1.7}}
 .tip-box{{margin-top:16px;padding-top:14px;border-top:1px solid #eef1ed;background:none}}
 .tip-box b{{display:block;font-size:12.5px;font-weight:800;color:#55605a;margin-bottom:5px}}
 .tip-box p{{margin:0;font-size:13px;line-height:1.65;color:#6b766f;word-break:keep-all}}
 #howto{{scroll-margin-top:12px}}
 /* Fast/slow is a property of the track, not of the chips under it. */
 .pace-scale{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;
      margin:-2px 0 8px;font-size:11px;color:#7c887f}}
 .pace-default{{color:#17613e;font-weight:700}}
 .facility-row.anchor{{color:#55605a}}
 .facility-row.anchor .facility-name{{font-weight:700;color:#344238}}
 .facility-row.anchor .facility-km{{color:#344238}}
 /* The bar carries the two numbers the decision rests on, so a runner who has
    scrolled past the summary never has to scroll back to act. */
 /* Kakao cannot rotate its map, so north stays put and the cone turns. */
 .user-heading{{position:absolute;left:50%;top:50%;width:0;height:0;
      margin:-30px 0 0 -13px;border-left:13px solid transparent;
      border-right:13px solid transparent;border-bottom:26px solid rgba(229,50,46,.55);
      transform-origin:50% 100%;pointer-events:none;filter:drop-shadow(0 1px 2px rgba(0,0,0,.25))}}
 .user-heading[hidden]{{display:none}}
 .run-float{{position:fixed;z-index:890;left:16px;right:16px;
      bottom:calc(84px + env(safe-area-inset-bottom));pointer-events:none}}
 .run-start{{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;
      min-height:56px;border:0;border-radius:16px;background:#0a7d43;color:#fff;
      font-family:inherit;font-size:16px;font-weight:800;letter-spacing:-.025em;
      cursor:pointer;pointer-events:auto;box-shadow:0 10px 28px rgba(10,60,35,.32)}}
 .run-start.running{{background:#c0392b}}
 .run-start:focus-visible{{outline:3px solid #8ee0bb;outline-offset:3px}}
 .run-start:disabled{{background:#9aa69e;cursor:not-allowed}}
 .run-start span{{font-size:13px;line-height:1}}
 .edit-steps{{margin:18px 0 0;padding-left:19px;font-size:13.5px;line-height:1.7;color:#3d473f}}
 .edit-steps code{{padding:2px 5px;border-radius:4px;background:#eef1ed;color:#263c2b;font-family:inherit;font-size:.95em;white-space:nowrap}}
 /* One persistent bar on every page, with the run given the extra weight
    Strava gives Record. */
 .tab-bar{{position:fixed;z-index:900;left:0;right:0;bottom:0;display:flex;
      padding:7px 10px calc(7px + env(safe-area-inset-bottom));gap:6px;
      background:rgba(255,255,255,.97);border-top:1px solid #e2e7df;
      box-shadow:0 -6px 22px rgba(20,45,30,.09);backdrop-filter:saturate(1.6) blur(8px)}}
 .tab{{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;
      justify-content:center;gap:3px;min-height:52px;border-radius:13px;
      color:#8a958d;text-decoration:none;font-size:11.5px;font-weight:700;
      letter-spacing:-.01em}}
 .tab-icon{{font-size:17px;line-height:1}}
 .tab.current{{color:#0a7d43;background:#eef7f1}}
 .tab.primary.current{{color:#fff;background:#0a7d43}}
 /* Pinned to the viewport, not to the map: the control it answers for sits at
    the bottom of a scrolled page, where the map -- and .edit-toast inside it --
    is long gone. Above the tab bar (z 900) and the run CTA (z 890). */
 .page-toast{{position:fixed;z-index:970;left:16px;right:16px;margin:0 auto;max-width:420px;
      bottom:calc(80px + env(safe-area-inset-bottom));
      display:flex;align-items:center;gap:10px;min-height:48px;padding:12px 15px;border-radius:14px;
      background:rgba(20,32,24,.96);color:#fff;font-size:14px;font-weight:700;line-height:1.45;
      letter-spacing:-.01em;box-shadow:0 10px 30px rgba(10,28,19,.34);backdrop-filter:blur(8px);
      word-break:keep-all;cursor:pointer;
      opacity:0;transform:translateY(10px);transition:opacity .2s ease-out,transform .2s ease-out}}
 .page-toast::before{{content:'✅';flex:0 0 auto;font-size:16px;line-height:1}}
 .page-toast[data-tone="error"]{{background:#a32b1e}}
 .page-toast[data-tone="error"]::before{{content:'⚠️'}}
 .page-toast[hidden]{{display:none}}
 .page-toast[data-open]{{opacity:1;transform:none}}
 .page-toast-text{{flex:1;min-width:0}}
 /* The run page parks a 56px start button at 84px; the toast clears it. */
 body.page-run .page-toast{{bottom:calc(150px + env(safe-area-inset-bottom))}}
 /* Nothing competes with the map once the runner is moving. */
 body.running .wrap,body.running footer{{display:none}}
 body.running .run-float{{bottom:calc(20px + env(safe-area-inset-bottom))}}
 body.running #map{{height:calc(100svh - 132px);min-height:0}}
 /* Whether the runner is on the course is the reason to run it here, so it
    is painted over the map rather than announced to screen readers alone. */
 .map-wrap{{position:relative}}
 .run-hud{{position:absolute;z-index:540;left:14px;right:14px;bottom:14px;display:flex;
      align-items:center;gap:9px;padding:11px 14px;border-radius:13px;
      background:rgba(20,32,24,.92);color:#fff;font-size:13.5px;font-weight:700;
      letter-spacing:-.01em;box-shadow:0 8px 26px rgba(0,0,0,.28)}}
 .run-hud[hidden]{{display:none}}
 .run-dot{{flex-shrink:0;width:9px;height:9px;border-radius:50%;background:#8ee0bb}}
 .run-hud[data-tone="warn"]{{background:rgba(140,52,40,.94)}}
 .run-hud[data-tone="warn"] .run-dot{{background:#ffc9a8}}
 .run-hud[data-tone="live"] .run-dot{{animation:runpulse 1.8s ease-in-out infinite}}
 @keyframes runpulse{{50%{{opacity:.25}}}}
 footer{{color:#55605a;font-size:13px;padding:8px 20px 96px;text-align:center;line-height:1.6}}
 body.page-run footer{{padding-bottom:172px}}
 footer a{{display:inline-block;padding:8px 4px;color:inherit}}
 @media (max-width:760px){{.brand{{height:48px;padding:0 16px}}
      .facts{{gap:6px}}.fact{{padding:11px 8px}}.fact b{{font-size:17px}}.fact span{{font-size:12px}}
      #map{{height:clamp(280px,42svh,380px);min-height:0}}
      .edit-tools{{left:8px;right:8px;bottom:calc(8px + env(safe-area-inset-bottom));gap:5px}}
      .edit-tool{{min-height:38px;padding:0 3px;font-size:11.5px;gap:0}}
      .edit-tool.mode svg{{display:none}}
      .edit-primary{{min-height:44px;padding:0 11px;font-size:13px}}
      .edit-quick{{right:8px;top:8px;gap:5px}}
      .edit-toast{{bottom:calc(60px + env(safe-area-inset-bottom))}}
      .view-toggle{{right:10px;top:10px}}
      .course-head{{gap:8px}}.badge{{width:28px;height:28px;font-size:15px}}
      .badge-tip{{max-width:calc(100vw - 52px)}}
      .wrap{{display:block;padding:0 16px 96px}}.card,.panel{{padding:18px;margin-bottom:12px;border-radius:16px}}h1{{font-size:22px;line-height:1.25;word-break:keep-all}}
      footer{{padding-bottom:96px}}.course-metrics{{gap:6px}}.edit-bar{{left:10px;right:10px;bottom:calc(8px + env(safe-area-inset-bottom));}}
      .metric-value{{font-size:19px;line-height:1.2;font-variant-numeric:tabular-nums;white-space:nowrap}}.metric-label{{font-size:12px;line-height:1.35}}
      footer{{padding-bottom:96px}}}}
 @media (orientation:landscape) and (max-width:900px){{#map{{height:280px}}.wrap{{padding-bottom:72px}}}}
 @media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important;animation-duration:.001ms!important;transition-duration:.001ms!important}}}}
</style></head><body class="page-{page}">
<header class="brand"><strong>러니웨어<span class="brand-tagline">: 어디서든 러닝 코스 짜기!</span></strong></header>
<div class="map-wrap"><div id="map"><div class="view-toggle" aria-label="지도 보기 전환">
 <button id="shapeView" type="button"{shape_view_active}>{shape_view_label}</button>
 <button id="guideView" type="button"{guide_view_active}>러닝 안내</button>
 </div>{edit_chrome}<div class="run-hud" id="runHud" hidden><span class="run-dot" aria-hidden="true"></span><span id="runStatus" role="status" aria-live="polite"></span></div></div>
<div class="wrap">
{page_sections}
</div>
{run_float}{tab_bar}{PAGE_TOAST_HTML}
<footer>러니웨어 · 배경 지도: Kakao Maps · 경로 데이터
<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors · ODbL</a> · NASA SRTM · 서울시 공공데이터<br>
GPS는 러니웨어 서버에 저장되지 않습니다 · <a href="/terms">이용·안전</a> · <a href="/privacy">개인정보</a> · <a href="/data-licenses">데이터 출처</a></footer>
<script>
 const segs = {segments};
 const shapeRoute = {shape_route};
 const kms = {km_markers};
 const dirs = {dir_markers};
 const initialEditPath = {edit_path};
 const initialEditGeometry = {edit_geometry};
 const editEnabled = {str(edit_enabled).lower()};
 const editNotice = {json.dumps(edit_notice, ensure_ascii=False)};
 const initialLengthKm = {course.length_km:.2f};
 let editEndpoint = '{base_url}/c/{cid}/edit';
 // Every course-addressed link on the page, kept in step with edits. Declared
 // out here beside editEndpoint, not inside kakao.maps.load() where it used to
 // live: the share handler below is bound in this scope and could not see it,
 // so every press threw ReferenceError and the button did nothing at all.
 let currentCourseUrl = '{base_url}/c/{cid}';
 const runStatus = document.getElementById('runStatus');
 const pageToast = document.getElementById('pageToast');
 const pageToastText = document.getElementById('pageToastText');
 let pageToastTimer = null;
 const hidePageToast = () => {{
   if (!pageToast) return;
   clearTimeout(pageToastTimer); pageToastTimer = null;
   delete pageToast.dataset.open;
   pageToast.hidden = true;
 }};
 const showPageToast = (text, tone) => {{
   if (!pageToast) return;
   clearTimeout(pageToastTimer);
   pageToast.dataset.tone = tone || 'ok';
   // Unhidden before the text changes so the live region announces the change
   // rather than swallowing it inside a display:none subtree.
   pageToast.hidden = false;
   pageToastText.textContent = text;
   void pageToast.offsetWidth;  // let the enter transition run from hidden
   pageToast.dataset.open = '';
   pageToastTimer = setTimeout(hidePageToast, 2600);
 }};
 if (pageToast) pageToast.addEventListener('click', hidePageToast);
 // The only copy path left when the async clipboard is missing or refused --
 // an insecure origin, or a permission the browser declines. window.prompt was
 // the old fallback: several mobile browsers suppress it outright, which is
 // how "nothing happens" survived even where the handler did run.
 const copyBySelection = text => {{
   const field = document.createElement('textarea');
   field.value = text;
   field.setAttribute('readonly', '');
   field.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
   document.body.appendChild(field);
   field.focus(); field.select();
   if (field.setSelectionRange) field.setSelectionRange(0, text.length);
   let copied = false;
   try {{ copied = document.execCommand('copy'); }} catch (err) {{ copied = false; }}
   field.remove();
   return copied;
 }};
 const shareBtn = document.getElementById('shareCourse');
 if (shareBtn) shareBtn.addEventListener('click', () => {{
   // Sharing an edited course used to send the original route's link, so the
   // url is read at press time from the value setCourseLinks() keeps current.
   const url = currentCourseUrl;
   const done = () => showPageToast('링크가 복사되었습니다', 'ok');
   // Each failure says what to do next instead of failing silently.
   const fallback = () => {{
     if (copyBySelection(url)) done();
     else showPageToast('링크를 복사하지 못했어요. 주소창의 주소를 복사해 주세요.', 'error');
   }};
   if (navigator.clipboard && navigator.clipboard.writeText)
     navigator.clipboard.writeText(url).then(done).catch(fallback);
   else fallback();
 }});
 // Declared like its siblings rather than leaned on as the implicit global
 // an id="" attribute creates: once the edit chrome stopped shipping on every
 // page that global vanished, and `if(editOverlay)` threw a ReferenceError
 // that aborted the rest of kakao.maps.load() -- taking every handler bound
 // after it -- the run start control and the map view toggle among them.
 const editOverlay = document.getElementById('editOverlay');
 const mapPanSurface = document.getElementById('mapPanSurface');
 const editCancel = document.getElementById('editCancel');
 const editSave = document.getElementById('editSave');
 const editSaveLabel = document.getElementById('editSaveLabel');
 const panTool = document.getElementById('panTool');
 const eraserTool = document.getElementById('eraserTool');
 const drawTool = document.getElementById('drawTool');
 const editUndo = document.getElementById('editUndo');
 const editRedo = document.getElementById('editRedo');
 const editTools = document.querySelector('.edit-tools');
 const editToast = document.getElementById('editToast');
 const editToastText = document.getElementById('editToastText');
 const editToastAction = document.getElementById('editToastAction');
 const editDistance = document.getElementById('editDistance');
 const PAGE = {page_json};
 const mapNode = document.getElementById('map');
 // Single visible + announced feedback channel. `role="status"` on the toast
 // keeps the screen-reader behaviour the sr-only bar used to provide, without
 // duplicating announcements across two live regions.
 let toastTimer = null;
 let toastAction = null;
 // A failure the runner cannot dismiss is a panel over their map; one that
 // vanishes before it is read is no message at all. Three seconds, or a tap.
 const AUTO_DISMISS_MS = {{info:3600, success:3600, busy:0, error:3000, blocked:3000}};
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
   const failing = tone === 'error' || tone === 'blocked';
   const ms = failing ? AUTO_DISMISS_MS[tone]
     : action ? (action.persist ? 0 : 6000)
     : AUTO_DISMISS_MS[tone || 'info'];
   if (ms) toastTimer = setTimeout(hideEditToast, ms);
 }};
 if (editToastAction) editToastAction.addEventListener('click', event => {{
   event.stopPropagation();
   const run = toastAction; hideEditToast(); if (run) run();
 }});
 // Anywhere on the toast dismisses it without running the action -- tapping a
 // message to make it go away is the one gesture nobody has to be taught.
 if (editToast) editToast.addEventListener('click', () => hideEditToast());
 const initLocalCourseEditor = () => {{
   const source = initialEditPath.map(([,lat,lon])=>[lat,lon]);
   const all = source;
   const latMin=Math.min(...all.map(point=>point[0])), latMax=Math.max(...all.map(point=>point[0]));
   const lonMin=Math.min(...all.map(point=>point[1])), lonMax=Math.max(...all.map(point=>point[1]));
   const latSpan=Math.max(latMax-latMin,.003), lonSpan=Math.max(lonMax-lonMin,.003);
   const toSvg=([lat,lon]) => [70+(lon-lonMin)/lonSpan*860, 650-(lat-latMin)/latSpan*580];
   const pathFor=points => points.map((point,index) => `${{index?'L':'M'}} ${{toSvg(point).map(value=>value.toFixed(1)).join(' ')}}`).join(' ');
   const arrowsFor=points=>{{let markup='',target=24,cum=0,prev=toSvg(points[0]);for(const point of points.slice(1)){{const next=toSvg(point),dx=next[0]-prev[0],dy=next[1]-prev[1],length=Math.hypot(dx,dy);while(length&&cum+length>=target){{const t=(target-cum)/length,x=prev[0]+dx*t,y=prev[1]+dy*t,angle=Math.atan2(dy,dx)*180/Math.PI;markup+=`<path d="M -6 -3.7 5.2 0 -6 3.7 -2.4 0Z" transform="translate(${{x.toFixed(1)}} ${{y.toFixed(1)}}) rotate(${{angle.toFixed(1)}})" fill="#fff" stroke="#064f38" stroke-width="1" stroke-linejoin="round"/>`;target+=36;}}cum+=length;prev=next;}}return markup;}};
   mapNode.innerHTML='<div class="local-course-editor"><div class="local-course-hint"><strong>지도를 불러오지 못했어요 · 로컬 코스 편집 체험</strong><span id="localCourseHint" role="status" aria-live="polite">구간 선택을 누른 뒤 바꿀 코스 선을 탭하세요.</span></div><svg id="localCourseCanvas" viewBox="0 0 1000 720" role="application" aria-label="로컬 코스 구간 선택 캔버스"></svg><div class="local-editor-actions"><button id="localEditRoute" class="local-primary" type="button">코스 편집</button><div class="local-edit-tools" role="toolbar" aria-label="로컬 코스 편집 도구"><button id="localSegment" class="edit-tool mode" type="button" aria-label="바꿀 코스 구간 선택" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14.5 5.5 4 4M4 20l3.8-.8L19 8a2.1 2.1 0 0 0-3-3L4.8 16.2 4 20Z"/><path d="M12.5 7.5l4 4"/></svg><span>구간 선택</span></button><button id="localEditUndo" class="edit-quick-btn" type="button" aria-label="마지막 수정 실행 취소" title="한 번 되돌리기"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 7-4 4 4 4"/><path d="M5 11h8a6 6 0 0 1 6 6v1"/></svg></button><button id="localEditCancel" class="edit-quick-btn reset" type="button" aria-label="모든 수정 초기화" title="전체 초기화"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12a8 8 0 1 0 2.3-5.7L4 8.6"/><path d="M4 4v4.6h4.6"/></svg></button><button id="localEditSave" class="edit-primary" type="button" aria-label="수정한 코스를 새 코스로 저장"><span>저장</span></button></div></div></div>';
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
   // The offline editor demo belongs to the editor page. Elsewhere a missing
   // map is a missing map, and pretending otherwise put a "코스 편집" button
   // on pages that no longer own editing.
   if (PAGE === 'edit') initLocalCourseEditor();
   else mapNode.innerHTML = '<div class="map-error"><strong>지도를 불러오지 못했어요</strong>'
     + '<span>네트워크를 확인한 뒤 새로고침해 주세요.</span></div>';
   // Everything that tracks a run lives inside kakao.maps.load below, so
   // without a map the start control has nothing behind it. A dead button
   // that gives no reason is worse than one that says why -- but only the
   // run page has anything to explain.
   const deadCta = document.getElementById('runCta');
   if (deadCta) {{
     deadCta.disabled = true;
     deadCta.textContent = '지도 연결 필요';   // icon goes with the action
   }}
   const deadHud = document.getElementById('runHud');
   if (deadHud && PAGE === 'run') {{
     deadHud.hidden = false;
     deadHud.dataset.tone = 'warn';
     document.getElementById('runStatus').textContent =
       '지도를 불러오지 못해 실시간 코스 안내를 시작할 수 없어요';
   }}
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
 // Named so stopping a run can restore the whole-course view it replaced.
 const fitRoute = () => {{
   if (routePath.length) map.setBounds(bounds, 42, 42, 42, 42);
 }};
 fitRoute();
 // Kakao can reset interaction flags while applying bounds. Keep the default
 // course view explicitly draggable; segment selection opts out through applyMode().
 const syncMapInteraction = () => {{
   const selecting = editing && (editMode === 'erase' || editMode === 'draw'
     || document.body.classList.contains('mobile-edit-pan'));
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
 // Per-edge OSM shape points, parallel to the edges of editNodes.
 let editGeom = initialEditGeometry.slice();
 let selectedRange = null, gapRange = null;
 // Freehand strokes stay local until the runner explicitly asks to preview a
 // walkable route. Lifting a finger merely starts another guide stroke.
 let draftStrokes = [], activeStroke = null;
 let routePreviewReady = false;
 const clearDraft = () => {{
   selectedRange=null;gapRange=null;draftStrokes=[];activeStroke=null;routePreviewReady=false;
 }};
 let undoStack = [], redoStack = [];
 let draftLines = [];
 let selectPointer = null;
 let editBusy = false;
 let editLengthKm = initialLengthKm;
 syncMapInteraction();
 // The toast used to appear on every sweep and every stroke, so a wide bar
 // sat across the map for the whole edit. Progress is already visible in the
 // route itself and the live distance; only what a runner cannot see -- a
 // failure, or work in flight -- earns the interruption.
 const TOAST_TONES = new Set(['error', 'busy']);
 const setEditStatus = (text, tone, action) => {{
   if (!TOAST_TONES.has(tone) && !action) {{ hideEditToast(); return; }}
   showEditToast(text, tone, action);
 }};
 const setEditDistance = km => {{
   if (typeof km === 'number' && isFinite(km)) editLengthKm = km;
   if (editDistance) {{
     editDistance.textContent = editLengthKm.toFixed(2) + 'km';
   }}
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
 const setTraits = (id, traits) => {{
   const host = document.getElementById(id);
   if (!host || !traits) return;
   host.replaceChildren();
   for (const trait of traits) {{
     const chip = document.createElement('span');
     chip.className = 'trait';
     const icon = document.createElement('i');
     icon.setAttribute('aria-hidden', 'true');
     icon.textContent = trait.emoji;
     chip.append(icon, trait.label);   // textContent: never innerHTML
     host.appendChild(chip);
   }}
 }};
 // An empty box is hidden rather than left as an empty heading: a "좋은 점"
 // header with nothing under it reads as a rendering failure.
 const setNotes = (boxId, listId, notes) => {{
   const box = document.getElementById(boxId);
   const list = document.getElementById(listId);
   if (!box || !list) return;
   list.replaceChildren();
   for (const note of notes || []) {{
     const item = document.createElement('li');
     item.textContent = note;
     list.appendChild(item);
   }}
   box.hidden = !(notes && notes.length);
 }};
 const setFacilityRows = (id, rows) => {{
   const host = document.getElementById(id);
   if (!host) return;
   host.replaceChildren();
   if (!rows || !rows.length) return;
   if (rows.every(row => row.anchor)) {{
     const empty = document.createElement('p');
     empty.className = 'facility-empty';
     empty.textContent = '코스 10m 안에 편의점·화장실이 없어요. 출발 전에 준비해 주세요.';
     host.appendChild(empty);
   }}
   const list = document.createElement('ul');
   list.className = 'facility-rows';
   for (const row of rows) {{
     const item = document.createElement('li');
     item.className = row.anchor ? 'facility-row anchor' : 'facility-row';
     for (const [cls, value, hidden] of [
       ['facility-km', row.at_km, false], ['facility-icon', row.emoji, true],
       ['facility-name', row.name, false], ['facility-kind', row.kind, false],
     ]) {{
       if (!value) continue;
       const cell = document.createElement('span');
       cell.className = cls;
       if (hidden) cell.setAttribute('aria-hidden', 'true');
       cell.textContent = value;   // facility names are external data
       item.appendChild(cell);
     }}
     list.appendChild(item);
   }}
   host.appendChild(list);
 }};
 // Pace model constants come from src/runart/pace.py so the browser and the
 // server cannot drift into two different answers for the same course.
 const PACE = {pace_model};
 let paceSeconds = {DEFAULT_PACE_S};
 let effortKm = {course.length_km:.4f};
 const paceRange = document.getElementById('paceRange');
 const paceValueEl = document.getElementById('paceValue');
 const paceTierEl = document.getElementById('paceTier');
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
   // The summary table repeats the headline numbers; a stale copy there is
   // the one a runner screenshots.
   if (paceValueEl) paceValueEl.textContent = fmtPace(paceSeconds);
   if (paceTierEl) paceTierEl.textContent = tier.name;
   if (paceRange) paceRange.setAttribute('aria-valuetext',
     fmtPace(paceSeconds) + ' 퍼 킬로미터, ' + tier.name);
 }};
 const setPace = seconds => {{
   paceSeconds = Math.min(PACE.slowest_s, Math.max(PACE.fastest_s,
     Math.round(seconds / PACE.step_s) * PACE.step_s));
   if (paceRange) paceRange.value = String(paceSeconds);
   renderEffort();
 }};
 if (paceRange) paceRange.addEventListener('input', ev => setPace(Number(ev.target.value)));
 renderEffort();
 // Every link that names a course id has to follow the edit; a stale one
 // hands over a different route than the page is describing.
 const setCourseLinks = courseId => {{
   const gpx = document.getElementById('gpxLink');
   // No id means the edited route is too complex to put in a link. The old
   // links still open a real course, so they stay: stripping them left the
   // page with dead buttons and nothing saying why.
   if (!courseId) return;
   currentCourseUrl = {base_url_json} + '/c/' + encodeURIComponent(courseId);
   if (gpx) gpx.href = currentCourseUrl + '.gpx';
   // Editing again must address the course now on screen, not the one the
   // page was rendered from.
   editEndpoint = currentCourseUrl + '/edit';
   // An edited course is fully described by its id, so the other two tabs can
   // carry the edit across without saving first. Leaving them on the original
   // id is how a runner edits a route and then reads about the old one.
   for (const tab of document.querySelectorAll('.tab[data-page]'))
     tab.href = currentCourseUrl + tab.dataset.page;
 }};
 let currentSummary = initialSummary;
 // The name the course would keep if the runner saves without typing. It
 // follows every edit, so a course that grew from 4.8km to 5.3km offers the
 // 5.3km name rather than the one it had when the page loaded.
 let currentNamePlaceholder = initialSummary.name_placeholder || '';
 const applySummary = summary => {{
   // The panel is cosmetic; the route is not. Half-applying a summary used to
   // throw partway through and take the caller's route update down with it,
   // leaving the busy toast pinned and the edit lost.
   if (!summary || typeof summary.length_km !== 'number') return;
   currentSummary = summary;
   if (summary.name_placeholder) currentNamePlaceholder = summary.name_placeholder;
   setText('courseTitle', summary.title);
   setCourseLinks(summary.course_id);
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
   setFacilityRows('facilityList', summary.facility_rows);
   setTraits('courseTraits', summary.traits);
   setNotes('courseGoodBox', 'courseGood', summary.highlights);
   setNotes('courseCareBox', 'courseCare', summary.cautions);
   const badges = document.getElementById('courseBadges');
   if (badges && summary.badges) {{
     badges.replaceChildren();
     summary.badges.forEach((badge, index) => badges.appendChild(buildBadge(badge, index)));
     bindBadgeTips();
   }}
 }};
 // Badges explain themselves. Hover and keyboard focus are handled in CSS;
 // this adds the tap behaviour a phone needs, where there is no hover at all
 // and `title=` shows nothing. One tip open at a time, dismissed by the next
 // tap anywhere or by Escape.
 let openBadge = null;
 const closeBadgeTip = () => {{
   if (!openBadge) return;
   openBadge.classList.remove('open');
   const button = openBadge.querySelector('.badge');
   if (button) button.setAttribute('aria-expanded', 'false');
   openBadge = null;
 }};
 const buildBadge = (badge, index) => {{
   const wrap = document.createElement('span');
   wrap.className = 'badge-wrap';
   const button = document.createElement('button');
   button.type = 'button';
   button.className = 'badge';
   button.setAttribute('aria-label', badge.label);
   button.setAttribute('aria-expanded', 'false');
   button.setAttribute('aria-describedby', 'badgeTip' + index);
   const glyph = document.createElement('span');
   glyph.setAttribute('aria-hidden', 'true');
   glyph.textContent = badge.emoji;
   button.appendChild(glyph);
   const tip = document.createElement('span');
   tip.className = 'badge-tip';
   tip.id = 'badgeTip' + index;
   tip.setAttribute('role', 'tooltip');
   const label = document.createElement('b');
   label.textContent = badge.label;
   tip.append(label, badge.detail || '');   // textContent: never innerHTML
   wrap.append(button, tip);
   return wrap;
 }};
 const bindBadgeTips = () => {{
   for (const button of document.querySelectorAll('.badge-wrap .badge')) {{
     if (button.dataset.tipBound) continue;
     button.dataset.tipBound = '1';
     button.addEventListener('click', event => {{
       event.stopPropagation();
       const wrap = button.parentElement;
       const wasOpen = wrap === openBadge;
       closeBadgeTip();
       if (wasOpen) return;
       wrap.classList.add('open');
       button.setAttribute('aria-expanded', 'true');
       openBadge = wrap;
     }});
   }}
 }};
 bindBadgeTips();
 document.addEventListener('click', closeBadgeTip);
 document.addEventListener('keydown', event => {{
   if (event.key === 'Escape') closeBadgeTip();
 }});
 // One in-flight edit request at a time: a second replacement must not race
 // the first and apply its node indexes to a route that has already changed.
 const setEditBusy = value => {{
   editBusy = value;
   if (editTools) editTools.setAttribute('aria-busy', String(value));
   for (const button of [panTool, eraserTool, drawTool, editUndo, editRedo, editCancel, editSave]) {{
     if (!button) continue;
     if (value) button.disabled = true;
     else if (button === editUndo) button.disabled = !undoStack.length;
     else if (button === editRedo) button.disabled = !redoStack.length;
     else button.disabled = false;
   }}
 }};
 // Draft state rides along with the original route so undo/redo can restore
 // an erased red span and every unfinished freehand stroke exactly as drawn.
 const snapshot = () => ({{nodes:editNodes.map(point=>[...point]),geom:editGeom.slice(),
   km:editLengthKm,summary:currentSummary,
   selected:selectedRange?[...selectedRange]:null,gap:gapRange?[...gapRange]:null,
   strokes:draftStrokes.map(stroke=>stroke.map(point=>({{...point}})) ),
   previewReady:routePreviewReady}});
 const restore = state => {{editNodes=state.nodes.map(point=>[...point]);editGeom=state.geom.slice();
   selectedRange=state.selected?[...state.selected]:null;gapRange=state.gap?[...state.gap]:null;
   draftStrokes=(state.strokes||[]).map(stroke=>stroke.map(point=>({{...point}})) );activeStroke=null;
   routePreviewReady=Boolean(state.previewReady);
   setEditDistance(state.km);applySummary(state.summary);renderDraft();}};
 const metresBetween = (a,b) => {{
   const dy=(b.lat-a.lat)*111320;
   const dx=(b.lon-a.lon)*88800;
   return Math.hypot(dx,dy);
 }};
 // Finger lifts are independent strokes. Never flatten them: doing so draws
 // a connector the runner never made and can turn a 1m gap into a junction.
 const draftPayloadStrokes = () => draftStrokes
   .filter(stroke=>stroke.length>1)
   .map(stroke=>stroke.map(point=>({{...point}})));
 const draftConnection = () => routePreviewReady ||
   (!draftPayloadStrokes().length && !gapRange);
 // One button, and what it says is what it does. Erasing is destructive so it
 // is red; confirming a sketch is a look-before-you-leap step so it is blue;
 // saving is the green one at the end.
 const syncPrimary = () => {{
   if(!editSave)return;
   const drawn=draftPayloadStrokes().length>0;
   const action=selectedRange?'erase':(gapRange||drawn)?'verify':'save';
   editSave.dataset.action=action;
   editSave.disabled=editBusy;
   if(editSaveLabel)editSaveLabel.textContent=
     action==='erase'?'선택 구간 지우기':action==='verify'?'도보 경로 확인':'저장';
   editSave.setAttribute('aria-label',
     action==='erase'?'선택한 구간을 코스에서 지우기'
     :action==='verify'?'그린 선을 도보 경로로 확인하기'
     :'수정한 코스를 새 코스로 저장');
   setEditDistance();
 }};
 // One tapped edge is rarely the stretch a runner means; dragging either end
 // along the route grows the selection the way Fi shapes a zone.
 const nearestNodeIndex = point => {{
   let best={{index:0,d:Infinity}};
   for(let index=0;index<editNodes.length;index++){{
     const screen=screenPoint(editNodes[index]);
     const d=Math.hypot(point.x-screen.x,point.y-screen.y);
     if(d<best.d)best={{index,d}};
   }}
   return best.index;
 }};
 const mapPoint = event => {{
   const rect=mapNode.getBoundingClientRect();
   return {{x:event.clientX-rect.left,y:event.clientY-rect.top}};
 }};
 let dragEnd=null;
 const HANDLE_GRAB_PX=26;
 // Which end of the swept range, if any, the pointer has hold of. The drawing
 // overlay covers the map while a tool is active, so the handles cannot
 // receive their own pointer events and are hit-tested here instead.
 const handleAt = point => {{
   if(!selectedRange)return null;
   const ends=[selectedRange[0],selectedRange[1]];
   let best=null,bestD=HANDLE_GRAB_PX;
   ends.forEach((nodeIndex,end)=>{{
     const screen=screenPoint(editNodes[nodeIndex]);
     const d=Math.hypot(point.x-screen.x,point.y-screen.y);
     if(d<=bestD){{bestD=d;best=end;}}
   }});
   return best;
 }};
 // Either end can move either way: dragging outward grows the range, inward
 // shrinks it back. An over-sweep used to be undoable only by starting over.
 const moveHandle = point => {{
   if(dragEnd===null||!selectedRange)return;
   const index=nearestNodeIndex(point);
   const other=selectedRange[dragEnd?0:1];
   const lo=Math.min(index,other),hi=Math.max(index,other);
   if(lo===hi)return;                       // never collapse to a single node
   selectedRange=[lo,hi];
   dragEnd=index<=other?0:1;
   renderDraft();
 }};
 const renderDraft = () => {{
   draftLines.forEach(line=>line.setMap(null));draftLines=[];
   if (editing) {{
     // An erased span is a real open draft: keep the untouched route green,
     // and preserve the removed geometry only as translucent red guidance.
     if(gapRange){{
       if(gapRange[0]>0)draftLines.push(new kakao.maps.Polyline({{map,path:drawnPath(0,gapRange[0]),strokeColor:'#087b59',strokeWeight:7,strokeOpacity:.96,strokeStyle:'solid'}}));
       if(gapRange[1]<editNodes.length-1)draftLines.push(new kakao.maps.Polyline({{map,path:drawnPath(gapRange[1],editNodes.length-1),strokeColor:'#087b59',strokeWeight:7,strokeOpacity:.96,strokeStyle:'solid'}}));
     }}else{{
       draftLines.push(new kakao.maps.Polyline({{map,path:drawnPath(0,editNodes.length-1),strokeColor:'#087b59',strokeWeight:7,strokeOpacity:.96,strokeStyle:'solid'}}));
     }}
     if(gapRange){{
       // Erased is gone. Keeping the removed line on screen, even faintly,
       // read as "still there" and left runners rubbing at it again. Only the
       // two ends stay, because those are what a replacement has to reach.
       const gap=editNodes.slice(gapRange[0],gapRange[1]+1);
       for(const endpoint of [gap[0],gap[gap.length-1]]){{
         draftLines.push(new kakao.maps.CustomOverlay({{map,position:new kakao.maps.LatLng(endpoint[1],endpoint[2]),content:'<span class="gap-end" aria-hidden="true"></span>',xAnchor:.5,yAnchor:.5,zIndex:7}}));
       }}
     }}
     // Freehand stays freehand while editing. Road snapping and route
     // generation happen only after a connected draft passes save validation.
     for(const stroke of draftStrokes){{
       if(stroke.length<2)continue;
       const path=stroke.map(point=>new kakao.maps.LatLng(point.lat,point.lon));
       draftLines.push(new kakao.maps.Polyline({{map,path,strokeColor:'#fff',strokeWeight:9,strokeOpacity:.86,strokeStyle:'solid'}}));
       // The blue stroke is input guidance, not a claim that the runner can
       // walk through every pixel it crosses.  경로 확인 replaces it with the
       // solid route snapped to the pedestrian graph.
       draftLines.push(new kakao.maps.Polyline({{map,path,strokeColor:'#1668dc',strokeWeight:5,strokeOpacity:.72,strokeStyle:'shortdash'}}));
     }}
     if(selectedRange){{
       const selected=editNodes.slice(selectedRange[0],selectedRange[1]+1);
       draftLines.push(new kakao.maps.Polyline({{map,path:drawnPath(selectedRange[0],selectedRange[1]),strokeColor:'#fff',strokeWeight:13,strokeOpacity:.96,strokeStyle:'solid'}}));
       draftLines.push(new kakao.maps.Polyline({{map,path:drawnPath(selectedRange[0],selectedRange[1]),strokeColor:'#e0522d',strokeWeight:8,strokeOpacity:1,strokeStyle:'solid'}}));
       const ends=[selected[0],selected[selected.length-1]];
       ends.forEach((endpoint,end)=>{{
         const marker=new kakao.maps.CustomOverlay({{map,position:new kakao.maps.LatLng(endpoint[1],endpoint[2]),content:'<span class="edit-anchor" data-end="'+end+'" role="button" tabindex="0" aria-label="'+(end?'선택 끝점':'선택 시작점')+' 드래그해 구간 넓히기"></span>',xAnchor:.5,yAnchor:.5,zIndex:8}});
         draftLines.push(marker);
       }});
     }}
   }}
   syncPrimary();
   dropScreenCache();
   if(editUndo)editUndo.disabled=editBusy||!undoStack.length;
   if(editRedo)editRedo.disabled=editBusy||!redoStack.length;
   if(editSave)editSave.disabled=editBusy;
 }};
 // 'pan' is the no-tool state and remains the default so one-finger map
 // movement works normally until the user picks up a tool.
 const syncToolPressed = () => {{
   if(panTool)panTool.setAttribute('aria-pressed',String(!editMode));
   if(eraserTool)eraserTool.setAttribute('aria-pressed',String(editMode==='erase'));
   if(drawTool)drawTool.setAttribute('aria-pressed',String(editMode==='draw'));
 }};
 const applyMode = () => {{
   resetMapGesture();
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
   setEditStatus(
     editMode==='erase'?'지울 코스 선을 문지르세요. 끝점을 끌어 넓힐 수도 있어요.'
     :editMode==='draw'?'붉은 구간의 한쪽 끝에서 다른 쪽 끝까지 자유롭게 이어 그리세요.'
     :'지도 이동 모드예요. 끌어서 코스를 살펴보세요.','info');
 }};
 // A selected line stays highlighted, but the tool releases so the map can be
 // moved again while the user decides whether to replace it.
 const releaseTool = () => {{
   editMode=null;
   applyMode();
 }};
 const setEditing = value => {{
   editing=value;document.body.classList.toggle('editing',value);
   const coarsePointer=(window.matchMedia&&window.matchMedia('(pointer: coarse)').matches)
     || navigator.maxTouchPoints>0;
   document.body.classList.toggle('mobile-edit-pan',value&&coarsePointer);
   setLayers(routeLayers,!value);setLayers(shapeLayers,false);setLayers(guideLayers,!value);
   showZoomControl(!value);
   if(!value){{editMode=null;clearDraft();document.body.classList.remove('tool-active');}}
   syncMapInteraction();
   renderDraft();setEditDistance();
   if(!value)return hideEditToast();
   setEditStatus(editNotice||'지우개로 구간을 지운 뒤 이어 그리세요. 붉은 선 양 끝이 아니어도 남은 초록 선에 닿으면 이어집니다.','info');
 }};
 const projection={{
   containerPointFromCoords:latlng=>map.getProjection().containerPointFromCoords(latlng),
   coordsFromContainerPoint:point=>map.getProjection().coordsFromContainerPoint(point),
 }};
 let dropScreenCache = () => {{}};
 // Kakao's panBy() animates. Calling it once per pointermove restarts that
 // animation every frame, so the map crawls a few pixels and springs back --
 // which is what "the map won't drag" looked like on the info page, where 80
 // facility markers meant almost every drag started on one of them. Moving
 // the centre through the projection is immediate and lands under the finger.
 const panByPixels = (dx, dy) => {{
   const centre = projection.containerPointFromCoords(map.getCenter());
   map.setCenter(projection.coordsFromContainerPoint(
     new kakao.maps.Point(centre.x + dx, centre.y + dy)));
   dropScreenCache();
 }};
 // A full HTML hit surface receives actual mobile touches (not just synthetic
 // PointerEvents dispatched directly onto an empty SVG). One owner handles
 // pan and pinch, so neither the browser nor the SDK also moves the same map.
 let mapGesture=null;
 function resetMapGesture(){{mapGesture=null;}}
 if(mapPanSurface){{
   const sampleTouches=event=>{{
     const rect=mapNode.getBoundingClientRect();
     const points=Array.from(event.targetTouches).slice(0,2)
       .map(t=>({{id:t.identifier,x:t.clientX-rect.left,y:t.clientY-rect.top}}));
     if(!points.length)return null;
     const center={{x:points.reduce((s,p)=>s+p.x,0)/points.length,
                   y:points.reduce((s,p)=>s+p.y,0)/points.length}};
     return {{center,count:points.length,ids:points.map(p=>p.id).sort().join(','),
       distance:points.length===2?Math.hypot(points[0].x-points[1].x,points[0].y-points[1].y):0}};
   }};
   const rebaseGesture=sample=>{{mapGesture=sample?{{...sample,
     baseDistance:sample.distance,baseLevel:map.getLevel()}}:null;}};
   const consume=event=>{{if(event.cancelable)event.preventDefault();event.stopPropagation();}};
   const beginTouch=event=>{{
     if(!editing||editMode||editBusy)return resetMapGesture();
     consume(event);rebaseGesture(sampleTouches(event));
   }};
   mapPanSurface.addEventListener('touchstart',beginTouch,{{passive:false}});
   mapPanSurface.addEventListener('touchmove',event=>{{
     if(!editing||editMode||editBusy)return resetMapGesture();
     consume(event);
     const next=sampleTouches(event);
     if(!next||!mapGesture||next.ids!==mapGesture.ids){{rebaseGesture(next);return;}}
     panByPixels(mapGesture.center.x-next.center.x,mapGesture.center.y-next.center.y);
     if(next.count===2&&mapGesture.baseDistance>0&&next.distance>0){{
       const level=Math.max(1,Math.min(14,mapGesture.baseLevel-
         Math.round(Math.log2(next.distance/mapGesture.baseDistance))));
       if(level!==map.getLevel()){{
         const anchor=projection.coordsFromContainerPoint(new kakao.maps.Point(next.center.x,next.center.y));
         map.setLevel(level,{{anchor,animate:false}});dropScreenCache();
       }}
     }}
     mapGesture.center=next.center;
   }},{{passive:false}});
   // Rebase immediately when a finger lifts: pinch -> pan must not wait for
   // the remaining finger to lift and touch down again.
   mapPanSurface.addEventListener('touchend',event=>{{consume(event);rebaseGesture(sampleTouches(event));}},{{passive:false}});
   mapPanSurface.addEventListener('touchcancel',resetMapGesture);
   window.addEventListener('blur',resetMapGesture);
 }}
 const screenPoint = node => projection.containerPointFromCoords(new kakao.maps.LatLng(node[1],node[2]));
 // The route as it is actually drawn: every graph node plus the OSM way shape
 // points between them. `seg` carries the node index each sub-point belongs
 // to, so a tap that lands on the drawn curve still resolves to an index the
 // server understands. Without this the editor drew straight chords while the
 // info and run pages drew the real street, and the same course had two lines.
 const drawnPoints = (lo, hi) => {{
   const out=[];
   for(let i=lo;i<hi;i++){{
     out.push({{lat:editNodes[i][1],lon:editNodes[i][2],seg:i}});
     for(const [lat,lon] of (editGeom[i]||[])) out.push({{lat,lon,seg:i}});
   }}
   out.push({{lat:editNodes[hi][1],lon:editNodes[hi][2],seg:Math.max(lo,hi-1)}});
   return out;
 }};
 const drawnPath = (lo, hi) =>
   drawnPoints(lo, hi).map(point=>new kakao.maps.LatLng(point.lat,point.lon));
 // Projecting several hundred points per pointermove is the one thing in the
 // editor that could miss a frame, so the projection is cached and thrown away
 // whenever the ground or the route moves under it.
 let screenCache=null;
 const screenLine = () => {{
   if(screenCache)return screenCache;
   screenCache=drawnPoints(0,editNodes.length-1).map(point=>{{
     const screen=projection.containerPointFromCoords(new kakao.maps.LatLng(point.lat,point.lon));
     return {{x:screen.x,y:screen.y,seg:point.seg}};
   }});
   return screenCache;
 }};
 dropScreenCache = () => {{ screenCache=null; }};
 kakao.maps.event.addListener(map,'idle',dropScreenCache);
 kakao.maps.event.addListener(map,'zoom_changed',dropScreenCache);
 const overlayPoint = event => {{const rect=editOverlay.getBoundingClientRect();return {{x:event.clientX-rect.left,y:event.clientY-rect.top}};}};
 const distanceToSegment = (point,a,b) => {{
   const dx=b.x-a.x,dy=b.y-a.y,len2=dx*dx+dy*dy||1;
   const t=Math.max(0,Math.min(1,((point.x-a.x)*dx+(point.y-a.y)*dy)/len2));
   return Math.hypot(point.x-(a.x+dx*t),point.y-(a.y+dy*t));
 }};
 // `lo`/`hi` bound the search to node indexes near what is already selected.
 // A closed loop passes close to itself, so a globally-nearest search could
 // answer with a segment on the far side of the course -- which is how one
 // sweep of the eraser suddenly swallowed most of the route.
 const nearestSegment = (point, lo, hi) => {{
   const line=screenLine();
   const from=(lo===undefined)?0:Math.max(0,lo);
   const to=(hi===undefined)?editNodes.length-1:Math.min(editNodes.length-1,hi);
   let best={{index:from,d:Infinity}};
   for(let i=0;i<line.length-1;i++){{
     if(line[i].seg<from||line[i].seg>=to)continue;
     const d=distanceToSegment(point,line[i],line[i+1]);
     if(d<best.d)best={{index:line[i].seg,d}};
   }}
   return best;
 }};
 const postEdit = async body => {{
   const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),3500);
   try{{const response=await fetch(editEndpoint,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body),signal:controller.signal}});const payload=await response.json();if(!response.ok)throw new Error(payload.error||'코스 선을 처리하지 못했어요.');return payload;}}
   finally{{clearTimeout(timer);}}
 }};
 // The eraser widens a range as it sweeps: a runner rubs out the stretch that
 // is wrong rather than tapping one edge of it and hoping that was the one.
 const HIT_SLOP = 28;
 // How far past the swept range the next sample may reach. A finger moves
 // along the line; it does not jump 30 junctions in one frame, so anything
 // further away is the loop passing near itself and is ignored.
 const ERASE_REACH = 24;
 const eraseAt = point => {{
   if(gapRange){{
     setEditStatus('먼저 붉은 구간을 선으로 잇거나 실행 취소해 주세요.','error',{{label:'닫기',run:()=>{{}}}});
     return false;
   }}
   const lo=selectedRange?selectedRange[0]-ERASE_REACH:undefined;
   const hi=selectedRange?selectedRange[1]+ERASE_REACH:undefined;
   const hit=nearestSegment(point,lo,hi);
   if(hit.d>HIT_SLOP)return false;
   selectedRange = selectedRange
     ? [Math.min(selectedRange[0],hit.index),Math.max(selectedRange[1],hit.index+1)]
     : [hit.index,hit.index+1];
   renderDraft();
   return true;
 }};
 const selectSegmentAt = point => {{
   if(eraseAt(point))
     setEditStatus('지울 구간을 골랐어요. 선택 구간 지우기를 누르세요.','info');
   else
     setEditStatus('코스 선 가까이를 문질러 주세요.','error',{{label:'닫기',run:()=>{{}}}});
 }};
 // Erasing an out-and-back needs no replacement: both its ends are the same
 // place, so the loop is already closed once it is gone. The server is the
 // only thing that can tell, so ask it, and fall back to an open gap when the
 // span really does need a line drawn across it.
 const eraseSelection = async () => {{
   if(editBusy||!selectedRange)return;
   undoStack.push(snapshot());if(undoStack.length>40)undoStack.shift();redoStack=[];
   gapRange=[...selectedRange];selectedRange=null;draftStrokes=[];activeStroke=null;routePreviewReady=false;
   releaseTool();renderDraft();
   setEditBusy(true);setEditStatus('지운 구간을 정리하고 있어요…','busy');
   try{{
     const payload=await postEdit({{action:'snap',path:editNodes.map(point=>point[0]),
       strokes:[],from_index:gapRange[0],to_index:gapRange[1]}});
     setEditBusy(false);
     if(payload.gap_open){{
       // setEditStatus only surfaces error and busy, which keeps the map
       // clear. This one has to be seen: the span stayed and the runner is
       // the only one who can say what replaces it.
       showEditToast(payload.note||'지운 구간을 이을 선을 그려 주세요.','info');
       return;
     }}
     editNodes=payload.path.map(point=>[...point]);
     editGeom=(payload.geometry||[]).slice();
     gapRange=null;routePreviewReady=true;
     setEditDistance(payload.length_km);applySummary(payload.summary);renderDraft();
     // The shorter line and the new distance are the confirmation; clearing
     // the busy toast is all that is left to do.
     setEditStatus('');
     if(payload.note)showEditToast(payload.note,'info');
   }}catch(error){{
     setEditBusy(false);
     // The erase itself stands; only the tidy-up failed. Leaving the gap open
     // is the honest state and the drawing still works from here.
     showEditToast('지운 구간을 이을 선을 그려 주세요.','info');
   }}
 }};
 // ---- 그리기: preserve the freehand draft until save -------------------
 const DRAW_STEP_PX=5;
 let dragActive=false,lastDrawPoint=null;
 const coordsAt = point => {{
   const latlng=projection.coordsFromContainerPoint(new kakao.maps.Point(point.x,point.y));
   return {{lat:latlng.getLat(),lon:latlng.getLng()}};
 }};
 const beginFreeDraw = point => {{
   // No gap required. Drawing adds to the course; only the eraser is allowed
   // to remove an existing edge occurrence.
   undoStack.push(snapshot());if(undoStack.length>40)undoStack.shift();redoStack=[];
   activeStroke=[coordsAt(point)];draftStrokes.push(activeStroke);
   dragActive=true;lastDrawPoint=point;renderDraft();
   return true;
 }};
 const appendFreeDraw = (point,force) => {{
   if(!dragActive||!activeStroke)return;
   if(!force&&lastDrawPoint&&Math.hypot(point.x-lastDrawPoint.x,point.y-lastDrawPoint.y)<DRAW_STEP_PX)return;
   activeStroke.push(coordsAt(point));lastDrawPoint=point;renderDraft();
 }};
 const endFreeDraw = point => {{
   if(!dragActive)return;
   appendFreeDraw(point,true);dragActive=false;activeStroke=null;lastDrawPoint=null;renderDraft();
 }};
 if(panTool)panTool.addEventListener('click',()=>setMode('pan'));
 if(eraserTool)eraserTool.addEventListener('click',()=>setMode('erase'));
 if(drawTool)drawTool.addEventListener('click',()=>setMode('draw'));
 if(editOverlay){{
   const editPointers=new Map();
   let twoFingerPan=false,panCenter=null;
   const centerOfPointers=()=>{{const points=[...editPointers.values()].map(value=>value.current);return {{x:points.reduce((sum,p)=>sum+p.x,0)/points.length,y:points.reduce((sum,p)=>sum+p.y,0)/points.length}};}};
   const endOverlayPointer=event=>{{
     if(!editPointers.has(event.pointerId))return;
     const info=editPointers.get(event.pointerId);
     info.current=overlayPoint(event);
     editPointers.delete(event.pointerId);
     if(twoFingerPan){{
       if(editPointers.size===0){{twoFingerPan=false;panCenter=null;selectPointer=null;}}
       else panCenter=centerOfPointers();
       return;
     }}
     if(event.pointerId===selectPointer){{
       selectPointer=null;
       if(!editMode)return;
       if(dragEnd!==null){{
         dragEnd=null;
         document.body.classList.remove('handle-drag');
         setEditStatus('구간을 조절했어요. 지우기를 누르세요.','info');
         return;
       }}
       // Releasing only ends this freehand stroke. The draft remains exactly
       // as drawn and can be continued with another stroke.
       if(editMode==='draw'){{endFreeDraw(info.current);return;}}
       const still=Math.hypot(info.current.x-info.start.x,info.current.y-info.start.y)<=10;
       // A tap with the eraser is a sweep of length zero: mark the one
       // segment under it.
       if(still)selectSegmentAt(info.current);
       else setEditStatus('지울 구간을 골랐어요. 지우기를 누르세요.','info');
     }}
   }};
   editOverlay.addEventListener('pointerdown',event=>{{
     if(!editing||editBusy)return;
     const point=overlayPoint(event);editPointers.set(event.pointerId,{{start:point,current:point}});
     editOverlay.setPointerCapture(event.pointerId);
     if(event.pointerType==='touch'&&editPointers.size>=2){{
       twoFingerPan=true;panCenter=centerOfPointers();selectPointer=null;return;
     }}
     if(twoFingerPan)return;
     selectPointer=event.pointerId;
     // On a phone the overlay is also the reliable map-drag surface while no
     // edit tool is selected.  Keeping this branch ahead of hit testing makes
     // 지도 이동 immune to route lines and editor markers under the finger.
     if(!editMode)return;
     if(editMode==='erase'&&handleAt(point)!==null){{
       // Grabbing an end of the swept range adjusts it -- in either
       // direction. Dragging inward is how you take back an over-sweep.
       dragEnd=handleAt(point);
       document.body.classList.add('handle-drag');
       return;
     }}
     if(editMode==='erase')eraseAt(point);
     else if(editMode==='draw')beginFreeDraw(point);
   }});
   editOverlay.addEventListener('pointermove',event=>{{
     if(!editPointers.has(event.pointerId))return;
     const point=overlayPoint(event),info=editPointers.get(event.pointerId),previous=info.current;info.current=point;
     if(twoFingerPan){{
       if(editPointers.size<2)return;
       const next=centerOfPointers();
       if(panCenter)panByPixels(panCenter.x-next.x,panCenter.y-next.y);
       panCenter=next;return;
     }}
     if(event.pointerId!==selectPointer)return;
     if(!editMode){{
       panByPixels(previous.x-point.x,previous.y-point.y);
       return;
     }}
     if(dragEnd!==null){{moveHandle(point);return;}}
     if(editMode==='erase')eraseAt(point);
     else if(editMode==='draw'&&dragActive)appendFreeDraw(point,false);
   }});
   editOverlay.addEventListener('pointerup',endOverlayPointer);
   editOverlay.addEventListener('pointercancel',event=>{{
     editPointers.delete(event.pointerId);if(event.pointerId===selectPointer)selectPointer=null;
     dragActive=false;activeStroke=null;lastDrawPoint=null;renderDraft();
   }});
 }}
 if(editUndo)editUndo.addEventListener('click',()=>{{
   if(editBusy||!undoStack.length)return;
   redoStack.push(snapshot());
   restore(undoStack.pop());
   setEditStatus('한 번 되돌렸어요.','info');
 }});
 if(editRedo)editRedo.addEventListener('click',()=>{{
   if(editBusy||!redoStack.length)return;
   undoStack.push(snapshot());
   restore(redoStack.pop());
   setEditStatus('다시 실행했어요.','info');
 }});
 // Reset returns to the route that was present on entering this editor, but
 // never exits the editing screen. The discarded draft remains one tap away.
 if(editCancel)editCancel.addEventListener('click',()=>{{
   if(editBusy)return;
   const hadEdits=Boolean(undoStack.length||selectedRange||gapRange||draftStrokes.length);
   const discarded={{...snapshot(),stack:undoStack.slice(),redo:redoStack.slice()}};
   editNodes=initialEditPath.map(point=>[...point]);
   editGeom=initialEditGeometry.slice();
   clearDraft();undoStack=[];redoStack=[];editMode=null;applyMode();
   setEditDistance(initialLengthKm);applySummary(initialSummary);renderDraft();
   if(!hadEdits)return;
   showEditToast('원본 코스로 되돌렸어요.','info',{{label:'실행 취소',run:()=>{{
     undoStack=discarded.stack;redoStack=discarded.redo;restore(discarded);
   }}}});
 }});
 // A finger stroke is only intent. Before naming or creating anything, snap
 // it to the pedestrian graph and paint that real walkable route on the map
 // so the runner can inspect exactly what will be saved.
 const previewDrawnRoute = async () => {{
   const strokes=draftPayloadStrokes();
   if(!strokes.length&&!gapRange)return false;
   // The erased range is authoritative. Drawing may replace that range or add
   // connected loops, but it cannot expand deletion into untouched green line.
   const body={{action:'snap',path:editNodes.map(point=>point[0]),strokes}};
   if(gapRange){{body.from_index=gapRange[0];body.to_index=gapRange[1];}}
   setEditBusy(true);setEditStatus('도보 가능한 길로 경로를 확인하고 있어요…','busy');
   try{{
     const payload=await postEdit(body);
     editNodes=payload.path.map(point=>[...point]);
     editGeom=(payload.geometry||[]).slice();
     selectedRange=null;gapRange=null;draftStrokes=[];activeStroke=null;
     routePreviewReady=true;editMode=null;applyMode();
     setEditDistance(payload.length_km);applySummary(payload.summary);renderDraft();
     setEditBusy(false);
     if(payload.note)setEditStatus(payload.note,'blocked',{{label:'닫기',run:()=>{{}}}});
     else setEditStatus('도보 경로를 지도에 표시했어요. 확인한 뒤 저장해 주세요.','success');
     return true;
   }}
   catch(error){{
     setEditStatus(error.name==='AbortError'?'경로 확인 시간이 초과됐어요. 그린 선은 그대로 남아 있어요.':(error.message||'코스 선이 이어지지 않았어요. 실제 코스 선과 교차하도록 이어 그려 주세요.'),'error',{{label:'닫기',run:()=>{{}}}});
     setEditBusy(false);return false;
   }}
 }};
 // Naming is deliberately after the walkable preview. The field is empty and
 // the current name sits behind it in grey, so typing remains optional.
 const nameSheet = document.getElementById('nameSheet');
 const nameSheetInput = document.getElementById('nameSheetInput');
 const nameSheetSave = document.getElementById('nameSheetSave');
 const nameSheetCancel = document.getElementById('nameSheetCancel');
 const closeNameSheet = () => {{
   if(!nameSheet)return;
   nameSheet.hidden=true;
   if(editSave)editSave.focus();
 }};
 const openNameSheet = () => {{
   if(!nameSheet||!nameSheetInput)return;
   nameSheetInput.value='';
   nameSheetInput.placeholder=currentNamePlaceholder;
   nameSheet.hidden=false;
   nameSheetInput.focus();
 }};
 const commitSave = async () => {{
   if(!editing||editBusy)return;
   const name=nameSheetInput?nameSheetInput.value.trim():'';
   const strokes=draftPayloadStrokes();
   if(!draftConnection()||strokes.length||gapRange){{
     closeNameSheet();
     setEditStatus('먼저 도보 경로를 확인해 주세요. 이어지지 않은 선은 저장할 수 없어요.','error',{{label:'닫기',run:()=>{{}}}});
     return;
   }}
   closeNameSheet();
   setEditBusy(true);setEditStatus('수정한 코스를 저장 중…','busy');
   const body={{action:'save',path:editNodes.map(point=>point[0]),name}};
   try{{
     const payload=await postEdit(body);
     setEditStatus('저장했어요. 새 코스로 이동합니다…','success');
     location.assign(payload.preview_url);
   }}
   catch(error){{
     setEditStatus(error.name==='AbortError'?'저장 시간이 초과됐어요. 수정한 선은 그대로 남아 있어요.':(error.message||'저장하지 못했어요. 다시 시도해 주세요.'),'error',{{label:'닫기',run:()=>{{}}}});
     setEditBusy(false);
   }}
 }};
 if(editSave)editSave.addEventListener('click',async()=>{{
   if(!editing||editBusy)return;
   const action=editSave.dataset.action;
   if(action==='erase'){{eraseSelection();return;}}
   if(action==='verify'){{await previewDrawnRoute();return;}}
   openNameSheet();
 }});
 if(nameSheetSave)nameSheetSave.addEventListener('click',commitSave);
 if(nameSheetCancel)nameSheetCancel.addEventListener('click',closeNameSheet);
 if(nameSheetInput)nameSheetInput.addEventListener('keydown',event=>{{
   if(event.key==='Enter'){{event.preventDefault();commitSave();}}
   if(event.key==='Escape'){{event.preventDefault();closeNameSheet();}}
 }});
 if(nameSheet)nameSheet.addEventListener('click',event=>{{
   if(event.target===nameSheet)closeNameSheet();
 }});
 // No entry button any more -- arriving on this page IS the intent.
 if (PAGE === 'edit' && editEnabled) setEditing(true);
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
   // The same glyph the facility list uses, so a marker on the map and a row
   // in the list are recognisably the same thing.
   el.textContent = m.type === 'restroom' ? '🚻' : '🏪';
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
       panByPixels(facilityPointer.lastX-ev.clientX,facilityPointer.lastY-ev.clientY);
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
 if ({opens_on_shape_js}) setMapMode('shape');
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
 const runHud = document.getElementById('runHud');
 // The off-course distance is the reason to run the course on this page at
 // all; it used to live in an sr-only node where nobody could see it.
 const setStatus = (text, tone) => {{
   runStatus.textContent = text;
   if (!runHud) return;
   runHud.hidden = !text;
   runHud.dataset.tone = tone || 'info';
 }};
 // Kakao's JS SDK exposes no bearing/rotation API, so the map cannot turn
 // under the runner. North therefore stays fixed on screen -- as asked -- and
 // the heading rides on the marker as a cone, the way Google Maps and Apple
 // Maps show it on a north-up map.
 let heading = null;
 const applyHeading = () => {{
   const cone = document.querySelector('.user-heading');
   if (!cone) return;
   cone.hidden = heading === null;
   if (heading !== null) cone.style.transform = `rotate(${{heading}}deg)`;
 }};
 const onOrientation = event => {{
   const deg = typeof event.webkitCompassHeading === 'number'
     ? event.webkitCompassHeading                       // iOS: already true north
     : (event.absolute && typeof event.alpha === 'number' ? 360 - event.alpha : null);
   if (deg === null || Number.isNaN(deg)) return;
   heading = Math.round(deg);
   applyHeading();
 }};
 const watchHeading = () => {{
   // iOS 13+ gates the sensor behind a user gesture; this runs inside the tap.
   const gate = window.DeviceOrientationEvent
     && DeviceOrientationEvent.requestPermission;
   const listen = () => window.addEventListener('deviceorientation', onOrientation, true);
   if (gate) DeviceOrientationEvent.requestPermission().then(state => {{
     if (state === 'granted') listen();
   }}).catch(() => {{}});
   else if (window.DeviceOrientationEvent) listen();
 }};
 const updatePosition = pos => {{
   const lat = pos.coords.latitude;
   const lon = pos.coords.longitude;
   const acc = Math.round(pos.coords.accuracy || 0);
   const off = Math.round(nearestRouteM(lat, lon));
   const posLatLng = new kakao.maps.LatLng(lat, lon);
   if (!userMarker) {{
     userMarker = addOverlay(posLatLng,
       '<div class="user-dot" title="현재 위치">'
       + '<span class="user-heading" hidden aria-hidden="true"></span></div>',
       guideLayers);
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
   applyHeading();
   const away = off > 80;
   setStatus(away ? `코스에서 약 ${{off}}m 벗어났어요`
                  : `코스 위를 달리는 중 · 오차 ${{acc}}m`, away ? 'warn' : 'live');
 }};
 const locationError = err => {{
   const msg = err.code === 1 ? '브라우저 설정에서 위치 권한을 허용해 주세요'
     : err.code === 2 ? 'GPS 신호가 약해 위치를 찾지 못했어요'
     : '위치 확인 시간이 초과됐어요';
   setStatus(msg, 'warn');
   setCta('다시 시도', false);
   watchId = null;
 }};
 const runCta = document.getElementById('runCta');
 // Street level: about one block across, so the next turn is legible.
 const RUN_FOLLOW_LEVEL = 3;
 const runIcon = () => {{
   const icon = document.createElement('span');
   icon.setAttribute('aria-hidden', 'true');
   icon.textContent = '▶';
   return icon;
 }};
 const setCta = (label, running) => {{
   if (!runCta) return;
   runCta.replaceChildren(runIcon(), document.createTextNode(label));
   runCta.classList.toggle('running', !!running);
 }};
 const stopRun = () => {{
   navigator.geolocation.clearWatch(watchId);
   watchId = null;
   setCta('달리기 시작', false);
   document.body.classList.remove('running');
   window.removeEventListener('deviceorientation', onOrientation, true);
   fitRoute();
   setStatus('GPS 안내 중지', 'info');
 }};
 // One entry point for both the bar and the map control: two copies of this
 // is how the button on the map ends up meaning something else.
 const startRun = () => {{
   if (!window.isSecureContext) {{
     setStatus('위치 기능은 HTTPS 연결에서만 쓸 수 있어요', 'warn');
     return;
   }}
   if (!navigator.geolocation) {{
     setStatus('이 브라우저는 위치 기능을 지원하지 않아요', 'warn');
     return;
   }}
   if (watchId !== null) {{
     stopRun();
     return;
   }}
   // Bring the map into view first: permission is being asked for something
   // the runner cannot see if the summary is still filling the screen.
   mapNode.scrollIntoView({{block:'start'}});
   setStatus('GPS 위치를 확인하는 중이에요', 'info');
   setMapMode('guide');
   // A whole-course view is for choosing a course; running one needs the next
   // corner. Strava, Runna and AllTrails all close in the moment you start.
   map.setLevel(RUN_FOLLOW_LEVEL);
   document.body.classList.add('running');
   watchHeading();
   setCta('달리기 중지', true);
   watchId = navigator.geolocation.watchPosition(updatePosition, locationError, {{
     enableHighAccuracy:true, maximumAge:3000, timeout:12000
   }});
 }};
 if (runCta) runCta.addEventListener('click', startRun);
 if (!window.isSecureContext || !navigator.geolocation) {{
   if (runCta) runCta.disabled = true;
   setCta('위치 추적 사용 불가', false);
   setStatus(!window.isSecureContext ? 'HTTPS 연결에서 위치 기능을 사용할 수 있어요' : '이 브라우저는 위치 기능을 지원하지 않아요', 'warn');
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
