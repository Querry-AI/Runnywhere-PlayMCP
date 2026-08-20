"""Kakao Tools widget serialization for a single, already-built course.

This module is presentation-only.  It never generates or restores a course;
the MCP boundary must hand it a Course that is already available in memory.
"""

import json
import re
import unicodedata
import urllib.parse

from .course import Course
from .naming import course_badges, course_title

WIDGET_NAME = "runnywhere_course"
WIDGET_MAX_BYTES = 12_000
_COURSE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,4096}$")
_COPY_MARKUP_RE = re.compile(r"[\\`*_{}\[\]<>#|]")


class WidgetBuildError(ValueError):
    """The course cannot be represented by the supported Kakao contract."""


class WidgetTooLargeError(WidgetBuildError):
    """The serialized widget exceeded Runnywhere's conservative size cap."""


def _plain_text(value: object, max_chars: int) -> str:
    """Return bounded single-line text without control characters."""
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = "".join(
        ch for ch in text if not unicodedata.category(ch).startswith("C")
    )
    return text[:max_chars]


def _copy_value(value: object, max_chars: int) -> str:
    """Remove tokens that could turn a data value into Kakao share markup."""
    return _COPY_MARKUP_RE.sub("", _plain_text(value, max_chars)).strip()


def _origin(base_url: str) -> str:
    value = base_url.rstrip("/")
    parts = urllib.parse.urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise WidgetBuildError("base_url must be an HTTP(S) origin")
    return value


def _target_url(url: str) -> dict:
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username
        or parts.password
    ):
        raise WidgetBuildError("button target must be HTTP(S)")
    return {"url": url, "pcUrl": url}


def _button(label: str, url: str) -> dict:
    return {
        "type": "Button",
        "label": label,
        "onClickAction": {"payload": {"target": _target_url(url)}},
    }


def _has_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_has_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_has_key(child, key) for child in value)
    return False


def _validate_envelope(payload: dict) -> None:
    if set(payload) != {"widget", "copy_text", "name"}:
        raise WidgetBuildError("unexpected widget envelope")
    if payload["name"] != WIDGET_NAME or _has_key(payload, "status"):
        raise WidgetBuildError("unsupported widget metadata")
    card = payload["widget"]
    if not isinstance(card, dict) or card.get("type") != "Card":
        raise WidgetBuildError("widget must be a Card")
    children = card.get("children")
    if not isinstance(children, list) or not children:
        raise WidgetBuildError("widget Card must have children")
    for child in children:
        if not isinstance(child, dict):
            raise WidgetBuildError("widget child must be an object")
        if child.get("type") == "Text":
            if set(child) != {"type", "value"} or not child["value"]:
                raise WidgetBuildError("Text requires value")
        elif child.get("type") == "Button":
            if not child.get("label"):
                raise WidgetBuildError("Button requires label")
            try:
                target = child["onClickAction"]["payload"]["target"]
                _target_url(target["url"])
                if "pcUrl" in target:
                    _target_url(target["pcUrl"])
            except (KeyError, TypeError) as exc:
                raise WidgetBuildError("Button target is incomplete") from exc
        else:
            raise WidgetBuildError("unsupported widget child")
    if not isinstance(payload["copy_text"], str) or not payload["copy_text"]:
        raise WidgetBuildError("copy_text is required")


def _serialize(payload: dict) -> str:
    _validate_envelope(payload)
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    )
    if len(serialized.encode("utf-8")) >= WIDGET_MAX_BYTES:
        raise WidgetTooLargeError("widget is too large")
    return serialized


def build_course_widget(
    course: Course,
    course_id: str,
    base_url: str,
    *,
    lead_text: str = "",
) -> str:
    """Return a compact Kakao Card envelope for one confirmed course."""
    if not _COURSE_ID_RE.fullmatch(course_id):
        raise WidgetBuildError("invalid course id")
    origin = _origin(base_url)
    preview_url = f"{origin}/c/{course_id}"
    gpx_url = f"{preview_url}.gpx"

    badges = course_badges(course)
    badge_text = "".join(_plain_text(badge.get("emoji", ""), 4) for badge in badges)
    title = _plain_text(f"{badge_text} {course_title(course)}", 80)
    location = _plain_text(course.params.location_name or "지정한 출발점", 120)
    metrics = _plain_text(
        f"{course.length_km:.1f}km · 누적 오르막 {course.ascent_m:.0f}m",
        120,
    )

    children: list[dict] = []
    lead = _copy_value(lead_text, 180)
    if lead:
        children.append({"type": "Text", "value": lead})
    children.extend([
        {"type": "Text", "value": title},
        {"type": "Text", "value": metrics},
        {"type": "Text", "value": f"출발·도착: {location}"},
        _button("코스 지도 열기", preview_url),
        _button("GPX 다운로드", gpx_url),
    ])

    copy_title = _copy_value(title, 80)
    copy_location = _copy_value(location, 120)
    copy_text = "\n".join([
        f"**{copy_title}**",
        f"- 거리: {course.length_km:.1f}km",
        f"- 출발·도착: {copy_location}",
        f"- 지도: {preview_url}",
    ])
    return _serialize({
        "widget": {"type": "Card", "children": children},
        "copy_text": copy_text,
        "name": WIDGET_NAME,
    })
