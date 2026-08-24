"""Runnywhere MCP server — Agentic Player 10 (PlayMCP).

- Streamable HTTP, stateless (PRD §9): course ids are self-contained parameter
  tokens; the in-process cache is a performance layer only.
- 7 stateless, idempotent tools (PRD §5.1). Tool errors are returned as
  refined guidance text, never raw exceptions (PRD §5.2).
- Preview pages / GPX / shape share links are served by the same app (§5.6).
"""

import concurrent.futures
import functools
import html
import asyncio
import logging
import multiprocessing
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import graph as graphmod
from .animal_presets import (MISSING as PRESET_MISSING, PresetMatch,
                             find_nearby_animal_presets,
                             find_nearest_animal_preset, get_animal_preset,
                             preset_status)
from .course import (Course, CourseError, course_from_path, generate_course,
                     reroute_segment, snap_drawn_segment)
from .courseplan import (CASE_EXACT, CASE_FAR, CASE_NEARBY, NEARBY_RADIUS_M,
                         SAME_START_M, CoursePlan, build_course_plan)
from .facilities import LABELS_KO, facilities_along
from .geocode import resolve_location
from .geo import haversine_m
from .gpx import to_gpx
from .exploration import (atlas_html, create_relay, decode_relay,
                          passport_html, home_html, legal_html, record_run,
                          relay_html)
from .models import (CourseParams, CourseWaypoint, DEFAULT_PACE_MIN_PER_KM, decode_course_id,
                     decode_shape_token, encode_course_id)
from .render import (card_svg, course_edit_summary, course_markdown,
                     course_thumbnail_svg, markdown_text, preview_html,
                     route_points)
from .shapes import (MAX_ANIMAL_ART_KM, SHAPES, find_min_clean_course,
                     generate_shape_course, list_shapes)
from .rfs import route_rfs_summary  # noqa: F401  (re-export for tests)
from .widget import WidgetTooLargeError, build_course_widget

DEFAULT_BASE_URL = (
    "https://runnywhere-kakaotools.playmcp-endpoint.kakaocloud.io"
)
BASE_URL = os.environ.get("RUNART_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
_BASE_PARTS = urllib.parse.urlparse(BASE_URL)
if (_BASE_PARTS.scheme not in {"http", "https"} or not _BASE_PARTS.hostname
        or _BASE_PARTS.username or _BASE_PARTS.password
        or _BASE_PARTS.path not in {"", "/"}
        or _BASE_PARTS.params or _BASE_PARTS.query or _BASE_PARTS.fragment):
    raise RuntimeError("RUNART_BASE_URL must be an HTTP(S) origin without credentials or a path")
KAKAO_JAVASCRIPT_KEY = os.environ.get("KAKAO_JAVASCRIPT_KEY", "")
ROUTE_EDIT_ENABLED = os.environ.get("RUNART_ROUTE_EDIT", "1") == "1"
KAKAO_WIDGETS_ENABLED = os.environ.get("RUNART_KAKAO_WIDGETS", "1") == "1"
if KAKAO_JAVASCRIPT_KEY and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", KAKAO_JAVASCRIPT_KEY):
    raise RuntimeError("KAKAO_JAVASCRIPT_KEY has an invalid format")
LEGAL_CONTACT = os.environ.get("RUNART_LEGAL_CONTACT", "")
if LEGAL_CONTACT and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", LEGAL_CONTACT):
    raise RuntimeError("RUNART_LEGAL_CONTACT must be an email address")
log = logging.getLogger("runart")
FONT_PATH = Path(__file__).resolve().parent / "assets" / "PretendardVariable.woff2"
RELEASE_SHA = next((
    os.environ[name] for name in (
        "RUNART_RELEASE_SHA", "GIT_COMMIT", "SOURCE_VERSION", "REVISION_ID")
    if os.environ.get(name)
), "unknown")
_WARM_READY = threading.Event()

mcp = FastMCP(
    "Runnywhere",
    instructions=(
        "Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!) creates runnable "
        "courses on real pedestrian roads in Seoul. Route each turn by these "
        "rules, in order. (1) New course: call create_seoul_running_course "
        "for 러닝 코스/달리기 코스/그려줘/짜줘/만들어줘/추천해줘/GPS 아트 "
        "only when the conversation contains an explicit Seoul start place "
        "or both coordinates. Copy that start exactly; never invent, infer, "
        "or substitute a location. If it is missing, ask the user for the "
        "start and do not call a course tool. A location-only reply is valid "
        "only immediately after the user established course-creation intent "
        "or was asked for the missing start. Use standard for ordinary runs, "
        "best_animal when no animal is named, and dog/cat/rabbit/whale for "
        "강아지·댕댕이/고양이·야옹이/토끼/고래. (2) Existing-course "
        "changes use refine_course. (3) Questions about 화장실, 편의점, 물, "
        "공원, or facilities near the current course use "
        "find_facilities_near_course with its most recent course_id. (4) "
        "Requests to show the same summary, map, or GPX use get_course_status. "
        "Never regenerate a course for rules 2-4. Never claim that a Seoul "
        "course is unsupported before the appropriate new-course call. When "
        "a result includes structuredContent.assistant_text, say it exactly "
        "once as normal conversational text outside the widget; do not copy "
        "that guidance into the card."
    ),
    stateless_http=True,
    json_response=True,
    host=os.environ.get("HOST", "127.0.0.1"),
    port=int(os.environ.get("PORT", "8000")),
)

_RO = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

# Performance cache only — every entry is reproducible from its id
# (stateless). Failures are deterministic too, so they are cached as well:
# re-asking for an impossible shape answers instantly instead of re-searching.
_course_cache: dict[str, "Course | CourseError"] = {}
_CACHE_MAX = 512
_animal_recommendation_cache: dict[tuple, Course] = {}
_CACHE_LOCK = threading.RLock()
_COURSE_LINK_RE = re.compile(
    rf"{re.escape(BASE_URL)}/c/([A-Za-z0-9_-]{{1,4096}})(?:\.gpx)?"
    r"(?=$|[\s)\]?#])"
)


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
MCP_OUTER_RESPONSE_BUDGET_S = 2.85
ANIMAL_RESPONSE_BUDGET_S = 2.65
# Plain-course generation must also stay inside the PlayMCP p99 3s budget.
GENERAL_RESPONSE_BUDGET_S = 2.65
ROUTE_EDIT_RESPONSE_BUDGET_S = 2.65
ROUTE_EDIT_MAX_CONCURRENT = max(1, int(os.environ.get("RUNART_MAX_CONCURRENT_ROUTE_EDITS", "1")))
# At an arbitrary address (no station preset), we first try to draw the animal
# from that exact start briefly; if it cannot complete, we substitute a nearby
# station's verified preset and cache that decision under the requested point.
# The outer 2.85s cap also includes queueing and response serialization.
ADDRESS_TRY_BUDGET_S = 0.8
# The plain-course choice still has to be generated. Below this much remaining
# budget it is dropped: an option is never worth risking the whole answer.
PLAIN_OPTION_MIN_BUDGET_S = 0.6


def _budget_left(started: float) -> float:
    """Outer-budget remainder for work added after the course itself."""
    return max(0.0, MCP_OUTER_RESPONSE_BUDGET_S - (time.monotonic() - started))
# Seoul is ~30km across, so this bounds the "nearest verified preset anywhere"
# scan without excluding any real start point.
PRESET_SEARCH_RADIUS_M = 30_000.0
PLAN_RESULT_CODES = {
    CASE_EXACT: "course_ready",
    CASE_NEARBY: "nearby_course_ready",
    CASE_FAR: "exact_shape_unavailable",
}


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
        started = time.monotonic()
        outcome = "cancelled"
        try:
            with anyio.fail_after(MCP_OUTER_RESPONSE_BUDGET_S):
                result = await anyio.to_thread.run_sync(
                    functools.partial(fn, *args, **kwargs),
                    abandon_on_cancel=True,
                )
                if isinstance(result, CallToolResult):
                    outcome = str((result.structuredContent or {}).get(
                        "result_code", "mcp_error" if result.isError else "success"))
                else:
                    outcome = "success"
                return result
        except TimeoutError:
            outcome = "generation_timeout"
            return _mcp_result(
                "⏱️ 요청 처리를 3초 안에 마치지 못했어요. 같은 요청을 한 번 더 "
                "시도하거나 위치·거리를 조금 단순하게 알려주세요.",
                code="generation_timeout", is_error=True, retryable=True,
            )
        except Exception:  # noqa: BLE001
            outcome = "internal_error"
            # Tools must never surface a raw exception: FastMCP would turn it
            # into an MCP error carrying the internal message (a corrupt
            # course_id used to leak "Error -3 while decompressing data").
            # Cancellation derives from BaseException and is deliberately
            # left to propagate.
            log.exception("tool %s failed", getattr(fn, "__name__", "?"))
            return _mcp_result(
                "⚠️ 요청을 처리하지 못했어요. 입력값을 다시 확인하거나 잠시 후 "
                "한 번 더 시도해 주세요.",
                code="internal_error", is_error=True, retryable=True,
            )
        finally:
            log.info(
                "mcp_tool tool=%s outcome=%s duration_ms=%d",
                getattr(fn, "__name__", "?"), outcome,
                round((time.monotonic() - started) * 1000),
            )
    # FastMCP must not infer a scalar output schema: the MCP boundary can
    # return CallToolResult for structured errors while sync functions remain
    # directly testable as strings.
    wrapper.__annotations__["return"] = CallToolResult
    return wrapper


def _mcp_result(text: str, *, code: str, is_error: bool = False,
                retryable: bool = False,
                assistant_text: str | None = None) -> CallToolResult:
    content = [TextContent(type="text", text=text)]
    structured = {
        "result_code": code,
        "retryable": retryable,
    }
    if assistant_text:
        # Kakao reads the widget envelope from content[0]. The separate block
        # and structured value let the host speak guidance as ordinary chat
        # copy without placing a sentence inside the visual card.
        content.append(TextContent(type="text", text=assistant_text))
        structured["assistant_text"] = assistant_text
    return CallToolResult(
        content=content,
        structuredContent=structured,
        isError=is_error,
    )


def _extract_single_course_id(text: str) -> str | None:
    """Return one unique id from links emitted by this server."""
    course_ids = _extract_course_ids(text)
    return course_ids[0] if len(course_ids) == 1 else None


def _extract_course_ids(text: str) -> list[str]:
    """Return unique server course ids in the same order users see them."""
    return list(dict.fromkeys(_COURSE_LINK_RE.findall(text)))


def _cached_course(course_id: str) -> Course | None:
    """Read the performance cache without restoring or regenerating a route."""
    with _CACHE_LOCK:
        cached = _course_cache.get(course_id)
    return cached if isinstance(cached, Course) else None


def _widget_lead_text(text: str) -> str:
    """Keep controlled context that appears before the course Markdown."""
    marker = "\n## "
    return text.split(marker, 1)[0] if marker in text else ""


def _try_course_widget(text: str, course_type: str) -> str | None:
    """Build from a cached course only; every mismatch keeps Markdown."""
    if not KAKAO_WIDGETS_ENABLED:
        log.info(
            "mcp_widget tool=create_seoul_running_course "
            "state=ineligible reason=disabled"
        )
        return None
    if course_type not in {
        "standard", "best_animal", "dog", "cat", "rabbit", "whale"
    }:
        log.info(
            "mcp_widget tool=create_seoul_running_course "
            "state=ineligible reason=course_type"
        )
        return None
    # A best-animal survey intentionally contains several course links. Its
    # first link is the featured course produced just above the alternatives,
    # so preserve that visible ordering instead of discarding the whole result.
    course_id = (
        next(iter(_extract_course_ids(text)), None)
        if course_type == "best_animal"
        else _extract_single_course_id(text)
    )
    if course_id is None:
        log.info(
            "mcp_widget tool=create_seoul_running_course "
            "state=ineligible reason=no_primary_id"
        )
        return None
    course = _cached_course(course_id)
    if course is None:
        log.info(
            "mcp_widget tool=create_seoul_running_course "
            "state=fallback reason=cache_miss"
        )
        return None
    try:
        widget = build_course_widget(
            course, course_id, BASE_URL
        )
    except WidgetTooLargeError:
        log.warning(
            "mcp_widget tool=create_seoul_running_course "
            "state=fallback reason=too_large"
        )
        return None
    except Exception:  # noqa: BLE001 — widget failures must preserve Markdown
        log.warning(
            "mcp_widget tool=create_seoul_running_course "
            "state=fallback reason=build_error"
        )
        return None
    log.info(
        "mcp_widget tool=create_seoul_running_course "
        "state=emitted reason=course_ready"
    )
    return widget


def _any_animal_matches(probe: CourseParams,
                        max_distance_m: float) -> list[PresetMatch]:
    """Verified presets of any animal around a start point, nearest first."""
    matches: list[PresetMatch] = []
    for shape in SURVEY_SHAPES:
        matches.extend(find_nearby_animal_presets(
            probe.model_copy(update={"shape": shape}), max_distance_m))
    matches.sort(key=lambda match: match.distance_m)
    return matches


def _plain_course_here(probe: CourseParams, distance_km: float | None,
                       timeout_s: float) -> Course | None:
    """The plain-course choice. Every failure drops the choice, not the answer."""
    if timeout_s < PLAIN_OPTION_MIN_BUDGET_S:
        return None
    try:
        params = probe.model_copy(update={
            "shape": None, "distance_km": distance_km or 5.0})
    except ValidationError:
        return None
    try:
        return _get_course(params, timeout_s=timeout_s)
    except (CourseError, _GenerationTimeout):
        return None


def _exact_animal_course(text: str, shape: str,
                         lat: float, lon: float) -> Course | None:
    """The requested animal at the requested start, if the generator drew it.

    Read from the text the generator already produced: the plan must never
    pay for a second generation of a course we are holding.
    """
    if text.startswith(("🔎", "⚠️", "⏱️")):
        return None
    course_id = _extract_single_course_id(text)
    if course_id is None:
        return None
    course = _cached_course(course_id)
    if course is None or course.params.shape != shape:
        return None
    moved = haversine_m(lat, lon, course.params.lat, course.params.lon)
    return course if moved < SAME_START_M else None


def _animal_course_plan(request: dict, shape: str, text: str,
                        timeout_s: float) -> CoursePlan | None:
    """Order the three choices for one animal request, inside the budget."""
    try:
        lat, lon, name = resolve_location(
            request.get("location"), request.get("lat"), request.get("lon"),
            timeout_s=min(timeout_s, ADDRESS_TRY_BUDGET_S))
        probe = CourseParams(
            lat=lat, lon=lon, location_name=name,
            distance_km=SHAPES[shape].min_km, shape=shape,
            include_hills=bool(request.get("include_hills")),
            night_mode=bool(request.get("night_mode")),
            need_facilities=request.get("need_facilities") or [],
        )
    except (CourseError, ValidationError):
        return None
    # Nearest first throughout: a runner standing at the requested start
    # judges an alternative by how far they have to walk to it, and the
    # Markdown fallback names the same course for the same reason.
    near = find_nearby_animal_presets(probe, NEARBY_RADIUS_M)
    radius = NEARBY_RADIUS_M if near else PRESET_SEARCH_RADIUS_M
    plan = build_course_plan(
        requested_name=name,
        shape=shape,
        exact=_exact_animal_course(text, shape, lat, lon),
        shape_matches=near or find_nearby_animal_presets(
            probe, PRESET_SEARCH_RADIUS_M),
        animal_matches=_any_animal_matches(probe, radius),
        standard=_plain_course_here(probe, request.get("distance_km"), timeout_s),
    )
    if plan is not None:
        for choice in (plan.primary, *plan.alternatives):
            _cache_put(choice.course_id, choice.course)
    return plan


def _plan_widget(plan: CoursePlan) -> str | None:
    """Serialize a plan, or keep the Markdown answer by returning None."""
    try:
        widget = build_course_widget(
            plan.primary.course, plan.primary.course_id, BASE_URL,
            alternatives=plan.alternatives)
    except WidgetTooLargeError:
        log.warning("mcp_widget tool=create_seoul_running_course "
                    f"state=fallback reason=too_large case={plan.case}")
        return None
    except Exception:  # noqa: BLE001 - widget failures must preserve Markdown
        log.warning("mcp_widget tool=create_seoul_running_course "
                    f"state=fallback reason=build_error case={plan.case}")
        return None
    log.info("mcp_widget tool=create_seoul_running_course "
             f"state=emitted reason=plan case={plan.case} "
             f"choices={1 + len(plan.alternatives)}")
    return widget


def _planned_course_result(text: str, *, course_type: str, request: dict,
                           timeout_s: float) -> CallToolResult | None:
    """Answer an animal request with the full choice matrix, or None."""
    if not KAKAO_WIDGETS_ENABLED or course_type not in SHAPES:
        return None
    # A rejection with no course behind it stays conversational copy.
    if text.startswith(("⏱️", "⚠️")):
        return None
    plan = _animal_course_plan(request, course_type, text, timeout_s)
    if plan is None:
        return None
    # The plan owns one short spoken sentence for every case. Generator copy
    # can contain scoring rationale that belongs on the detail page, not in a
    # concise chat handoff beside the widget.
    lead = plan.lead
    widget = _plan_widget(plan)
    if widget is None:
        return None
    return _mcp_result(
        widget, code=PLAN_RESULT_CODES[plan.case], assistant_text=lead
    )


def _course_tool_result(text: str, *, course_type: str,
                        request: dict | None = None,
                        timeout_s: float = 0.0) -> CallToolResult:
    """Classify controlled tool copy into a stable MCP result contract."""
    if request is not None:
        planned = _planned_course_result(
            text, course_type=course_type, request=request, timeout_s=timeout_s)
        if planned is not None:
            return planned
    if text.startswith("⏱️"):
        return _mcp_result(
            text, code="generation_timeout", is_error=True, retryable=True)
    if text.startswith("🔎"):
        code = "nearby_course_ready" if "/c/" in text else "exact_shape_unavailable"
        return _mcp_result(text, code=code)
    if text.startswith("⚠️"):
        if "위치를 찾지 못" in text or "출발 위치가 필요" in text:
            return _mcp_result(text, code="location_not_found", is_error=True)
        # A short-distance animal request returns a useful verified alternative,
        # not an infrastructure failure.
        if "추천 거리" in text:
            return _mcp_result(text, code="exact_shape_unavailable")
        return _mcp_result(text, code="invalid_request", is_error=True)
    widget = _try_course_widget(text, course_type)
    if widget is not None:
        return _mcp_result(
            widget, code="course_ready", assistant_text=_widget_lead_text(text)
        )
    return _mcp_result(text, code="course_ready")


def _get_course(params: CourseParams, timeout_s: float | None = None) -> Course:
    cid = encode_course_id(params)
    with _CACHE_LOCK:
        hit = _course_cache.get(cid)
    if isinstance(hit, Course):
        return hit
    if isinstance(hit, CourseError):
        raise hit
    # Course ids encode parameters, not the routed node path.  After a process
    # restart a detail URL therefore has no hot-cache entry.  Restore the same
    # build-verified station preset used by the recommendation instead of
    # generating a different route for the same URL.
    preset = get_animal_preset(params)
    if isinstance(preset, Course):
        _cache_put(cid, preset)
        return preset
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


def _cache_animal_recommendation(
        course: Course, requested_params: CourseParams | None = None) -> None:
    """Cache the selected course under the request that produced it.

    Nearby verified fallbacks keep their own start coordinates. Caching only
    under those coordinates would repeat the full exact-start search for an
    identical arbitrary-address request on every call.
    """
    with _CACHE_LOCK:
        if len(_animal_recommendation_cache) >= _CACHE_MAX:
            _animal_recommendation_cache.pop(next(iter(_animal_recommendation_cache)))
        params = requested_params or course.params
        _animal_recommendation_cache[_animal_recommendation_key(params)] = course


def _get_animal_recommendation(params: CourseParams) -> Course | None:
    with _CACHE_LOCK:
        return _animal_recommendation_cache.get(_animal_recommendation_key(params))


def _build_params(location, lat, lon, distance_km, duration_min, include_hills,
                  night_mode, need_facilities, shape=None,
                  timeout_s: float | None = None) -> tuple[CourseParams, str]:
    """Returns (params, note). note explains any interpretation we made
    (e.g. duration→distance conversion) so the user sees the reasoning."""
    note = ""
    rlat, rlon, name = resolve_location(location, lat, lon, timeout_s=timeout_s)
    if distance_km is None and duration_min:
        distance_km = round(duration_min / DEFAULT_PACE_MIN_PER_KM, 1)
        note = f"⏱️ {duration_min:g}분 → 6:30/km 페이스 기준 약 {distance_km:g}km로 잡았어요.\n"
    if distance_km is None:
        distance_km = 5.0
        note = "거리를 말씀하지 않으셔서 기본 5km로 잡았어요. 바꾸고 싶으면 말씀해 주세요.\n"
    try:
        params = CourseParams(
            lat=rlat, lon=rlon, location_name=name, distance_km=distance_km,
            include_hills=include_hills, night_mode=night_mode,
            need_facilities=need_facilities or [], shape=shape,
        )
    except ValidationError as exc:
        # The tool schema no longer bounds distance/duration, so an out-of-range
        # value lands here. Answer in Korean instead of leaking a pydantic dump.
        raise CourseError(
            "거리는 1km에서 42.195km 사이로 알려주세요. "
            "시간으로 요청하실 때는 10분에서 360분 사이가 가능해요."
        ) from exc
    return params, note


def _run(params: CourseParams, note: str = "",
         timeout_s: float | None = None) -> str:
    try:
        course = _get_course(params, timeout_s=timeout_s)
        facs = facilities_along(route_points(course), params.need_facilities or None)
        return note + course_markdown(course, BASE_URL, facs)
    except CourseError as e:
        return f"⚠️ {e}"
    except _GenerationTimeout:
        return ("⏱️ 도로망 후보가 많아 이번 탐색을 3초 안에 마치지 못했어요. "
                "같은 요청을 한 번 더 시도하거나, 거리를 조금 줄여 요청해 주세요.")


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


ANIMAL_GUIDE_TAIL = ("💡 동물 GPS 아트 코스는 **강남·잠실처럼 길이 바둑판으로 뻗은 동네**나 "
                     "**큰 공원 근처**에서 또렷하게 완성돼요. 그런 출발점으로 다시 "
                     "요청하시면 동물 코스를 찾아드릴게요.")


def _rank_animal_matches(matches: list[PresetMatch]) -> list[PresetMatch]:
    """Deterministic fallback order: silhouette, proximity, then balance."""
    def key(match: PresetMatch) -> tuple[float, float, float]:
        similarity = float(match.course.shape_similarity or 0.0)
        distance = match.distance_m
        # Only breaks a tie after the two explicit product priorities.
        balance = distance / 2000.0 + (1.0 - similarity)
        return (-similarity, distance, balance)
    return sorted(matches, key=key)


def _animal_relocation_offer(params: CourseParams,
                             matches: list[PresetMatch] | None = None) -> str:
    """Keep an animal request an animal request; never silently return plain."""
    matches = _rank_animal_matches(
        matches if matches is not None
        else find_nearby_animal_presets(params, 30_000.0))
    safe_name = markdown_text(params.location_name or "요청한 출발지")
    shape_name = SHAPES[params.shape].name_ko if params.shape in SHAPES else "요청한 동물"
    if not matches:
        return (
            f"🔎 **{safe_name}을 정확한 출발점으로 하는 검증된 {shape_name} "
            "프리셋은 없어요.**\n"
            f"이 결과는 **{shape_name}에만 해당**하며, 일반 코스와 다른 동물 "
            "코스는 별도로 생성할 수 있어요. 가까운 검증 프리셋도 찾지 못했으니 "
            "출발 위치를 바꿔 다시 요청해 주세요."
        )
    match = matches[0]
    actual = markdown_text(match.course.params.location_name or "가까운 출발점")
    km = match.distance_m / 1000.0
    cid = encode_course_id(match.course.params)
    _cache_put(cid, match.course)
    _cache_animal_recommendation(match.course, requested_params=params)
    return (
        f"🔎 **{safe_name}을 정확한 출발점으로 하는 검증된 {shape_name} "
        f"프리셋은 없어요.** 이 결과는 **{shape_name}에만 해당**하며, 일반 "
        "코스와 다른 동물 코스는 별도로 생성할 수 있어요.\n"
        f"대신 약 **{km:.1f}km** 떨어진 **{actual}**에서 바로 사용할 수 있는 "
        f"{shape_name} 검증 코스를 찾았어요. 요청한 출발점과 다른 대안입니다.\n"
        f"- 지도·러닝 가이드: {BASE_URL}/c/{cid}\n"
        f"- GPX 다운로드: {BASE_URL}/c/{cid}.gpx"
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
                   requested_distance_km: float | None = None,
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
        if isinstance(preset, Course):
            # Station start with a verified preset: use it directly.
            courses[key] = preset
            continue
        if preset is None:
            # Station known to have no clean course for this animal.
            nearby = find_nearest_animal_preset(probe)
            if nearby is not None:
                courses[key] = nearby.course
                if not nearby.is_exact:
                    nearby_matches[key] = nearby
            continue
        # Arbitrary address: judge each animal from this exact start first;
        # nearest station presets only cover the ones that fail below.
        cached = _get_animal_recommendation(probe)
        if cached is None:
            missing[key] = probe
        else:
            courses[key] = cached
    if missing:
        survey_fn = functools.partial(find_min_clean_course, total_budget_s=1.3)
        courses.update(_offload_map(survey_fn, missing, timeout_s=timeout_s))
        for key, probe in missing.items():
            if courses.get(key) is None or courses.get(key) is _TIMED_OUT:
                nearby = find_nearest_animal_preset(probe)
                if nearby is not None:
                    courses[key] = nearby.course
                    if not nearby.is_exact:
                        nearby_matches[key] = nearby
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
        lines.append("위에 가장 잘 맞는 코스를 먼저 만들었어요. "
                     "다른 동물이 좋으면 동물 이름을, 더 길게 뛰고 싶으면 "
                     "원하는 거리를 말씀해 주세요.")
    elif timed_out_any:
        lines.append("도로망 후보가 많아 탐색을 멈췄어요. 원하는 동물 하나를 골라 다시 요청해 주세요.")
        return "\n".join(lines)
    else:
        # Nothing completes here: keep the animal intent and offer relocation.
        probe = CourseParams(lat=lat, lon=lon, location_name=name,
                             distance_km=requested_distance_km or 5.0,
                             shape=SURVEY_SHAPES[0])
        return _animal_relocation_offer(probe)
    # First try shows the best runnable course at the requested start, then
    # lists other animals as modification options.
    # A course at another start is an option, not an automatic replacement.
    ready = {key: c for key, c in courses.items()
             if c is not None and c is not _TIMED_OUT
             and key not in nearby_matches}
    if not ready:
        matches = [nearby_matches[key] for key in SURVEY_SHAPES
                   if key in nearby_matches]
        probe = CourseParams(lat=lat, lon=lon, location_name=name,
                             distance_km=requested_distance_km or 5.0,
                             shape=matches[0].course.params.shape)
        return "\n".join(lines) + "\n\n" + _animal_relocation_offer(probe, matches)
    featured_key = min(
        ready,
        key=(lambda key: (
            abs(ready[key].length_km - requested_distance_km),
            -float(ready[key].shape_similarity or 0.0),
        )) if requested_distance_km is not None
        else (lambda key: (
            -float(ready[key].shape_similarity or 0.0),
            ready[key].length_km,
        )),
    )
    featured = ready[featured_key]
    spec = SHAPES[featured_key]
    note = (f"⚡ **요청 조건에 가장 잘 맞는 모양을 바로 준비했어요** — "
            f"{spec.emoji} {spec.name_ko} {featured.length_km:.1f}km.\n"
            "다른 동물이 좋으면 아래 목록에서 골라 수정해 주세요.\n")
    featured_md = _serve_course(featured, note)
    return featured_md + "\n\n" + "\n".join(lines)


def generate_running_course(
    location: Annotated[str | None, Field(description=(
        "Exact Seoul start place stated by the user. Never infer, invent, or "
        "default a missing location; ask the user instead."
    ))] = None,
    # Ranges live in the descriptions, not as ge/le: a schema rejection happens
    # before the tool body and surfaces a raw pydantic dump instead of the
    # Korean guidance the user needs (e.g. asking for a course in Busan).
    lat: Annotated[float | None, Field(description="Start latitude (alternative to location). Seoul only: 37.4-37.72")] = None,
    lon: Annotated[float | None, Field(description="Start longitude (alternative to location). Seoul only: 126.76-127.19")] = None,
    distance_km: Annotated[float | None, Field(description="Target distance in km, 1-42.195")] = None,
    duration_min: Annotated[float | None, Field(description="Target duration in minutes, 10-360; converted to distance at 6:30/km if distance_km is absent")] = None,
    include_hills: Annotated[bool, Field(description="True to include uphill training segments (3-8% grade); False prefers flat routes")] = False,
    night_mode: Annotated[bool, Field(description="Prefer well-lit streets with safety CCTV coverage for night runs")] = False,
    need_facilities: Annotated[list[str] | None, Field(description="Facility types the course should pass: convenience_store, restroom, water, park")] = None,
) -> str:
    """Generates a loop running course in Seoul from Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!), snapped to
    real pedestrian roads and scored with the Running Friendliness Score built
    from Seoul open data (sidewalk width, slope, lighting, safety CCTV, parks).
    Safe, runner-friendly streets are preferred by default. Provide a start
    location (place name or lat/lon) and a target distance or duration.
    Returns course stats, a map preview link, and a GPX download link. This is
    only for a new course with an explicit user-provided start. Do not use it
    for questions about facilities near an existing course."""
    started = time.monotonic()

    def remaining() -> float:
        return max(0.01, GENERAL_RESPONSE_BUDGET_S - (time.monotonic() - started))

    try:
        params, note = _build_params(location, lat, lon, distance_km, duration_min,
                                     include_hills, night_mode, need_facilities,
                                     timeout_s=remaining())
    except CourseError as e:
        return f"⚠️ {e}"
    return _run(params, note, timeout_s=remaining())


def generate_animal_course(
    shape: Annotated[str | None, Field(description="Animal shape key: cat, dog, rabbit, whale")] = None,
    location: Annotated[str | None, Field(description=(
        "Exact Seoul start place stated by the user. Never infer, invent, or "
        "default a missing location; ask the user instead."
    ))] = None,
    lat: Annotated[float | None, Field(description="Start latitude (alternative to location). Seoul only: 37.4-37.72")] = None,
    lon: Annotated[float | None, Field(description="Start longitude (alternative to location). Seoul only: 126.76-127.19")] = None,
    distance_km: Annotated[float | None, Field(description="Target distance in km, 1-42.195")] = None,
    duration_min: Annotated[float | None, Field(description="Target duration in minutes, 10-360")] = None,
    include_hills: Annotated[bool, Field(description="Include uphill segments")] = False,
    night_mode: Annotated[bool, Field(description="Prefer well-lit, CCTV-covered streets")] = False,
    need_facilities: Annotated[list[str] | None, Field(description="Facility types to pass by")] = None,
    shape_token: Annotated[str | None, Field(description="Share token like 'whale-5k' from a friend's course link; recreates the same shape at this user's location")] = None,
) -> str:
    """Generates a GPS-art running course shaped like an animal (cat, dog,
    rabbit, whale) snapped to real pedestrian roads in Seoul, from
    Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!). Shape quality decides the distance: call WITHOUT a shape
    to get, for each animal, the shortest distance at which it completes as
    a clean reference-like silhouette at this location, so the user can
    choose. Call with a shape and no distance to draw that animal at its own
    shortest clean distance. If a forced distance cannot be drawn well,
    alternatives are suggested instead of returning a bad course. Accepts a
    shape_token from a shared link to recreate a friend's shape here. This is
    only for a new course with an explicit user-provided start. Do not use it
    for questions about facilities near an existing course."""
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
            rlat, rlon, name = resolve_location(
                location, lat, lon, timeout_s=remaining())
        except CourseError as e:
            return f"⚠️ {e}"
        return _animal_survey(rlat, rlon, name, distance_km,
                              timeout_s=remaining())
    if distance_km is None and duration_min is None and shape in SHAPES:
        # Shape chosen, distance left open → quality-first: draw at the
        # shortest distance where the silhouette completes cleanly.
        try:
            rlat, rlon, name = resolve_location(
                location, lat, lon, timeout_s=remaining())
        except CourseError as e:
            return f"⚠️ {e}"
        probe = CourseParams(lat=rlat, lon=rlon, location_name=name,
                             distance_km=SHAPES[shape].min_km, shape=shape,
                             include_hills=include_hills, night_mode=night_mode,
                             need_facilities=need_facilities or [])
        preset = get_animal_preset(probe)
        if isinstance(preset, Course):
            # Station start with a verified preset: serve it directly.
            _cache_animal_recommendation(preset)
            return _serve_course(preset, _verified_animal_note(name, shape, preset))
        if preset is None:
            # Station known to have no clean course: nearest other preset.
            nearby = find_nearest_animal_preset(probe)
            if nearby is None:
                return _animal_relocation_offer(probe)
            return _animal_relocation_offer(probe, [nearby])
        # Arbitrary address (no preset entry): judge the animal from this
        # exact start first, within ADDRESS_TRY_BUDGET_S.
        cached = _get_animal_recommendation(probe)
        course = cached
        if course is None:
            try:
                course = _offload(
                    find_min_clean_course, probe,
                    timeout_s=min(remaining(), ADDRESS_TRY_BUDGET_S))
            except _GenerationTimeout:
                course = None
        if course is not None:
            if cached is not None and (
                    abs(course.params.lat - probe.lat) > 0.00001
                    or abs(course.params.lon - probe.lon) > 0.00001):
                match = PresetMatch(
                    course,
                    haversine_m(probe.lat, probe.lon,
                                course.params.lat, course.params.lon),
                )
                return _animal_relocation_offer(probe, [match])
            _cache_animal_recommendation(course)
            spec = SHAPES[shape]
            note = (f"{spec.emoji} {spec.name_ko} 모양이 가장 깔끔하게 완성되는 "
                    f"11km 이내 최상 코스 {course.length_km:.1f}km로 그렸어요. "
                    f"{REFERENCE_FEATURES.get(shape, '')}\n")
            return _serve_course(course, note)
        # Couldn't complete from this address in time: substitute one of the
        # nearby stations' verified presets (random pick keeps repeat
        # requests from always steering to the same station).
        candidates = _rank_animal_matches(find_nearby_animal_presets(probe))
        if candidates:
            return _animal_relocation_offer(probe, candidates)
        # No clean fit at this location: serve a plain runner-friendly course
        # instead of a dead end, then steer to grid-street areas.
        return _animal_relocation_offer(probe)
    try:
        params, note = _build_params(location, lat, lon, distance_km, duration_min,
                                     include_hills, night_mode, need_facilities,
                                     shape=shape, timeout_s=remaining())
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
        if (minimum is not None and nearby is not None
                and not nearby.is_exact):
            return _animal_relocation_offer(params, [nearby])
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


def create_seoul_running_course(
    course_type: Annotated[
        Literal["standard", "best_animal", "dog", "cat", "rabbit", "whale"],
        Field(description=(
            "Required course intent. standard=일반 러닝/달리기 코스; "
            "best_animal=동물 종류를 지정하지 않은 동물/GPS 아트 추천; "
            "dog=강아지·댕댕이; cat=고양이·야옹이; rabbit=토끼; whale=고래. "
            "동물 표현이 있으면 standard를 선택하지 마세요."
        )),
    ],
    location: Annotated[str | None, Field(description=(
        "사용자가 말한 서울 내 출발 위치를 한 글자도 추론하지 말고 그대로 전달하세요. "
        "지하철역, 장소명, 도로명·지번 주소를 지원합니다. "
        "예: 강남역, 성신여대역, 경복궁역, 서울숲, 테헤란로8길 8. "
        "대화에 출발지가 없으면 임의 위치를 만들지 말고 사용자에게 물어보며, 이 툴을 "
        "호출하지 마세요. lat/lon을 모두 전달한 경우에만 생략할 수 있습니다."
    ))] = None,
    lat: Annotated[float | None, Field(description=(
        "Start latitude instead of location; provide together with lon. Seoul only: 37.4-37.72"
    ))] = None,
    lon: Annotated[float | None, Field(description=(
        "Start longitude instead of location; provide together with lat. Seoul only: 126.76-127.19"
    ))] = None,
    distance_km: Annotated[float | None, Field(description=(
        "사용자가 명시한 목표 거리(km), 1-42.195. 생략 시 standard는 기본 5km, "
        "동물 코스는 가장 선명한 검증 거리를 서버가 선택합니다."
    ))] = None,
    duration_min: Annotated[float | None, Field(description=(
        "사용자가 명시한 목표 시간(분), 10-360. 말하지 않았으면 생략하며, "
        "distance_km와 함께 있으면 거리를 우선합니다."
    ))] = None,
    include_hills: Annotated[bool, Field(description=(
        "오르막·언덕·업힐 훈련을 요청했을 때만 true; 평지 또는 언급이 없으면 false"
    ))] = False,
    night_mode: Annotated[bool, Field(description=(
        "야간·밤·가로등·CCTV·안전 경로를 요청했을 때 true; 언급이 없으면 false"
    ))] = False,
    need_facilities: Annotated[list[str] | None, Field(description=(
        "요청한 경유 시설만 전달: convenience_store, restroom, water, park"
    ))] = None,
) -> CallToolResult:
    """Creates a standard running course or animal-shaped GPS-art course on
    real pedestrian roads in Seoul with Runnywhere(러니웨어). Use it for a new 러닝 코스, 달리기 코스,
    그려줘, 추천해줘, or animal/GPS-art request only after the user supplied
    a Seoul start. Use standard for an ordinary run, best_animal when no
    animal is named, or dog/cat/rabbit/whale for the named animal. Never invent
    a missing start; ask for it without calling. Do not use this for an
    existing course_id, "이 코스 근처 화장실", map/GPX repetition, completion,
    or relays. Speak structuredContent.assistant_text once as normal text
    outside the widget; never repeat it in the card."""
    common = dict(
        location=location, lat=lat, lon=lon, distance_km=distance_km,
        duration_min=duration_min, include_hills=include_hills,
        night_mode=night_mode, need_facilities=need_facilities,
    )
    started = time.monotonic()
    if course_type == "standard":
        return _course_tool_result(
            generate_running_course(**common), course_type=course_type)
    shape = None if course_type == "best_animal" else course_type
    text = generate_animal_course(shape=shape, **common)
    return _course_tool_result(text, course_type=course_type, request=common,
                               timeout_s=_budget_left(started))


@functools.wraps(generate_running_course)
def _legacy_generate_running_course(*args, **kwargs) -> CallToolResult:
    """Bridge a cached pre-unification Preview call to the latest response."""
    return _course_tool_result(
        generate_running_course(*args, **kwargs), course_type="standard"
    )


@functools.wraps(generate_animal_course)
def _legacy_generate_animal_course(*args, **kwargs) -> CallToolResult:
    """Bridge a cached pre-unification animal call to the latest response."""
    shape = kwargs.get("shape")
    if shape is None and args:
        shape = args[0]
    course_type = shape if shape in SHAPES else "best_animal"
    started = time.monotonic()
    text = generate_animal_course(*args, **kwargs)
    # Positional calls only ever carry the shape, so the start is in kwargs.
    request = {key: kwargs.get(key) for key in (
        "location", "lat", "lon", "distance_km", "include_hills",
        "night_mode", "need_facilities")}
    if request["location"] is None and request["lat"] is None:
        return _course_tool_result(text, course_type=course_type)
    return _course_tool_result(
        text, course_type=course_type, request=request,
        timeout_s=_budget_left(started),
    )


def list_available_shapes() -> str:
    """Lists the animal shapes supported by Runnywhere(러니웨어: 어디서든
    러닝 코스 짜기!). Use only when the user asks which shapes are available.
    For a course at any location, use create_seoul_running_course instead."""
    lines = ["러니웨어에서 그릴 수 있는 모양:"]
    for s in list_shapes():
        lines.append(f"- {s['emoji']} {s['name_ko']} (`{s['shape']}`) — {s['min_km']:g}km 이상 권장")
    lines.append("출발 위치와 함께 동물 모양을 요청하면, 각 동물이 가장 깔끔하게 "
                 "완성되는 최단 거리를 먼저 보여드려요. 예: \"경복궁역에서 동물 모양 코스 추천해줘\"")
    return "\n".join(lines)


def find_facilities_near_course(
    course_id: Annotated[str, Field(description=(
        "Exact course_id from the most recent generated or selected Runnywhere "
        "course in this conversation"
    ))],
    facility_types: Annotated[list[str] | None, Field(description=(
        "Requested filters only: convenience_store=편의점, restroom=화장실, "
        "water=음수대/물, park=공원. Omit to return all supported facilities."
    ))] = None,
) -> str:
    """Finds convenience stores, restrooms, drinking water, or parks within
    10m of an existing Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!)
    course. Use this for follow-ups such as "이 코스 근처 화장실 찾아줘",
    "이 경로에 편의점 있어?", "달리다가 물 마실 곳", or "코스 주변 공원".
    Requires the most recent prior course_id. Return facilities for that exact
    course; never regenerate, refine, summarize, create, or recommend a course."""
    try:
        params = decode_course_id(course_id)
        course = _get_course(params, timeout_s=GENERAL_RESPONSE_BUDGET_S)
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
    """Refines an existing Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!)
    course by changing distance, hills, night mode, animal shape, start, or
    facilities. Requires a prior course_id; never use it for the first course
    creation or recommendation."""
    started = time.monotonic()

    def remaining() -> float:
        return max(0.01, GENERAL_RESPONSE_BUDGET_S - (time.monotonic() - started))

    try:
        params = decode_course_id(course_id)
    except Exception:
        return "⚠️ course_id가 올바르지 않아요. 코스 응답에 있는 지도 링크의 id를 사용해 주세요."
    updates: dict = {}
    has_requested_change = False
    if distance_km is not None:
        updates["distance_km"] = distance_km
        has_requested_change = True
    if include_hills is not None:
        updates["include_hills"] = include_hills
        has_requested_change = True
    if night_mode is not None:
        updates["night_mode"] = night_mode
        has_requested_change = True
    if shape is not None:
        updates["shape"] = None if shape == "none" else shape
        has_requested_change = True
    if need_facilities is not None:
        updates["need_facilities"] = need_facilities
        has_requested_change = True
    if location is not None:
        try:
            rlat, rlon, name = resolve_location(
                location, None, None, timeout_s=remaining())
        except CourseError as e:
            return f"⚠️ {e}"
        updates.update(lat=rlat, lon=rlon, location_name=name)
        has_requested_change = True
    # Conversational refinement starts a fresh automatic route. Manual editor
    # state must never leak into a changed-distance/shape/location URL.
    updates["manual_waypoints"] = []
    if not has_requested_change:
        return "바꿀 조건을 말씀해 주세요 (거리, 오르막, 야간 모드, 모양, 출발점, 편의시설)."
    return _run(params.model_copy(update=updates), timeout_s=remaining())


def get_course_status(
    course_id: Annotated[str, Field(description="Course id from a previously generated course")],
) -> str:
    """Retrieves the summary, map, and GPX links for an existing
    Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!) course_id. Never use it
    to create or recommend a new course. Do not use it for 화장실, 편의점, 물,
    공원, or other facility questions; use find_facilities_near_course."""
    try:
        params = decode_course_id(course_id)
    except Exception:
        return "⚠️ course_id가 올바르지 않아요. 코스 응답에 있는 지도 링크의 id를 사용해 주세요."
    return _run(params, timeout_s=GENERAL_RESPONSE_BUDGET_S)


def record_animal_completion(
    course_id: Annotated[str, Field(description="Completed animal course id")],
    passport_token: Annotated[str | None, Field(description="Existing passport token; omit for the first completed animal")] = None,
) -> str:
    """Records a completed animal course in a stateless passport for
    Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!). Use only when the user
    explicitly reports completing an existing animal course_id; never use it
    for course creation or recommendation."""
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
    """Starts or extends a Shape Relay with an existing animal course_id in
    Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!). Use only for an explicit
    relay request after a course exists; never use it to create or recommend a
    new course. A relay holds up to eight courses."""
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
for _fn, _name, _title, _open_world in (
    (create_seoul_running_course, "create_seoul_running_course",
     "서울 러닝 코스 생성", True),
    (_legacy_generate_running_course, "generate_running_course",
     "Generate running course (compatibility)", True),
    (_legacy_generate_animal_course, "generate_animal_course",
     "Generate animal course (compatibility)", True),
    (list_available_shapes, "list_available_shapes",
     "List available shapes", False),
    (find_facilities_near_course, "find_facilities_near_course",
     "Find facilities near course", False),
    (refine_course, "refine_course", "Refine course", True),
    (get_course_status, "get_course_status", "Get course status", False),
    (record_animal_completion, "record_animal_completion",
     "Record animal-course completion", False),
    (extend_shape_relay, "extend_shape_relay", "Extend shape relay", False),
):
    mcp.add_tool(
        offloaded(_fn), name=_name,
        annotations=ToolAnnotations(
            title=_title, openWorldHint=_open_world, **_RO),
    )


# ---------- Preview web (same server, PRD §5.6) ----------

@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> Response:
    return JSONResponse({
        "ok": True,
        "ready": _WARM_READY.is_set(),
        "service": "runnywhere",
        "release_sha": RELEASE_SHA,
        "animal_presets": preset_status(),
    })


@mcp.custom_route("/assets/PretendardVariable.woff2", methods=["GET"])
async def pretendard_font(_: Request) -> Response:
    if not FONT_PATH.exists():
        return PlainTextResponse("font asset unavailable", status_code=404)
    return Response(
        FONT_PATH.read_bytes(), media_type="font/woff2",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


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
        course = _get_course(params, timeout_s=GENERAL_RESPONSE_BUDGET_S)
    except Exception:
        return PlainTextResponse("잘못된 코스 링크입니다.", status_code=404)
    return Response(card_svg(course), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/c/{course_id}/thumb.svg", methods=["GET"])
async def course_thumbnail(request: Request) -> Response:
    """Text-free route artwork used by the compact Kakao course widget."""
    try:
        params = decode_course_id(request.path_params["course_id"])
        course = _get_course(params, timeout_s=GENERAL_RESPONSE_BUDGET_S)
    except Exception:
        return PlainTextResponse("잘못된 코스 링크입니다.", status_code=404)
    return Response(course_thumbnail_svg(course), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/c/{course_id}/route.json", methods=["GET"])
async def course_route_json(request: Request) -> Response:
    """Course polyline for the animal-atlas overlay (verified presets only:
    the atlas must never trigger CPU generation from an unauthenticated URL)."""
    try:
        params = decode_course_id(request.path_params["course_id"])
        course = get_animal_preset(params)
    except Exception:
        return JSONResponse({"error": "bad course id"}, status_code=404)
    if not isinstance(course, Course):
        return JSONResponse({"error": "not a verified course"}, status_code=404)
    points = [[round(lat, 5), round(lon, 5)] for lat, lon in route_points(course)]
    return JSONResponse({"points": points, "km": round(course.length_km, 1)},
                        headers={"Cache-Control": "public, max-age=86400"})


class _CourseEditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    path: list[int] = Field(min_length=3, max_length=1200)
    stroke: list[CourseWaypoint] = Field(default_factory=list, max_length=96)
    from_index: int | None = None
    to_index: int | None = None


@mcp.custom_route("/c/{course_id}/edit", methods=["POST"])
async def edit_course_route(request: Request) -> Response:
    if not ROUTE_EDIT_ENABLED:
        return JSONResponse({"error": "경로 수정 기능을 잠시 사용할 수 없어요."}, status_code=503)
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type != "application/json":
        return JSONResponse({"error": "JSON 형식의 코스 선이 필요해요."}, status_code=400)
    try:
        current = decode_course_id(request.path_params["course_id"])
    except Exception:
        return JSONResponse({"error": "잘못된 코스 링크입니다."}, status_code=404)
    try:
        payload = _CourseEditPayload.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "코스 선 정보를 확인해 주세요."}, status_code=400)
    edited = current.model_copy(update={"shape": None, "manual_waypoints": [], "manual_path": []})
    try:
        with anyio.fail_after(ROUTE_EDIT_RESPONSE_BUDGET_S):
            if payload.action == "snap":
                if payload.from_index is None or payload.to_index is None:
                    return JSONResponse({"error": "지운 구간의 양 끝을 확인해 주세요."}, status_code=400)
                course = await anyio.to_thread.run_sync(
                    functools.partial(
                        snap_drawn_segment, edited, payload.path,
                        payload.from_index, payload.to_index, payload.stroke,
                    ), abandon_on_cancel=True,
                )
            elif payload.action == "reroute":
                if payload.from_index is None or payload.to_index is None:
                    return JSONResponse({"error": "바꿀 구간을 다시 선택해 주세요."}, status_code=400)
                course = await anyio.to_thread.run_sync(
                    functools.partial(
                        reroute_segment, edited, payload.path,
                        payload.from_index, payload.to_index,
                    ), abandon_on_cancel=True,
                )
            elif payload.action == "save":
                course = await anyio.to_thread.run_sync(
                    functools.partial(course_from_path, edited, payload.path),
                    abandon_on_cancel=True,
                )
            else:
                return JSONResponse({"error": "지원하지 않는 편집 동작입니다."}, status_code=400)
    except CourseError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except (TimeoutError, _GenerationTimeout):
        return JSONResponse(
            {"error": "대체 보행로를 찾는 데 시간이 걸렸어요. 더 짧은 구간을 선택해 주세요."},
            status_code=503,
        )
    if payload.action in {"snap", "reroute"}:
        g = graphmod.get_graph()
        return JSONResponse({
            "path": [[node, round(g.nodes[node]["lat"], 6), round(g.nodes[node]["lon"], 6)]
                     for node in course.path],
            "length_km": round(course.length_km, 2),
            # The detail panels below the map describe *this* course, so they
            # have to follow the edit rather than keep describing the original.
            # Measured cost of the extra work: ~25ms, well inside the budget.
            "summary": course_edit_summary(course),
        }, headers={"Cache-Control": "no-store"})
    new_id = encode_course_id(course.params)
    if len(new_id) > 4096:
        return JSONResponse(
            {"error": "수정한 코스 선이 너무 복잡해 링크로 저장할 수 없어요. 더 짧은 구간씩 바꿔 주세요."},
            status_code=422,
        )
    _cache_put(new_id, course)
    return JSONResponse(
        {
            "course_id": new_id,
            "preview_url": f"{BASE_URL}/c/{new_id}",
            "length_km": round(course.length_km, 2),
            "ascent_m": round(course.ascent_m),
            "rfs": course.rfs["score"],
        },
        headers={"Cache-Control": "no-store"},
    )


@mcp.custom_route("/c/{course_id}", methods=["GET"])
async def preview(request: Request) -> Response:
    raw = request.path_params["course_id"]
    is_gpx = raw.endswith(".gpx")
    cid = raw[:-4] if is_gpx else raw
    try:
        params = decode_course_id(cid)
        course = _get_course(params, timeout_s=GENERAL_RESPONSE_BUDGET_S)
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
                        headers={"Cache-Control": "public, max-age=300"})


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
            courses.append(
                preset if isinstance(preset, Course)
                else _get_course(params, timeout_s=GENERAL_RESPONSE_BUDGET_S)
            )
        page = relay_html(token, courses, BASE_URL)
    except Exception:
        return PlainTextResponse("잘못된 Shape Relay 링크입니다.", status_code=404)
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=3600"})


# ---------- rate limiting (PRD §8) ----------

class _TokenBucketMiddleware:
    """Per-client token bucket: RATE_LIMIT_RPS steady, 2x burst. In-process by
    design — PlayMCP in KC fronts a single container for this contest."""

    def __init__(self, app, rps: float = 20.0,
                 max_body_bytes: int = 65_536,
                 max_concurrent_mcp: int = 16,
                 max_concurrent_route_edits: int = ROUTE_EDIT_MAX_CONCURRENT,
                 trust_proxy_hops: int = 0):
        self.app = app
        self.rps = rps
        self.burst = rps * 2
        self.max_body_bytes = max_body_bytes
        self.max_concurrent_mcp = max_concurrent_mcp
        self.max_concurrent_route_edits = max_concurrent_route_edits
        self.trust_proxy_hops = max(0, trust_proxy_hops)
        self.buckets: dict[str, tuple[float, float]] = {}
        self._active_mcp = 0
        self._active_route_edits = 0
        self._active_lock = asyncio.Lock()

    def _client_key(self, scope) -> str:
        """Bucket key. Behind a load balancer the TCP peer is the balancer, so
        every Kakao Tools user would share one bucket. X-Forwarded-For fixes
        that but is client-writable, so it is only read when the operator
        declares how many trusted proxies sit in front (RUNART_TRUST_PROXY_HOPS)
        and only the entry that a trusted hop appended is used."""
        peer = (scope.get("client") or ("?",))[0]
        if not self.trust_proxy_hops:
            return peer
        for key, value in scope.get("headers", []):
            if key.lower() != b"x-forwarded-for":
                continue
            chain = [p.strip() for p in value.decode("latin-1").split(",") if p.strip()]
            if not chain:
                break
            # Rightmost entries are appended by the proxies closest to us and
            # cannot be forged by the caller; anything further left can.
            return chain[max(0, len(chain) - self.trust_proxy_hops)]
        return peer

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
                     b"font-src 'self'; "
                     b"connect-src 'self' https://*.kakao.com https://*.daum.net"),
                ])
                message["headers"] = headers
            await send(message)
        path = scope.get("path", "")
        client = self._client_key(scope)
        # Health/readiness must remain observable during abusive traffic; the
        # hosting platform needs it to distinguish overload from a dead server.
        if path == "/healthz":
            return await self.app(scope, receive, send_with_security_headers)

        # Reject oversized MCP frames before JSON parsing/decompression can
        # consume significant memory. Also enforce the limit for chunked bodies
        # where Content-Length is absent or dishonest.
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = self.max_body_bytes + 1
        if declared > self.max_body_bytes:
            from starlette.responses import PlainTextResponse as _P
            return await _P("request body too large", status_code=413)(
                scope, receive, send_with_security_headers)
        received = 0
        original_receive = receive

        async def limited_receive():
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        # One logical Streamable HTTP tool call can produce several protocol
        # requests (POST plus session cleanup). Charge those frames fractionally
        # so RATE_LIMIT_RPS remains a tool-call limit rather than rejecting a
        # healthy client for protocol bookkeeping. Public web routes retain a
        # full token cost.
        cost = 0.25 if path == "/mcp" else 1.0
        import time as _t
        now = _t.monotonic()
        tokens, ts = self.buckets.get(client, (self.burst, now))
        tokens = min(self.burst, tokens + (now - ts) * self.rps)
        if tokens < cost:
            from starlette.responses import PlainTextResponse as _P
            return await _P("rate limit exceeded", status_code=429)(scope, receive, send_with_security_headers)
        if len(self.buckets) > 10_000:  # bound memory
            self.buckets.clear()
        self.buckets[client] = (tokens - cost, now)

        admitted_mcp = False
        admitted_edit = False
        is_edit = scope.get("method") == "POST" and path.startswith("/c/") and path.endswith("/edit")
        if path == "/mcp" or is_edit:
            async with self._active_lock:
                active = self._active_route_edits if is_edit else self._active_mcp
                limit = self.max_concurrent_route_edits if is_edit else self.max_concurrent_mcp
                if active >= limit:
                    from starlette.responses import PlainTextResponse as _P
                    return await _P("server busy", status_code=429)(
                        scope, receive, send_with_security_headers)
                if is_edit:
                    self._active_route_edits += 1
                    admitted_edit = True
                else:
                    self._active_mcp += 1
                    admitted_mcp = True
        try:
            return await self.app(scope, limited_receive,
                                  send_with_security_headers)
        except _RequestBodyTooLarge:
            from starlette.responses import PlainTextResponse as _P
            return await _P("request body too large", status_code=413)(
                scope, original_receive, send_with_security_headers)
        finally:
            if admitted_mcp:
                async with self._active_lock:
                    self._active_mcp -= 1
            if admitted_edit:
                async with self._active_lock:
                    self._active_route_edits -= 1


class _RequestBodyTooLarge(Exception):
    """An HTTP request exceeded the configured body limit."""


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
        if preset_status().startswith("ok"):
            _WARM_READY.set()
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
    status = preset_status()
    if status.startswith("ok"):
        log.info("animal presets: %s", status)
    else:
        log.error(
            "animal presets: NOT LOADED (%s) — the atlas will be empty and "
            "animal answers lose the verified fast path", status)


def create_app():
    """App factory (uvicorn workers). Rate limit wraps the whole app."""
    _log_geocoding_status()
    threading.Thread(target=_warm, daemon=True).start()
    app = mcp.streamable_http_app()
    return _TokenBucketMiddleware(
        app,
        rps=float(os.environ.get("RATE_LIMIT_RPS", "20")),
        trust_proxy_hops=int(os.environ.get("RUNART_TRUST_PROXY_HOPS", "0")),
        max_body_bytes=int(os.environ.get("RUNART_MAX_BODY_BYTES", "65536")),
        max_concurrent_mcp=int(os.environ.get("RUNART_MAX_CONCURRENT_MCP", "4")),
    )


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
