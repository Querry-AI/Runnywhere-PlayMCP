#!/usr/bin/env python3
"""Generate build-time presets for ordinary (non-shape) running courses.

Keys are the runtime's own course ids, so serving a preset returns exactly the
route generate_course would have produced. Output is checkpointed after every
start point, so an interrupted run resumes.

    python scripts/build_standard_presets.py --workers 8
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import os
import re
import time
from pathlib import Path

from runart.animal_presets import graph_fingerprint
from runart.course import CourseError, course_route_issues, generate_course
from runart.geocode import GAZETTEER, resolve_location
from runart.graph import get_graph
from runart.models import CourseParams
from runart.standard_presets import (DEFAULT_DISTANCES_KM, FORMAT_VERSION,
                                     PRESET_PATH, preset_id, serialize_course,
                                     serialize_failure)
from runart.stations import SEOUL_METRO_STATIONS

# The runtime asks for the primary route plus three opposite-direction variants
# (server._standard_alternatives). A request is only fully served when all four
# are present, so the catalogue stores the same set.
VARIANTS = (None, 2, 4, 6)
DEFAULT_DISTANCES = DEFAULT_DISTANCES_KM


def _station_name(name: str) -> str:
    return name if name.endswith("역") else f"{name}역"


def _name_forms(raw: str) -> list[str]:
    """Both spellings a user might reach this station by.

    The table stores disambiguated names like 경복궁(정부서울청사); that exact
    string does not resolve, so building only from it silently drops the
    station -- while a user typing 경복궁역 resolves fine and then misses the
    catalogue entirely.
    """
    forms = [_station_name(raw)]
    stripped = re.sub(r"\([^)]*\)", "", raw).strip()
    if stripped and stripped != raw:
        forms.append(_station_name(stripped))
    return forms


def _start_points(include_gazetteer: bool) -> list[tuple[float, float, str]]:
    """Resolve every buildable start the same way a live request does."""
    names = [form for row in SEOUL_METRO_STATIONS for form in _name_forms(row[1])]
    if include_gazetteer:
        names.extend(GAZETTEER)
    seen: dict[tuple[float, float, str], None] = {}
    for name in names:
        try:
            lat, lon, resolved = resolve_location(name, None, None, timeout_s=1.0)
        except (CourseError, Exception):  # noqa: BLE001 - unresolvable is not fatal
            continue
        seen.setdefault((round(lat, 5), round(lon, 5), resolved), None)
    return list(seen)


def _jobs(points, distances, night: bool = False):
    for lat, lon, name in points:
        for distance in distances:
            for variant in VARIANTS:
                try:
                    params = CourseParams(lat=lat, lon=lon, location_name=name,
                                          distance_km=distance, night_mode=night)
                except Exception:  # noqa: BLE001 - outside the supported bounds
                    break
                if variant is not None:
                    params = params.model_copy(update={"route_variant": variant})
                yield params


def _generate(params: CourseParams):
    key = preset_id(params)
    try:
        course = generate_course(params)
        issues = course_route_issues(course, get_graph())
        if issues:
            print(f"rejected unsafe: {params.location_name} "
                  f"{params.distance_km:g}km v{params.route_variant} "
                  f"({', '.join(issues)})", flush=True)
            return key, None
    except CourseError as exc:
        if params.night_mode:
            # Measured on the deployed server: a night request that succeeds
            # costs 709ms on average, one that fails only 227ms -- the search
            # gives up quickly when no lit route exists. Storing the cheap half
            # would buy nothing and would freeze a refusal that a later
            # lighting update could have turned into a course.
            return key, None
        # Record the failure verbatim: the runtime then raises the same error
        # instead of re-running a search already known to fail.
        return key, serialize_failure(exc)
    except Exception as exc:  # noqa: BLE001 - one bad start must not abort a build
        print(f"unavailable after {type(exc).__name__}: {params.location_name} "
              f"{params.distance_km:g}km v{params.route_variant}", flush=True)
        return key, None
    return key, serialize_course(course)


def _read_existing(path: Path, fingerprint: str) -> dict:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        if (payload.get("format_version") == FORMAT_VERSION
                and payload.get("graph_fingerprint") == fingerprint):
            return payload.get("entries", {})
    except (OSError, ValueError):
        pass
    return {}


def _write(path: Path, fingerprint: str, entries: dict) -> None:
    payload = {"format_version": FORMAT_VERSION,
               "graph_fingerprint": fingerprint, "entries": entries}
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    # Generation is time-bounded: running many builds in parallel slows each
    # one and makes a bounded search give up early, storing a failure (or a
    # worse route) the runtime would never produce. Measured at 8 workers:
    # 3% of stored failures and 1% of stored routes disagreed with a live
    # single-threaded run. Default to 1; raise it only if you re-audit.
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path, default=PRESET_PATH)
    parser.add_argument("--distances", type=float, nargs="*", default=list(DEFAULT_DISTANCES))
    parser.add_argument("--limit-starts", type=int, default=0,
                        help="build only the first N start points (smoke runs)")
    parser.add_argument("--no-gazetteer", action="store_true")
    parser.add_argument("--night", action="store_true",
                        help="build night_mode entries (successes only)")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--retry-unavailable", action="store_true",
                        help="re-run entries previously stored as unavailable")
    parser.add_argument("--retry-failed", action="store_true",
                        help="re-run entries previously stored as unavailable")
    args = parser.parse_args()

    fingerprint = graph_fingerprint()
    entries = {} if args.fresh else _read_existing(args.output, fingerprint)
    if args.retry_failed:
        entries = {k: v for k, v in entries.items() if v is not None}
    points = _start_points(not args.no_gazetteer)
    if args.limit_starts:
        points = points[:args.limit_starts]
    if args.retry_unavailable:
        entries = {k: v for k, v in entries.items() if v is not None}
    jobs = [p for p in _jobs(points, args.distances, night=args.night)
            if preset_id(p) not in entries]
    total = len(points) * len(args.distances) * len(VARIANTS)
    print(f"start points {len(points)} x distances {len(args.distances)} x "
          f"variants {len(VARIANTS)} = {total} entries; "
          f"{len(entries)} already stored, {len(jobs)} to build", flush=True)

    started = time.monotonic()
    done = 0
    if args.workers <= 0:
        for params in jobs:
            key, value = _generate(params)
            entries[key] = value
            done += 1
            if done % 100 == 0:
                _write(args.output, fingerprint, entries)
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            for key, value in pool.map(_generate, jobs, chunksize=4):
                entries[key] = value
                done += 1
                if done % 100 == 0:
                    _write(args.output, fingerprint, entries)
                    rate = done / max(0.001, time.monotonic() - started)
                    print(f"  {done}/{len(jobs)}  {rate:.1f}/s  "
                          f"남은 시간 약 {(len(jobs)-done)/max(rate,0.01)/60:.0f}분", flush=True)
    _write(args.output, fingerprint, entries)
    built = sum(1 for v in entries.values() if v is not None)
    size = args.output.stat().st_size / 1024 / 1024
    print(f"완료: {built}/{len(entries)} 엔트리 생성 · {size:.1f}MB · "
          f"{(time.monotonic()-started)/60:.1f}분", flush=True)
    import hashlib
    h = hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(f"data_integrity.py EXPECTED_SHA256 에 등록할 값:\n"
          f'    "{args.output.name}": "{h}",', flush=True)


if __name__ == "__main__":
    main()
