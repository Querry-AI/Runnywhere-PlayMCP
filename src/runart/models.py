"""Course parameter model and self-contained stateless course ids.

A course_id encodes the full parameter set (compressed, urlsafe). Any server
instance can reconstruct and re-generate the exact same course from the id
alone — no session, no database (PRD §5.1 stateless design).
"""

import base64
import json
import re
import zlib

from pydantic import BaseModel, Field, model_validator

FACILITY_TYPES = ("convenience_store", "restroom", "water", "park")

COURSE_NAME_MAX_CHARS = 24
# Control characters and the bidi overrides. A course name is echoed into a
# page title, a GPX file name and an MCP markdown reply; escaping covers the
# markup, this covers the characters escaping cannot make safe to display.
_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean_course_name(name: str) -> str:
    """A course name as it is safe to store and show, or "" for none."""
    if not isinstance(name, str):
        return ""
    text = _UNSAFE_NAME_CHARS.sub("", name)
    return " ".join(text.split())[:COURSE_NAME_MAX_CHARS].strip()



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


class CourseWaypoint(BaseModel):
    lat: float = Field(ge=37.4, le=37.72)
    lon: float = Field(ge=126.76, le=127.19)


class CourseParams(BaseModel):
    lat: float = Field(ge=37.4, le=37.72, description="Start latitude (Seoul)")
    lon: float = Field(ge=126.76, le=127.19, description="Start longitude (Seoul)")
    location_name: str = Field(default="", max_length=120)
    distance_km: float = Field(default=DEFAULT_DISTANCE_KM, ge=1.0, le=42.195)
    include_hills: bool = False
    night_mode: bool = False
    # Internal route alternatives, not an extra user-facing tool call. Zero
    # keeps legacy generation and IDs unchanged; nonzero rotates the search.
    route_variant: int = Field(default=0, ge=0, le=7)
    shape: str | None = Field(default=None, max_length=32)
    need_facilities: list[str] = Field(default_factory=list, max_length=8)
    manual_waypoints: list[CourseWaypoint] = Field(default_factory=list, max_length=6)
    manual_path: list[int] = Field(default_factory=list, max_length=1200)
    # A name the runner typed when saving an edit. Empty means "use the
    # generated title", and canonical() drops it in that case so every
    # course_id minted before this field existed still decodes byte-identically.
    custom_name: str = Field(default="", max_length=24)

    @model_validator(mode="after")
    def validate_manual_waypoints(self):
        if self.manual_waypoints and len(self.manual_waypoints) < 2:
            raise ValueError("manual courses require at least two waypoints")
        if self.manual_path and len(self.manual_path) < 3:
            raise ValueError("manual path requires at least three nodes")
        return self

    def canonical(self) -> dict:
        d = self.model_dump()
        d["lat"] = round(d["lat"], 5)
        d["lon"] = round(d["lon"], 5)
        d["distance_km"] = round(d["distance_km"], 2)
        d["need_facilities"] = sorted(set(d["need_facilities"]) & set(FACILITY_TYPES))
        if not d["route_variant"]:
            d.pop("route_variant")
        if d["manual_waypoints"]:
            d["manual_waypoints"] = [
                {"lat": round(point["lat"], 5), "lon": round(point["lon"], 5)}
                for point in d["manual_waypoints"]
            ]
        else:
            # Preserve the compact legacy payload and therefore every existing id.
            d.pop("manual_waypoints")
        if not d["manual_path"]:
            d.pop("manual_path")
        d["custom_name"] = clean_course_name(d["custom_name"])
        if not d["custom_name"]:
            d.pop("custom_name")
        return d


def encode_course_id(params: CourseParams) -> str:
    raw = json.dumps(params.canonical(), sort_keys=True, separators=(",", ":"))
    packed = base64.urlsafe_b64encode(zlib.compress(raw.encode(), 9)).decode().rstrip("=")
    return packed


def decode_course_id(course_id: str) -> CourseParams:
    if not isinstance(course_id, str) or len(course_id) > 4096:
        raise ValueError("course_id too large")
    padded = course_id + "=" * (-len(course_id) % 4)
    try:
        raw = _safe_decompress(base64.urlsafe_b64decode(padded), 16_384)
        raw = raw.decode()
        return CourseParams(**json.loads(raw))
    except ValueError:
        # binascii.Error and json.JSONDecodeError are both ValueError subclasses.
        raise
    except Exception as exc:
        # zlib.error is NOT a ValueError; letting it escape leaked
        # "Error -3 while decompressing data" through the MCP tool result.
        raise ValueError("invalid course_id") from exc


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
