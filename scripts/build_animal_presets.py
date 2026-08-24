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
from runart.stations import SEOUL_METRO_STATIONS


def _station_name(name: str) -> str:
    return name if name.endswith("역") else f"{name}역"


def _generate(job):
    line, name, lat, lon, shape, per_distance_seconds = job
    try:
        params = CourseParams(lat=lat, lon=lon, location_name=_station_name(name),
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
    parser.add_argument("--revalidate", action="store_true",
                        help="rebuild unsafe entries and normalize every closed-loop start")
    args = parser.parse_args()
    fingerprint = graph_fingerprint()
    entries = {} if args.fresh else _read_existing(args.output, fingerprint)
    # Transfer rows that resolve to exactly the same coordinate share presets.
    unique_rows = list({(row[2], row[3]): row for row in SEOUL_METRO_STATIONS}.values())
    station_names = {
        f"{lat:.5f},{lon:.5f}": _station_name(name)
        for _, name, lat, lon, *_ in unique_rows
    }
    invalid: set[str] = set()
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
        if not jobs and (normalized or metadata_normalized):
            _write(args.output, fingerprint, entries)
    finally:
        if pool is not None:
            pool.shutdown()


if __name__ == "__main__":
    main()
