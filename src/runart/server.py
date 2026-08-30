"""Runnywhere MCP server — Agentic Player 10 (PlayMCP).

- Streamable HTTP, stateless (PRD §9): course ids are self-contained parameter
  tokens; the in-process cache is a performance layer only.
- 7 stateless, idempotent tools (PRD §5.1). Tool errors are returned as
  refined guidance text, never raw exceptions (PRD §5.2).
- Preview pages / GPX / shape share links are served by the same app (§5.6).
"""

import concurrent.futures
import contextvars
import functools
import html
import asyncio
import logging
import math
import multiprocessing
import os
from collections import Counter
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import graph as graphmod
from .animal_presets import (MISSING as PRESET_MISSING, PresetMatch,
                             all_verified_animal_presets,
                             animal_preset_is_blocked,
                             find_nearby_animal_presets,
                             find_nearest_animal_preset, get_animal_preset,
                             preset_status)
from .course import (MAX_VIA_POINTS, Course, CourseAccessError, CourseError,
                     CourseGapOpen, DistanceMissError, course_from_path,
                     ensure_course_runnable, generate_course, reroute_segment,
                     route_via_points, snap_drawn_segment, snap_drawn_strokes)
from .courseplan import (CASE_EXACT, EFFORT_TOLERANCE, KIND_REQUESTED, NEARBY_RADIUS_M,
                         RECOMMENDATION_COUNT, SAME_START_M,
                         CourseChoice, CoursePlan, build_course_plan, requested_distance,
                         route_signature)
from .facilities import LABELS_KO, facilities_along
from .geocode import (DISTRICT_STATIONS, STATION_DISTRICTS, district_scope,
                      is_citywide_scope, resolve_location)
from .geo import haversine_m
from .gpx import to_gpx
from .exploration import (atlas_html, create_relay, decode_relay,
                          passport_html, home_html, legal_html, record_run,
                          relay_html)
from .models import (COURSE_NAME_MAX_CHARS, FACILITY_TYPES, CourseParams, CourseWaypoint,
                     DEFAULT_PACE_MIN_PER_KM, decode_course_id,
                     decode_shape_token, encode_course_id)
from .render import (card_svg, course_edit_summary, course_markdown,
                     course_thumbnail_svg, edit_path_geometry, edit_path_nodes,
                     markdown_text, preview_html, route_points)
from .shapes import (MAX_ANIMAL_ART_KM, SHAPES, find_min_clean_course,
                     generate_shape_course, list_shapes)
from .rfs import route_rfs_summary  # noqa: F401  (re-export for tests)
from .rfs import has_sufficient_night_lighting
from .park_presets import PARK_SPOTS, park_courses, select_park_courses
from .standard_presets import get_standard_preset, nearest_start_preset
from .naming import COURSE_EDIT_NOTICE
from .widget import (WidgetTooLargeError, build_course_widget,
                     course_response_facts, response_start_name)

DEFAULT_BASE_URL = (
    "https://runnywhere-kakaotools.playmcp-endpoint.kakaocloud.io"
)
# What a start place may be. The four forms all resolve, but the tools used to
# describe only "a Seoul place", so an assistant reading the schema had no
# reason to pass a shop name or a lot-number address and asked for a station
# instead.
LOCATION_FORMS_KO = (
    "지하철역(강남역), 가게·건물 상호명(스타벅스 서울숲점), "
    "도로명 주소(서울 성동구 아차산로 100), 지번 주소(마포구 상암동 1601)"
)
LOCATION_FIELD_KO = (
    "사용자가 말한 서울 내 출발 위치를 한 글자도 추론하지 말고 그대로 전달하세요. "
    f"{LOCATION_FORMS_KO}를 모두 지원합니다. "
    "사용자가 상호명이나 주소를 말했다면 가까운 역 이름으로 바꾸지 말고 말한 그대로 넘기세요. "
    "대화에 출발지가 없으면 임의 위치를 지어내지 말고 생략한 채로 호출하세요. "
    "거리·동물·야간 조건만으로는 출발지를 대신할 수 없어 서버가 되묻습니다(missing). "
    "'서울 시내', '아무데나'도 그대로 전달하면 되묻습니다. "
    "구 이름(강남구, 마포구)은 유효한 범위(district)이며 그 구의 출발지로 최대 3개를 고릅니다. "
    "역·주소·상호명은 specific입니다. 공원 요청은 출발지 없이 바로 호출할 수 있습니다. "
    "lat/lon을 모두 전달해도 생략 가능합니다."
)
LOCATION_FIELD_EN = (
    "Exact Seoul start place stated by the user: a subway station, a shop or "
    "building name, a road-name address, or a lot-number address. Pass it "
    "verbatim -- do not substitute a nearby station for a shop name or "
    "address. Never invent a location: when the user did not state one, omit "
    "this field and call: missing or city-wide wording (서울 시내, 아무데나) "
    "asks for a start. A Seoul district (강남구) is a valid district scope; "
    "a named place/address is specific. Only park requests can omit a start."
)
REFINE_LOCATION_FIELD_EN = (
    "Specific new Seoul start: station, shop/building, road-name or lot-number address. "
    "Pass it verbatim; never invent or substitute a nearby station. "
    "District-only or city-wide wording is not a specific replacement start."
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
_RAW_RELEASE_SHA = next((
    os.environ[name] for name in (
        "RUNART_RELEASE_SHA", "GIT_COMMIT", "SOURCE_VERSION", "REVISION_ID")
    if os.environ.get(name)
), "unknown")


def _validated_release_sha(value: str, *, production: bool) -> str:
    """Reject unverifiable production images without weakening local setup."""
    if production and not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise RuntimeError(
            "production RUNART_RELEASE_SHA must be the exact 40-character Git SHA")
    return value.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", value) else value


RELEASE_SHA = _validated_release_sha(
    _RAW_RELEASE_SHA,
    production=os.environ.get("RUNART_ENV", "development").lower() == "production",
)
_WARM_READY = threading.Event()

_PRODUCTION_HOST = _BASE_PARTS.netloc
_TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        _PRODUCTION_HOST,
        "localhost:*", "127.0.0.1:*", "testserver",
    ],
    allowed_origins=[
        "https://preview-chatgpt.kakao.com", BASE_URL,
        "http://localhost:*", "http://127.0.0.1:*",
    ],
)

mcp = FastMCP(
    "Runnywhere",
    instructions=(
        "Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!) creates runnable "
        "courses on real pedestrian roads in Seoul. Route each turn by these "
        "rules, in order. (1) New course: call create_seoul_running_course "
        "EXACTLY ONCE per recommendation request. One call returns up to three distinct "
        "courses together, including '코스 3개' and night-run requests. Never "
        "call once per course or vary arguments to fill a list. If the result "
        "has only one or two eligible courses, show those without repeat calls or padding. "
        "Only zero eligible courses is insufficient_courses; explain it without repeat calls. "
        "Night recommendations require measured lighting >=0.40 and use the label "
        "'야간 조명 많음'. This is a dataset threshold, not a safety guarantee. Never silently "
        "substitute poorly lit or unknown-lighting routes. Do not claim safety "
        "merely because night_mode was requested. "
        "Use this tool for 러닝 코스/달리기 코스/그려줘/짜줘/만들어줘/추천해줘/GPS 아트 "
        "with a Seoul district (district), a specific place/address or both coordinates (specific). "
        "Exception: park/waterside requests set need_facilities=['park'] and may omit "
        "a start: the server picks up to 3 eligible registered destinations randomly. With a "
        "start, it picks up to 3 nearest eligible places; do not preselect or call 3 times. "
        "Copy an explicit start exactly; never invent, infer, "
        "or substitute a location. If it is missing, omit location and call once; "
        "the server asks for a start (missing_start), even with distance/animal/night conditions. "
        "Pass city-wide wording (서울 시내, 아무데나) unchanged; it also means missing. A location-only reply is valid "
        "only immediately after the user established course-creation intent "
        "or was asked for the missing start. Use standard for ordinary runs, "
        "best_animal only for animal/GPS-art requests without a named animal, "
        "never infer animal art from 그려줘 alone, and dog/cat/rabbit/whale for "
        "강아지·댕댕이/고양이·야옹이/토끼/고래. "
        "For a new course, include need_facilities=park only when the user "
        "explicitly asks for 공원, 강변, 한강, 하천, 호수, 물가, 수변 or 물 보면서 running. "
        "These all use the same five-destination catalogue. Drinking water/음수대 "
        "uses water, not park. Never infer park mode merely from nearby green space. "
        "(2) Direct users to the linked web editor for changes. refine_course and "
        "find_facilities_near_course require an explicitly supplied valid course_id; "
        "never infer an ID from an earlier widget or assume it survived in context. (3) "
        "Requests to show the same summary, map, or GPX use get_course_status. "
        "Never regenerate a course for rules 2-4. Never claim that a Seoul "
        "course is unsupported before the appropriate new-course call. When "
        "structuredContent.assistant_text_in_widget is true, the explanation "
        "is already rendered above the course list; do not repeat it. Otherwise, when "
        "a result includes structuredContent.assistant_text, your response "
        "MUST begin with that exact sentence verbatim as normal conversational "
        "text before introducing the widget. Never paraphrase, replace, or "
        "repeat it, and do not copy that guidance into the card. "
        "For closing prose use structuredContent.assistant_final_text verbatim, "
        "as the complete closing response, without extra paraphrases or a completion claim. "
        f"Always end a successful course response with '{COURSE_EDIT_NOTICE}'. "
        "Describe locations only from course_selection.actual_start_names. A requested "
        "district or park_selection.origin is a search reference, not proof that the "
        "returned routes are inside that district or start at that origin. Never say "
        "'강남구에서 뛰는 코스' just because 강남구 was requested. "
        "course_selection describes the "
        "ACTUAL returned routes; requested_course_type is only the request, "
        "never evidence that this shape was produced. If primary_matches_requested_shape "
        "is false, say which alternative shape was returned. Never call a rabbit "
        "or standard course a dog course. A widget payload does not confirm host "
        "rendering: never claim a card is visible or direct users to a button "
        "above. Use the provided map link instead."
    ),
    stateless_http=True,
    json_response=True,
    host=os.environ.get("HOST", "127.0.0.1"),
    port=int(os.environ.get("PORT", "8000")),
    transport_security=_TRANSPORT_SECURITY,
)

_RO = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

# Performance cache only — every entry is reproducible from its id
# (stateless). Failures are deterministic too, so they are cached as well:
# re-asking for an impossible shape answers instantly instead of re-searching.
_course_cache: dict[str, "Course | CourseError"] = {}
_course_inflight: dict[str, concurrent.futures.Future] = {}
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
# Card requests reserve time for the higher-priority local/effort candidate.
# Context-local so concurrent requests and direct generator calls are isolated.
_ANIMAL_CARD_DEADLINE = contextvars.ContextVar("animal_card_deadline", default=None)
# Plain-course generation must also stay inside the PlayMCP p99 3s budget.
GENERAL_RESPONSE_BUDGET_S = 2.65
ROUTE_EDIT_RESPONSE_BUDGET_S = 2.65
ROUTE_EDIT_MAX_CONCURRENT = max(1, int(os.environ.get("RUNART_MAX_CONCURRENT_ROUTE_EDITS", "1")))
# At an arbitrary address (no station preset), we first try to draw the animal
# from that exact start; if it cannot complete, we substitute a nearby station's
# verified preset and cache that decision under the requested point.
#
# This used to be 0.8s while the shape search itself is built around a 2.2s cap
# (shapes.MIN_CLEAN_TOTAL_BUDGET_S), so the search was killed before it could
# finish and almost every address request fell back to a station. Measured over
# eight non-station starts, the searches that do succeed take 84ms to 1.65s:
# 0.8s captured three of seven, 1.8s captures five and every cat course, which
# scored zero before. What is left over are searches that exhaust the shape
# search's own cap, so a larger slice here would not save them either.
#
# Budget: 2.65s for the tool, minus geocoding (two Kakao variants, ~0.5s worst
# case) and the widget build, leaves this comfortably inside the outer 2.85s
# cap that also covers queueing and response serialization.
ADDRESS_TRY_BUDGET_S = 1.8
# The plain-course choice still has to be generated. Below this much remaining
# budget it is dropped: an option is never worth risking the whole answer.
PLAIN_OPTION_MIN_BUDGET_S = 0.6


def _budget_left(started: float) -> float:
    """Outer-budget remainder for work added after the course itself."""
    return max(0.0, MCP_OUTER_RESPONSE_BUDGET_S - (time.monotonic() - started))
# Seoul is ~30km across, so this bounds the "nearest verified preset anywhere"
# scan without excluding any real start point.
PRESET_SEARCH_RADIUS_M = 30_000.0


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



def _speak_verbatim(result: CallToolResult, text: str) -> CallToolResult:
    """Hand the host our exact wording for a reply that carries no widget.

    Without this the host wrote its own sentence and inverted the options: the
    start-change question offers a *nearby* start, and Preview announced it as
    "서울숲에서 고래 모양 코스 만들기". The text is already in content[0]; this only
    names it as the closing prose the tool description tells the host to reuse.
    """
    result.structuredContent.update(
        assistant_final_text=text, assistant_final_text_verbatim=True,
        assistant_final_text_position="replace",
        assistant_final_text_is_complete=True)
    return result


def _mcp_result(text: str, *, code: str, is_error: bool = False,
                retryable: bool = False,
                assistant_text: str | None = None,
                assistant_text_in_widget: bool = False,
                assistant_final_text: str | None = None,
                course_selection: dict | None = None) -> CallToolResult:
    content = [TextContent(type="text", text=text)]
    structured = {
        "result_code": code,
        "retryable": retryable,
        "release_sha": RELEASE_SHA,
    }
    # Every reply that is plain copy is also its own closing prose. The host
    # uses structuredContent.assistant_final_text verbatim, and where the field
    # was missing it wrote its own sentence instead -- inverting the
    # start-change options in Preview. A widget envelope is never prose, so it
    # is left alone and keeps whatever the caller passed.
    is_widget = text.lstrip().startswith('{"widget"')
    if not assistant_final_text and not is_widget:
        structured.update(
            assistant_final_text=text, assistant_final_text_verbatim=True,
            assistant_final_text_position="replace",
            assistant_final_text_is_complete=True)
    if assistant_text:
        # Kakao reads the widget envelope from content[0]. Keep that position;
        # mark explanations already rendered by its leading Markdown so the
        # assistant does not repeat them after the recommendations.
        content.append(TextContent(type="text", text=assistant_text))
        structured["assistant_text"] = assistant_text
        structured["assistant_text_position"] = (
            "widget_intro" if assistant_text_in_widget else "before_widget")
        structured["assistant_text_in_widget"] = assistant_text_in_widget
        structured["assistant_text_verbatim"] = True
    if course_selection is not None:
        structured["course_selection"] = course_selection
    if assistant_final_text:
        # Some hosts pass only text content to the assistant. Supply the same
        # factual closing copy there, without changing Kakao's content[0].
        content.append(TextContent(type="text", text=assistant_final_text))
        structured["assistant_final_text"] = assistant_final_text
        structured["assistant_final_text_verbatim"] = True
        structured["assistant_final_text_position"] = "after_widget"
        structured["assistant_final_text_is_complete"] = True
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
    if not isinstance(cached, Course):
        return None
    try:
        ensure_course_runnable(cached)
    except CourseAccessError as exc:
        _cache_put(course_id, exc)
        return None
    return cached


def _try_course_result(text: str, course_type: str) -> CallToolResult | None:
    """Ground even a non-planned response in its cached course, without generation."""
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
    selection = _course_selection(
        [course_response_facts(course, course_id, BASE_URL)], course_type)
    final_text = _plan_final_text(selection)
    lead = f"{markdown_text(selection['primary']['start'])}에서 출발하는 코스예요."
    if not selection["primary_matches_requested_shape"]:
        lead = final_text.split("\n", 1)[0]
    text = final_text  # Never reuse stale location/effort claims from generator prose.
    widget = None
    try:
        widget = build_course_widget(course, course_id, BASE_URL)
    except WidgetTooLargeError:
        log.warning(
            "mcp_widget tool=create_seoul_running_course "
            "state=fallback reason=too_large"
        )
    except Exception:  # noqa: BLE001 — widget failures must preserve Markdown
        log.warning(
            "mcp_widget tool=create_seoul_running_course "
            "state=fallback reason=build_error"
        )
    if widget is not None:
        log.info(
            "mcp_widget tool=create_seoul_running_course "
            "state=emitted reason=course_ready"
        )
    return _mcp_result(
        widget if widget is not None else text, code="course_ready",
        assistant_text=lead, assistant_final_text=final_text,
        assistant_text_in_widget=False,
        course_selection=selection,
    )


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
    # 🔎 is a course somewhere else and ⚠️ carries no course at all. A course
    # under an interpretation note is still the exact one that was asked for.
    if text.startswith(("🔎", "⚠️")):
        return None
    course_id = _extract_single_course_id(text)
    if course_id is None:
        return None
    course = _cached_course(course_id)
    if course is None or course.params.shape != shape:
        return None
    moved = haversine_m(lat, lon, course.params.lat, course.params.lon)
    return course if moved < SAME_START_M else None


def _standard_alternatives(probe: CourseParams, standard: Course | None,
                           timeout_s: float) -> list[PresetMatch]:
    """Find distinct routes in one tool budget, keeping start/effort/preferences."""
    if standard is None or timeout_s < PLAIN_OPTION_MIN_BUDGET_S:
        return []
    seen = {route_signature(standard)}
    matches: list[PresetMatch] = []
    # Try opposite directions first. A route's variant travels in its ID so
    # map/GPX restoration does not silently revert to the primary route.
    probes = {variant: probe.model_copy(update={"shape": None, "route_variant": variant})
              for variant in (2, 4, 6)}
    courses = {}
    missing = {}
    for variant, params in probes.items():
        cid = encode_course_id(params)
        with _CACHE_LOCK:
            cached = _course_cache.get(cid)
        if isinstance(cached, CourseError):
            continue
        course = _cached_course(cid)
        if course is None:
            # Variants never reach _get_course, so the build-time catalogue has
            # to be consulted here too or every alternative is regenerated.
            course = get_standard_preset(params)
            if isinstance(course, CourseError):
                _cache_put(cid, course)
                continue
            if course is not None:
                _cache_put(cid, course)
        if course is not None:
            courses[variant] = course
        else:
            missing[variant] = params
    if missing:
        generated = _offload_map(generate_course, missing, timeout_s=timeout_s)
        for variant, course in generated.items():
            if isinstance(course, Course):
                _cache_put(encode_course_id(probes[variant]), course)
        courses.update(generated)
    for variant in probes:
        course = courses.get(variant)
        if not isinstance(course, Course):
            continue
        if probe.night_mode and not has_sufficient_night_lighting(course.rfs):
            continue
        signature = route_signature(course)
        if signature is None or signature in seen:
            continue
        seen.add(signature)
        matches.append(PresetMatch(course, 0))
    return matches


def _animal_course_plan(request: dict, shape: str, text: str,
                        timeout_s: float) -> CoursePlan | None:
    """Shared priority order for standard, named and unspecified animal asks."""
    started = time.monotonic()
    target = requested_distance(request.get("distance_km"), request.get("duration_min"))
    try:
        lat, lon, name = request.get("_resolved") or _resolve_start(
            request.get("location"), request.get("lat"), request.get("lon"),
            timeout_s=min(timeout_s, ADDRESS_TRY_BUDGET_S))
        probe = CourseParams(
            lat=lat, lon=lon, location_name=name,
            distance_km=target if target is not None else 5.0,
            shape=shape if shape in SHAPES else None,
            include_hills=bool(request.get("include_hills")),
            night_mode=bool(request.get("night_mode")),
            need_facilities=request.get("need_facilities") or [],
        )
    except (CourseError, ValidationError):
        return None
    cached = [c for cid in _extract_course_ids(text)
              if (c := _cached_course(cid)) is not None]
    standard = next((c for c in cached if c.params.shape is None and
                     haversine_m(lat, lon, c.params.lat, c.params.lon) < SAME_START_M
                     and (not probe.night_mode or has_sufficient_night_lighting(c.rfs))), None)
    # No base route means no variants: never spend the remaining budget
    # re-running an already failed standard generation.
    if shape == "standard" and standard is None:
        return None
    if shape == "standard" and not _eligible_matches([PresetMatch(standard, 0)], request, shape):
        return None
    # Do not preselect by nearest shape: the closest preset may be 12km for a
    # 5km ask. Rank every available candidate by actual start and route length.
    # The preset lookup treats preferences as exclusions; the card planner
    # must instead keep these candidates and assess their measured features.
    preset_probe = probe.model_copy(update={
        "include_hills": False, "night_mode": False, "need_facilities": []})
    # Always inspect verified presets first. Night/facility requests used to
    # suppress this local evidence and jump directly into slow generation;
    # the shared hard gates below decide whether each preset is actually valid.
    animals = (find_nearby_animal_presets(preset_probe, NEARBY_RADIUS_M)
               if shape in SHAPES
               else _any_animal_matches(preset_probe, NEARBY_RADIUS_M))
    animals.extend(PresetMatch(c, haversine_m(lat, lon, c.params.lat, c.params.lon))
                   for c in cached if c.params.shape)
    animals = _eligible_matches(animals, request, shape, allow_animal_alternatives=shape == "standard")
    if shape != "standard":
        standard = None
    standards = []
    # Count distinct roads, not duplicate presets, before deciding whether
    # the two remaining slots need CPU work.
    signatures = {route_signature(m.course) for m in animals}
    if standard is not None:
        signatures.add(route_signature(standard))
    if shape == "standard" and len(signatures) < RECOMMENDATION_COUNT:
        standards = _eligible_matches(_standard_alternatives(
            probe, standard, max(0, timeout_s - (time.monotonic() - started))), request, shape)
    plan = build_course_plan(
        requested_name=name,
        shape=shape,
        exact=None,  # Cached exact animals are already measured/filtered above.
        shape_matches=[], animal_matches=animals, standard=standard,
        standard_matches=standards,
        distance_km=request.get("distance_km"), duration_min=request.get("duration_min"),
        include_hills=bool(request.get("include_hills")),
        night_mode=bool(request.get("night_mode")),
        need_facilities=request.get("need_facilities") or [],
    )
    if plan is not None:
        for choice in (plan.primary, *plan.alternatives):
            _cache_put(choice.course_id, choice.course)
    return plan


def _plan_widget(plan: CoursePlan, *, start_notice: str = "") -> str | None:
    """Serialize a plan; on failure the caller renders that SAME plan as text."""
    try:
        widget = build_course_widget(
            plan.primary.course, plan.primary.course_id, BASE_URL,
            alternatives=plan.alternatives, primary_note=plan.primary.match_note,
            start_notice=start_notice)
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


def _course_selection(choices: list[dict], course_type: str) -> dict:
    """Actual output facts, separate from the shape the user asked for."""

    def matches(choice: dict) -> bool:
        actual = choice["course_type"]
        return actual != "standard" if course_type == "best_animal" else actual == course_type

    return {
        "requested_count": RECOMMENDATION_COUNT,
        "returned_count": len(choices),
        "actual_start_names": list(dict.fromkeys(c["start"] for c in choices)),
        "requested_course_type": course_type,
        "primary_matches_requested_shape": matches(choices[0]),
        "requested_shape_offered": any(matches(choice) for choice in choices),
        "primary": choices[0],
        "alternatives": choices[1:],
    }


def _course_summary(facts: dict) -> str:
    """A real link works even when the host does not display its widget."""
    return (f"{facts['title']} ({facts['shape_label']}) · "
            f"{facts['distance_km']:.1f}km · 약 {facts['duration_min']}분\n"
            f"[지도 보기]({facts['map_url']})")


def _start_change_notice(selection: dict) -> str:
    """Disclose measured relocation without claiming a missing exact option."""
    requested = selection.get("requested_start")
    choices = [selection["primary"], *selection["alternatives"]]
    relocated = [c for c in choices if c.get("is_start_alternative")]
    if not requested or not relocated:
        return ""
    places = ", ".join(dict.fromkeys(c["start"] for c in relocated))
    if selection["requested_start_offered"]:
        exact_places = ", ".join(dict.fromkeys(
            c["start"] for c in choices if not c["is_start_alternative"]))
        return (f"{exact_places} 출발 코스와 {places} 출발 대안을 함께 추천해요. "
                "대안은 요청한 출발 위치와 달라요.")
    if any(c["start"] == requested for c in relocated):
        # Different entrances/preset anchors may share a station name.
        return (f"요청한 지점과 출발 위치가 다른 {places} 코스 "
                f"{len(relocated)}개를 대안으로 추천해요.")
    return (f"{requested} 출발 코스가 아닌, {places} 출발 대안 "
            f"{len(relocated)}개를 추천해요.")


def _plan_final_text(selection: dict) -> str:
    primary = selection["primary"]
    choices = [primary, *selection["alternatives"]]
    if selection.get("recommendation_mode") == "park_catalog":
        places = ", ".join(f"[{markdown_text(c['start'])}]({c['map_url']})" for c in choices)
        body = f"{places}에서 출발하는 코스를 추천해요."
        if notice := selection.get("start_change_notice"):
            body = f"{notice} {body}"
        return f"{body}\n\n{COURSE_EDIT_NOTICE}"
    prefix = "추천 코스예요."
    if not selection["primary_matches_requested_shape"]:
        requested = SHAPES.get(selection["requested_course_type"])
        label = f"{requested.name_ko} 모양" if requested else "요청한 모양"
        prefix = f"첫 추천은 {label} 대신 {primary['shape_label']} 코스예요."
    count = len(choices)
    if count > 1:
        prefix = f"서로 다른 코스 {count}개를 함께 추천해요. {prefix}"
    if notice := selection.get("start_change_notice"):
        prefix = notice + "\n\n" + prefix
    return prefix + "\n\n" + "\n\n".join(_course_summary(c) for c in choices) + f"\n\n{COURSE_EDIT_NOTICE}"


@dataclass(frozen=True)
class CandidateRejection:
    """Sanitized per-candidate evidence kept inside one tool invocation."""

    course: Course
    distance_m: float
    reasons: tuple[str, ...]
    is_start_alternative: bool


_COURSE_TYPE_LABELS = {
    "standard": "일반 러닝 코스", "best_animal": "동물 모양",
    "dog": "강아지 모양", "cat": "고양이 모양",
    "rabbit": "토끼 모양", "whale": "고래 모양",
}
_REASON_LABELS = {
    "distance": "거리", "terrain": "지형", "lighting": "야간 조명",
    "night_animal": "야간 조명", "shape": "모양",
    "start_distance": "출발지", "district": "출발 지역",
}


def _public_course_arguments(request: dict, course_type: str) -> dict:
    keys = ("location", "lat", "lon", "distance_km", "duration_min",
            "strict_distance", "include_hills", "night_mode", "need_facilities",
            "allow_nearby_start")
    arguments = {key: request[key] for key in keys
                 if key in request and request[key] is not None}
    arguments["course_type"] = course_type
    return arguments


def _reason_labels(reasons: set[str]) -> list[str]:
    labels = []
    for reason in sorted(reasons):
        label = LABELS_KO.get(reason) or _REASON_LABELS.get(reason) or reason
        if label not in labels:
            labels.append(label)
    return labels


def _requested_conditions_text(request: dict, course_type: str) -> str:
    labels = []
    if request.get("distance_km") is not None:
        if request.get("strict_distance"):
            tolerance_m = round(max(50.0, request["distance_km"] * 1000 * 0.025))
            labels.append(
                f"정확히 {request['distance_km']:g}km(허용 오차 ±{tolerance_m}m)")
        else:
            labels.append(f"{request['distance_km']:g}km")
    elif request.get("duration_min") is not None:
        labels.append(f"약 {request['duration_min']:g}분")
    if request.get("include_hills") is False:
        labels.append("평지")
    elif request.get("include_hills") is True:
        labels.append("오르막")
    if request.get("night_mode"):
        labels.append("야간 조명")
    labels.extend(LABELS_KO.get(kind, kind)
                  for kind in sorted(set(request.get("need_facilities") or [])))
    labels.append(_COURSE_TYPE_LABELS[course_type])
    return " + ".join(labels)


def _verified_relaxation_options(request: dict, course_type: str) -> list[dict]:
    """Build only options that a real rejected candidate passes after replay."""
    evidence = list(request.get("_candidate_rejections", {}).values())
    evidence.sort(key=lambda item: (
        len(set(item.reasons)), item.distance_m, item.course.length_km,
        encode_course_id(item.course.params)))
    options = []
    seen = set()
    for item in evidence:
        reasons = set(item.reasons)
        arguments = _public_course_arguments(request, course_type)
        changed = []
        copy = []
        changed_to_candidate_start = "start_distance" in reasons
        if changed_to_candidate_start:
            arguments["location"] = item.course.params.location_name
            arguments.pop("lat", None)
            arguments.pop("lon", None)
            arguments["allow_nearby_start"] = False
            changed.extend(["location", "lat", "lon", "allow_nearby_start"])
            copy.append(f"출발지를 {item.course.params.location_name}(으)로 변경")
        elif item.is_start_alternative and not request.get("allow_nearby_start"):
            arguments["allow_nearby_start"] = True
            changed.append("allow_nearby_start")
            copy.append(f"출발지를 {item.course.params.location_name}까지 허용")
        if "distance" in reasons:
            arguments["distance_km"] = round(item.course.length_km, 3)
            arguments.pop("duration_min", None)
            changed.extend(["distance_km", "duration_min"])
            copy.append(f"거리를 {item.course.length_km:.1f}km로 변경")
        if "terrain" in reasons:
            arguments["include_hills"] = not item.course.is_flat
            changed.append("include_hills")
            copy.append("오르막을 허용" if not item.course.is_flat else "평지로 변경")
        if reasons & {"lighting", "night_animal"}:
            arguments["night_mode"] = False
            changed.append("night_mode")
            copy.append("야간 조명 조건 해제")
        missing_facilities = sorted(reasons & set(FACILITY_TYPES))
        if missing_facilities:
            kept = [kind for kind in arguments.get("need_facilities", [])
                    if kind not in missing_facilities]
            if kept:
                arguments["need_facilities"] = kept
            else:
                arguments.pop("need_facilities", None)
            changed.append("need_facilities")
            copy.append("·".join(LABELS_KO.get(kind, kind)
                                 for kind in missing_facilities) + " 조건 해제")
        # Shape/district/radius changes need a separate measured search and are
        # not safe to synthesize from this candidate.
        if reasons & {"shape", "district"} or not changed:
            continue
        trial = {key: value for key, value in arguments.items() if key != "course_type"}
        trial["_resolved"] = (
            (item.course.params.lat, item.course.params.lon,
             item.course.params.location_name)
            if changed_to_candidate_start else request.get("_resolved"))
        match = PresetMatch(item.course, 0 if changed_to_candidate_start else item.distance_m)
        if not _eligible_matches([match], trial, course_type):
            continue
        if (item.is_start_alternative and not changed_to_candidate_start
                and not arguments.get("allow_nearby_start")):
            continue
        signature = tuple(sorted((key, repr(value)) for key, value in arguments.items()))
        if signature in seen:
            continue
        seen.add(signature)
        label = f"{_COURSE_TYPE_LABELS[course_type]} 유지 · " + " · ".join(copy)
        options.append({
            "choice": len(options) + 1,
            "tool": "create_seoul_running_course",
            "label": label,
            "changed_fields": list(dict.fromkeys(changed)),
            "arguments": arguments,
        })
        if len(options) == 3:
            break
    return options


def _failure_result(request: dict, course_type: str) -> CallToolResult:
    start = response_start_name((request.get("_resolved") or (None, None,
                                request.get("location") or "요청한 출발지"))[2])
    evidence = list(request.get("_candidate_rejections", {}).values())
    if not evidence:
        # With no rejected candidate there is nothing to relax, but the
        # catalogue may still hold a build-verified course nearby. Offer that
        # real start instead of ending the turn with no way forward; never
        # name a start that was not measured.
        nearby = None
        resolved = request.get("_resolved")
        if resolved:
            nearby = nearest_start_preset(
                resolved[0], resolved[1],
                requested_distance(request.get("distance_km"),
                                   request.get("duration_min")))
        options = []
        if nearby is not None:
            course, away = nearby
            near_name = course.params.location_name
            text = (
                f"{start}에서는 만족스러운 코스 경로를 생성할 수 없어요.\n"
                f"주변 {near_name}(직선 {round(away):,}m)에서 출발하는 "
                f"{course.length_km:.1f}km 코스는 어떠세요?"
            )
            arguments = _public_course_arguments(request, course_type)
            arguments["location"] = near_name
            arguments.pop("lat", None)
            arguments.pop("lon", None)
            arguments["allow_nearby_start"] = False
            options = [{
                "choice": 1, "tool": "create_seoul_running_course",
                "label": f"출발지를 {near_name}(으)로 변경",
                "changed_fields": ["location", "lat", "lon", "allow_nearby_start"],
                "arguments": arguments,
            }]
            text += f"\n1. {options[0]['label']}"
        else:
            text = (
                f"{start} 출발 요청 조건을 확인했지만 검증 가능한 후보를 확보하지 못했어요. "
                "어떤 조건을 바꾸면 실제 코스가 생기는지 근거가 없어 임의로 제안하지 않을게요."
            )
        text += "\n\n코스가 생성되면 지도 보기를 열어 경로를 직접 편집할 수 있어요."
        result = _speak_verbatim(_mcp_result(text, code="no_candidate_evidence", is_error=True), text)
        result.structuredContent.update(
            requires_confirmation=bool(options), relaxation_options=options,
            confirmation_options=options,
            repeat_tool_call=False,
            requested_start=start, actual_start_names=[])
        return result
    best = min(evidence, key=lambda item: (
        len(set(item.reasons)), item.distance_m, item.course.length_km))
    labels = _reason_labels(set(best.reasons))
    options = _verified_relaxation_options(request, course_type)
    text = (
        f"{start} 출발 기준으로 {_requested_conditions_text(request, course_type)} 조건을 "
        "모두 만족하는 코스는 현재 확인되지 않았어요.\n\n"
        f"가장 가까운 검증 후보는 {'·'.join(labels)} 조건을 충족하지 못했어요."
    )
    if options:
        text += "\n\n원하시면 실제 검증 후보가 있는 조건 변경을 골라 다시 찾아볼게요.\n"
        text += "\n".join(f"{option['choice']}. {option['label']}" for option in options)
    else:
        text += "\n\n후보는 있었지만 안전하게 제안할 조건 변경을 검증하지 못했어요."
    text += ("\n\n조건을 완화해 코스를 찾으면, 지도 보기를 열어 경로를 직접 편집해 "
             "거리와 모양을 조정할 수도 있어요.")
    result = _speak_verbatim(_mcp_result(text, code="constraint_mismatch", is_error=True), text)
    result.structuredContent.update(
        requires_confirmation=bool(options), relaxation_options=options,
        confirmation_options=options,
        repeat_tool_call=False,
        requested_start=start, actual_start_names=[])
    return result


def _timeout_result(request: dict, course_type: str) -> CallToolResult:
    text = (
        "탐색 시간을 넘겨 조건 충족 여부를 확인하지 못했어요. 같은 조건으로 한 번 더 "
        "시도하거나 조건을 바꿔 다시 요청해 주세요.\n\n"
        "코스가 생성되면 지도 보기를 열어 경로를 직접 편집할 수 있어요."
    )
    result = _speak_verbatim(_mcp_result(
        text, code="generation_timeout", is_error=True, retryable=True), text)
    result.structuredContent.update(
        requires_confirmation=False, relaxation_options=[],
        repeat_tool_call=False,
        requested_start=(request.get("_resolved") or (None, None,
                         request.get("location")))[2], actual_start_names=[])
    return result


def _recommendation_shortage(count: int, *, night_mode: bool = False,
                             district: str | None = None,
                             request: dict | None = None,
                             relax: dict | None = None,
                             course_type: str = "standard") -> CallToolResult:
    if request is not None:
        return _failure_result(request, course_type)
    condition = "가로등 데이터가 야간 최소 기준을 충족하는 " if night_mode else "서로 다른 "
    text = f"현재 조건에서 {condition}코스를 찾지 못했어요. 출발지나 거리 조건을 조정해 다시 요청해 주세요."
    if night_mode:
        counts = Counter(_course_district(c) for c in all_verified_animal_presets()
                         if has_sufficient_night_lighting(c.rfs))
        alternatives = sorted((d for d in counts if d and d != district), key=lambda d: (-counts[d], d))[:3]
        text = f"{district + '에서는' if district else '이 출발지에서는'} 조명이 확인된 야간 코스를 찾지 못했어요. 야간 최소 기준에 못 미치거나 조명이 미확인인 코스는 제외했어요."
        if alternatives:
            text += "\n" + ", ".join(alternatives) + " 쪽에는 조명이 확인된 코스가 있어요. 다른 거리·시설 조건도 맞는지는 새 요청에서 확인해요."
    options = []
    if relax and night_mode:
        # Naming other districts is not something the runner can act on. Offer
        # the one change that is measured to help here: drop the night filter.
        arguments = {key: relax[key] for key in
                     ("location", "lat", "lon", "distance_km", "duration_min",
                      "strict_distance", "include_hills", "need_facilities",
                      "course_type") if relax.get(key) is not None}
        arguments["night_mode"] = False
        options = [{"choice": 1, "tool": "create_seoul_running_course",
                    "label": "야간 조명 조건 해제하고 찾기",
                    "changed_fields": ["night_mode"], "arguments": arguments}]
        text += f"\n1. {options[0]['label']}"
    incomplete = _mcp_result(
        text, code="insufficient_courses", is_error=True, retryable=False)
    incomplete.structuredContent.update(
        requested_count=RECOMMENDATION_COUNT, available_count=count,
        requires_confirmation=bool(options), confirmation_options=options,
        relaxation_options=options, repeat_tool_call=False)
    return incomplete


def _complete_recommendation(result: CallToolResult, *, night_mode: bool = False) -> CallToolResult:
    """One call returns up to three eligible routes, including partial bundles."""
    metadata = result.structuredContent or {}
    if result.isError or metadata.get("result_code") not in {"course_ready", "nearby_course_ready"}:
        return result
    count = metadata.get("course_selection", {}).get("returned_count", 0)
    if 1 <= count <= RECOMMENDATION_COUNT:
        return result
    return _recommendation_shortage(count, night_mode=night_mode)


def _start_change_question(plan: CoursePlan, course_type: str, request: dict) -> CallToolResult:
    """Return no route payload until the user chooses a change of start/type."""
    start = response_start_name(plan.requested_start)
    label = {"dog": "강아지", "cat": "고양이", "rabbit": "토끼", "whale": "고래",
             "best_animal": "동물 모양", "standard": "일반 러닝"}[course_type]
    text = (f"{start}에서 출발하는 요청 조건의 {label} 코스를 찾지 못했어요.\n"
            f"1. 가까운 출발지의 {label} 코스를 알려드릴까요?")
    common = {key: request[key] for key in ("location", "lat", "lon", "distance_km",
              "duration_min", "strict_distance", "include_hills", "night_mode", "need_facilities")
              if key in request and request[key] is not None}
    options = [dict(choice=1, tool="create_seoul_running_course",
                    arguments=dict(common, course_type=course_type, allow_nearby_start=True))]
    if course_type != "standard":
        text += f"\n2. 아니면 {start}에서 출발하는 일반 러닝 코스를 찾아드릴까요?"
        options.append(dict(choice=2, tool="create_seoul_running_course",
                            arguments=dict(common, course_type="standard", allow_nearby_start=False)))
    text += "\n선택해 주시면 기존 거리·지형·시설 조건을 유지해 찾아드릴게요."
    result = _speak_verbatim(
        _mcp_result(text, code="start_change_confirmation_required"), text)
    result.structuredContent.update(requires_confirmation=True, conditions_satisfied=False,
        confirmation_options=options, next_action="Wait for the user's explicit choice. Do not call again in this turn. "
        "For an ambiguous yes to two options, ask which option. Preserve the original conditions.")
    return result


def _planned_course_result(text: str, *, course_type: str, request: dict,
                           timeout_s: float) -> CallToolResult | None:
    """Answer any course request with consistently ranked candidates, or None."""
    if course_type not in {*SHAPES, "best_animal", "standard"}:
        return None
    # An animal timeout may still leave the reserved local-candidate budget.
    # Only skip when that budget is gone. A course request is answered
    # with a card whenever a route can be built, and "this distance draws a
    # poor silhouette" is a request we can still answer with real courses.
    # When nothing can be built the plan returns None and the copy stands.
    if (text.startswith("⏱️") and not _extract_course_ids(text)
            and timeout_s < PLAIN_OPTION_MIN_BUDGET_S):
        return None
    plan = _animal_course_plan(request, course_type, text, timeout_s)
    if plan is None:
        return None
    return _result_from_course_plan(plan, course_type, request)


def _result_from_course_plan(plan: CoursePlan, course_type: str,
                             request: dict) -> CallToolResult:
    """Apply start-consent rules to one already measured course plan."""
    if plan.requested_start and request.get("allow_nearby_start") is not True:
        exact = [choice for choice in (plan.primary, *plan.alternatives) if not choice.is_detour]
        if not exact:
            return _start_change_question(plan, course_type, request)
        # Never fill unused card slots with an unapproved change of origin.
        if len(exact) != 1 + len(plan.alternatives):
            effort = " (기본 5km)" if course_type == "standard" and not requested_distance(
                request.get("distance_km"), request.get("duration_min")) else ""
            plan = replace(plan, primary=exact[0], alternatives=tuple(exact[1:]), case=CASE_EXACT,
                           lead=f"{response_start_name(plan.requested_start)} 출발 코스 {len(exact)}개{effort}를 추천해요.")
    return _plan_result(plan, course_type)


def _plan_result(plan: CoursePlan, course_type: str) -> CallToolResult:
    # The plan owns one short spoken sentence for every case. Generator copy
    # can contain scoring rationale that belongs on the detail page, not in a
    # concise chat handoff beside the widget.
    lead = plan.lead
    selection = _course_selection(
        [dict(course_response_facts(c.course, c.course_id, BASE_URL),
              # Straight-line displacement, not a walking distance estimate.
              start_offset_m=round(c.distance_m),
              is_start_alternative=bool(plan.requested_start) and c.is_detour)
         for c in (plan.primary, *plan.alternatives)], course_type)
    selection["recommendation_mode"] = plan.case
    selection["requested_start"] = (
        response_start_name(plan.requested_start) if plan.requested_start else None)
    selection["requested_start_offered"] = (
        any(not c.is_detour for c in (plan.primary, *plan.alternatives))
        if plan.requested_start else None)
    selection["start_change_notice"] = _start_change_notice(selection)
    final_text = _plan_final_text(selection)
    widget = (_plan_widget(plan, start_notice=selection["start_change_notice"])
              if KAKAO_WIDGETS_ENABLED else None)
    if widget is None:
        # Never return the original generator's requested-animal copy once a
        # different plan was selected. Keep every actual choice and map link.
        lines = [lead, "추천 코스", _course_summary(selection["primary"])]
        if selection["start_change_notice"]:
            lines.insert(0, selection["start_change_notice"])
        if selection["alternatives"]:
            lines.extend(["다른 코스도 있어요", *[
                _course_summary(c) for c in selection["alternatives"]]])
        lines.append(COURSE_EDIT_NOTICE)
        return _mcp_result(
            "\n\n".join(lines),
            code="nearby_course_ready" if plan.primary.is_detour else "course_ready",
            assistant_text=lead, assistant_final_text=final_text,
            course_selection=selection,
        )
    return _mcp_result(
        widget, code=("nearby_course_ready" if plan.primary.is_detour else "course_ready"),
        assistant_text=lead, assistant_text_in_widget=False,
        assistant_final_text=final_text, course_selection=selection,
    )


def _park_course_result(request: dict, course_type: str = "standard") -> CallToolResult:
    """Serve up to three eligible catalogue destinations, without routing."""
    location = request.get("location")
    district = request.get("_district")
    has_coordinates = request.get("lat") is not None or request.get("lon") is not None
    # A theme is not a secretly defaulted departure at 여의도.
    generic = {"공원", "한강", "한강공원", "강변", "하천", "물", "물가", "수변", "호수", "서울"}
    named_start = bool(location and location.strip() and not district
                       and not is_citywide_scope(location) and location.replace(" ", "") not in generic)
    origin = None
    origin_name = None
    if named_start or has_coordinates:
        try:
            lat, lon, origin_name = _resolve_start(
                location, request.get("lat"), request.get("lon"), timeout_s=ADDRESS_TRY_BUDGET_S)
            origin = (lat, lon)
        except CourseError as exc:
            code = "invalid_coordinates" if "서울 지역 좌표만" in str(exc) else "location_not_found"
            return _mcp_result(f"⚠️ {markdown_text(str(exc))}", code=code, is_error=True)
    distance = request.get("distance_km")
    duration = request.get("duration_min")
    if ((distance is not None and not 1 <= distance <= 42.195)
            or (duration is not None and not 10 <= duration <= 360)):
        return _mcp_result("거리는 1~42.195km, 시간은 10~360분 범위로 알려주세요.",
                           code="invalid_request", is_error=True)
    night = bool(request.get("night_mode"))
    try:
        catalogue = list(park_courses())
        if district:
            catalogue = [(s, c) for s, c in catalogue if s.district == district]
        # Park is the destination category, not a request for a nearby POI.
        # Other explicitly requested facilities still need actual 10m hits.
        park_request = {**request, "need_facilities": [k for k in request.get("need_facilities", []) if k != "park"]}
        eligible = _eligible_matches([PresetMatch(c, 0) for _, c in catalogue], park_request,
                                     "standard" if course_type == "best_animal" else course_type)
        for key in ("_stats", "_seen_candidates"):
            request[key] = park_request[key]
        ids = {encode_course_id(m.course.params) for m in eligible}
        candidates = [(s, c) for s, c in catalogue if encode_course_id(c.params) in ids]
        if district:
            # District results are deterministic; the no-start legacy park
            # catalogue alone retains its existing random destination picks.
            selected = sorted(candidates, key=lambda pair: pair[0].id)[:RECOMMENDATION_COUNT]
        else:
            selected = select_park_courses(origin, night_mode=night, candidates=candidates)
    except (CourseError, OSError, ValueError, RuntimeError, KeyError) as exc:
        log.warning("park catalogue unavailable: %s", exc)
        return _mcp_result("등록된 공원·강변 코스 데이터를 확인하지 못했어요. 잠시 후 다시 요청해 주세요.",
                           code="park_catalog_unavailable", is_error=True, retryable=False)
    if not selected:
        return _recommendation_shortage(len(selected), night_mode=night, district=district)
    choices = []
    destinations = []
    for spot, template in selected:
        # Recompute facts for the requested night profile; never change the
        # stored path or overwrite the catalogue's measured lighting values.
        course = course_from_path(template.params.model_copy(update={"night_mode": night}), template.path)
        cid = encode_course_id(course.params)
        _cache_put(cid, course)
        moved = haversine_m(*origin, course.params.lat, course.params.lon) if origin else 0.0
        choices.append(CourseChoice(course, cid, "standard", moved))
        destinations.append({"id": spot.id, "name": spot.name,
                             "origin_distance_m": round(moved) if origin else None,
                             "source_url": spot.source_url})
    lead = "각 공원·강변에서 출발하는 코스를 추천해요."
    if origin and destinations:
        # State the distance from the start the runner named. The host read
        # "각 공원에서 출발하는" as a course from 이촌역 and said so, while the
        # course actually began at 여의도한강공원 3.9km away.
        farthest = max(d["origin_distance_m"] or 0 for d in destinations)
        start_label = response_start_name(origin_name or location)
        lead = (f"요청 출발지 {start_label} · 실제 코스 출발지는 아래 공원·강변이며 "
                f"{start_label}에서 직선 최대 {round(farthest):,}m 떨어져 있어요.")
    if night:
        lead += " 야간 조명이 많은 코스만 포함했어요."
    # A named start must be compared against, or the host reads the widget and
    # says the course begins where the runner asked. Only a themed request with
    # no start of its own keeps None here.
    result = _plan_result(
        CoursePlan("park_catalog", lead, choices[0], tuple(choices[1:]),
                   # Name the start the runner typed. Geocoding 성수동 to
                   # 서울숲카페거리 and disclosing *that* gave the host a place the
                   # user never said, so it discarded the sentence and wrote
                   # "성수동에서 추천해봤어요" over courses at 반포한강공원.
                   requested_start=(location or origin_name) if origin else None),
        course_type)
    result.structuredContent["park_selection"] = {
        "mode": "district" if district else "nearest" if origin else "random", "distance_basis": "straight_line_to_course_start",
        "origin": {"name": origin_name, "lat": origin[0], "lon": origin[1]} if origin else None,
        "catalog_size": len(PARK_SPOTS), "destinations": destinations,
        "requested_location": location,
        "origin_role": "search_reference_only",
        "all_routes_start_at_origin": False,
    }
    return _complete_recommendation(result, night_mode=night)


StartScope = Literal["missing", "district", "specific", "invalid_coordinates"]


def _classify_start_scope(request: dict) -> StartScope:
    if request.get("lat") is not None and request.get("lon") is not None:
        return "specific"
    if request.get("lat") is not None or request.get("lon") is not None:
        return "invalid_coordinates"
    location = request.get("location")
    if not location or not location.strip() or is_citywide_scope(location):
        return "missing"
    return "district" if district_scope(location) else "specific"


def _course_district(course: Course) -> str | None:
    for spot in PARK_SPOTS:
        if course.params.location_name == spot.name and course.params.manual_path:
            return spot.district
    return STATION_DISTRICTS.get((round(course.params.lat, 5), round(course.params.lon, 5)))


def _eligible_matches(matches: list[PresetMatch], request: dict, course_type: str,
                      *, allow_animal_alternatives: bool = False) -> list[PresetMatch]:
    """Hard gates at the recommendation boundary; never mutate preset IDs."""
    target = requested_distance(request.get("distance_km"), request.get("duration_min"))
    district = request.get("_district")
    stats = request.setdefault("_stats", {"candidate_count": 0, "eligible_count": 0,
                                          "rejection_counts": Counter()})
    seen = request.setdefault("_seen_candidates", set())
    rejected = request.setdefault("_candidate_rejections", {})
    eligible = []
    for match in matches:
        course = match.course
        if request.get("_resolved"):
            lat, lon, _ = request["_resolved"]
            match = PresetMatch(course, haversine_m(lat, lon, course.params.lat, course.params.lon))
        reasons = []
        if district and _course_district(course) != district:
            reasons.append("district")
        if match.distance_m > NEARBY_RADIUS_M:
            reasons.append("start_distance")
        tolerance_km = (max(0.05, target * 0.025)
                        if target and request.get("strict_distance")
                        else target * EFFORT_TOLERANCE if target else 0.0)
        if target and abs(course.length_km - target) > tolerance_km + 1e-9:
            reasons.append("distance")
        hills = request.get("include_hills")
        if hills is not None and course.is_flat == hills:
            reasons.append("terrain")
        if request.get("night_mode") and not has_sufficient_night_lighting(course.rfs):
            reasons.append("lighting")
        # A daytime animal preset cannot acquire night parameters: its URL
        # would regenerate a different route after cache eviction/restart.
        if request.get("night_mode") and course.params.shape and not course.params.night_mode:
            reasons.append("night_animal")
        actual = course.params.shape or "standard"
        if (course_type == "best_animal" and actual == "standard"
                or course_type != "best_animal" and actual != course_type
                and not (course_type == "standard" and allow_animal_alternatives)):
            reasons.append("shape")
        facilities = sorted(set(request.get("need_facilities") or []))
        if facilities:
            # No result-list truncation: the 25th restroom must not hide the
            # first drinking fountain. Use the same road geometry as the map.
            found = {f["type"] for f in facilities_along(route_points(course), facilities, limit=None)}
            reasons.extend(kind for kind in facilities if kind not in found)
        cid = encode_course_id(course.params)
        if cid not in seen:
            seen.add(cid)
            stats["candidate_count"] += 1
            stats["rejection_counts"].update(reasons)
            stats["eligible_count"] += int(not reasons)
            if reasons:
                rejected[cid] = CandidateRejection(
                    course=course,
                    distance_m=match.distance_m,
                    reasons=tuple(sorted(set(reasons))),
                    is_start_alternative=(
                        bool(request.get("_resolved"))
                        and match.distance_m >= SAME_START_M
                    ),
                )
        if not reasons:
            eligible.append(match)
    return eligible


def _verified_animal_fast_result(request: dict, course_type: str,
                                 timeout_s: float) -> CallToolResult | None:
    """Serve or reject named-animal requests from local verified evidence."""
    if course_type not in SHAPES:
        return None
    before = request.get("_stats", {}).get("candidate_count", 0)
    plan = _animal_course_plan(request, course_type, "", timeout_s)
    if plan is not None:
        return _result_from_course_plan(plan, course_type, request)
    after = request.get("_stats", {}).get("candidate_count", 0)
    if after > before:
        return _failure_result(request, course_type)
    return None


def _district_course_result(request: dict, course_type: str) -> CallToolResult:
    district = request["_district"]
    pool = [c for c in all_verified_animal_presets() if _course_district(c) == district]
    night = bool(request.get("night_mode"))
    if course_type != "standard" and not night:
        eligible = _eligible_matches([PresetMatch(c, 0) for c in pool], request, course_type)
        eligible.sort(key=lambda m: (-(m.course.shape_similarity or 0),
                                     m.course.length_km, encode_course_id(m.course.params)))
        choices = []
        seen = set()
        for match in eligible:
            course = match.course
            signature = route_signature(course)
            name = course.params.location_name
            if signature in seen or name in seen:
                continue
            seen.update((signature, name))
            cid = encode_course_id(course.params)
            _cache_put(cid, course)
            choices.append(CourseChoice(course, cid, KIND_REQUESTED))
            if len(choices) == RECOMMENDATION_COUNT:
                break
        if not choices:
            # Without the request there is no rejected-candidate evidence, so a
            # district ask ended on "출발지나 거리 조건을 조정해 다시 요청해 주세요"
            # with nothing to act on.
            return _recommendation_shortage(0, district=district, request=request,
                                            course_type=course_type)
        return _plan_result(CoursePlan("district", f"{district} 안의 출발지에서 조건에 맞는 코스를 골랐어요.",
                                       choices[0], tuple(choices[1:])), course_type)
    stations = DISTRICT_STATIONS[district]
    if night:
        lit_points = Counter((round(c.params.lat, 5), round(c.params.lon, 5))
                             for c in pool if has_sufficient_night_lighting(c.rfs))
        stations = [s for s in stations if (round(s[0], 5), round(s[1], 5)) in lit_points]
        stations = sorted(stations, key=lambda s: (-lit_points[(round(s[0], 5), round(s[1], 5))], s[2], s[:2]))
    else:
        stations = sorted(stations, key=lambda s: (s[2], s[:2]))
    if not stations:
        return _recommendation_shortage(0, night_mode=night, district=district,
                                        relax=dict(request, course_type=course_type))
    # ponytail: one deterministic station, no cross-district retries; expand
    # only after measured coverage justifies spending another routing budget.
    lat, lon, name = stations[0]
    request.update(location=name, lat=lat, lon=lon, _resolved=(lat, lon, name))
    return _specific_course_result(request, course_type)


def _specific_course_result(request: dict, course_type: str) -> CallToolResult:
    started = time.monotonic()
    try:
        lat, lon, name = request.get("_resolved") or _resolve_start(
            request.get("location"), request.get("lat"), request.get("lon"),
            timeout_s=ADDRESS_TRY_BUDGET_S)
    except CourseError as exc:
        return _course_tool_result(f"⚠️ {markdown_text(str(exc))}", course_type=course_type, request=request)
    request.update(_resolved=(lat, lon, name))
    fast = _verified_animal_fast_result(
        request, course_type, timeout_s=_budget_left(started))
    if fast is not None:
        return fast
    common = {k: request.get(k) for k in ("location", "lat", "lon", "distance_km",
              "duration_min", "include_hills", "night_mode", "need_facilities")}
    common["include_hills"] = bool(common["include_hills"])
    common["night_mode"] = bool(common["night_mode"])
    common.update(location=name, lat=lat, lon=lon)
    text = (generate_running_course(**common) if course_type == "standard" else
            _animal_text_for_cards(shape=None if course_type == "best_animal" else course_type, **common))
    return _course_tool_result(text, course_type=course_type, request=request,
                               timeout_s=_budget_left(started))


def _dispatch_course_request(course_type: str, request: dict) -> CallToolResult:
    """All three public tool names share validation, selection and telemetry."""
    started = time.monotonic()
    request = dict(request)
    scope = _classify_start_scope(request)
    district = district_scope(request.get("location")) if scope == "district" else None
    request["_district"] = district
    valid_effort = all(value is None or isinstance(value, (int, float)) and not isinstance(value, bool)
                       and math.isfinite(value) and lo <= value <= hi
                       for value, lo, hi in ((request.get("distance_km"), 1, 42.195),
                                            (request.get("duration_min"), 10, 360)))
    result = None
    try:
        if scope == "invalid_coordinates":
            result = _mcp_result("위도와 경도를 함께 알려주세요.", code="invalid_coordinates", is_error=True)
        elif course_type not in {*SHAPES, "standard", "best_animal"}:
            result = _mcp_result("일반 코스 또는 강아지·고양이·토끼·고래를 골라 주세요.", code="invalid_request", is_error=True)
        elif not valid_effort:
            result = _mcp_result("거리는 1~42.195km, 시간은 10~360분 범위로 알려주세요.", code="invalid_request", is_error=True)
        elif set(request.get("need_facilities") or []) - set(FACILITY_TYPES):
            result = _mcp_result("편의점·화장실·음수대·공원 중 필요한 시설을 알려주세요.", code="invalid_request", is_error=True)
        elif "park" in (request.get("need_facilities") or []):
            result = _park_course_result(request, course_type)
        elif scope == "missing":
            text = "어디에서 출발할지 알려주세요.\n구 이름(강남구), 역 이름(성수역), 가게 이름, 도로명·지번 주소 모두 좋아요."
            if is_citywide_scope(request.get("location")):
                text += "\n서울 전체에서 고르기보다 조금 좁혀 주시면 실제로 갈 수 있는 코스를 찾아드릴 수 있어요. 구 이름만 알려주셔도 충분해요."
            result = _mcp_result(text, code="missing_start", is_error=True)
        elif district:
            result = _district_course_result(request, course_type)
        else:
            result = _specific_course_result(request, course_type)
        stats = request.get("_stats", {"candidate_count": 0, "eligible_count": 0, "rejection_counts": {}})
        conditions = {"course_type": course_type,
                      "distance_km": requested_distance(request.get("distance_km"), request.get("duration_min")) if valid_effort else None,
                      "strict_distance": bool(request.get("strict_distance")),
                      "terrain": None if request.get("include_hills") is None else "hills" if request["include_hills"] else "flat",
                      "facilities": request.get("need_facilities") or [], "night_mode": bool(request.get("night_mode"))}
        result.structuredContent.update(release_sha=RELEASE_SHA, start_scope=scope, district=district,
            **stats, conditions_requested=conditions, conditions_satisfied=(not result.isError and
                result.structuredContent.get("result_code") in {"course_ready", "nearby_course_ready"}),
            unmet_conditions=sorted(stats["rejection_counts"]) if result.isError else [])
        return result
    finally:
        stats = request.get("_stats", {})
        log.info("course_selection release_sha=%s start_scope=%s district=%s candidate_count=%d eligible_count=%d rejection_counts=%s result_code=%s duration_ms=%d",
                 RELEASE_SHA, scope, district, stats.get("candidate_count", 0), stats.get("eligible_count", 0),
                 dict(stats.get("rejection_counts", {})), (result.structuredContent or {}).get("result_code") if result else "internal_error",
                 round((time.monotonic() - started) * 1000))


def _course_tool_result(text: str, *, course_type: str,
                        request: dict | None = None,
                        timeout_s: float = 0.0) -> CallToolResult:
    """Classify controlled tool copy into a stable MCP result contract."""
    # Input/location failures must not start another generation in the planner.
    if text.startswith("⚠️"):
        if "위치를 찾지 못" in text or "출발 위치가 필요" in text:
            return _mcp_result(text, code="location_not_found", is_error=True)
        if "서울 지역 좌표만" in text or "위도와 경도" in text:
            return _mcp_result(text, code="invalid_coordinates", is_error=True)
        if ("거리는 1km에서 42.195km" in text or "시간은 10~360분" in text
                # The duration ceiling explains itself in its own words; match
                # the fact, not one phrasing, so it is not read as a shortage.
                or "분까지 가능해요" in text):
            return _mcp_result(text, code="invalid_request", is_error=True)
        if "지도 검색 서비스" in text:
            return _mcp_result(text, code="location_unavailable", is_error=True, retryable=True)
    if request is not None:
        planned = _planned_course_result(
            text, course_type=course_type, request=request, timeout_s=timeout_s)
        if planned is not None:
            return _complete_recommendation(planned, night_mode=bool(request.get("night_mode")))
    # Whether a course came back decides success, not the leading emoji. The
    # prefix convention is still how failures tell themselves apart, but a
    # course request is answered with a card whenever a route exists, and one
    # note wearing the wrong prefix must not be able to hide it again.
    if text.startswith("⏱️") and not _extract_course_ids(text):
        return (_timeout_result(request, course_type) if request is not None
                else _mcp_result(text, code="generation_timeout",
                                 is_error=True, retryable=True))
    if request is not None:
        return _recommendation_shortage(0, night_mode=bool(request.get("night_mode")),
                                        district=request.get("_district"), request=request,
                                        course_type=course_type)
    if text.startswith("🔎"):
        code = "nearby_course_ready" if "/c/" in text else "exact_shape_unavailable"
        result = _mcp_result(text, code=code)
        if request is not None:
            return _complete_recommendation(result, night_mode=bool(request.get("night_mode")))
        return result
    if text.startswith("⚠️"):
        if "위치를 찾지 못" in text or "출발 위치가 필요" in text:
            return _mcp_result(text, code="location_not_found", is_error=True)
        # A short-distance animal request returns a useful verified alternative,
        # not an infrastructure failure.
        if "추천 거리" in text:
            return _mcp_result(text, code="exact_shape_unavailable")
        return _recommendation_shortage(0)
    cached_result = _try_course_result(text, course_type)
    if cached_result is not None:
        if request is not None:
            return _complete_recommendation(cached_result, night_mode=bool(request.get("night_mode")))
        return cached_result
    if request is not None:
        return _recommendation_shortage(
            0, night_mode=bool(request.get("night_mode")), request=request,
            course_type=course_type)
    return _mcp_result(text, code="course_ready")


def _get_course(params: CourseParams, timeout_s: float | None = None) -> Course:
    cid = encode_course_id(params)
    with _CACHE_LOCK:
        hit = _course_cache.get(cid)
        if isinstance(hit, Course):
            try:
                ensure_course_runnable(hit)
            except CourseAccessError as exc:
                _course_cache[cid] = exc
                raise
            return hit
        if isinstance(hit, CourseError):
            raise hit
        pending = _course_inflight.get(cid)
        leader = pending is None
        if leader:
            pending = concurrent.futures.Future()
            _course_inflight[cid] = pending

    if not leader:
        try:
            return pending.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as exc:
            raise _GenerationTimeout from exc

    try:
        if animal_preset_is_blocked(params):
            raise CourseAccessError()
        # Course ids encode parameters, not the routed node path. After a
        # process restart a detail URL therefore has no hot-cache entry.
        # Restore the build-verified station preset before generating.
        preset = get_animal_preset(params)
        if isinstance(preset, Course):
            course = preset
        elif (standard := get_standard_preset(params)) is not None:
            # Build-time outcome for these exact parameters. Generation is
            # deterministic and budget-independent, so this is the same result
            # a live search would reach -- only the wait is removed.
            if isinstance(standard, CourseError):
                raise standard
            course = standard
        else:
            try:
                course = _offload(
                    generate_shape_course if params.shape else generate_course,
                    params, timeout_s=timeout_s)
            except CourseError as exc:
                if params.shape in SHAPES and timeout_s is None:
                    recovered = _offload(find_min_clean_course, params)
                    if recovered is not None:
                        course = recovered
                    else:
                        raise
                else:
                    raise
        _cache_put(cid, course)
        pending.set_result(course)
        return course
    except BaseException as exc:
        if isinstance(exc, CourseError):
            _cache_put(cid, exc)
        pending.set_exception(exc)
        raise
    finally:
        with _CACHE_LOCK:
            if _course_inflight.get(cid) is pending:
                _course_inflight.pop(cid, None)


def _cache_put(cid: str, value) -> None:
    if isinstance(value, Course):
        ensure_course_runnable(value)
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
    ensure_course_runnable(course)
    with _CACHE_LOCK:
        if len(_animal_recommendation_cache) >= _CACHE_MAX:
            _animal_recommendation_cache.pop(next(iter(_animal_recommendation_cache)))
        params = requested_params or course.params
        _animal_recommendation_cache[_animal_recommendation_key(params)] = course


def _get_animal_recommendation(params: CourseParams) -> Course | None:
    key = _animal_recommendation_key(params)
    with _CACHE_LOCK:
        course = _animal_recommendation_cache.get(key)
    if course is not None:
        try:
            ensure_course_runnable(course)
        except CourseAccessError:
            with _CACHE_LOCK:
                if _animal_recommendation_cache.get(key) is course:
                    _animal_recommendation_cache.pop(key, None)
            return None
    return course


def _resolve_start(location, lat, lon, timeout_s=None):
    if lat is not None and lon is not None:
        try:
            CourseWaypoint(lat=lat, lon=lon)
        except ValidationError as exc:
            raise CourseError("서울 지역 좌표만 지원해요. 위도와 경도를 확인해 주세요.") from exc
        return lat, lon, location or "지정한 좌표"
    return resolve_location(location, lat, lon, timeout_s=timeout_s)


def _build_params(location, lat, lon, distance_km, duration_min, include_hills,
                  night_mode, need_facilities, shape=None,
                  timeout_s: float | None = None) -> tuple[CourseParams, str]:
    """Returns (params, note). note explains any interpretation we made
    (e.g. duration→distance conversion) so the user sees the reasoning."""
    note = ""
    rlat, rlon, name = _resolve_start(location, lat, lon, timeout_s=timeout_s)
    asked_by_duration = distance_km is None and bool(duration_min)
    if distance_km is None and duration_min:
        distance_km = round(duration_min / DEFAULT_PACE_MIN_PER_KM, 1)
        # 🕒, not ⏱️. The ⚠️/⏱️ prefixes mark failures, and the result
        # classifier reads them as such — dressing a reading of what the user
        # asked for in the timeout prefix made a working course come back as
        # isError with no card, on the commonest phrasing there is.
        note = f"🕒 {duration_min:g}분 → 6:30/km 페이스 기준 약 {distance_km:g}km로 잡았어요.\n"
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
        if duration_min and asked_by_duration:
            # 360분 is inside the stated minute range but converts to 55km, so
            # the generic sentence refused a value it had just called valid.
            cap = int(42.195 * DEFAULT_PACE_MIN_PER_KM)
            raise CourseError(
                f"{duration_min:g}분은 6:30/km 기준 약 {distance_km:g}km예요. "
                f"코스는 42.195km까지 만들 수 있어서 시간으로는 약 {cap}분까지 가능해요. "
                f"{cap}분 이하로 다시 알려주세요."
            ) from exc
        raise CourseError(
            "거리는 1km에서 42.195km 사이로 알려주세요. "
            "시간으로 요청하실 때는 10분에서 360분 사이가 가능해요."
        ) from exc
    return params, note


def _run(params: CourseParams, note: str = "",
         timeout_s: float | None = None,
         asked_minutes: float | None = None) -> str:
    try:
        course = _get_course(params, timeout_s=timeout_s)
        if params.night_mode and not has_sufficient_night_lighting(course.rfs):
            raise CourseError("가로등이 충분한지 확인되지 않아 야간 코스로 추천할 수 없어요. "
                              "출발지나 거리를 바꿔 다시 요청해 주세요.")
        facs = facilities_along(route_points(course), params.need_facilities or None)
        return note + course_markdown(course, BASE_URL, facs)
    except DistanceMissError as e:
        # Answer in the unit the runner used. Someone who said "60분" never
        # mentioned kilometres, and "목표 9.2km를 찾지 못했어요" hands them a
        # number they did not give and a conversion to do themselves.
        if asked_minutes:
            nearest_min = round(e.nearest_km * DEFAULT_PACE_MIN_PER_KM)
            return (f"⚠️ {asked_minutes:g}분 정도로 뛸 만한 코스를 이 출발지에서 "
                    f"찾지 못했어요 (가장 가까운 코스가 약 {nearest_min}분). "
                    "시간을 조금 바꿔서 다시 알려주세요.")
        return f"⚠️ {e}"
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
                   timeout_s: float | None = None, *, include_hills: bool = False,
                   night_mode: bool = False, need_facilities: list[str] | None = None) -> str:
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
                          distance_km=SHAPES[key].min_km, shape=key,
                          include_hills=include_hills, night_mode=night_mode,
                          need_facilities=need_facilities or [])
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
    location: Annotated[str | None, Field(description=LOCATION_FIELD_EN)] = None,
    # Ranges live in the descriptions, not as ge/le: a schema rejection happens
    # before the tool body and surfaces a raw pydantic dump instead of the
    # Korean guidance the user needs (e.g. asking for a course in Busan).
    lat: Annotated[float | None, Field(description="Start latitude (alternative to location). Seoul only: 37.4-37.72")] = None,
    lon: Annotated[float | None, Field(description="Start longitude (alternative to location). Seoul only: 126.76-127.19")] = None,
    distance_km: Annotated[float | None, Field(description="Target distance in km, 1-42.195")] = None,
    duration_min: Annotated[float | None, Field(description="Target duration in minutes, 10-360; converted to distance at 6:30/km if distance_km is absent")] = None,
    include_hills: Annotated[bool, Field(description="True to include uphill training segments (3-8% grade); False prefers flat routes")] = False,
    night_mode: Annotated[bool, Field(description="Night runs require measured lighting >=0.4; CCTV remains a preference")] = False,
    need_facilities: Annotated[list[str] | None, Field(description=(
        "Facility types the course should pass: convenience_store, restroom, "
        "water, park. Pass park only when the user explicitly asks to run in "
        "or through a park, riverside park, or green trail."
    ))] = None,
) -> str:
    """Generates a loop running course in Seoul from Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!), snapped to
    real pedestrian roads and scored with the Running Friendliness Score built
    from Seoul open data (sidewalk width, slope, lighting, safety CCTV, parks).
    Safe, runner-friendly streets are preferred by default. Provide a start
    location (place name or lat/lon) and a target distance or duration.
    The MCP tool returns UP TO THREE distinct courses in ONE call. Call it only ONCE,
    never once per course, including requests for 코스 3개. This is
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
    # The ask was a duration only when no distance came with it; that is the
    # same condition _build_params used to convert one into the other.
    asked_minutes = duration_min if distance_km is None else None
    return _run(params, note, timeout_s=remaining(),
                asked_minutes=asked_minutes)


def generate_animal_course(
    shape: Annotated[str | None, Field(description="Animal shape key: cat, dog, rabbit, whale")] = None,
    location: Annotated[str | None, Field(description=LOCATION_FIELD_EN)] = None,
    lat: Annotated[float | None, Field(description="Start latitude (alternative to location). Seoul only: 37.4-37.72")] = None,
    lon: Annotated[float | None, Field(description="Start longitude (alternative to location). Seoul only: 126.76-127.19")] = None,
    distance_km: Annotated[float | None, Field(description="Target distance in km, 1-42.195")] = None,
    duration_min: Annotated[float | None, Field(description="Target duration in minutes, 10-360")] = None,
    include_hills: Annotated[bool, Field(description="Include uphill segments")] = False,
    night_mode: Annotated[bool, Field(description="Allow ordinary lighting; exclude dark or unknown-lighting routes")] = False,
    need_facilities: Annotated[list[str] | None, Field(description=(
        "Facility types to pass by. Pass park only for an explicit park, "
        "riverside park, or green-trail running request."
    ))] = None,
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
    for questions about facilities near an existing course. One MCP call returns THREE courses together; call only ONCE per recommendation, never once per course."""
    started = time.monotonic()

    def remaining() -> float:
        now = time.monotonic()
        deadline = _ANIMAL_CARD_DEADLINE.get()
        budget = ANIMAL_RESPONSE_BUDGET_S - (now - started)
        return max(0.01, min(budget, deadline - now) if deadline is not None else budget)

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
            rlat, rlon, name = _resolve_start(
                location, lat, lon, timeout_s=remaining())
        except CourseError as e:
            return f"⚠️ {e}"
        return _animal_survey(rlat, rlon, name, requested_distance(distance_km, duration_min),
                              timeout_s=remaining(), include_hills=include_hills,
                              night_mode=night_mode, need_facilities=need_facilities)
    if distance_km is None and duration_min is None and shape in SHAPES:
        # Shape chosen, distance left open → quality-first: draw at the
        # shortest distance where the silhouette completes cleanly.
        try:
            rlat, rlon, name = _resolve_start(
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


def _animal_text_for_cards(*args, **kwargs) -> str:
    """Leave a bounded generation slot for the local plain-course option."""
    if not KAKAO_WIDGETS_ENABLED:
        return generate_animal_course(*args, **kwargs)
    reserve = PLAIN_OPTION_MIN_BUDGET_S + 0.35
    token = _ANIMAL_CARD_DEADLINE.set(
        time.monotonic() + MCP_OUTER_RESPONSE_BUDGET_S - reserve)
    try:
        return generate_animal_course(*args, **kwargs)
    finally:
        _ANIMAL_CARD_DEADLINE.reset(token)


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
    location: Annotated[str | None, Field(description=LOCATION_FIELD_KO)] = None,
    lat: Annotated[float | None, Field(description=(
        "Start latitude instead of location; provide together with lon. Seoul only: 37.4-37.72"
    ))] = None,
    lon: Annotated[float | None, Field(description=(
        "Start longitude instead of location; provide together with lat. Seoul only: 126.76-127.19"
    ))] = None,
    distance_km: Annotated[float | None, Field(description=(
        "사용자가 명시한 목표 거리(km), 1-42.195. 생략 시 standard는 기본 5km, "
        "동물 코스는 가장 선명한 검증 거리를 서버가 선택합니다. "
        "park 추천도 등록된 고정 경로 중 요청 거리 ±10%를 만족할 때만 반환합니다."
    ))] = None,
    strict_distance: Annotated[bool, Field(description=(
        "사용자가 거리를 '정확히' 또는 '딱'이라고 표현했을 때만 true. "
        "허용 오차는 max(50m, 목표 거리의 2.5%)이며, 일반 거리 요청은 false."
    ))] = False,
    duration_min: Annotated[float | None, Field(description=(
        "사용자가 명시한 목표 시간(분), 10-360. 말하지 않았으면 생략하며, "
        "distance_km와 함께 있으면 거리를 우선합니다."
    ))] = None,
    include_hills: Annotated[bool | None, Field(description=(
        "오르막·언덕·업힐 요청은 true, 명시적인 평지 요청은 false, 지형 언급이 없으면 생략(null)."
    ))] = None,
    night_mode: Annotated[bool, Field(description=(
        "야간·밤·가로등·CCTV·안전 경로를 요청했을 때 true; 언급이 없으면 false. "
        "true이면 관측된 조명 점수 0.4 이상인 코스만 추천하고 '야간 조명 많음'으로 표시합니다. "
        "0.4 미만·미확인 코스는 제외합니다. 실제 안전을 보장하는 기준은 아닙니다."
    ))] = False,
    need_facilities: Annotated[list[Literal["convenience_store", "restroom", "water", "park"]] | None, Field(description=(
        "convenience_store=편의점, restroom=화장실, water=마실 물·음수대. "
        "공원·강변·한강·하천·호수·수변·물가·물 보면서 달리는 코스를 명시한 경우에만 "
        "park를 전달하세요. 등록된 5곳 중 조건에 맞는 최대 3곳을 무작위로, 출발지가 있으면 가까운 순으로 반환합니다. "
        "일반 코스 주변에 공원이 있다는 이유로 park를 추론하지 마세요."
    ))] = None,
    allow_nearby_start: Annotated[bool, Field(description=(
        "기본 false. 사용자가 가까운 다른 출발지 코스를 명시적으로 선택한 뒤에만 true. "
        "추천 요청 자체나 두 선택지에 대한 모호한 '네'를 동의로 해석하지 마세요. "
        "원래 출발지·모양·거리·지형·야간·시설 조건을 유지하세요."
    ))] = False,
) -> CallToolResult:
    """Runnywhere(러니웨어): the only tool for every new running course or
    animal-shaped GPS art (러닝 코스/그려줘). Call once. Never invent omitted start,
    distance, terrain, night, or facilities; omit location if unstated. Map
    exactly/딱 distance to strict_distance=true. missing
    start/서울 시내: ask and stop; district: stay in 구; specific: pass verbatim or lat/lon.
    standard=ordinary; best_animal=unnamed animal; dog/cat/rabbit/whale=named;
    그려줘 alone=standard. On start_change_confirmation_required ask the returned
    question with no course. NEXT user numbered choice uses that option's exact
    arguments; ambiguous yes means clarify. On failure/timeout use server text
    without adding a cause or route. Describe starts only from actual course_selection.
    Use assistant_final_text and web editor. 이 코스 근처 화장실 requires course_id."""
    common = dict(
        location=location, lat=lat, lon=lon, distance_km=distance_km,
        duration_min=duration_min, strict_distance=strict_distance,
        include_hills=include_hills,
        night_mode=night_mode, need_facilities=need_facilities,
        allow_nearby_start=allow_nearby_start,
    )
    return _dispatch_course_request(course_type, common)


@functools.wraps(generate_running_course)
def _legacy_generate_running_course(*args, **kwargs) -> CallToolResult:
    """Bridge a cached pre-unification Preview call to the latest response."""
    keys = ("location", "lat", "lon", "distance_km", "duration_min",
            "include_hills", "night_mode", "need_facilities")
    request = {**dict(zip(keys, args)), **kwargs}
    if request.get("include_hills") is False:
        request["include_hills"] = None
    return _dispatch_course_request("standard", request)


@functools.wraps(generate_animal_course)
def _legacy_generate_animal_course(*args, **kwargs) -> CallToolResult:
    """Bridge a cached pre-unification animal call to the latest response."""
    keys = ("shape", "location", "lat", "lon", "distance_km", "duration_min",
            "include_hills", "night_mode", "need_facilities", "shape_token")
    request = {**dict(zip(keys, args)), **kwargs}
    shape = request.pop("shape", None)
    token = request.pop("shape_token", None)
    if token and not shape:
        try:
            shape, request["distance_km"] = decode_shape_token(token)
        except (ValueError, KeyError):
            return _mcp_result("공유 토큰 형식이 올바르지 않아요. 예: whale-5k", code="invalid_request", is_error=True)
    if request.get("include_hills") is False:
        request["include_hills"] = None
    return _dispatch_course_request(shape or "best_animal", request)


# Internal compatibility bridges keep their signatures for direct callers and
# token restoration, but are intentionally not exposed as production MCP tools.
_legacy_generate_running_course.__doc__ = create_seoul_running_course.__doc__
_legacy_generate_animal_course.__doc__ = create_seoul_running_course.__doc__


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
        "Explicitly supplied valid Runnywhere course_id, taken from its map URL"
    ))],
    facility_types: Annotated[list[str] | None, Field(description=(
        "Requested filters only: convenience_store=편의점, restroom=화장실, "
        "water=음수대/물, park=공원. Omit to return all supported facilities."
    ))] = None,
) -> str:
    """Finds convenience stores, restrooms, drinking water, or parks within
    10m of an existing Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!)
    course. For "이 코스 근처 화장실 찾아줘", require an explicitly supplied
    valid course_id; never assume an earlier widget left one in context.
    Return facilities for that exact
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
    location: Annotated[str | None, Field(
        description=REFINE_LOCATION_FIELD_EN)] = None,
    need_facilities: Annotated[list[str] | None, Field(description=(
        "New facility requirements. Use park only when the user explicitly "
        "changes the course to park or riverside-park running."
    ))] = None,
) -> str:
    """Refines an existing Runnywhere(러니웨어: 어디서든 러닝 코스 짜기!)
    course by changing distance, hills, night mode, animal shape, start, or
    facilities. Requires an explicitly supplied valid course_id; do not refer
    back to a previous recommendation. Prefer the linked web editor for edits.
    Never use it for new course creation or recommendation."""
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
REGISTERED_SERVICE_NAME = "러니웨어:어디서든 러닝 코스 짜기! - 카카오툴"
for _fn, _name, _title, _open_world in (
    (create_seoul_running_course, "create_seoul_running_course",
     "서울 러닝 코스 생성", True),
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
        # PlayMCP's refresh validator requires the registered service name,
        # not just the short brand. Without it, old tool schemas stay active.
        description=f"{_fn.__doc__.strip()}\n{REGISTERED_SERVICE_NAME}",
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
    except CourseAccessError as exc:
        return PlainTextResponse(str(exc), status_code=403,
                                 headers={"Cache-Control": "no-store"})
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
    except CourseAccessError as exc:
        return PlainTextResponse(str(exc), status_code=403,
                                 headers={"Cache-Control": "no-store"})
    except Exception:
        return PlainTextResponse("잘못된 코스 링크입니다.", status_code=404)
    return Response(course_thumbnail_svg(course), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/c/{course_id}/route.json", methods=["GET"])
async def course_route_json(request: Request) -> Response:
    """Course polyline for the animal-atlas overlay (verified presets only:
    the atlas must never trigger CPU generation from an unauthenticated URL)."""
    try:
        cid = request.path_params["course_id"]
        params = decode_course_id(cid)
        if animal_preset_is_blocked(params):
            raise CourseAccessError()
        course = get_animal_preset(params)
    except CourseAccessError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403,
                            headers={"Cache-Control": "no-store"})
    except Exception:
        return JSONResponse({"error": "bad course id"}, status_code=404)
    if not isinstance(course, Course):
        return JSONResponse({"error": "not a verified course"}, status_code=404)
    points = [[round(lat, 5), round(lon, 5)] for lat, lon in route_points(course)]
    return JSONResponse({"points": points, "km": round(course.length_km, 1)},
                        headers={"Cache-Control": "public, max-age=86400"})


# A finger crossing a phone screen produces hundreds of points. The old 96
# cap rejected most real pencil strokes outright, and the runner saw a generic
# "check the line" error for drawing normally. The client thins the stroke
# before sending; this cap is the safety net, not the editing rule.
STROKE_MAX_POINTS = 600


def _unchanged_note(before: list[int], after: list[int]) -> str:
    """Explain a replacement that came back identical to what it replaced."""
    if before != after:
        return ""
    return ("이 구간을 대신할 다른 보행로가 없어 같은 길로 이어졌어요. "
            "두 지역을 잇는 유일한 길이라, 더 넓은 범위를 지우거나 "
            "다른 곳을 경유점으로 짚어 주세요.")


def _save_drawn_draft(params, from_index: int | None, to_index: int | None,
                      path: list[int], strokes: list[list[CourseWaypoint]],
                      name: str) -> Course:
    """Snap a freehand draft to walkable roads at the moment it is saved.

    Connectivity is authoritative here: an incomplete draft stays editable in
    the browser, but cannot become a saved course.
    """
    snapped = snap_drawn_strokes(params, path, from_index, to_index, strokes)
    saved = course_from_path(params, snapped.path, name)
    saved.note = snapped.note
    return saved


class _CourseEditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    path: list[int] = Field(min_length=3, max_length=1200)
    stroke: list[CourseWaypoint] = Field(
        default_factory=list, max_length=STROKE_MAX_POINTS)
    strokes: list[list[CourseWaypoint]] = Field(
        default_factory=list, max_length=STROKE_MAX_POINTS)
    # Points tapped on the map for the span to pass through. Bounded by the
    # same cap route_via_points enforces, so an oversized request is refused
    # by validation rather than after the routing work.
    vias: list[CourseWaypoint] = Field(
        default_factory=list, max_length=MAX_VIA_POINTS)
    name: str = Field(default="", max_length=COURSE_NAME_MAX_CHARS)
    from_index: int | None = None
    to_index: int | None = None

    @model_validator(mode="after")
    def validate_strokes(self):
        if self.stroke and self.strokes:
            raise ValueError("stroke와 strokes를 동시에 보낼 수 없어요")
        total = len(self.stroke) + sum(len(part) for part in self.strokes)
        if total > STROKE_MAX_POINTS:
            raise ValueError(f"그린 선의 점이 {total}개로 너무 많아요")
        return self

    def drawn_strokes(self) -> list[list[CourseWaypoint]]:
        return self.strokes or ([self.stroke] if self.stroke else [])


def _payload_problem(error: Exception, body: object) -> str:
    """Name what is actually wrong with an edit request.

    "코스 선 정보를 확인해 주세요" told a runner who had just drawn a normal
    line nothing they could act on, and hid a plain length-cap rejection.
    """
    if not isinstance(body, dict):
        return "코스 편집 요청 형식이 올바르지 않아요. 새로고침한 뒤 다시 시도해 주세요."
    stroke = body.get("stroke")
    strokes = body.get("strokes")
    path = body.get("path")
    if isinstance(stroke, list) and len(stroke) > STROKE_MAX_POINTS:
        return (f"그린 선의 점이 {len(stroke)}개로 너무 많아요. "
                "조금 짧게 나눠 그려 주세요.")
    if isinstance(strokes, list):
        total = sum(len(part) for part in strokes if isinstance(part, list))
        if total > STROKE_MAX_POINTS:
            return (f"그린 선의 점이 {total}개로 너무 많아요. "
                    "조금 짧게 나눠 그려 주세요.")
    supplied_points = list(stroke) if isinstance(stroke, list) else []
    if isinstance(strokes, list):
        supplied_points.extend(
            point for part in strokes if isinstance(part, list) for point in part)
    if any(
        not isinstance(point, dict)
        or not (37.4 <= float(point.get("lat", 0) or 0) <= 37.72)
        or not (126.76 <= float(point.get("lon", 0) or 0) <= 127.19)
        for point in supplied_points
    ):
        return "그린 선이 서울 밖으로 나갔어요. 지도 안쪽 도로를 따라 그려 주세요."
    if isinstance(path, list) and len(path) > 1200:
        return "코스가 너무 복잡해졌어요. 저장한 뒤 이어서 수정해 주세요."
    if isinstance(path, list) and len(path) < 3:
        return "코스 선을 불러오지 못했어요. 새로고침한 뒤 다시 시도해 주세요."
    if not isinstance(body.get("action"), str):
        return "지원하지 않는 편집 동작이에요. 새로고침한 뒤 다시 시도해 주세요."
    return f"코스 선을 처리하지 못했어요 ({type(error).__name__}). 다시 시도해 주세요."


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
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse(
            {"error": "코스 편집 요청을 읽지 못했어요. 새로고침한 뒤 다시 시도해 주세요."},
            status_code=400)
    try:
        payload = _CourseEditPayload.model_validate(body)
    except (ValidationError, ValueError, TypeError) as exc:
        return JSONResponse({"error": _payload_problem(exc, body)}, status_code=400)
    edited = current.model_copy(update={"shape": None, "manual_waypoints": [], "manual_path": []})
    try:
        with anyio.fail_after(ROUTE_EDIT_RESPONSE_BUDGET_S):
            if payload.action == "snap":
                course = await anyio.to_thread.run_sync(
                    functools.partial(
                        snap_drawn_strokes, edited, payload.path,
                        payload.from_index, payload.to_index, payload.drawn_strokes(),
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
            elif payload.action == "via":
                if payload.from_index is None or payload.to_index is None:
                    return JSONResponse({"error": "바꿀 구간을 다시 선택해 주세요."}, status_code=400)
                course = await anyio.to_thread.run_sync(
                    functools.partial(
                        route_via_points, edited, payload.path,
                        payload.from_index, payload.to_index, payload.vias,
                    ), abandon_on_cancel=True,
                )
            elif payload.action == "save":
                course = await anyio.to_thread.run_sync(
                    functools.partial(course_from_path, edited, payload.path,
                                      payload.name),
                    abandon_on_cancel=True,
                )
            elif payload.action == "save_draft":
                # Missing indexes are not an error any more: where the stroke
                # meets the course is enough to know what to replace.
                course = await anyio.to_thread.run_sync(
                    functools.partial(
                        _save_drawn_draft, edited,
                        payload.from_index, payload.to_index,
                        payload.path, payload.drawn_strokes(), payload.name,
                    ),
                    abandon_on_cancel=True,
                )
            else:
                return JSONResponse({"error": "지원하지 않는 편집 동작입니다."}, status_code=400)
    except CourseGapOpen as exc:
        # Not a failed edit. The erase stands and the runner is being asked for
        # the line across it, so returning an error here would throw away the
        # eraser's work and make rubbing something out look broken.
        return JSONResponse({
            "gap_open": True,
            "path": edit_path_nodes(payload.path),
            "geometry": edit_path_geometry(payload.path),
            "note": str(exc),
        }, headers={"Cache-Control": "no-store"})
    except CourseError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except (TimeoutError, _GenerationTimeout):
        return JSONResponse(
            {"error": "대체 보행로를 찾는 데 시간이 걸렸어요. 더 짧은 구간을 선택해 주세요."},
            status_code=503,
        )
    if payload.action in {"snap", "reroute", "via"}:
        return JSONResponse({
            "path": edit_path_nodes(course.path),
            # The editor draws the same OSM way shapes the other two pages
            # draw. Without this it drew straight chords between graph nodes
            # and the edited line sat visibly off the road it followed.
            "geometry": edit_path_geometry(course.path),
            "length_km": round(course.length_km, 2),
            # Some stretches are the only walkable link between two parts of
            # the city -- an unnamed 660m hillside footway by 개운산 is a cut
            # edge in the graph -- so erasing one and asking for a replacement
            # returns the same line. Saying nothing made the editor look
            # broken; naming it lets the runner move on.
            # An identical result means something only for the eraser: a span
            # with no alternative. Pulling the line back to where it started
            # returns the original path by design and needs no comment.
            "note": course.note or (
                _unchanged_note(payload.path, course.path)
                if payload.action == "reroute" else ""),
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
            # What the drawing could not be given, if anything.
            "note": course.note,
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
    except CourseAccessError as exc:
        return PlainTextResponse(str(exc), status_code=403,
                                 headers={"Cache-Control": "no-store"})
    except Exception:
        return PlainTextResponse("잘못된 코스 링크입니다.", status_code=404)
    if is_gpx:
        name = (params.location_name or "Runnywhere") + f" {course.length_km:.1f}km"
        return Response(to_gpx(name, route_points(course)), media_type="application/gpx+xml",
                        headers={"Content-Disposition": f'attachment; filename="runnywhere-{cid[:12]}.gpx"'})
    return _course_page(course, "info")


def _course_page(course: Course, page: str) -> Response:
    """One course, rendered for one of its three pages.

    Only the info page reads facilities; the run and editor pages show neither
    the list nor the counts, and the lookup is pure work for their budget.
    """
    # The editor has no use for them, but a runner following the course wants
    # to see the next convenience store as much as a runner reading about it.
    facs = (
        facilities_along(route_points(course), ["convenience_store", "restroom"],
                         limit=80)
        if page in ("info", "run") else []
    )
    return HTMLResponse(preview_html(
        course, facs, BASE_URL,
        kakao_javascript_key=KAKAO_JAVASCRIPT_KEY, page=page))


def _course_subpage(request: Request, page: str) -> Response:
    try:
        params = decode_course_id(request.path_params["course_id"])
        course = _get_course(params, timeout_s=GENERAL_RESPONSE_BUDGET_S)
    except CourseAccessError as exc:
        return PlainTextResponse(str(exc), status_code=403,
                                 headers={"Cache-Control": "no-store"})
    except Exception:
        return PlainTextResponse("잘못된 코스 링크입니다.", status_code=404)
    return _course_page(course, page)


@mcp.custom_route("/c/{course_id}/run", methods=["GET"])
async def course_run_page(request: Request) -> Response:
    """Running the course: effort figures, then live location on the map."""
    return _course_subpage(request, "run")


@mcp.custom_route("/c/{course_id}/editor", methods=["GET"])
async def course_editor_page(request: Request) -> Response:
    """Redrawing the course. /edit is the POST API this page calls."""
    return _course_subpage(request, "edit")


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
    except CourseAccessError as exc:
        return PlainTextResponse(str(exc), status_code=403,
                                 headers={"Cache-Control": "no-store"})
    except Exception:
        return PlainTextResponse("잘못된 Shape Relay 링크입니다.", status_code=404)
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=3600"})


# ---------- rate limiting (PRD §8) ----------

class _TokenBucketMiddleware:
    """Per-client token bucket: RATE_LIMIT_RPS steady, 2x burst. In-process by
    design. MCP admission uses a bounded, short-lived queue so a normal traffic
    burst waits for capacity instead of failing merely because several users
    clicked at the same moment."""

    def __init__(self, app, rps: float = 20.0,
                 max_body_bytes: int = 65_536,
                 max_concurrent_mcp: int = 10,
                 max_queued_mcp: int = 32,
                 mcp_queue_timeout_s: float = 1.0,
                 max_concurrent_route_edits: int = ROUTE_EDIT_MAX_CONCURRENT,
                 trust_proxy_hops: int = 0):
        self.app = app
        self.rps = rps
        self.burst = rps * 2
        self.max_body_bytes = max_body_bytes
        self.max_concurrent_mcp = max(1, max_concurrent_mcp)
        self.max_queued_mcp = max(0, max_queued_mcp)
        self.mcp_queue_timeout_s = max(0.01, mcp_queue_timeout_s)
        self.max_concurrent_route_edits = max_concurrent_route_edits
        self.trust_proxy_hops = max(0, trust_proxy_hops)
        self.buckets: dict[str, tuple[float, float]] = {}
        self._active_mcp = 0
        self._queued_mcp = 0
        self._active_route_edits = 0
        self._active_lock = asyncio.Lock()
        self._mcp_slots = asyncio.Semaphore(self.max_concurrent_mcp)

    async def _admit_mcp(self) -> tuple[bool, float, str]:
        """Acquire a bounded MCP slot, returning admission and wait metadata."""
        started = time.monotonic()
        async with self._active_lock:
            capacity = self.max_concurrent_mcp + self.max_queued_mcp
            if self._active_mcp + self._queued_mcp >= capacity:
                return False, 0.0, "queue_full"
            self._queued_mcp += 1
        try:
            await asyncio.wait_for(
                self._mcp_slots.acquire(), timeout=self.mcp_queue_timeout_s)
        except TimeoutError:
            async with self._active_lock:
                self._queued_mcp -= 1
            return False, time.monotonic() - started, "queue_timeout"
        except BaseException:
            async with self._active_lock:
                self._queued_mcp -= 1
            raise
        async with self._active_lock:
            self._queued_mcp -= 1
            self._active_mcp += 1
        return True, time.monotonic() - started, "admitted"

    async def _release_mcp(self) -> None:
        async with self._active_lock:
            self._active_mcp -= 1
        self._mcp_slots.release()

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
        if path == "/mcp":
            admitted_mcp, waited_s, reason = await self._admit_mcp()
            if not admitted_mcp:
                log.warning(
                    "mcp_admission outcome=rejected reason=%s wait_ms=%d active=%d queued=%d",
                    reason, round(waited_s * 1000), self._active_mcp,
                    self._queued_mcp,
                )
                from starlette.responses import PlainTextResponse as _P
                return await _P(
                    "server busy; retry shortly",
                    status_code=429,
                    headers={"Retry-After": "1", "Cache-Control": "no-store"},
                )(scope, receive, send_with_security_headers)
            if waited_s >= 0.01:
                log.info(
                    "mcp_admission outcome=admitted wait_ms=%d active=%d queued=%d",
                    round(waited_s * 1000), self._active_mcp,
                    self._queued_mcp,
                )
        elif is_edit:
            async with self._active_lock:
                if self._active_route_edits >= self.max_concurrent_route_edits:
                    from starlette.responses import PlainTextResponse as _P
                    return await _P("server busy", status_code=429)(
                        scope, receive, send_with_security_headers)
                self._active_route_edits += 1
                admitted_edit = True
        try:
            return await self.app(scope, limited_receive,
                                  send_with_security_headers)
        except _RequestBodyTooLarge:
            from starlette.responses import PlainTextResponse as _P
            return await _P("request body too large", status_code=413)(
                scope, original_receive, send_with_security_headers)
        finally:
            if admitted_mcp:
                await self._release_mcp()
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
        park_courses()
        all_verified_animal_presets()
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
        max_concurrent_mcp=int(os.environ.get("RUNART_MAX_CONCURRENT_MCP", "10")),
        max_queued_mcp=int(os.environ.get("RUNART_MAX_QUEUED_MCP", "32")),
        mcp_queue_timeout_s=float(os.environ.get("RUNART_MCP_QUEUE_TIMEOUT_S", "1.0")),
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
