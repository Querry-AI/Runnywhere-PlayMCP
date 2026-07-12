"""Load test against the PlayMCP spec: avg <= 100ms, p99 <= 3000ms (PRD §7.1).

Usage: server running on localhost:8000, then
    .venv/bin/python scripts/loadtest.py [n_requests] [concurrency]

The default 1,000-call run includes cold misses and enough steady-state traffic
to measure the production cache rather than being dominated by 30 first-time
animal/location combinations.
"""

import asyncio
import json
import os
import secrets
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("RUNART_LOADTEST_URL", "http://localhost:8000/mcp")
REPORT_PATH = os.environ.get("RUNART_LOADTEST_REPORT", "")
SPOTS = ["시청", "강남역", "여의도한강공원", "석촌호수", "서울숲", "올림픽공원",
         "뚝섬한강공원", "홍대", "잠실", "왕십리"]
SHAPES = [None, None, None, "whale", "cat", "dog"]  # ~50% GPS art


async def worker(n_calls: int, latencies: dict):
    async with streamablehttp_client(URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for _ in range(n_calls):
                loc = secrets.choice(SPOTS)
                shape = secrets.choice(SHAPES)
                dist = secrets.choice([3, 4, 5, 5, 7, 10])
                t0 = time.perf_counter()
                if shape:
                    # Product contract: animal art chooses its own cleanest
                    # distance under 11km; forcing random distances measures a
                    # different and intentionally slower validation workflow.
                    result = await s.call_tool(
                        "generate_animal_course", {"shape": shape, "location": loc})
                else:
                    result = await s.call_tool(
                        "generate_running_course", {"location": loc, "distance_km": dist})
                if result.isError:
                    raise RuntimeError(f"tool error during load test: {result.content}")
                latencies["art" if shape else "course"].append(
                    (time.perf_counter() - t0) * 1000)


def _report(name: str, vals: list[float]):
    if not vals:
        return
    vals.sort()
    avg = statistics.mean(vals)
    p99 = vals[int(len(vals) * 0.99)]
    print(f"  {name}: n={len(vals)} avg={avg:.0f}ms p50={vals[len(vals) // 2]:.0f}ms "
          f"p99={p99:.0f}ms max={vals[-1]:.0f}ms")
    return {
        "name": name, "n": len(vals), "avg_ms": round(avg, 2),
        "p50_ms": round(vals[len(vals) // 2], 2), "p99_ms": round(p99, 2),
        "max_ms": round(vals[-1], 2),
    }


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    latencies: dict = {"course": [], "art": []}
    per = max(1, n // conc)
    t0 = time.perf_counter()
    await asyncio.wait_for(
        asyncio.gather(*(worker(per, latencies) for _ in range(conc))), timeout=240)
    wall = time.perf_counter() - t0
    all_vals = sorted(latencies["course"] + latencies["art"])
    print(f"n={len(all_vals)} conc={conc} wall={wall:.1f}s rps={len(all_vals) / wall:.1f}")
    course_report = _report("일반 코스", latencies["course"])
    art_report = _report("GPS 아트", latencies["art"])
    avg = statistics.mean(all_vals)
    p99 = all_vals[int(len(all_vals) * 0.99)]
    print(f"전체: avg={avg:.0f}ms p99={p99:.0f}ms — "
          f"평균 100ms {'PASS' if avg <= 100 else 'FAIL'} / "
          f"p99 3000ms {'PASS' if p99 <= 3000 else 'FAIL'}")
    if REPORT_PATH:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "endpoint": URL, "requests": len(all_vals), "concurrency": conc,
            "wall_seconds": round(wall, 3), "rps": round(len(all_vals) / wall, 2),
            "overall": {
                "avg_ms": round(avg, 2), "p99_ms": round(p99, 2),
                "avg_100ms_pass": avg <= 100, "p99_3000ms_pass": p99 <= 3000,
            },
            "groups": [report for report in (course_report, art_report) if report],
        }
        path = Path(REPORT_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print(f"심사 증빙 저장: {path}")


if __name__ == "__main__":
    asyncio.run(main())
