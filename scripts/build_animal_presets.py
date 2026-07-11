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
from runart.models import CourseParams
from runart.shapes import SHAPES, find_best_reference_course
from runart.stations import SEOUL_METRO_STATIONS


def _generate(job):
    line, name, lat, lon, shape, per_distance_seconds = job
    try:
        params = CourseParams(lat=lat, lon=lon, location_name=f"{name}역",
                              distance_km=SHAPES[shape].min_km, shape=shape)
        course = find_best_reference_course(
            params, per_distance_budget_s=per_distance_seconds)
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
    args = parser.parse_args()
    fingerprint = graph_fingerprint()
    entries = {} if args.fresh else _read_existing(args.output, fingerprint)
    # Transfer rows that resolve to exactly the same coordinate share presets.
    unique_rows = list({(row[2], row[3]): row for row in SEOUL_METRO_STATIONS}.values())
    jobs = []
    for row in unique_rows:
        for shape in SHAPES:
            key = preset_key(row[2], row[3], shape)
            should_run = (key not in entries
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
    finally:
        if pool is not None:
            pool.shutdown()


if __name__ == "__main__":
    main()
