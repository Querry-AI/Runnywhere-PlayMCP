"""Load test against the PlayMCP spec: avg <= 100ms, p99 <= 3000ms (PRD §7.1).

Usage: server running on localhost:8000, then
    .venv/bin/python scripts/loadtest.py [n_requests] [concurrency]

The default 1,000-call run includes cold misses and enough steady-state traffic
to measure the production cache rather than being dominated by 30 first-time
animal/location combinations.
"""

import asyncio
from collections import Counter
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("RUNART_LOADTEST_URL", "http://localhost:8000/mcp")
REPORT_PATH = os.environ.get("RUNART_LOADTEST_REPORT", "")
SPOTS = [
    "시청", "강남역", "여의도한강공원", "석촌호수", "서울숲", "올림픽공원",
    "뚝섬한강공원", "홍대", "잠실", "왕십리", "경복궁역", "성신여대역",
    "구파발역", "신설동역", "광화문역", "혜화역", "건대입구역", "사당역",
    "노원역", "수유역", "천호역", "마곡역", "합정역", "선릉역",
]
SHAPES = [None, None, None, "whale", "cat", "dog"]  # ~50% GPS art
DISTANCES = [3, 4, 5, 5, 7, 10]
CORPUS_VERSION = "runart-contest-v1"
CALL_TIMEOUT_S = max(0.1, float(os.environ.get("RUNART_LOADTEST_CALL_TIMEOUT_S", "10")))
RUN_TIMEOUT_S = max(1.0, float(os.environ.get("RUNART_LOADTEST_RUN_TIMEOUT_S", "240")))


def _request_for(index: int) -> tuple[str, dict]:
    """Return one deterministic request from the fixed promotion corpus."""
    loc = SPOTS[index % len(SPOTS)]
    shape = SHAPES[(index // len(SPOTS)) % len(SHAPES)]
    dist = DISTANCES[(index // (len(SPOTS) * len(SHAPES))) % len(DISTANCES)]
    if shape:
        return "art", {"course_type": shape, "location": loc}
    return "course", {
        "course_type": "standard", "location": loc, "distance_km": dist,
    }


def _partition_indices(n: int, concurrency: int) -> list[list[int]]:
    """Split exactly n calls without truncation or accidental overrun."""
    return [list(range(worker, n, concurrency)) for worker in range(concurrency)]


def _nearest_rank(values: list[float], percentile: float) -> float:
    """Standards-correct nearest-rank percentile for a non-empty sample."""
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _promotion_failed(*, complete: bool, outcomes: Counter,
                      avg_ms: float, p99_ms: float) -> bool:
    """Capacity/protocol/latency gate; product no-results remain evidence."""
    return bool(
        not complete or outcomes["timeout"] or outcomes["protocol_error"]
        or outcomes["http_429"] or outcomes["session_error"]
        or avg_ms > 100 or p99_ms > 3000
    )


async def worker(indices: list[int], latencies: dict, outcomes: Counter):
    attempted = 0
    try:
        async with streamablehttp_client(URL) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for position, index in enumerate(indices):
                    group, arguments = _request_for(index)
                    attempted += 1
                    t0 = time.perf_counter()
                    try:
                        result = await asyncio.wait_for(
                            s.call_tool("create_seoul_running_course", arguments),
                            timeout=CALL_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        outcomes["timeout"] += 1
                    except Exception as exc:  # noqa: BLE001 - protocol evidence
                        label = "http_429" if "429" in str(exc) else "protocol_error"
                        outcomes[label] += 1
                    else:
                        code = (result.structuredContent or {}).get(
                            "result_code", "unclassified")
                        outcomes[code] += 1
                        if result.isError:
                            outcomes["mcp_error"] += 1
                    elapsed_ms = (time.perf_counter() - t0) * 1000
                    latencies["all"].append(elapsed_ms)
                    latencies[group].append(elapsed_ms)
                    latencies["cold" if position == 0 else "warm"].append(elapsed_ms)
    except Exception:  # initialization/transport failed before calls could run
        outcomes["session_error"] += max(0, len(indices) - attempted)


def _report(name: str, vals: list[float]):
    if not vals:
        return
    ordered = sorted(vals)
    avg = statistics.mean(ordered)
    p99 = _nearest_rank(ordered, 0.99)
    p50 = _nearest_rank(ordered, 0.50)
    print(f"  {name}: n={len(ordered)} avg={avg:.0f}ms p50={p50:.0f}ms "
          f"p99={p99:.0f}ms max={ordered[-1]:.0f}ms")
    return {
        "name": name, "n": len(ordered), "avg_ms": round(avg, 2),
        "p50_ms": round(p50, 2), "p99_ms": round(p99, 2),
        "max_ms": round(ordered[-1], 2),
    }


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    if n < 1 or conc < 1:
        raise SystemExit("n_requests and concurrency must be positive")
    conc = min(conc, n)
    latencies: dict = {"all": [], "course": [], "art": [], "cold": [], "warm": []}
    outcomes: Counter = Counter()
    t0 = time.perf_counter()
    await asyncio.wait_for(
        asyncio.gather(*(worker(indices, latencies, outcomes)
                         for indices in _partition_indices(n, conc))),
        timeout=RUN_TIMEOUT_S)
    wall = time.perf_counter() - t0
    all_vals = sorted(latencies["all"])
    completed = len(all_vals)
    print(f"attempted={n} timed={completed} conc={conc} wall={wall:.1f}s "
          f"rps={completed / wall:.1f}")
    course_report = _report("일반 코스", latencies["course"])
    art_report = _report("GPS 아트", latencies["art"])
    _report("cold", latencies["cold"])
    _report("warm", latencies["warm"])
    if not all_vals:
        raise SystemExit("no tool invocation produced a timed outcome")
    avg = statistics.mean(all_vals)
    p99 = _nearest_rank(all_vals, 0.99)
    complete = completed == n and outcomes["session_error"] == 0
    print(f"전체: avg={avg:.0f}ms p99={p99:.0f}ms — "
          f"평균 100ms {'PASS' if avg <= 100 else 'FAIL'} / "
          f"p99 3000ms {'PASS' if p99 <= 3000 else 'FAIL'} / "
          f"전 호출 계측 {'PASS' if complete else 'FAIL'}")
    print("결과 분포: " + ", ".join(
        f"{key}={value}" for key, value in sorted(outcomes.items())))
    if REPORT_PATH:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "endpoint": URL,
            "release_sha": os.environ.get("RUNART_RELEASE_SHA", "unknown"),
            "corpus_version": CORPUS_VERSION,
            "attempted_requests": n, "timed_requests": completed,
            "concurrency": conc, "call_timeout_seconds": CALL_TIMEOUT_S,
            "wall_seconds": round(wall, 3), "rps": round(completed / wall, 2),
            "overall": {
                "avg_ms": round(avg, 2), "p99_ms": round(p99, 2),
                "avg_100ms_pass": avg <= 100, "p99_3000ms_pass": p99 <= 3000,
                "all_attempts_timed": complete,
                "mcp_errors": outcomes["mcp_error"],
            },
            "outcomes": dict(sorted(outcomes.items())),
            "groups": [report for report in (course_report, art_report) if report],
        }
        path = Path(REPORT_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as report:
            report.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        print(f"심사 증빙 저장: {path}")
    # Expected product-level no-result/confirmation outcomes may carry MCP
    # isError=true and still be valid terminal responses. Report that count,
    # but fail promotion only for transport/capacity loss or latency breach.
    if _promotion_failed(
            complete=complete, outcomes=outcomes, avg_ms=avg, p99_ms=p99):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
