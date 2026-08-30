#!/usr/bin/env python3
"""Check that every bundled landmark can actually start a course.

A gazetteer entry is a hand-picked coordinate, and a landmark is an area
rather than a point: 남산 sat on the summit, 여의도 mid-island, 망원한강공원 on
a riverside spur. The pedestrian graph cannot close a loop from any of them,
so every distance failed with "순환 코스를 만들지 못했어요" -- invisibly, because
the catalogue simply stored the failure and served it instantly.

    python scripts/audit_gazetteer_starts.py            # every entry
    python scripts/audit_gazetteer_starts.py 남산 여의도   # only these
    python scripts/audit_gazetteer_starts.py --search    # propose new coords

Exit status is 1 when any entry builds nothing at all, so this can gate a
release. Entries that miss some distances but hold the 5km default pass:
an island park genuinely has no 4km loop, and the runtime answers those with
a measured near-miss rather than a refusal.
"""

from __future__ import annotations

import argparse
import math
import sys

from runart.course import CourseError, course_route_issues, generate_course
from runart.geo import haversine_m
from runart.geocode import GAZETTEER, resolve_location
from runart.graph import get_graph
from runart.models import CourseParams
from runart.standard_presets import DEFAULT_DISTANCES_KM

DEFAULT_KM = 5.0


def _builds(lat: float, lon: float, name: str, distances) -> list[float | None]:
    """Actual length per requested distance; None where the catalogue would drop it.

    Generation succeeding is not enough. The build also runs
    course_route_issues, and a coordinate with no graph node near it produces
    a route that starts hundreds of metres away -- stored as nothing at all
    (start_too_far). Applying the same check here is the difference between
    an audit that agrees with the catalogue and one that does not.
    """
    out: list[float | None] = []
    for km in distances:
        try:
            course = generate_course(CourseParams(
                lat=lat, lon=lon, location_name=name, distance_km=km,
                include_hills=False, night_mode=False, need_facilities=[],
                shape=None))
            out.append(None if course_route_issues(course, get_graph())
                       else round(course.length_km, 2))
        except (CourseError, Exception):  # noqa: BLE001 - any refusal is a miss
            out.append(None)
    return out


def _ring(lat: float, lon: float, radius_m: float, points: int = 16):
    for i in range(points):
        theta = 2 * math.pi * i / points
        yield (lat + (radius_m * math.cos(theta)) / 111320.0,
               lon + (radius_m * math.sin(theta)) / (111320.0 * math.cos(math.radians(lat))))


def _propose(lat: float, lon: float, name: str, distances) -> tuple | None:
    """Nearest point that builds every distance, searched outward."""
    for radius in (60, 90, 120, 150, 190, 230, 270, 300):
        for clat, clon in _ring(lat, lon, radius):
            if all(x is not None for x in _builds(clat, clon, name, distances)):
                return round(clat, 4), round(clon, 4), haversine_m(lat, lon, clat, clon)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*", help="entries to check (default: all)")
    parser.add_argument("--search", action="store_true",
                        help="propose a replacement coordinate for each failure")
    parser.add_argument("--distances", type=float, nargs="*",
                        default=list(DEFAULT_DISTANCES_KM))
    args = parser.parse_args()

    names = args.names or list(GAZETTEER)
    dead: list[str] = []
    partial: list[str] = []

    for name in names:
        if name not in GAZETTEER:
            print(f"{name}: not in the gazetteer", file=sys.stderr)
            return 2
        # Resolve the way a live request does. A name ending in 역 is answered
        # from the station table, which shadows its gazetteer row entirely --
        # reading GAZETTEER directly reported 서울역 as dead when the coordinate
        # a runner actually gets, 370m away, builds every distance.
        listed = GAZETTEER[name]
        lat, lon, resolved_name = resolve_location(name, None, None, timeout_s=1.0)
        shadowed = haversine_m(listed[0], listed[1], lat, lon) > 1.0
        lengths = _builds(lat, lon, resolved_name, args.distances)
        built = [km for km, got in zip(args.distances, lengths) if got is not None]
        holds_default = DEFAULT_KM in built or not any(
            abs(km - DEFAULT_KM) < 1e-9 for km in args.distances)

        if not built or not holds_default:
            status = "DEAD"
            dead.append(name)
        elif len(built) < len(args.distances):
            status = "partial"
            partial.append(name)
        else:
            status = "ok"

        shown = " ".join(f"{km:g}:{'-' if got is None else format(got, '.1f')}"
                         for km, got in zip(args.distances, lengths))
        mark = " (shadowed)" if shadowed else ""
        print(f"{name:<22}{status:<8}{len(built)}/{len(args.distances)}  {shown}{mark}")

        if args.search and status == "DEAD":
            found = _propose(lat, lon, resolved_name, args.distances)
            print(f"    -> ({found[0]}, {found[1]}) {found[2]:.0f}m away" if found
                  else "    -> no loop-capable point within 300m")
        sys.stdout.flush()

    print(f"\n{len(names)} entries · dead {len(dead)} · partial {len(partial)}")
    if dead:
        print("dead:", ", ".join(dead))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
