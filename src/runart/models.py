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


def _safe_decompress(packed: bytes, max_bytes: int) -> bytes:
    inflater = zlib.decompressobj()
    raw = inflater.decompress(packed, max_bytes + 1)
    if len(raw) > max_bytes or inflater.unconsumed_tail:
        raise ValueError("compressed payload too large")
    raw += inflater.flush(max_bytes + 1 - len(raw))
    if len(raw) > max_bytes:
        raise ValueError("compressed payload too large")
    return raw


class CourseParams(BaseModel):
    lat: float = Field(ge=37.4, le=37.72, description="Start latitude (Seoul)")
    lon: float = Field(ge=126.76, le=127.19, description="Start longitude (Seoul)")
    location_name: str = Field(default="", max_length=120)
    distance_km: float = Field(default=DEFAULT_DISTANCE_KM, ge=1.0, le=42.195)
    include_hills: bool = False
    night_mode: bool = False
    shape: str | None = Field(default=None, max_length=32)
    need_facilities: list[str] = Field(default_factory=list, max_length=8)

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
    if not isinstance(course_id, str) or len(course_id) > 4096:
        raise ValueError("course_id too large")
    padded = course_id + "=" * (-len(course_id) % 4)
    raw = _safe_decompress(base64.urlsafe_b64decode(padded), 16_384)
    raw = raw.decode()
    return CourseParams(**json.loads(raw))


def encode_shape_token(shape: str, distance_km: float) -> str:
    """Location-independent share token: the *shape* travels, not the course."""
    return f"{shape}-{distance_km:g}k"


def decode_shape_token(token: str) -> tuple[str, float]:
    if not isinstance(token, str) or len(token) > 64:
        raise ValueError("shape token too large")
    shape, _, dist = token.rpartition("-")
    distance = float(dist.rstrip("k"))
    if not shape or not (1.0 <= distance <= 42.195):
        raise ValueError("invalid shape token")
    return shape, distance
