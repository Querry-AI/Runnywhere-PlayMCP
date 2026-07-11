"""Runnywhere MCP server — Agentic Player 10 (PlayMCP).

- Streamable HTTP, stateless (PRD §9): course ids are self-contained parameter
  tokens; the in-process cache is a performance layer only.
- 9 stateless, idempotent tools (PRD §5.1). Tool errors are returned as
  refined guidance text, never raw exceptions (PRD §5.2).
- Preview pages / GPX / shape share links are served by the same app (§5.6).
"""

import concurrent.futures
import functools
import html
import logging
import multiprocessing
import os
import re
import threading
import time
import urllib.parse

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from typing import Annotated

from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .animal_presets import (MISSING as PRESET_MISSING, PresetMatch,
                             find_nearest_animal_preset, get_animal_preset)
from .course import Course, CourseError, generate_course
from .facilities import LABELS_KO, facilities_along
from .geocode import resolve_location
from .gpx import to_gpx
from .geo import haversine_m
from .exploration import (atlas_html, create_relay, decode_passport,
                          decode_relay, passport_html, passport_summary,
                          home_html, legal_html, record_run, relay_html,
                          weekly_recommendation)
from .models import (CourseParams, DEFAULT_PACE_MIN_PER_KM, decode_course_id,
                     decode_shape_token, encode_course_id)
from .render import (card_svg, course_markdown, markdown_text, preview_html,
                     route_points)
from .shapes import (MAX_ANIMAL_ART_KM, SHAPES, find_min_clean_course,
                     generate_shape_course, list_shapes)
from .rfs import route_rfs_summary  # noqa: F401  (re-export for tests)

BASE_URL = os.environ.get(
    "RUNART_BASE_URL",
    "https://runnywhere.playmcp-endpoint.kakaocloud.io",
).rstrip("/")
_BASE_PARTS = urllib.parse.urlparse(BASE_URL)
if (_BASE_PARTS.scheme not in {"http", "https"} or not _BASE_PARTS.hostname
        or _BASE_PARTS.username or _BASE_PARTS.password
        or _BASE_PARTS.path not in {"", "/"}
        or _BASE_PARTS.params or _BASE_PARTS.query or _BASE_PARTS.fragment):
    raise RuntimeError("RUNART_BASE_URL must be an HTTP(S) origin without credentials or a path")
KAKAO_JAVASCRIPT_KEY = os.environ.get("KAKAO_JAVASCRIPT_KEY", "")
if KAKAO_JAVASCRIPT_KEY and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", KAKAO_JAVASCRIPT_KEY):
    raise RuntimeError("KAKAO_JAVASCRIPT_KEY has an invalid format")
LEGAL_CONTACT = os.environ.get("RUNART_LEGAL_CONTACT", "")
if LEGAL_CONTACT and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", LEGAL_CONTACT):
    raise RuntimeError("RUNART_LEGAL_CONTACT must be an email address")
log = logging.getLogger("runart")

mcp = FastMCP(
    "Runnywhere",
    instructions=(
        "Runnywhere(러니웨어) — 어디서든 러닝 코스 짜기! "
        "It designs running courses in Seoul through conversation: "
        "loop courses by distance, animal-shaped GPS-art courses, hill/flat "
        "preference, night-safety routing, and nearby facilities."
    ),
    stateless_http=True,
    json_response=True,
    host=os.environ.get("HOST", "127.0.0.1"),
    port=int(os.environ.get("PORT", "8000")),
)

_RO = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

# Performance cache only — every entry is reproducible from its id
# (stateless). Failures are deterministic too, so they are cached as well:
# re-asking for an impossible shape answers instantly instead of re-searching.
_course_cache: dict[str, "Course | CourseError"] = {}
_CACHE_MAX = 512
_animal_recommendation_cache: dict[tuple, Course] = {}
_CACHE_LOCK = threading.RLock()


# ---------- CPU offload (PlayMCP p99 <= 3s) ----------
#
# Course generation is CPU-bound (networkx Dijkstra). Running it inline blocks
# the event loop, so health/readiness probes fail and the gateway returns
# "no healthy upstream" 503s under any load. A small spawn-based process pool
# keeps the CPU work out of the web process: the event loop stays free, and a
# single 1-vCPU web worker + 2 pool workers uses ~3 graph copies (~1.7GB)
# instead of 8 web workers (~4.5GB) that previously risked OOM crashes.
_POOL: "concurrent.futures.ProcessPoolExecutor | None" = None
_POOL_LOCK = threading.Lock()
_POOL_BROKEN = False
ANIMAL_RESPONSE_BUDGET_S = 2.7


class _GenerationTimeout(RuntimeError):
    """The search did not finish before the MCP response deadline."""


_TIMED_OUT = object()


def _get_pool() -> "concurrent.futures.ProcessPoolExecutor | None":
    global _POOL, _POOL_BROKEN
    if _POOL_BROKEN:
        return None
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None and not _POOL_BROKEN:
                try:
                    workers = max(1, int(os.environ.get("RUNART_POOL_WORKERS", "2")))
                    ctx = multiprocessing.get_context("spawn")
                    _POOL = concurrent.futures.ProcessPoolExecutor(
                        max_workers=workers, mp_context=ctx)
                except Exception:  # noqa: BLE001 — never let pool setup crash a request
                    _POOL_BROKEN = True
                    _POOL = None
    return _POOL


def _offload(fn, *args, timeout_s: float | None = None):
    """Run a CPU-bound generator in the process pool, blocking the calling
    worker thread (not the event loop). Falls back to in-process execution if
    the pool is unavailable or broken, so a pool failure degrades latency
    rather than breaking the tool."""
    global _POOL, _POOL_BROKEN
    pool = _get_pool()
    if pool is not None:
        try:
            future = pool.submit(fn, *args)
            try:
                return future.result(timeout=timeout_s)
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                raise _GenerationTimeout from exc
        except _GenerationTimeout:
            raise
        except CourseError:
            raise  # a real generation error — propagate as-is
        except concurrent.futures.process.BrokenProcessPool:
            with _POOL_LOCK:
                _POOL_BROKEN = True
                _POOL = None
        except Exception as exc:  # noqa: BLE001 — degrade to bounded inline
            log.debug("process-pool offload failed; using bounded inline path: %s", exc)
    if timeout_s is not None and not _intrinsically_bounded(fn):
        # Never run an unbounded fallback inside an MCP request.
        raise _GenerationTimeout
    # The course generators enforce their own tighter anytime deadlines. If
    # the process pool is unavailable (container semaphore limit, transient
    # worker crash), running these bounded functions inline is preferable to
    # failing every animal request without attempting a single candidate.
    return fn(*args)


def _offload_map(fn, items: dict, timeout_s: float | None = None) -> dict:
    """Run fn over several inputs in parallel across the pool (used by the
    animal survey so four animals generate concurrently, not 4x sequentially)."""
    pool = _get_pool()
    if pool is not None:
        try:
            futures = {k: pool.submit(fn, v) for k, v in items.items()}
            done, pending = concurrent.futures.wait(
                futures.values(), timeout=timeout_s)
            for fut in pending:
                fut.cancel()
            out = {}
            for k, fut in futures.items():
                if fut not in done:
                    out[k] = _TIMED_OUT
                    continue
                try:
                    out[k] = fut.result()
                except CourseError:
                    out[k] = None
            return out
        except concurrent.futures.process.BrokenProcessPool:
            global _POOL, _POOL_BROKEN
            with _POOL_LOCK:
                _POOL_BROKEN = True
                _POOL = None
        except Exception as exc:  # noqa: BLE001
            log.debug("process-pool map failed; using bounded inline path: %s", exc)
    if timeout_s is not None:
        if not _intrinsically_bounded(fn):
            return {k: _TIMED_OUT for k in items}
        deadline = time.monotonic() + timeout_s
        out = {}
        for key, value in items.items():
            if time.monotonic() >= deadline:
                out[key] = _TIMED_OUT
                continue
            out[key] = _inline_or_none(fn, value)
        return out
    return {k: _inline_or_none(fn, v) for k, v in items.items()}


def _intrinsically_bounded(fn) -> bool:
    """Whether fn has an internal wall-clock deadline below the MCP cap."""
    while isinstance(fn, functools.partial):
        fn = fn.func
    return fn in {generate_course, generate_shape_course, find_min_clean_course}


def _inline_or_none(fn, arg):
    try:
        return fn(arg)
    except CourseError:
        return None


def offloaded(fn):
    """Run a sync MCP tool body in a worker thread so the event loop stays free
    for health/readiness probes while the tool blocks (it waits on the process
    pool, or on the Kakao geocoding call). FastMCP calls sync tools directly on
    the event loop; wrapping them as async + to_thread is what keeps a single
    web worker responsive under load. functools.wraps preserves the signature
    FastMCP introspects for the input schema."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(
            functools.partial(fn, *args, **kwargs))
    return wrapper


def _get_course(params: CourseParams, timeout_s: float | None = None) -> Course:
    cid = encode_course_id(params)
    with _CACHE_LOCK:
        hit = _course_cache.get(cid)
    if isinstance(hit, Course):
        return hit
    if isinstance(hit, CourseError):
        raise hit
    try:
        course = _offload(
            generate_shape_course if params.shape else generate_course, params,
            timeout_s=timeout_s)
    except CourseError as e:
        if params.shape in SHAPES and timeout_s is None:
            recovered = _offload(find_min_clean_course, params)
            if recovered is not None:
                _cache_put(cid, recovered)
                return recovered
        _cache_put(cid, e)
        raise
    _cache_put(cid, course)
    return course


def _cache_put(cid: str, value) -> None:
    with _CACHE_LOCK:
        if len(_course_cache) >= _CACHE_MAX:
            _course_cache.pop(next(iter(_course_cache)))
        _course_cache[cid] = value


def _animal_recommendation_key(params: CourseParams) -> tuple:
    return (
        round(params.lat, 4), round(params.lon, 4), params.shape,
        params.include_hills, params.night_mode,
        tuple(sorted(params.need_facilities)),
    )


def _cache_animal_recommendation(course: Course) -> None:
    with _CACHE_LOCK:
        if len(_animal_recommendation_cache) >= _CACHE_MAX:
            _animal_recommendation_cache.pop(next(iter(_animal_recommendation_cache)))
        _animal_recommendation_cache[_animal_recommendation_key(course.params)] = course


def _get_animal_recommendation(params: CourseParams) -> Course | None:
    with _CACHE_LOCK:
        return _animal_recommendation_cache.get(_animal_recommendation_key(params))


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


def _run(params: CourseParams, note: str = "",
         timeout_s: float | None = None) -> str:
    try:
        course = _get_course(params, timeout_s=timeout_s)
        facs = facilities_along(route_points(course), params.need_facilities or None)
        return note + course_markdown(course, BASE_URL, facs)
    except CourseError as e:
        return f"⚠️ {e}"


def _serve_course(course, note: str = "") -> str:
    """Render an already-generated course and keep it warm in the cache."""
    _cache_put(encode_course_id(course.params), course)
    facs = facilities_along(route_points(course), course.params.need_facilities or None)
    return note + course_markdown(course, BASE_URL, facs)


def _animal_help(name: str) -> str:
    """Fast, generation-free guidance shown when a chosen animal can't be drawn
    here. Re-running the full four-animal survey inline would double the tool
    latency past the p99 3s budget, so point the user at the survey instead."""
    return (
        f"동물 이름 없이 \"{name}에서 동물 코스 추천해줘\"라고 하시면, "
        "이 근처에서 또렷하게 완성되는 동물만 골라서 보여드릴게요. "
        "강남·잠실처럼 길이 바둑판인 동네나 큰 공원 근처에서 성공률이 높아요."
    )


def _animal_timeout_message(name: str, shape: str) -> str:
    spec = SHAPES[shape]
    name = markdown_text(name)
    return (
        f"⏱️ {name}에서 3초 안에 또렷한 {spec.name_ko} 코스를 찾지 못했어요. "
        "도로망 후보가 많아 이번 탐색은 여기서 멈췄습니다. "
        f'같은 요청을 한 번 더 시도하거나, "{name}에서 동물 코스 추천해줘"라고 '
        "말하면 더 빨리 완성되는 모양을 확인할 수 있어요."
    )


# Quality-first suggestion order requested for the survey (PRD §5.4 동물 4종).
SURVEY_SHAPES = ("dog", "cat", "whale", "rabbit")
REFERENCE_FEATURES = {
    "dog": "큰 머리·짧은 주둥이·넓은 몸통·짧은 다리·올라간 꼬리",
    "cat": "큰 머리·뾰족 귀 2개·긴 몸통·짧은 다리·길게 올라간 꼬리",
    "whale": "큰 타원 몸통·둥근 머리·좁은 꼬리목·갈라진 V 꼬리",
    "rabbit": "네모 몸통·위로 솟은 긴 네모 귀 2개",
}


def _nearby_start_text(requested_name: str, match: PresetMatch) -> str:
    """Clear, decision-ready explanation when a nearby preset is substituted."""
    requested_name = markdown_text(requested_name)
    actual = markdown_text(match.course.params.location_name or "가까운 출발점")
    metres = int(round(match.distance_m / 50.0) * 50)
    walk_min = max(1, round(match.distance_m / 80.0))
    return (f"요청한 {requested_name}에서는 모양이 흐려져, 가장 가까운 검증 코스로 바꿨어요.\n"
            f"📍 실제 출발·도착: **{actual}** · 약 {metres:,}m 이동 · 도보 약 {walk_min}분\n")


def _verified_animal_note(requested_name: str, shape: str,
                          course: Course,
                          nearby: PresetMatch | None = None) -> str:
    spec = SHAPES[shape]
    lead = ("✅ 바로 달릴 수 있는 검증 코스예요.\n"
            if nearby is None else "✅ 가까운 곳에서 바로 달릴 수 있는 검증 코스를 찾았어요.\n")
    moved = _nearby_start_text(requested_name, nearby) if nearby is not None else ""
    return (lead + moved
            + f"선택 이유: {spec.emoji} {spec.name_ko} 특유의 "
              f"**{REFERENCE_FEATURES.get(shape, '대표 특징')}** 같은 특징이 가장 또렷한 "
              f"**11km 이내 최상 코스**예요.\n")


def _animal_survey(lat: float, lon: float, name: str,
                   timeout_s: float | None = None) -> str:
    """Per-animal shortest clean-completion distance at this start point.

    The reference GPS-art routes look good because the shape picks its own
    size; forcing a requested distance is how courses stop reading as
    animals. So instead of drawing anything yet, tell the user at which
    minimal distance each animal completes cleanly and let them choose.
    """
    safe_name = markdown_text(name)
    lines = [
        "✅ **지금 선택할 수 있는 검증된 동물 코스예요.**",
        f"📍 {safe_name} 기준 · 11km 이내 · 필요하면 주변 2km 안의 더 또렷한 출발점까지 함께 찾았어요.",
    ]
    found_any = False
    timed_out_any = False
    # All four animals are generated in parallel across the process pool, so the
    # survey stays within the p99 3s budget instead of taking 4x sequentially.
    probes = {
        key: CourseParams(lat=lat, lon=lon, location_name=name,
                          distance_km=SHAPES[key].min_km, shape=key)
        for key in SURVEY_SHAPES
    }
    # Survey is a quick preview: four animals share two pool workers (two
    # rounds), so give each a tighter budget to keep the whole call under the
    # p99 3s cap. A user who then picks one animal gets the fuller budget.
    # Reuse recommendations already verified by a previous survey or by a
    # direct animal request. Only missing animals enter the process pool; this
    # makes repeated conversational turns sub-100ms without weakening gates.
    courses = {}
    nearby_matches = {}
    missing = {}
    for key, probe in probes.items():
        preset = get_animal_preset(probe)
        if preset is None or preset is PRESET_MISSING:
            nearby = find_nearest_animal_preset(probe)
            if nearby is not None:
                courses[key] = nearby.course
                if not nearby.is_exact:
                    nearby_matches[key] = nearby
                continue
        if preset is not PRESET_MISSING:
            courses[key] = preset
            continue
        cached = _get_animal_recommendation(probe)
        if cached is None:
            missing[key] = probe
        else:
            courses[key] = cached
    if missing:
        survey_fn = functools.partial(find_min_clean_course, total_budget_s=1.3)
        courses.update(_offload_map(survey_fn, missing, timeout_s=timeout_s))
    for key in SURVEY_SHAPES:
        spec = SHAPES[key]
        course = courses.get(key)
        if course is _TIMED_OUT:
            timed_out_any = True
            lines.append(
                f"- {spec.emoji} {spec.name_ko}: 3초 안에 후보 확인을 마치지 못했어요"
            )
            continue
        if course is None:
            lines.append(f"- {spec.emoji} {spec.name_ko}: 이 근처 도로망에서는 "
                         f"{MAX_ANIMAL_ART_KM:g}km 이내에 적절한 코스가 없어요")
            continue
        found_any = True
        cid = encode_course_id(course.params)
        _cache_put(cid, course)
        _cache_animal_recommendation(course)
        nearby = nearby_matches.get(key)
        start = ""
        if nearby is not None:
            metres = int(round(nearby.distance_m / 50.0) * 50)
            start = (f" · **{markdown_text(course.params.location_name)}에서 출발**"
                     f"(약 {metres:,}m 이동)")
        lines.append(
            f"- {spec.emoji} {spec.name_ko}: **추천 {course.length_km:.1f}km** · "
            f"{REFERENCE_FEATURES[key]}{start} · "
            f"미리보기 {BASE_URL}/c/{cid}"
        )
    if found_any:
        lines.append("어떤 동물로 뛸까요? 동물만 골라주시면 위 최상 코스로 확정해 드려요. "
                     "(더 길게 뛰고 싶으면 거리도 함께 말씀해 주세요)")
    elif timed_out_any:
        lines.append("도로망 후보가 많아 탐색을 멈췄어요. 원하는 동물 하나를 골라 다시 요청해 주세요.")
    else:
        lines.append("강남·잠실처럼 길이 바둑판인 동네나 큰 공원 근처 출발점에서 성공률이 높아요.")
    return "\n".join(lines)


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
    """Generates a loop running course in Seoul from Runnywhere(러니웨어), snapped to
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


def generate_animal_course(
    shape: Annotated[str | None, Field(description="Animal shape key: cat, dog, rabbit, whale")] = None,
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
    rabbit, whale) snapped to real pedestrian roads in Seoul, from
    Runnywhere(러니웨어). Shape quality decides the distance: call WITHOUT a shape
    to get, for each animal, the shortest distance at which it completes as
    a clean reference-like silhouette at this location, so the user can
    choose. Call with a shape and no distance to draw that animal at its own
    shortest clean distance. If a forced distance cannot be drawn well,
    alternatives are suggested instead of returning a bad course. Accepts a
    shape_token from a shared link to recreate a friend's shape here."""
    started = time.monotonic()

    def remaining() -> float:
        return max(0.01, ANIMAL_RESPONSE_BUDGET_S - (time.monotonic() - started))

    if shape_token and not shape:
        try:
            shape, distance_km = decode_shape_token(shape_token)
        except (ValueError, KeyError):
            return "⚠️ 공유 토큰 형식이 올바르지 않아요. 예: whale-5k"
    if shape and shape not in SHAPES:
        return (
            "⚠️ 현재는 강아지, 고양이, 고래, 토끼 코스만 가능하며 "
            "다른 동물 코스는 추후 업데이트 예정입니다. 죄송합니다."
        )
    if not shape:
        # No shape yet → survey: shortest clean-completion distance per
        # animal, so the user picks a shape knowing what it will cost.
        try:
            rlat, rlon, name = resolve_location(location, lat, lon)
        except CourseError as e:
            return f"⚠️ {e}"
        return _animal_survey(rlat, rlon, name, timeout_s=remaining())
    if distance_km is None and duration_min is None and shape in SHAPES:
        # Shape chosen, distance left open → quality-first: draw at the
        # shortest distance where the silhouette completes cleanly.
        try:
            rlat, rlon, name = resolve_location(location, lat, lon)
        except CourseError as e:
            return f"⚠️ {e}"
        probe = CourseParams(lat=rlat, lon=rlon, location_name=name,
                             distance_km=SHAPES[shape].min_km, shape=shape,
                             include_hills=include_hills, night_mode=night_mode,
                             need_facilities=need_facilities or [])
        preset = get_animal_preset(probe)
        nearby = None
        if preset is None or preset is PRESET_MISSING:
            nearby = find_nearest_animal_preset(probe)
            if nearby is not None:
                preset = nearby.course
        if preset is None:
            spec = SHAPES[shape]
            safe_name = markdown_text(name)
            return (f"⚠️ **이 위치에서는 {spec.name_ko} 코스를 추천할 수 없어요.**\n"
                    f"- 이유: {safe_name} 주변 2km 안에서 11km 이내 품질 기준을 통과한 "
                    f"{spec.name_ko} 코스가 없어요.\n"
                    f"- 다음 선택: 동물 이름 없이 **\"{safe_name}에서 동물 코스 추천해줘\"**라고 "
                    "말하면 가능한 모양을 바로 찾아드려요.")
        if preset is not PRESET_MISSING:
            _cache_animal_recommendation(preset)
            note = _verified_animal_note(
                name, shape, preset,
                nearby if nearby is not None and not nearby.is_exact else None)
            return _serve_course(preset, note)
        cached = _get_animal_recommendation(probe)
        if cached is not None:
            spec = SHAPES[shape]
            note = (f"{spec.emoji} {spec.name_ko} 모양이 가장 깔끔하게 완성되는 "
                    f"11km 이내 최상 코스 {cached.length_km:.1f}km로 그렸어요. "
                    f"{REFERENCE_FEATURES.get(shape, '')}\n")
            return _serve_course(cached, note)
        try:
            course = _offload(
                find_min_clean_course, probe, timeout_s=remaining())
        except _GenerationTimeout:
            return _animal_timeout_message(name, shape)
        if course is not None:
            _cache_animal_recommendation(course)
            spec = SHAPES[shape]
            note = (f"{spec.emoji} {spec.name_ko} 모양이 가장 깔끔하게 완성되는 "
                    f"11km 이내 최상 코스 {course.length_km:.1f}km로 그렸어요. "
                    f"{REFERENCE_FEATURES.get(shape, '')}\n")
            return _serve_course(course, note)
        # No clean fit at this location. Return fast guidance instead of
        # re-running a full survey (which would push latency past p99 3s).
        spec = SHAPES[shape]
        return (
            f"⚠️ {markdown_text(name)}에서는 {spec.name_ko} 실루엣이 11km 이내에 또렷하게 "
            f"완성되지 않았어요. " + _animal_help(name)
        )
    try:
        params, note = _build_params(location, lat, lon, distance_km, duration_min,
                                     include_hills, night_mode, need_facilities, shape=shape)
    except CourseError as e:
        return f"⚠️ {e}"
    if shape in SHAPES and distance_km is not None:
        # A forced short distance is the common blob-producing path. Discover
        # the location-specific clean minimum first and skip generation when
        # the request is below it; show all four verified choices instead.
        baseline = params.model_copy(update={"distance_km": SHAPES[shape].min_km})
        preset = get_animal_preset(baseline)
        nearby = None
        if preset is None or preset is PRESET_MISSING:
            nearby = find_nearest_animal_preset(baseline)
            if nearby is not None:
                preset = nearby.course
        preset_unavailable = preset is None
        minimum = (_get_animal_recommendation(baseline)
            if preset is PRESET_MISSING else preset)
        if minimum is None and not preset_unavailable:
            try:
                minimum = _offload(
                    find_min_clean_course, baseline, timeout_s=remaining())
            except _GenerationTimeout:
                return _animal_timeout_message(params.location_name, shape)
            if minimum is not None:
                _cache_animal_recommendation(minimum)
        if (minimum is not None
                and abs(minimum.length_km - distance_km) / distance_km <= 0.10):
            note = _verified_animal_note(
                params.location_name, shape, minimum,
                nearby if nearby is not None and not nearby.is_exact else None)
            note += (f"거리도 요청한 {distance_km:g}km의 ±10% 안에 들어와 "
                     "이 코스로 바로 확정했어요.\n")
            return _serve_course(minimum, note)
        if minimum is not None and distance_km < minimum.params.distance_km:
            spec = SHAPES[shape]
            nearby_text = (_nearby_start_text(params.location_name, nearby)
                           if nearby is not None and not nearby.is_exact else "")
            return (
                f"⚠️ **{distance_km:g}km보다 모양이 또렷한 대안을 추천해요.**\n"
                + nearby_text
                + f"- 요청 거리: {distance_km:g}km\n"
                + f"- 추천 거리: **{minimum.length_km:.1f}km**\n"
                + f"- 이유: 더 짧으면 {spec.name_ko}의 {REFERENCE_FEATURES[shape]} 특징들이 "
                  "도로에 겹쳐 알아보기 어려워져요.\n"
                + f"- 바로 사용하려면 **\"{minimum.length_km:.1f}km로 확정\"**이라고 말해 주세요."
            )
    try:
        out = _run(params, note, timeout_s=remaining())
    except _GenerationTimeout:
        return _animal_timeout_message(params.location_name, shape)
    if shape in SHAPES and out.startswith("⚠️"):
        out += " " + _animal_help(params.location_name)
    return out


def list_available_shapes() -> str:
    """Lists animal shapes available for GPS-art running courses in
    Runnywhere(러니웨어), with the minimum recommended distance for each shape."""
    lines = ["러니웨어에서 그릴 수 있는 모양:"]
    for s in list_shapes():
        lines.append(f"- {s['emoji']} {s['name_ko']} (`{s['shape']}`) — {s['min_km']:g}km 이상 권장")
    lines.append("출발 위치와 함께 동물 모양을 요청하면, 각 동물이 가장 깔끔하게 "
                 "완성되는 최단 거리를 먼저 보여드려요. 예: \"경복궁역에서 동물 모양 코스 추천해줘\"")
    return "\n".join(lines)


def find_facilities_near_course(
    course_id: Annotated[str, Field(description="Course id from a previously generated course")],
    facility_types: Annotated[list[str] | None, Field(description="Filter: convenience_store, restroom, water, park")] = None,
) -> str:
    """Lists convenience stores, restrooms, drinking fountains, and parks
    within 10m of a Runnywhere(러니웨어) course line, with the km mark where the
    course passes each one."""
    try:
        params = decode_course_id(course_id)
        course = _get_course(params)
    except CourseError as e:
        return f"⚠️ {e}"
    except Exception:
        return "⚠️ course_id가 올바르지 않아요. 코스 응답에 있는 지도 링크의 id를 사용해 주세요."
    facs = facilities_along(route_points(course), facility_types, limit=15)
    if not facs:
        return "코스 10m 반경에서 해당 시설을 찾지 못했어요. 조건 없이 다시 조회해 보세요."
    lines = [f"🏃 {course.length_km:.1f}km 코스 주변 시설:"]
    lines += [f"- {f['at_km']:g}km 지점 · {LABELS_KO[f['type']]} ({f['dist_m']}m 옆)" for f in facs]
    return "\n".join(lines)


def refine_course(
    course_id: Annotated[str, Field(description="Course id to modify")],
    distance_km: Annotated[float | None, Field(ge=1, le=42.195, description="New target distance")] = None,
    include_hills: Annotated[bool | None, Field(description="Change hill preference")] = None,
    night_mode: Annotated[bool | None, Field(description="Change night-safety mode")] = None,
    shape: Annotated[str | None, Field(description="Change animal shape, or 'none' to remove the shape")] = None,
    location: Annotated[str | None, Field(description="New start place name")] = None,
    need_facilities: Annotated[list[str] | None, Field(description="New facility requirements")] = None,
) -> str:
    """Regenerates an existing Runnywhere(러니웨어) course with changed conditions
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


def get_course_status(
    course_id: Annotated[str, Field(description="Course id from a previously generated course")],
) -> str:
    """Retrieves an existing Runnywhere(러니웨어) course by id and re-issues its
    summary, map preview link, GPX download link, and shape share link."""
    try:
        params = decode_course_id(course_id)
    except Exception:
        return "⚠️ course_id가 올바르지 않아요. 코스 응답에 있는 지도 링크의 id를 사용해 주세요."
    return _run(params)


def explore_animal_collection(
    location: Annotated[str | None, Field(description="Current place in Seoul for the nearest undiscovered animal recommendation")] = None,
    lat: Annotated[float | None, Field(ge=37.4, le=37.72, description="Current latitude, alternative to location")] = None,
    lon: Annotated[float | None, Field(ge=126.76, le=127.19, description="Current longitude, alternative to location")] = None,
    passport_token: Annotated[str | None, Field(description="Optional stateless passport token returned after recording completed animal courses")] = None,
) -> str:
    """Opens the Runnywhere(러니웨어) Seoul animal-art atlas and recommends
    this week's nearest verified course not yet recorded in the user's
    stateless animal passport. Returns concise links, not the full atlas data."""
    try:
        rlat, rlon, name = resolve_location(location, lat, lon)
        passport = decode_passport(passport_token)
    except CourseError as e:
        return f"⚠️ {e}"
    except Exception:
        return "⚠️ passport_token이 올바르지 않아요. 토큰 없이 새 도감을 시작할 수 있어요."
    course = weekly_recommendation(rlat, rlon, passport_token)
    summary = passport_summary(passport)
    lines = [
        "🗺️ **서울 동물지도가 열렸어요.**",
        f"- 탐험 지도: {BASE_URL}/animals",
        f"- 도감 현황: {summary['runs']}회 완주 · {len(summary['shapes'])}/4종 발견",
    ]
    if passport_token:
        lines.append(f"- 나의 동물도감: {BASE_URL}/passport/{passport_token}")
    if course is not None:
        cid = encode_course_id(course.params)
        distance = int(round(haversine_m(rlat, rlon, course.params.lat, course.params.lon)))
        spec = SHAPES[course.params.shape]
        lines.extend([
            "",
            "**이번 주 가장 가까운 미발견 동물**",
            f"- {spec.emoji} {spec.name_ko} · {markdown_text(course.params.location_name)} · {course.length_km:.1f}km",
            f"- 현재 위치에서 약 {distance:,}m · 코스 보기 {BASE_URL}/c/{cid}",
        ])
    lines.append("완주 후 course_id와 함께 **동물도감에 기록해줘**라고 말해 주세요.")
    return "\n".join(lines)


def record_animal_completion(
    course_id: Annotated[str, Field(description="Completed animal course id")],
    passport_token: Annotated[str | None, Field(description="Existing passport token; omit for the first completed animal")] = None,
) -> str:
    """Records a completed Runnywhere(러니웨어) animal course by returning a
    new self-contained passport token. No account, session, or server-side
    personal data is stored; repeating the same input is idempotent."""
    try:
        token, summary = record_run(course_id, passport_token)
    except RuntimeError:
        return "⚠️ 동물도감 보안 설정이 준비되지 않았어요. 운영자에게 문의해 주세요."
    except Exception:
        return "⚠️ 동물 코스 course_id 또는 passport_token이 올바르지 않아요."
    shapes = " ".join(SHAPES[key].emoji for key in summary["shapes"])
    lines = [
        "🎉 **완주를 동물도감에 기록했어요.**",
        f"- 발견: {len(summary['shapes'])}/4종 {shapes}",
        f"- 누적 완주: {summary['runs']}회",
    ]
    if summary["badges"]:
        lines.append("- 새 배지: " + " · ".join(
            f"🏅 {markdown_text(b)}" for b in summary["badges"]))
    lines.extend([
        f"- 나의 도감: {BASE_URL}/passport/{token}",
        "- 다음 대화의 passport_token에는 위 도감 링크를 그대로 사용하세요.",
        "- 개인정보 안내: 이 링크를 가진 사람은 완주 코스와 출발역을 볼 수 있어요.",
        "다음에는 **내 도감에서 가장 가까운 미발견 동물 찾아줘**라고 말해보세요.",
    ])
    return "\n".join(lines)


def extend_shape_relay(
    course_id: Annotated[str, Field(description="Animal course id to add as the next relay leg")],
    relay_token: Annotated[str | None, Field(description="Existing relay token; omit to start a new relay")] = None,
) -> str:
    """Starts or extends a Runnywhere(러니웨어) Shape Relay with another
    neighborhood's version of the same animal. The self-contained relay token
    supports up to eight legs and stores no user account or server session."""
    try:
        token, data = create_relay(course_id, relay_token)
    except RuntimeError:
        return "⚠️ Shape Relay 보안 설정이 준비되지 않았어요. 운영자에게 문의해 주세요."
    except ValueError as e:
        if "same shape" in str(e):
            return "⚠️ 릴레이에는 같은 동물 코스만 이어 붙일 수 있어요."
        return "⚠️ 동물 코스 course_id 또는 relay_token이 올바르지 않아요."
    spec = SHAPES[data["shape"]]
    return "\n".join([
        f"{spec.emoji} **{spec.name_ko} Shape Relay {len(data['legs'])}번째 주자를 연결했어요.**",
        f"- 공동 작품 보기: {BASE_URL}/relay/{token}",
        "- 다음 주자의 relay_token에는 위 공동 작품 링크를 그대로 사용하세요.",
        "친구는 자기 동네에서 같은 동물 코스를 만든 뒤 이 토큰과 course_id를 함께 전달하면 돼요.",
    ])


# Register each tool as an async offloaded wrapper (frees the event loop for
# health checks) while keeping the sync functions above directly callable by
# tests. offloaded() preserves the signature/docstring FastMCP needs.
for _fn, _title in (
    (generate_running_course, "Generate running course"),
    (generate_animal_course, "Generate animal-shaped course"),
    (list_available_shapes, "List available shapes"),
    (find_facilities_near_course, "Find facilities near course"),
    (refine_course, "Refine course"),
    (get_course_status, "Get course status"),
    (explore_animal_collection, "Explore Seoul animal-art collection"),
    (record_animal_completion, "Record animal-course completion"),
    (extend_shape_relay, "Extend shape relay"),
):
    mcp.add_tool(
        offloaded(_fn), name=_fn.__name__,
        annotations=ToolAnnotations(title=_title, **_RO),
    )


# ---------- Preview web (same server, PRD §5.6) ----------

@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> Response:
    return JSONResponse({"ok": True, "service": "runnywhere"})


@mcp.custom_route("/", methods=["GET"])
async def home(_: Request) -> Response:
    return HTMLResponse(home_html(BASE_URL),
                        headers={"Cache-Control": "public, max-age=3600"})


@mcp.custom_route("/terms", methods=["GET"])
async def terms(_: Request) -> Response:
    return HTMLResponse(legal_html("terms", LEGAL_CONTACT),
                        headers={"Cache-Control": "public, max-age=3600"})


@mcp.custom_route("/privacy", methods=["GET"])
async def privacy(_: Request) -> Response:
    return HTMLResponse(legal_html("privacy", LEGAL_CONTACT),
                        headers={"Cache-Control": "public, max-age=3600"})


@mcp.custom_route("/data-licenses", methods=["GET"])
async def data_licenses(_: Request) -> Response:
    return HTMLResponse(legal_html("licenses", LEGAL_CONTACT),
                        headers={"Cache-Control": "public, max-age=3600"})


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
        name = (params.location_name or "Runnywhere") + f" {course.length_km:.1f}km"
        return Response(to_gpx(name, route_points(course)), media_type="application/gpx+xml",
                        headers={"Content-Disposition": f'attachment; filename="runnywhere-{cid[:12]}.gpx"'})
    facs = facilities_along(route_points(course), ["convenience_store", "restroom"], limit=80)
    return HTMLResponse(preview_html(
        course, facs, BASE_URL, kakao_javascript_key=KAKAO_JAVASCRIPT_KEY))


@mcp.custom_route("/s/{token}", methods=["GET"])
async def share_shape(request: Request) -> Response:
    """Shape share landing: the *shape* travels, not the course (PRD §2.2)."""
    token = request.path_params["token"]
    try:
        shape, dist = decode_shape_token(token)
    except (ValueError, KeyError):
        return PlainTextResponse("잘못된 공유 링크입니다.", status_code=404)
    if shape not in SHAPES:
        return PlainTextResponse("지원하지 않는 동물 모양입니다.", status_code=404)
    safe_shape = html.escape(shape)
    prompt = html.escape(
        f"내 위치에서 {dist:g}km {shape} 모양 러닝 코스 만들어줘 (shape_token: {token})")
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>러니웨어 모양 공유</title>
<style>body{{font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;max-width:560px;
margin:48px auto;padding:0 20px;line-height:1.7}}</style></head><body>
<h2>🐾 친구가 {dist:g}km '{safe_shape}' 모양 코스를 공유했어요</h2>
<p><b>러니웨어</b> · 어디서든 러닝 코스 짜기!</p>
<p>AI 채팅에 아래 문장을 붙여넣으면, 같은 모양이 <b>내 동네 도로망</b>에 그려져요.</p>
<pre style="background:#f4f4f4;padding:14px;border-radius:10px;white-space:pre-wrap">{prompt}</pre>
<p style="margin-top:28px;font-size:12px;color:#68706b"><a href="/terms">이용·안전</a> · <a href="/privacy">개인정보</a> · <a href="/data-licenses">© OpenStreetMap contributors · 데이터 출처</a></p>
</body></html>""")


@mcp.custom_route("/animals", methods=["GET"])
async def animal_atlas(_: Request) -> Response:
    return HTMLResponse(atlas_html(BASE_URL, KAKAO_JAVASCRIPT_KEY),
                        headers={"Cache-Control": "public, max-age=3600"})


@mcp.custom_route("/passport/{token}", methods=["GET"])
async def animal_passport_page(request: Request) -> Response:
    token = request.path_params["token"]
    try:
        page = passport_html(token, BASE_URL)
    except Exception:
        return PlainTextResponse("잘못된 동물도감 링크입니다.", status_code=404)
    return HTMLResponse(page, headers={"Cache-Control": "private, no-store"})


@mcp.custom_route("/relay/{token}", methods=["GET"])
async def shape_relay_page(request: Request) -> Response:
    token = request.path_params["token"]
    try:
        data = decode_relay(token)
        courses = []
        for cid in data["legs"]:
            params = decode_course_id(cid)
            preset = get_animal_preset(params)
            courses.append(preset if isinstance(preset, Course) else _get_course(params))
        page = relay_html(token, courses, BASE_URL)
    except Exception:
        return PlainTextResponse("잘못된 Shape Relay 링크입니다.", status_code=404)
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=3600"})


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

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"x-permitted-cross-domain-policies", b"none"),
                    # Kakao Maps validates the registered JavaScript SDK
                    # origin; send only the origin on cross-site SDK requests.
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"geolocation=(self), camera=(), microphone=()"),
                    (b"content-security-policy",
                     b"default-src 'self'; base-uri 'self'; object-src 'none'; "
                     b"frame-ancestors 'none'; script-src 'self' 'unsafe-inline' "
                     b"https://dapi.kakao.com https://t1.daumcdn.net; "
                     b"style-src 'self' 'unsafe-inline'; img-src 'self' data: "
                     b"https://*.kakaocdn.net https://*.daumcdn.net; "
                     b"connect-src 'self' https://*.kakao.com https://*.daum.net"),
                ])
                message["headers"] = headers
            await send(message)
        client = (scope.get("client") or ("?",))[0]
        import time as _t
        now = _t.monotonic()
        tokens, ts = self.buckets.get(client, (self.burst, now))
        tokens = min(self.burst, tokens + (now - ts) * self.rps)
        if tokens < 1.0:
            from starlette.responses import PlainTextResponse as _P
            return await _P("rate limit exceeded", status_code=429)(scope, receive, send_with_security_headers)
        if len(self.buckets) > 10_000:  # bound memory
            self.buckets.clear()
        self.buckets[client] = (tokens - 1.0, now)
        return await self.app(scope, receive, send_with_security_headers)


def _warm() -> None:
    """Load the graph, spatial index, and precomputed weights before traffic;
    pre-warm the process-pool workers (each must load its own graph copy) and
    prime the cache with a representative course (PRD §7.1 cache warming)."""
    try:
        from . import graph as graphmod
        graphmod.get_graph()
        graphmod._node_index()
        # Pre-load the graph inside each pool worker so the first real animal
        # request is not stuck behind a cold ~2.5s graph load per process.
        pool = _get_pool()
        if pool is not None:
            try:
                workers = getattr(pool, "_max_workers", 2)
                warms = [pool.submit(graphmod.warmup) for _ in range(workers * 2)]
                for w in warms:
                    w.result()
            except Exception as exc:  # noqa: BLE001 — pool warm is best-effort
                log.debug("process-pool warmup skipped: %s", exc)
        _get_course(CourseParams(lat=37.5665, lon=126.9780,
                                 location_name="시청", distance_km=5.0))
    except Exception as exc:  # noqa: BLE001 — warming must never block startup
        log.warning("startup cache warmup failed; requests will warm lazily: %s", exc)


def _log_geocoding_status() -> None:
    """One startup line so operators can see immediately whether address /
    station lookups will work. The most common '위치를 찾지 못했어요' cause is a
    missing KAKAO_REST_API_KEY in the deploy env — surface it loudly, but
    never print the key itself (PRD §8)."""
    import logging

    log = logging.getLogger("runart")
    if os.environ.get("KAKAO_REST_API_KEY"):
        log.info("geocoding: Kakao Local API enabled (address/keyword search on)")
    else:
        log.warning(
            "geocoding: KAKAO_REST_API_KEY not set — only the offline gazetteer "
            "works; arbitrary addresses like '관철동 7-14' will fail. Set the env "
            "var in the deploy to enable Kakao address/station lookups."
        )
    token_secret = os.environ.get("RUNART_TOKEN_SECRET", "")
    if len(token_secret) < 32:
        log.error(
            "security: RUNART_TOKEN_SECRET is missing or shorter than 32 chars; "
            "passport and relay token issuance is disabled"
        )
    if not KAKAO_JAVASCRIPT_KEY:
        log.warning("maps: KAKAO_JAVASCRIPT_KEY not set; preview maps show fallback guidance")


def create_app():
    """App factory (uvicorn workers). Rate limit wraps the whole app."""
    _log_geocoding_status()
    threading.Thread(target=_warm, daemon=True).start()
    app = mcp.streamable_http_app()
    return _TokenBucketMiddleware(app, rps=float(os.environ.get("RATE_LIMIT_RPS", "20")))


def main() -> None:
    import uvicorn

    # Memory-bounded by default: the ~560MB graph is loaded once per process.
    # CPU-bound generation is offloaded to a spawn process pool (RUNART_POOL_
    # WORKERS), so a single web worker keeps the event loop free for health
    # checks while total RAM stays ~1.7GB (1 web + 2 pool) instead of the old
    # 8 web workers (~4.5GB) that risked OOM crashes and 503 storms.
    workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    uvicorn.run("runart.server:create_app", factory=True,
                host=mcp.settings.host, port=mcp.settings.port, workers=workers,
                log_level="info", access_log=False)  # no per-request URL logging (좌표 미기록)


if __name__ == "__main__":
    main()
