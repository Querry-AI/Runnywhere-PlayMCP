"""RunArt MCP server — Agentic Player 10 (PlayMCP).

- Streamable HTTP, stateless (PRD §9): course ids are self-contained parameter
  tokens; the in-process cache is a performance layer only.
- 6 read-only, idempotent tools (PRD §5.1). Tool errors are returned as
  refined guidance text, never raw exceptions (PRD §5.2).
- Preview pages / GPX / shape share links are served by the same app (§5.6).
"""

import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from typing import Annotated

from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .course import Course, CourseError, generate_course
from .facilities import LABELS_KO, facilities_along
from .geocode import resolve_location
from .gpx import to_gpx
from .models import (CourseParams, DEFAULT_PACE_MIN_PER_KM, decode_course_id,
                     decode_shape_token, encode_course_id)
from .render import card_svg, course_markdown, preview_html
from .shapes import generate_shape_course, list_shapes
from .rfs import route_rfs_summary  # noqa: F401  (re-export for tests)

BASE_URL = os.environ.get(
    "RUNART_BASE_URL",
    "https://runnywhere.playmcp-endpoint.kakaocloud.io",
).rstrip("/")

mcp = FastMCP(
    "RunArt",
    instructions=(
        "RunArt(런아트) designs running courses in Seoul through conversation: "
        "loop courses by distance, animal-shaped GPS-art courses, hill/flat "
        "preference, night-safety routing, and nearby facilities."
    ),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
)

_RO = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

# Performance cache only — every entry is reproducible from its id
# (stateless). Failures are deterministic too, so they are cached as well:
# re-asking for an impossible shape answers instantly instead of re-searching.
_course_cache: dict[str, "Course | CourseError"] = {}
_CACHE_MAX = 512


def _get_course(params: CourseParams) -> Course:
    cid = encode_course_id(params)
    hit = _course_cache.get(cid)
    if isinstance(hit, Course):
        return hit
    if isinstance(hit, CourseError):
        raise hit
    try:
        course = generate_shape_course(params) if params.shape else generate_course(params)
    except CourseError as e:
        _cache_put(cid, e)
        raise
    _cache_put(cid, course)
    return course


def _cache_put(cid: str, value) -> None:
    if len(_course_cache) >= _CACHE_MAX:
        _course_cache.pop(next(iter(_course_cache)))
    _course_cache[cid] = value


def _build_params(location, lat, lon, distance_km, duration_min, include_hills,
                  night_mode, need_facilities, shape=None) -> tuple[CourseParams, str]:
    """Returns (params, note). note explains any interpretation we made
    (e.g. duration→distance conversion) so the user sees the reasoning."""
    note = ""
    rlat, rlon, name = resolve_location(location, lat, lon)
    if distance_km is None and duration_min:
        distance_km = round(duration_min / DEFAULT_PACE_MIN_PER_KM, 1)
        note = f"⏱️ {duration_min:g}분 → 6:30/km 페이스 기준 약 {distance_km:g}km로 잡았어요.\n"
    if distance_km is None:
        distance_km = 5.0
        note = "거리를 말씀하지 않으셔서 기본 5km로 잡았어요. 바꾸고 싶으면 말씀해 주세요.\n"
    params = CourseParams(
        lat=rlat, lon=rlon, location_name=name, distance_km=distance_km,
        include_hills=include_hills, night_mode=night_mode,
        need_facilities=need_facilities or [], shape=shape,
    )
    return params, note


def _run(params: CourseParams, note: str = "") -> str:
    try:
        course = _get_course(params)
        facs = facilities_along(course.points, params.need_facilities or None)
        return note + course_markdown(course, BASE_URL, facs)
    except CourseError as e:
        return f"⚠️ {e}"


@mcp.tool(
    annotations=ToolAnnotations(title="Generate running course", **_RO),
)
def generate_running_course(
    location: Annotated[str | None, Field(description="Start place name in Seoul, e.g. '시청', '광화문'")] = None,
    lat: Annotated[float | None, Field(ge=37.4, le=37.72, description="Start latitude (alternative to location; Seoul area)")] = None,
    lon: Annotated[float | None, Field(ge=126.76, le=127.19, description="Start longitude (alternative to location; Seoul area)")] = None,
    distance_km: Annotated[float | None, Field(ge=1, le=42.195, description="Target distance in km")] = None,
    duration_min: Annotated[float | None, Field(ge=10, le=360, description="Target duration in minutes; converted to distance at 6:30/km if distance_km is absent")] = None,
    include_hills: Annotated[bool, Field(description="True to include uphill training segments (3-8% grade); False prefers flat routes")] = False,
    night_mode: Annotated[bool, Field(description="Prefer well-lit streets with safety CCTV coverage for night runs")] = False,
    need_facilities: Annotated[list[str] | None, Field(description="Facility types the course should pass: convenience_store, restroom, water, park")] = None,
) -> str:
    """Generates a loop running course in Seoul from RunArt(런아트), snapped to
    real pedestrian roads and scored with the Running Friendliness Score built
    from Seoul open data (sidewalk width, slope, lighting, safety CCTV, parks).
    Safe, runner-friendly streets are preferred by default. Provide a start
    location (place name or lat/lon) and a target distance or duration.
    Returns course stats, a map preview link, and a GPX download link."""
    try:
        params, note = _build_params(location, lat, lon, distance_km, duration_min,
                                     include_hills, night_mode, need_facilities)
    except CourseError as e:
        return f"⚠️ {e}"
    return _run(params, note)


@mcp.tool(
    annotations=ToolAnnotations(title="Generate animal-shaped course", **_RO),
)
def generate_animal_course(
    shape: Annotated[str | None, Field(description="Animal shape key: cat, dog, giraffe, rabbit, whale. See list_available_shapes")] = None,
    location: Annotated[str | None, Field(description="Start place name in Seoul")] = None,
    lat: Annotated[float | None, Field(ge=37.4, le=37.72, description="Start latitude (alternative to location; Seoul area)")] = None,
    lon: Annotated[float | None, Field(ge=126.76, le=127.19, description="Start longitude (alternative to location; Seoul area)")] = None,
    distance_km: Annotated[float | None, Field(ge=1, le=42.195, description="Target distance in km")] = None,
    duration_min: Annotated[float | None, Field(ge=10, le=360, description="Target duration in minutes")] = None,
    include_hills: Annotated[bool, Field(description="Include uphill segments")] = False,
    night_mode: Annotated[bool, Field(description="Prefer well-lit, CCTV-covered streets")] = False,
    need_facilities: Annotated[list[str] | None, Field(description="Facility types to pass by")] = None,
    shape_token: Annotated[str | None, Field(description="Share token like 'whale-5k' from a friend's course link; recreates the same shape at this user's location")] = None,
) -> str:
    """Generates a GPS-art running course shaped like an animal (cat, dog,
    giraffe, rabbit, whale) snapped to real pedestrian roads in Seoul, from
    RunArt(런아트). If the shape cannot be drawn well on the local road
    network, alternative shapes are suggested instead of returning a bad
    course. Accepts a shape_token from a shared link to recreate a friend's
    shape in this user's neighborhood."""
    if shape_token and not shape:
        try:
            shape, distance_km = decode_shape_token(shape_token)
        except (ValueError, KeyError):
            return "⚠️ 공유 토큰 형식이 올바르지 않아요. 예: whale-5k"
    if not shape:
        opts = ", ".join(s["shape"] for s in list_shapes())
        return f"⚠️ 어떤 동물 모양으로 뛸까요? 가능한 모양: {opts}"
    try:
        params, note = _build_params(location, lat, lon, distance_km, duration_min,
                                     include_hills, night_mode, need_facilities, shape=shape)
    except CourseError as e:
        return f"⚠️ {e}"
    return _run(params, note)


@mcp.tool(
    annotations=ToolAnnotations(title="List available shapes", **_RO),
)
def list_available_shapes() -> str:
    """Lists animal shapes available for GPS-art running courses in
    RunArt(런아트), with the minimum recommended distance for each shape."""
    lines = ["RunArt에서 그릴 수 있는 모양:"]
    for s in list_shapes():
        lines.append(f"- {s['emoji']} {s['name_ko']} (`{s['shape']}`) — {s['min_km']:g}km 이상 권장")
    lines.append("예: \"시청에서 5km 고래 모양으로 짜줘\"")
    return "\n".join(lines)


@mcp.tool(
    annotations=ToolAnnotations(title="Find facilities near course", **_RO),
)
def find_facilities_near_course(
    course_id: Annotated[str, Field(description="Course id from a previously generated course")],
    facility_types: Annotated[list[str] | None, Field(description="Filter: convenience_store, restroom, water, park")] = None,
) -> str:
    """Lists convenience stores, restrooms, drinking fountains, and parks
    within 100m of a RunArt(런아트) course, with the km mark where the course
    passes each one."""
    try:
        params = decode_course_id(course_id)
        course = _get_course(params)
    except CourseError as e:
        return f"⚠️ {e}"
    except Exception:
        return "⚠️ course_id가 올바르지 않아요. 코스 응답에 있는 지도 링크의 id를 사용해 주세요."
    facs = facilities_along(course.points, facility_types, limit=15)
    if not facs:
        return "코스 100m 반경에서 해당 시설을 찾지 못했어요. 조건 없이 다시 조회해 보세요."
    lines = [f"🏃 {course.length_km:.1f}km 코스 주변 시설:"]
    lines += [f"- {f['at_km']:g}km 지점 · {LABELS_KO[f['type']]} ({f['dist_m']}m 옆)" for f in facs]
    return "\n".join(lines)


@mcp.tool(
    annotations=ToolAnnotations(title="Refine course", **_RO),
)
def refine_course(
    course_id: Annotated[str, Field(description="Course id to modify")],
    distance_km: Annotated[float | None, Field(ge=1, le=42.195, description="New target distance")] = None,
    include_hills: Annotated[bool | None, Field(description="Change hill preference")] = None,
    night_mode: Annotated[bool | None, Field(description="Change night-safety mode")] = None,
    shape: Annotated[str | None, Field(description="Change animal shape, or 'none' to remove the shape")] = None,
    location: Annotated[str | None, Field(description="New start place name")] = None,
    need_facilities: Annotated[list[str] | None, Field(description="New facility requirements")] = None,
) -> str:
    """Regenerates an existing RunArt(런아트) course with changed conditions
    (distance, hills, night mode, shape, start location, facilities) —
    conversational iteration on a course the user already received."""
    try:
        params = decode_course_id(course_id)
    except Exception:
        return "⚠️ course_id가 올바르지 않아요. 코스 응답에 있는 지도 링크의 id를 사용해 주세요."
    updates: dict = {}
    if distance_km is not None:
        updates["distance_km"] = distance_km
    if include_hills is not None:
        updates["include_hills"] = include_hills
    if night_mode is not None:
        updates["night_mode"] = night_mode
    if shape is not None:
        updates["shape"] = None if shape == "none" else shape
    if need_facilities is not None:
        updates["need_facilities"] = need_facilities
    if location is not None:
        try:
            rlat, rlon, name = resolve_location(location, None, None)
        except CourseError as e:
            return f"⚠️ {e}"
        updates.update(lat=rlat, lon=rlon, location_name=name)
    if not updates:
        return "바꿀 조건을 말씀해 주세요 (거리, 오르막, 야간 모드, 모양, 출발점, 편의시설)."
    return _run(params.model_copy(update=updates))


@mcp.tool(
    annotations=ToolAnnotations(title="Get course status", **_RO),
)
def get_course_status(
    course_id: Annotated[str, Field(description="Course id from a previously generated course")],
) -> str:
    """Retrieves an existing RunArt(런아트) course by id and re-issues its
    summary, map preview link, GPX download link, and shape share link."""
    try:
        params = decode_course_id(course_id)
    except Exception:
        return "⚠️ course_id가 올바르지 않아요. 코스 응답에 있는 지도 링크의 id를 사용해 주세요."
    return _run(params)


# ---------- Preview web (same server, PRD §5.6) ----------

@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> Response:
    return JSONResponse({"ok": True, "service": "runart"})


@mcp.custom_route("/c/{course_id}/card.svg", methods=["GET"])
async def share_card(request: Request) -> Response:
    """SVG share card — og:image for the preview page, SNS-ready (PRD §2.2)."""
    try:
        params = decode_course_id(request.path_params["course_id"])
        course = _get_course(params)
    except Exception:
        return PlainTextResponse("잘못된 코스 링크입니다.", status_code=404)
    return Response(card_svg(course), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/c/{course_id}", methods=["GET"])
async def preview(request: Request) -> Response:
    raw = request.path_params["course_id"]
    is_gpx = raw.endswith(".gpx")
    cid = raw[:-4] if is_gpx else raw
    try:
        params = decode_course_id(cid)
        course = _get_course(params)
    except Exception:
        return PlainTextResponse("잘못된 코스 링크입니다.", status_code=404)
    if is_gpx:
        name = (params.location_name or "RunArt") + f" {course.length_km:.1f}km"
        return Response(to_gpx(name, course.points), media_type="application/gpx+xml",
                        headers={"Content-Disposition": f'attachment; filename="runart-{cid[:12]}.gpx"'})
    facs = facilities_along(course.points, ["convenience_store", "restroom"], limit=80)
    return HTMLResponse(preview_html(course, facs, BASE_URL))


@mcp.custom_route("/s/{token}", methods=["GET"])
async def share_shape(request: Request) -> Response:
    """Shape share landing: the *shape* travels, not the course (PRD §2.2)."""
    token = request.path_params["token"]
    try:
        shape, dist = decode_shape_token(token)
    except (ValueError, KeyError):
        return PlainTextResponse("잘못된 공유 링크입니다.", status_code=404)
    prompt = f"내 위치에서 {dist:g}km {shape} 모양 러닝 코스 만들어줘 (shape_token: {token})"
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>RunArt 모양 공유</title>
<style>body{{font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;max-width:560px;
margin:48px auto;padding:0 20px;line-height:1.7}}</style></head><body>
<h2>🐾 친구가 {dist:g}km '{shape}' 모양 코스를 공유했어요</h2>
<p>AI 채팅에 아래 문장을 붙여넣으면, 같은 모양이 <b>내 동네 도로망</b>에 그려져요.</p>
<pre style="background:#f4f4f4;padding:14px;border-radius:10px;white-space:pre-wrap">{prompt}</pre>
</body></html>""")


# ---------- rate limiting (PRD §8) ----------

class _TokenBucketMiddleware:
    """Per-client token bucket: RATE_LIMIT_RPS steady, 2x burst. In-process by
    design — PlayMCP in KC fronts a single container for this contest."""

    def __init__(self, app, rps: float = 20.0):
        self.app = app
        self.rps = rps
        self.burst = rps * 2
        self.buckets: dict[str, tuple[float, float]] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        client = (scope.get("client") or ("?",))[0]
        import time as _t
        now = _t.monotonic()
        tokens, ts = self.buckets.get(client, (self.burst, now))
        tokens = min(self.burst, tokens + (now - ts) * self.rps)
        if tokens < 1.0:
            from starlette.responses import PlainTextResponse as _P
            return await _P("rate limit exceeded", status_code=429)(scope, receive, send)
        if len(self.buckets) > 10_000:  # bound memory
            self.buckets.clear()
        self.buckets[client] = (tokens - 1.0, now)
        return await self.app(scope, receive, send)


def _warm() -> None:
    """Load the graph, spatial index, and precomputed weights before traffic;
    prime the cache with a representative course (PRD §7.1 cache warming)."""
    try:
        from . import graph as graphmod
        graphmod.get_graph()
        graphmod._node_index()
        _get_course(CourseParams(lat=37.5665, lon=126.9780,
                                 location_name="시청", distance_km=5.0))
    except Exception:  # noqa: BLE001 — warming must never block startup
        pass


def create_app():
    """App factory (uvicorn workers). Rate limit wraps the whole app."""
    import threading

    threading.Thread(target=_warm, daemon=True).start()
    app = mcp.streamable_http_app()
    return _TokenBucketMiddleware(app, rps=float(os.environ.get("RATE_LIMIT_RPS", "20")))


def main() -> None:
    import uvicorn

    # CPU-bound course generation → multiple workers; stateless + JSON
    # responses make any worker interchangeable (PRD §9).
    workers = int(os.environ.get("WEB_CONCURRENCY", str(min(8, os.cpu_count() or 4))))
    uvicorn.run("runart.server:create_app", factory=True,
                host=mcp.settings.host, port=mcp.settings.port, workers=workers,
                log_level="info", access_log=False)  # no per-request URL logging (좌표 미기록)


if __name__ == "__main__":
    main()
