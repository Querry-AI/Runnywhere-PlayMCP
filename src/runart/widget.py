"""Kakao Tools widget serialization for a single, already-built course.

This module is presentation-only.  It never generates or restores a course;
the MCP boundary must hand it a Course that is already available in memory.
"""

import json
import re
import unicodedata
import urllib.parse

from .course import Course
from .courseplan import CourseChoice
from .insights import CourseFacts, course_facts
from .naming import RUN_NAMES_KO, TRACK_EMOJI, short_place
from .pace import DEFAULT_PACE_S, effort
from .shapes import SHAPES

WIDGET_NAME = "runnywhere_course"
# Keep the two useful traits; the badge row wraps whole tags on narrow phones.
CARD_TRAIT_LIMIT = 2
FACILITY_EMOJI = {"convenience_store": "🏪", "restroom": "🚻"}
# The supplied dark reference uses white titles and quieter grey metrics.
# Keep the light theme legible too; the host still owns the chat background.
LISTING_TEXT = {"light": "#202020", "dark": "#f5f5f5"}
LISTING_SECONDARY = {"light": "#666666", "dark": "#a6a6a6"}
WIDGET_MAX_BYTES = 12_000
# A Kakao button label is one line on a phone; past this it truncates anyway.
LABEL_MAX_CHARS = 60
_COURSE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,4096}$")
_COPY_MARKUP_RE = re.compile(r"[\\`*_{}\[\]<>#|]")
_PAREN_SUFFIX_RE = re.compile(r"\([^()]*\)")


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


def _title_place(location_name: str) -> str:
    """Keep station titles compact by dropping parenthetical aliases."""
    place = short_place(location_name)
    return _PAREN_SUFFIX_RE.sub("", place).strip()


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
    listing = payload["widget"]
    if not isinstance(listing, dict) or listing.get("type") != "Card":
        raise WidgetBuildError("course listings require the Kakao-supported Card root")
    if set(listing) != {"type", "children", "size", "padding", "border", "background"}:
        raise WidgetBuildError("unsupported Card properties")
    if listing["size"] != "md" or listing["padding"] != {"x": "12px", "top": "12px"}:
        raise WidgetBuildError("course listings require a small horizontal inset")
    children = listing.get("children")
    if not isinstance(children, list) or not children:
        raise WidgetBuildError("widget Card must have children")
    for child in children:
        _validate_component(child)
    if not isinstance(payload["copy_text"], str) or not payload["copy_text"]:
        raise WidgetBuildError("copy_text is required")


def _validate_component(component: object) -> None:
    """Validate the small ChatKit subset rendered by Kakao Preview."""
    if not isinstance(component, dict):
        raise WidgetBuildError("widget child must be an object")
    kind = component.get("type")
    if kind == "Markdown":
        if set(component) != {"type", "value"} or not component.get("value"):
            raise WidgetBuildError("Markdown requires introductory text")
        return
    if kind in {"Row", "Col"}:
        allowed = {
            "type", "children", "gap", "align", "flex", "wrap", "minWidth",
            "padding",
        }
        if not set(component) <= allowed:
            raise WidgetBuildError(f"unsupported {kind} properties")
        children = component.get("children")
        if not isinstance(children, list) or not children:
            raise WidgetBuildError(f"{kind} requires children")
        for child in children:
            _validate_component(child)
        return
    if kind in {"Title", "Caption", "Text"}:
        allowed = {"type", "value", "size", "weight", "maxLines", "color"}
        if not set(component) <= allowed or not component.get("value"):
            raise WidgetBuildError(f"{kind} requires bounded text")
        return
    if kind == "Image":
        allowed = {
            "type", "src", "alt", "width", "height", "fit", "radius", "frame",
        }
        if not set(component) <= allowed or not component.get("alt"):
            raise WidgetBuildError("Image requires src and alt")
        _target_url(component.get("src", ""))
        return
    if kind == "Badge":
        allowed = {"type", "label", "size", "color", "variant", "pill"}
        if (
            not set(component) <= allowed
            or not isinstance(component.get("label"), str)
            or not component["label"]
            or component.get("color") != "info"
            or component.get("variant") != "soft"
            or component.get("size") != "sm"
            or component.get("pill") is not True
        ):
            raise WidgetBuildError("course tags must be soft blue pills")
        return
    if kind == "Button":
        allowed = {
            "type", "label", "onClickAction", "style", "variant", "size", "block",
            "pill", "color",
        }
        if not set(component) <= allowed or not component.get("label"):
            raise WidgetBuildError("Button requires label")
        try:
            target = component["onClickAction"]["payload"]["target"]
            _target_url(target["url"])
            if "pcUrl" in target:
                _target_url(target["pcUrl"])
        except (KeyError, TypeError) as exc:
            raise WidgetBuildError("Button target is incomplete") from exc
        return
    raise WidgetBuildError("unsupported widget child")


def _serialize(payload: dict) -> str:
    _validate_envelope(payload)
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    )
    if len(serialized.encode("utf-8")) >= WIDGET_MAX_BYTES:
        raise WidgetTooLargeError("widget is too large")
    return serialized


def _choice_label(choice: CourseChoice) -> str:
    """Compact alternative label: type, route length, and actual start."""
    shape = choice.course.params.shape or ""
    run_name = RUN_NAMES_KO.get(shape, "일반런")
    label = (
        f"{choice.emoji} {run_name} · {choice.course.length_km:.1f}km · "
        f"{choice.start_name}"
    )
    return _plain_text(label, LABEL_MAX_CHARS)


def _widget_title(course: Course) -> str:
    shape = SHAPES.get(course.params.shape or "")
    place = _title_place(course.params.location_name or "")
    if shape:
        return _plain_text(
            f"{shape.emoji} {place + ' ' + RUN_NAMES_KO[shape.key] if place else RUN_NAMES_KO[shape.key]}",
            40,
        )
    return _plain_text(f"{TRACK_EMOJI} {place + '런' if place else '러닝 코스'}", 40)


def _effort_line(course: Course) -> str:
    """Distance and the time it takes at the page's default pace.

    Ascent used to sit here, but a runner picking between three cards is
    budgeting minutes, not metres.  The number must be the one the detail
    page opens with, or the card promises a run the page then contradicts.
    """
    minutes = effort(course.length_km, DEFAULT_PACE_S)["duration_min"]
    return _plain_text(f"{course.length_km:.1f}km · 약 {minutes}분", 120)


def _card_traits(facts: CourseFacts) -> tuple[dict, ...]:
    """Grade plus whatever most distinguishes this course from its neighbours.

    Terrain is the second trait on the detail page, but three courses around
    one station are almost always all 도심 위주 -- repeating it on every card
    tells a runner comparing them nothing.  A specific opinion (신호, 조명,
    보도, 야간) wins the slot whenever the course has one.
    """
    if not facts.traits:
        return ()
    grade, *rest = facts.traits
    specific = next((trait for trait in rest[1:]), None)
    second = specific or (rest[0] if rest else None)
    return (grade, second) if second else (grade,)


def _character_tags(facts: CourseFacts) -> dict:
    """Reference category pills, above the title, with no outlined chips.

    Each tag wraps as a unit so a facility count cannot be separated from
    its icon. Do not truncate the whole row and hide the last facilities.
    """
    parts = [
        f"{trait['emoji']} {trait['label']}"
        for trait in _card_traits(facts)[:CARD_TRAIT_LIMIT]
    ]
    parts.extend(
        f"{FACILITY_EMOJI[kind]} {count}"
        for kind in FACILITY_EMOJI
        if (count := facts.facility_counts.get(kind, 0))
    )
    return {
        "type": "Row", "gap": "4px", "align": "center", "wrap": "wrap",
        "children": [
            {"type": "Badge", "label": _plain_text(part, 60), "size": "sm",
             "color": "info", "variant": "soft", "pill": True}
            for part in parts
        ],
    }


def _section_heading(title: str, *, lead: bool) -> dict:
    """One line naming the group.

    The explanatory caption under each heading said what the rows below it
    already showed, and two lines of chrome per group is what made a
    three-course card scroll.
    """
    return {
        "type": "Title" if lead else "Text",
        "value": _plain_text(title, 40), "size": "sm",
        "weight": "bold", "color": "#000000", "maxLines": 1,
    }


def _ascent_line(course: Course) -> str:
    """A quiet secondary metric below the distance/time pair."""
    return _plain_text(f"오르막 {course.ascent_m:.0f}m", 80)


def _course_card_row(course: Course, course_id: str, origin: str,
                     match_note: str = "") -> dict:
    """Reference listing: square image left, full information column right.

    The action belongs in the last line of that column, never in a third
    column that steals width from every title and metadata line.
    """
    preview_url = f"{origin}/c/{course_id}"
    title = _widget_title(course)
    facts = course_facts(course)
    button = _button("지도 보기", preview_url)
    button.update({
        "style": "primary", "color": "primary", "variant": "solid",
        "size": "sm", "pill": True,
    })
    return {
        "type": "Row",
        "gap": "8px",
        "align": "center",
        "padding": {"top": "8px", "bottom": "8px"},
        "children": [
            {
                "type": "Image",
                "src": f"{preview_url}/thumb.svg",
                "alt": _plain_text(f"{title} 실제 지도 코스", 120),
                "width": 88,
                "height": 88,
                "fit": "contain",
                "radius": "md",
                "frame": False,
            },
            {
                "type": "Col", "gap": "6px", "flex": 1, "minWidth": 0,
                "children": [
                    _character_tags(facts),
                    # Kakao's smallest Title is 18px; Text sm matches the
                    # reference listing's 14px title without a heading bump.
                    {"type": "Text", "value": title, "size": "sm", "weight": "semibold", "color": LISTING_TEXT, "maxLines": 1},
                    {
                        "type": "Row", "gap": "12px", "align": "center",
                        "children": [
                            {"type": "Col", "gap": "4px", "flex": 1, "minWidth": 0, "children": [
                                {"type": "Text", "value": _effort_line(course), "size": "sm", "weight": "bold", "color": LISTING_TEXT, "maxLines": 2},
                                {"type": "Caption", "value": _ascent_line(course), "size": "lg", "color": LISTING_SECONDARY, "maxLines": 1},
                            ]},
                            button,
                        ],
                    },
                ],
            },
        ],
    }


def course_response_facts(course: Course, course_id: str, base_url: str) -> dict:
    """Ground assistant prose in the same course and name as its widget row."""
    if not _COURSE_ID_RE.fullmatch(course_id):
        raise WidgetBuildError("invalid course id")
    shape = course.params.shape
    spec = SHAPES.get(shape or "")
    return {
        "course_id": course_id,
        "title": _copy_value(_widget_title(course), 80),
        "course_type": shape or "standard",
        "shape_label": f"{spec.name_ko} 모양" if spec else "일반 러닝",
        "start": _copy_value(course.params.location_name or "지정한 출발점", 120),
        "distance_km": round(course.length_km, 1),
        "duration_min": effort(course.length_km, DEFAULT_PACE_S)["duration_min"],
        "ascent_m": round(course.ascent_m),
        "map_url": f"{_origin(base_url)}/c/{course_id}",
    }


def build_course_widget(
    course: Course,
    course_id: str,
    base_url: str,
    *,
    alternatives: tuple[CourseChoice, ...] = (),
    primary_note: str = "",
    intro_text: str = "",
) -> str:
    """Return a Kakao-compatible listing for one confirmed course.

    ``alternatives`` are the other courses the runner may pick instead; each
    becomes a matching listing row below the primary course. Kakao renders the
    widget verbatim, so a choice that is not a button here is a choice the
    runner cannot reach.
    """
    if not _COURSE_ID_RE.fullmatch(course_id):
        raise WidgetBuildError("invalid course id")
    for choice in alternatives:
        if not _COURSE_ID_RE.fullmatch(choice.course_id):
            raise WidgetBuildError("invalid course id")
    origin = _origin(base_url)
    title = _widget_title(course)
    location = _title_place(course.params.location_name or "지정한 출발점")
    preview_url = f"{origin}/c/{course_id}"
    # Without a heading the cards read as an undifferentiated list and the
    # runner cannot tell which one answered their request.
    children: list[dict] = [
        _section_heading("추천 코스", lead=True),
        _course_card_row(course, course_id, origin, primary_note),
    ]
    if intro_text:
        # Kakao may render the tool widget before any assistant prose. A
        # Markdown sibling before the listing guarantees the explanation
        # appears first while preserving content[0]'s widget contract.
        children.insert(0, {"type": "Markdown", "value": _copy_value(intro_text, 600)})
    if alternatives:
        children.append(_section_heading("다른 코스도 있어요", lead=False))
        for choice in alternatives:
            children.append(_course_card_row(
                choice.course, choice.course_id, origin, choice.match_note
            ))

    copy_title = _copy_value(title, 80)
    copy_location = _copy_value(location, 120)
    copy_lines = [
        f"**{copy_title}**",
        f"- {copy_location} 출발·도착",
        f"- {_copy_value(_effort_line(course), 120)}",
        f"- 지도: {preview_url}",
        "- 경로 데이터: © OpenStreetMap contributors · ODbL 1.0",
    ]
    if primary_note:
        copy_lines.insert(1, f"- {_copy_value(primary_note, 160)}")
    if alternatives:
        copy_lines.extend(
            f"- {_copy_value(_choice_label(choice), LABEL_MAX_CHARS)}"
            + (f" · {_copy_value(choice.match_note, 160)}" if choice.match_note else "")
            for choice in alternatives
        )
    copy_text = "\n".join(copy_lines)
    if intro_text:
        copy_text = f"{_copy_value(intro_text, 600)}\n\n{copy_text}"
    return _serialize({
        "widget": {
            # Kakao drops Basic roots even though upstream ChatKit supports
            # them. Keep its proven Card envelope. The host may ignore border
            # overrides; do not trade a working widget for unverified roots.
            "type": "Card", "size": "md",
            "padding": {"x": "12px", "top": "12px"},
            "border": {"size": 0, "color": "transparent"},
            "background": "transparent",
            "children": children,
        },
        "copy_text": copy_text,
        "name": WIDGET_NAME,
    })
