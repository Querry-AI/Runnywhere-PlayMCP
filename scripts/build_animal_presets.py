#!/usr/bin/env python3
"""Generate all 289 station-row x 4 animal build-time presets.

The output is checkpointed after every station, so an interrupted run resumes.
Use --workers 2 on the production-sized graph to keep memory bounded.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import multiprocessing
import os
from pathlib import Path

from runart.animal_presets import (FORMAT_VERSION, PRESET_PATH,
                                   graph_fingerprint, preset_key,
                                   serialize_course)
from runart.course import (Course, course_route_issues,
                           rebase_closed_course_start)
from runart.graph import get_graph
from runart.models import CourseParams
from runart.shapes import SHAPES, find_best_reference_course
from runart.course import CourseError
from runart.courseplan import SAME_START_M
from runart.geo import haversine_m
from runart.geocode import GAZETTEER, resolve_location
from runart.stations import SEOUL_METRO_STATIONS


def _station_name(name: str) -> str:
    return name if name.endswith("역") else f"{name}역"


# Themes a runner names instead of a start: "한강 따라", "공원에서".
THEME_WORDS = frozenset({"공원", "한강", "한강공원", "강변", "하천", "물",
                         "물가", "수변", "호수", "서울"})


def _start_rows(include_gazetteer: bool) -> list[tuple[str, str, float, float]]:
    """Every start worth a shape preset, with its display name already final.

    Stations alone left every landmark to live shape generation: measured on
    the deployed server, best_animal at 뚝섬·잠원·망원·이촌·잠실한강공원 each spent
    2.6-2.7s only to answer that no animal course starts there. A stored
    result -- course or absence -- costs nothing to serve.
    """
    rows: list[tuple[str, str, float, float]] = []
    seen: set[tuple[float, float]] = set()
    for line, name, lat, lon, *_ in SEOUL_METRO_STATIONS:
        key = (round(lat, 5), round(lon, 5))
        if key in seen:
            continue  # transfer rows at one coordinate share a preset
        seen.add(key)
        rows.append((line, _station_name(name), lat, lon))
    if not include_gazetteer:
        return rows
    for landmark in GAZETTEER:
        # 한강공원 shares its coordinate with 여의도한강공원 and, being first,
        # would name the card. The server treats these as theme words, not
        # places (server._park_course_result), so they make poor start labels.
        if landmark in THEME_WORDS:
            continue
        try:
            lat, lon, resolved = resolve_location(landmark, None, None, timeout_s=1.0)
        except (CourseError, Exception):  # noqa: BLE001 - unresolvable is not fatal
            continue
        # 강남 and 테헤란로8길8 sit 90m and 98m from 강남역. Kept as their own
        # starts, they filled the other two card slots for a 강남역 request with
        # courses labelled by names the runner never said. Same place by the
        # runtime's own rule, so one preset is enough.
        if any(haversine_m(lat, lon, rlat, rlon) < SAME_START_M
               for _, _, rlat, rlon in rows):
            continue
        seen.add((round(lat, 5), round(lon, 5)))
        rows.append(("landmark", resolved, lat, lon))
    return rows


def _generate(job):
    line, name, lat, lon, shape, per_distance_seconds = job
    try:
        params = CourseParams(lat=lat, lon=lon, location_name=name,
                              distance_km=SHAPES[shape].min_km, shape=shape)
        course = find_best_reference_course(
            params, per_distance_budget_s=per_distance_seconds)
        if course is not None:
            course = rebase_closed_course_start(course)
            issues = course_route_issues(course, get_graph())
            if issues:
                print(
                    f"rejected unsafe: {line} {name} {shape} "
                    f"({', '.join(issues)})",
                    flush=True,
                )
                course = None
    except Exception as exc:  # one bad station must not abort the full build
        print(f"unavailable after {type(exc).__name__}: {line} {name} {shape}", flush=True)
        course = None
    return preset_key(lat, lon, shape), serialize_course(course) if course else None


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


def _restore(raw: dict) -> Course:
    return rebase_closed_course_start(Course(
        params=CourseParams(**raw["params"]),
        path=raw["path"],
        points=[tuple(point) for point in raw["points"]],
        length_m=raw["length_m"],
        ascent_m=raw["ascent_m"],
        rfs=raw["rfs"],
        shape_similarity=raw.get("shape_similarity"),
    ))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2,
                        help="process workers; 0 runs inline without semaphores")
    parser.add_argument("--output", type=Path, default=PRESET_PATH)
    parser.add_argument("--per-distance-seconds", type=float, default=5.0)
    parser.add_argument("--fresh", action="store_true",
                        help="ignore and replace all existing checkpoints")
    parser.add_argument("--retry-unavailable", action="store_true",
                        help="re-run only entries previously stored as unavailable")
    parser.add_argument("--no-gazetteer", action="store_true",
                        help="stations only; skip landmark starts")
    parser.add_argument("--prune", action="store_true",
                        help="drop stored entries whose start is no longer built")
    parser.add_argument("--revalidate", action="store_true",
                        help="rebuild unsafe entries and normalize every closed-loop start")
    args = parser.parse_args()
    fingerprint = graph_fingerprint()
    entries = {} if args.fresh else _read_existing(args.output, fingerprint)
    # Transfer rows that resolve to exactly the same coordinate share presets.
    unique_rows = _start_rows(not args.no_gazetteer)
    station_names = {
        f"{lat:.5f},{lon:.5f}": name for _, name, lat, lon in unique_rows
    }
    invalid: set[str] = set()
    dropped: list[str] = []
    normalized = 0
    metadata_normalized = 0
    if args.revalidate and entries:
        graph = get_graph()
        for key, raw in list(entries.items()):
            if raw is None:
                continue
            course = _restore(raw)
            expected_name = station_names.get(key.rsplit(",", 1)[0])
            if expected_name and course.params.location_name != expected_name:
                course.params = course.params.model_copy(
                    update={"location_name": expected_name}
                )
                metadata_normalized += 1
            issues = course_route_issues(course, graph)
            if issues:
                invalid.add(key)
                continue
            normalized_raw = serialize_course(course)
            if normalized_raw != raw:
                entries[key] = normalized_raw
                normalized += 1
        print(
            f"revalidation: {len(invalid)} unsafe, {normalized} routes normalized, "
            f"{metadata_normalized} station names normalized",
            flush=True,
        )
    if args.prune:
        wanted = {preset_key(lat, lon, shape)
                  for _, _, lat, lon in unique_rows for shape in SHAPES}
        dropped[:] = [key for key in entries if key not in wanted]
        for key in dropped:
            del entries[key]
        print(f"pruned {len(dropped)} entries for starts no longer built", flush=True)
    jobs = []
    for row in unique_rows:
        for shape in SHAPES:
            key = preset_key(row[2], row[3], shape)
            should_run = (key not in entries
                          or key in invalid
                          or (args.retry_unavailable and entries.get(key) is None))
            if should_run:
                jobs.append(row[:4] + (shape, args.per_distance_seconds))
    total = len(unique_rows) * len(SHAPES)
    print(f"animal presets: {len(entries)}/{total} cached, {len(jobs)} remaining", flush=True)
    if args.workers <= 0:
        results = map(_generate, jobs)
        pool = None
    else:
        ctx = multiprocessing.get_context("spawn")
        pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=args.workers, mp_context=ctx)
        results = pool.map(_generate, jobs)
    try:
        for completed, (key, value) in enumerate(results, 1):
            entries[key] = value
            # Frequent atomic checkpoints make a long full-Seoul build resumable.
            if completed % 4 == 0 or completed == len(jobs):
                _write(args.output, fingerprint, entries)
                ok = sum(value is not None for value in entries.values())
                processed = total - len(jobs) + completed
                print(f"{processed}/{total} processed; {ok} available, "
                      f"{len(entries) - ok} unavailable", flush=True)
        # A prune with nothing left to build still has to reach disk: the
        # only other writes are per-batch and end-of-run over the job list.
        if not jobs and (normalized or metadata_normalized or dropped):
            _write(args.output, fingerprint, entries)
    finally:
        if pool is not None:
            pool.shutdown()


if __name__ == "__main__":
    main()
