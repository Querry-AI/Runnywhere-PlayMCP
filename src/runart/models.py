"""Course parameter model and self-contained stateless course ids.

A course_id encodes the full parameter set (compressed, urlsafe). Any server
instance can reconstruct and re-generate the exact same course from the id
alone — no session, no database (PRD §5.1 stateless design).
"""

import base64
import json
import zlib

from pydantic import BaseModel, Field

FACILITY_TYPES = ("convenience_store", "restroom", "water", "park")

DEFAULT_DISTANCE_KM = 5.0
DEFAULT_PACE_MIN_PER_KM = 6.5


class CourseParams(BaseModel):
    lat: float = Field(ge=37.4, le=37.72, description="Start latitude (Seoul)")
    lon: float = Field(ge=126.76, le=127.19, description="Start longitude (Seoul)")
    location_name: str = ""
    distance_km: float = Field(default=DEFAULT_DISTANCE_KM, ge=1.0, le=42.195)
    include_hills: bool = False
    night_mode: bool = False
    shape: str | None = None
    need_facilities: list[str] = Field(default_factory=list)

    def canonical(self) -> dict:
        d = self.model_dump()
        d["lat"] = round(d["lat"], 5)
        d["lon"] = round(d["lon"], 5)
        d["distance_km"] = round(d["distance_km"], 2)
        d["need_facilities"] = sorted(set(d["need_facilities"]) & set(FACILITY_TYPES))
        return d


def encode_course_id(params: CourseParams) -> str:
    raw = json.dumps(params.canonical(), sort_keys=True, separators=(",", ":"))
    packed = base64.urlsafe_b64encode(zlib.compress(raw.encode(), 9)).decode().rstrip("=")
    return packed


def decode_course_id(course_id: str) -> CourseParams:
    padded = course_id + "=" * (-len(course_id) % 4)
    raw = zlib.decompress(base64.urlsafe_b64decode(padded)).decode()
    return CourseParams(**json.loads(raw))


def encode_shape_token(shape: str, distance_km: float) -> str:
    """Location-independent share token: the *shape* travels, not the course."""
    return f"{shape}-{distance_km:g}k"


def decode_shape_token(token: str) -> tuple[str, float]:
    shape, _, dist = token.rpartition("-")
    return shape, float(dist.rstrip("k"))
