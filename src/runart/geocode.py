"""Start-location resolution (PRD §7.3 fallback chain).

Order: ① direct coordinates ② offline gazetteer of Seoul running spots
(no network, keeps avg latency low) ③ Kakao Local keyword/address search when
`KAKAO_REST_API_KEY` is set (1s timeout, short TTL cache) ④ actionable guidance.

The API key never appears in logs, responses, or errors (PRD §8).
"""

import json
import logging
import math
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .course import CourseError
from .stations import SEOUL_METRO_STATIONS

log = logging.getLogger(__name__)

_LOCAL_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
_ADDRESS_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/address.json"
# 1s was too tight for Kakao's p95 and made lookups fail intermittently.
_TIMEOUT_S = 2.5
# Longest user-supplied location echoed back in an error message.
ECHO_LIMIT = 40
_SEOUL_BOUNDS = (37.4, 37.72, 126.76, 127.19)
# Kakao `rect` filter (lon,lat,lon,lat) covering all of Seoul. A center-point
# `radius` filter cannot cover Seoul (max 20km) and silently dropped stations
# near the city edge.
_SEOUL_RECT = "126.76,37.4,127.19,37.72"
_SEOUL_DISTRICTS = (
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구",
    "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구", "서초구", "성동구",
    "성북구", "송파구", "양천구", "영등포구", "용산구", "은평구", "종로구", "중구", "중랑구",
)


def _scope_text(text: str | None) -> str:
    return re.sub(r"[\s,.，。]", "", text or "")


def is_citywide_scope(text: str | None) -> bool:
    """Match whole scope expressions, never substrings of places/addresses."""
    city = r"서울(?:특별시|시)?"
    anywhere = r"(?:아무데나|아무곳(?:이나)?|어디(?:서든|든지?|나)|랜덤(?:으로)?|상관없(?:음|어요)|아무거나)"
    area = rf"(?:전역|시내|내|안|안의모든코스중에서|{anywhere})"
    return bool(re.fullmatch(
        rf"(?:{city}(?:에서|안에서|내에서)?(?:{area})?|{area})(?:에서|로|으로|요)?",
        _scope_text(text)))


def district_of(text: str | None) -> str | None:
    """Extract an administrative district from a Seoul address or scope."""
    match = re.search("|".join(_SEOUL_DISTRICTS), text or "")
    return match.group() if match else None


def district_scope(text: str | None) -> str | None:
    """Only a district alone (with a Seoul prefix/nearby suffix) is a scope."""
    match = re.fullmatch(
        r"(?:서울(?:특별시|시)?)?(" + "|".join(_SEOUL_DISTRICTS)
        + r")(?:근처|인근|주변|에서|내|안)?", _scope_text(text))
    return match[1] if match else None


# Derived once from bundled addresses; no geocoder, new dataset or guessed POI.
DISTRICT_STATIONS = {district: [] for district in _SEOUL_DISTRICTS}
STATION_DISTRICTS = {}
for _line, _name, _lat, _lon, _road, _lot in SEOUL_METRO_STATIONS:
    _district = district_of(_road) or district_of(_lot)
    if _district and _road.startswith("서울"):
        _label = re.sub(r"\([^)]*\)", "", _name).strip()
        _label = _label if _label.endswith("역") else f"{_label}역"
        DISTRICT_STATIONS[_district].append((_lat, _lon, _label))
        STATION_DISTRICTS[(round(_lat, 5), round(_lon, 5))] = _district

_ROAD_DISTRICT_HINTS = {
    "테헤란로": "강남구",
}
_NEIGHBORHOOD_DISTRICT_HINTS = {
    # Kakao's address search is less reliable for bare legal-dong addresses.
    # Add district context for common short forms such as "신설동 76-5".
    "신설동": "동대문구",
}

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    _SSL_CTX = ssl.create_default_context()

# Offline gazetteer: well-known running spots / landmarks across Seoul.
GAZETTEER: dict[str, tuple[float, float]] = {
    # 도심
    "서울시청": (37.5665, 126.9780), "시청": (37.5665, 126.9780),
    "광화문": (37.5760, 126.9769), "덕수궁": (37.5658, 126.9752),
    "청계천": (37.5690, 126.9880), "경복궁": (37.5796, 126.9770),
    "서울역": (37.5547, 126.9707), "명동": (37.5637, 126.9838),
    "동대문": (37.5714, 127.0095), "혜화": (37.5822, 127.0021),
    "남산": (37.5512, 126.9882), "남산타워": (37.5512, 126.9882),
    # 한강·하천
    "한강공원": (37.5285, 126.9328), "여의도한강공원": (37.5285, 126.9328),
    "반포한강공원": (37.5100, 126.9950), "뚝섬한강공원": (37.5310, 127.0660),
    "잠원한강공원": (37.5205, 127.0110), "망원한강공원": (37.5535, 126.8950),
    "이촌한강공원": (37.5170, 126.9720), "잠실한강공원": (37.5180, 127.0820),
    "중랑천": (37.5900, 127.0470), "안양천": (37.5230, 126.8790),
    "양재천": (37.4750, 127.0450), "탄천": (37.4990, 127.0700),
    "불광천": (37.5850, 126.9130), "성북천": (37.5850, 127.0230),
    # 공원·호수
    "서울숲": (37.5444, 127.0374), "올림픽공원": (37.5210, 127.1214),
    "석촌호수": (37.5061, 127.1050), "보라매공원": (37.4930, 126.9200),
    "북서울꿈의숲": (37.6210, 127.0410), "월드컵공원": (37.5710, 126.8790),
    "하늘공원": (37.5710, 126.8830), "선유도공원": (37.5430, 126.8990),
    "어린이대공원": (37.5480, 127.0810), "용산가족공원": (37.5220, 126.9790),
    "서서울호수공원": (37.5250, 126.8330), "푸른수목원": (37.4870, 126.8230),
    # 주요 역·거점
    "강남역": (37.4979, 127.0276), "강남": (37.4979, 127.0276),
    "역삼역": (37.5006, 127.0364), "선릉역": (37.5045, 127.0489),
    "잠실역": (37.5133, 127.1000), "잠실": (37.5133, 127.1000),
    "홍대입구": (37.5568, 126.9237), "홍대": (37.5568, 126.9237),
    "신촌": (37.5551, 126.9368), "이태원": (37.5346, 126.9946),
    "여의도": (37.5219, 126.9245), "성수": (37.5446, 127.0559),
    "건대입구": (37.5403, 127.0695), "왕십리": (37.5613, 127.0374),
    "신설동역": (37.5753, 127.0251), "신설동": (37.5753, 127.0251),
    "노원": (37.6542, 127.0568), "수유": (37.6380, 127.0250),
    "목동": (37.5266, 126.8644), "사당": (37.4766, 126.9816),
    "구로디지털단지": (37.4852, 126.9015), "가산디지털단지": (37.4817, 126.8827),
    "마곡": (37.5602, 126.8253), "상암": (37.5787, 126.8898),
    "은평": (37.6176, 126.9227), "천호": (37.5386, 127.1237),
    # 자주 쓰는 현재 위치 예시 주소. 공백 유무와 행정구 생략을 모두
    # 오프라인에서 처리해 Kakao API가 없거나 일시 실패해도 동작하게 한다.
    "테헤란로8길8": (37.4978, 127.0290),
    "테헤란로8길": (37.4978, 127.0290),
    "강남구테헤란로8길8": (37.4978, 127.0290),
    "서울특별시강남구테헤란로8길8": (37.4978, 127.0290),
}


def _normalize_station_query(value: str) -> str:
    """Normalize common station/address spelling without fuzzy matching."""
    value = value.strip().lower()
    value = re.sub(r"^서울(?:특별시|시)?", "서울특별시", value)
    return re.sub(r"[^0-9a-z가-힣]", "", value)


def _station_name(value: str) -> str:
    primary = re.sub(r"\([^)]*\)", "", value).strip()
    return primary if primary.endswith("역") else f"{primary}역"


def _station_aliases(line: str, official_name: str,
                     road_address: str, lot_address: str) -> set[str]:
    canonical = _station_name(official_name)
    base = canonical.removesuffix("역")
    aliases = {
        official_name, canonical, base,
        f"{line}호선 {official_name}", f"{line}호선 {canonical}",
        road_address, lot_address,
        re.sub(r"\([^)]*\)", "", road_address).strip(),
        re.sub(r"\s+\S+역\(\d+호선\)$", "", lot_address).strip(),
    }
    # Accept the common short Seoul prefix as well as the official one.
    for address in (road_address, lot_address):
        aliases.add(re.sub(r"^서울특별시", "서울", address))
        aliases.add(re.sub(r"^서울특별시", "서울시", address))
    return {alias for alias in aliases if alias}


def _build_station_lookup() -> dict[str, tuple[float, float, str]]:
    lookup: dict[str, tuple[float, float, str]] = {}
    shortened: dict[str, dict[str, tuple[float, float, str]]] = {}
    for line, official_name, lat, lon, road_address, lot_address in SEOUL_METRO_STATIONS:
        hit = (lat, lon, _station_name(official_name))
        for alias in _station_aliases(
                line, official_name, road_address, lot_address):
            # Keep the first line's point for an unqualified transfer-station
            # name; line-qualified aliases still resolve to their own record.
            lookup.setdefault(_normalize_station_query(alias), hit)
        primary = re.sub(r"\([^)]*\)", "", official_name).strip()
        base = primary.removesuffix("역")
        if base.endswith("입구") and len(base) > len("입구"):
            short = base.removesuffix("입구")
            for alias in (short, f"{short}역"):
                key = _normalize_station_query(alias)
                shortened.setdefault(key, {}).setdefault(hit[2], hit)
    # Users commonly omit 입구 (성신여대역, 홍대역). Only accept the short
    # spelling when it identifies one canonical station across every line.
    for key, stations in shortened.items():
        if len(stations) == 1:
            lookup.setdefault(key, next(iter(stations.values())))
    return lookup


_STATION_LOOKUP = _build_station_lookup()
_STATION_POINTS = tuple(dict.fromkeys(_STATION_LOOKUP.values()))


def _offline_station_search(location: str) -> tuple[float, float, str] | None:
    return _STATION_LOOKUP.get(_normalize_station_query(location))


def _echo(location: str) -> str:
    """Quote the runner's own query back, bounded and inert.

    The whole failure sentence now reaches the runner unescaped, so the one
    untrusted fragment in it has to be escaped here instead. Imported late:
    render pulls in the graph stack, and geocode is imported before it.
    """
    from .render import markdown_text
    shown = location if len(location) <= ECHO_LIMIT else location[:ECHO_LIMIT] + "…"
    return markdown_text(shown)


def _looks_like_station_query(location: str) -> bool:
    normalized = _normalize_station_query(location)
    return normalized.endswith("역") or bool(re.search(r"\d+호선", normalized))


def _station_identity(value: str) -> str:
    """Canonical station identity for strict provider-result comparison.

    This deliberately removes only transport qualifiers. It does not use
    substring or fuzzy matching: an unknown station must never match a Seoul
    shop merely because the shop name contains the query.
    """
    normalized = _normalize_station_query(value)
    normalized = re.sub(r"^(?:서울(?:특별시|시)?)?(?:지하철|전철)", "", normalized)
    return re.sub(r"\d+호선", "", normalized)


def _nearest_offline_station(lat: float, lon: float,
                             max_distance_m: float = 200.0
                             ) -> tuple[float, float, str] | None:
    """Snap Kakao subway results to the coordinates used by presets."""
    best = None
    best_m = max_distance_m
    lat_rad = math.radians(lat)
    for slat, slon, name in _STATION_POINTS:
        north_m = math.radians(slat - lat) * 6_371_000.0
        east_m = math.radians(slon - lon) * math.cos(lat_rad) * 6_371_000.0
        distance_m = math.hypot(north_m, east_m)
        if distance_m < best_m:
            best_m = distance_m
            best = (slat, slon, name)
    return best


def _in_seoul(lat: float, lon: float) -> bool:
    lat_lo, lat_hi, lon_lo, lon_hi = _SEOUL_BOUNDS
    return lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi


# Success-only cache. A short TTL keeps Kakao results fresh and bounds how long
# a raw user query remains in process memory. A lock is required because MCP
# requests can resolve locations concurrently in worker threads.
_SearchHit = tuple[float, float, str]
_SearchCacheEntry = tuple[float, _SearchHit]
_SEARCH_CACHE: dict[tuple[str, str], _SearchCacheEntry] = {}
_SEARCH_CACHE_MAX = 2048
_SEARCH_CACHE_TTL_S = max(
    0.0, float(os.environ.get("RUNART_KAKAO_CACHE_TTL_S", "600"))
)
_SEARCH_CACHE_LOCK = threading.RLock()


def _deadline_after(timeout_s: float | None) -> float | None:
    return None if timeout_s is None else time.monotonic() + max(0.01, timeout_s)


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _cache_get(kind: str, query: str) -> _SearchHit | None:
    key = (kind, query)
    with _SEARCH_CACHE_LOCK:
        entry = _SEARCH_CACHE.get(key)
        if entry is None:
            return None
        expires_at, hit = entry
        if expires_at <= time.monotonic():
            _SEARCH_CACHE.pop(key, None)
            return None
        return hit


def _cache_ok(kind: str, query: str, hit: _SearchHit) -> None:
    key = (kind, query)
    expires_at = time.monotonic() + _SEARCH_CACHE_TTL_S
    with _SEARCH_CACHE_LOCK:
        if key not in _SEARCH_CACHE and len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
            _SEARCH_CACHE.clear()
        _SEARCH_CACHE[key] = (expires_at, hit)


def _kakao_get(url: str, params: dict,
               deadline: float | None = None) -> list[dict]:
    """One Kakao Local API call with a single retry on transient failure.
    The key is read here and never leaves this function."""
    key = os.environ.get("KAKAO_REST_API_KEY")
    if not key:
        return []
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "dapi.kakao.com":
        raise ValueError("Kakao API URL must use the trusted HTTPS endpoint")
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"KakaoAK {key}"})
    for attempt in (0, 1):
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0.02:
            return []
        request_timeout = min(_TIMEOUT_S, remaining) if remaining is not None else _TIMEOUT_S
        try:
            # URL scheme and host are allowlisted immediately above.
            with urllib.request.urlopen(  # nosec B310
                    req, timeout=max(0.02, request_timeout), context=_SSL_CTX) as r:
                return json.load(r).get("documents", [])
        except urllib.error.HTTPError as exc:
            # Authentication/product errors cannot recover on retry. Distinguish
            # them from a genuine zero-result search without exposing the key.
            if exc.code in (401, 403):
                log.error("Kakao Local API unavailable: HTTP %s", exc.code)
                raise CourseError(
                    "지도 검색 서비스 연결이 준비되지 않았어요. 잠시 후 다시 시도해 주세요."
                ) from exc
            if attempt:
                return []
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                log.error("Kakao Local API TLS verification failed")
                raise CourseError(
                    "지도 검색 서비스에 안전하게 연결하지 못했어요. 잠시 후 다시 시도해 주세요."
                ) from exc
            if attempt:
                return []
        except Exception:  # noqa: BLE001 — timeout/HTTP/parse all fall through
            if attempt:
                return []
    return []


def _keyword_search(query: str, deadline: float | None = None
                    ) -> tuple[float, float, str] | None:
    """Kakao Local keyword search, restricted to Seoul via `rect`."""
    hit = _cache_get("kw", query)
    if hit:
        return hit
    docs = _kakao_get(_LOCAL_SEARCH_URL, {
        "query": query, "size": 5, "rect": _SEOUL_RECT,
    }, deadline)
    for doc in docs:
        lat, lon = float(doc["y"]), float(doc["x"])
        if _in_seoul(lat, lon):
            found = None
            if doc.get("category_group_code") == "SW8":
                found = _nearest_offline_station(lat, lon)
            if found is None:
                found = (lat, lon, doc.get("place_name") or query)
            _cache_ok("kw", query, found)
            return found
    return None


def _station_keyword_search(query: str, deadline: float | None = None
                            ) -> tuple[float, float, str] | None:
    """Resolve explicit station intent without accepting generic POIs.

    Kakao keyword search is used only as a conservative spelling/provider
    fallback. A result must be a subway category, snap to a bundled Seoul
    station, and have exactly the requested canonical station identity.
    """
    hit = _cache_get("station", query)
    if hit:
        return hit
    requested = _station_identity(query)
    docs = _kakao_get(_LOCAL_SEARCH_URL, {
        "query": query, "size": 5, "rect": _SEOUL_RECT,
    }, deadline)
    for doc in docs:
        if doc.get("category_group_code") != "SW8":
            continue
        try:
            lat, lon = float(doc["y"]), float(doc["x"])
        except (KeyError, TypeError, ValueError):
            continue
        if not _in_seoul(lat, lon):
            continue
        station = _nearest_offline_station(lat, lon)
        if station is None or _station_identity(station[2]) != requested:
            continue
        provider_name = _station_identity(doc.get("place_name") or "")
        if provider_name != requested:
            continue
        _cache_ok("station", query, station)
        return station
    return None


def _address_search(query: str, deadline: float | None = None
                    ) -> tuple[float, float, str] | None:
    """Kakao Local address search for user-entered current/home addresses.

    analyze_type=similar lets partial inputs like '관철동 7-14' (no 구/시)
    still resolve. Only Seoul-bound results are accepted because the route
    graph is Seoul-only.
    """
    hit = _cache_get("addr", query)
    if hit:
        return hit
    docs = _kakao_get(_ADDRESS_SEARCH_URL, {
        "query": query, "size": 5, "analyze_type": "similar",
    }, deadline)
    for doc in docs:
        lat, lon = float(doc["y"]), float(doc["x"])
        if _in_seoul(lat, lon):
            name = (
                (doc.get("road_address") or {}).get("address_name")
                or (doc.get("address") or {}).get("address_name")
                or query
            )
            found = (lat, lon, name)
            _cache_ok("addr", query, found)
            return found
    return None


# Address-shaped inputs (지번 '관철동 7-14', 도로명 '테헤란로8길 8', '~번지')
# must hit the address API first: keyword search treats them as POI names and
# often returns a similarly named shop — or nothing.
_ADDRESS_LIKE = re.compile(r"(?:[가-힣]+(?:동|가|로|길)\s*\d|(?:\d+-\d+)|\d+\s*번지)")


def _looks_like_address(query: str) -> bool:
    return bool(_ADDRESS_LIKE.search(query))


def _address_query_variants(query: str) -> list[str]:
    """Natural Korean address variants for Kakao address search.

    Users often omit "서울특별시 강남구" or type road-name suffixes with a
    space ("테헤란로 8길 8"). Kakao address search is stricter, so we try a
    small deterministic set of safer Seoul-prefixed variants.
    """
    q = " ".join(query.strip().split())
    if not q:
        return []
    compact_road = re.sub(r"([가-힣]+(?:로|길))\s+(\d+(?:로|길))", r"\1\2", q)
    compact_road = re.sub(r"([가-힣]+(?:로|길)\d+(?:로|길))\s+(\d+)", r"\1 \2", compact_road)

    # Normalize all common Seoul prefixes. Kakao accepts the official form
    # most consistently, especially when a district and legal-dong follow.
    official_seoul = re.sub(
        r"^서울(?:특별시|시)?\s*", "서울특별시 ", compact_road
    ).strip() if re.match(r"^서울(?:특별시|시)?", compact_road) else ""

    variants = [q, compact_road, official_seoul]
    has_seoul = q.startswith(("서울 ", "서울시", "서울특별시"))
    has_district = any(d in q for d in _SEOUL_DISTRICTS)
    for road, district in _ROAD_DISTRICT_HINTS.items():
        if road in compact_road and not has_district:
            variants.append(f"{district} {compact_road}")
            variants.append(f"서울특별시 {district} {compact_road}")
            break
    for neighborhood, district in _NEIGHBORHOOD_DISTRICT_HINTS.items():
        if neighborhood in compact_road and not has_district:
            variants.append(f"{district} {compact_road}")
            variants.append(f"서울특별시 {district} {compact_road}")
            break
    if has_district and not has_seoul:
        variants.append(f"서울특별시 {compact_road}")
    elif not has_seoul:
        variants.append(f"서울특별시 {compact_road}")

    out = []
    seen = set()
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _run_keyword_search(location: str, deadline: float | None = None
                        ) -> tuple[float, float, str] | None:
    return _keyword_search(location.strip(), deadline)


def _run_address_search(location: str, deadline: float | None = None
                        ) -> tuple[float, float, str] | None:
    for query in _address_query_variants(location):
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0.02:
            return None
        hit = _address_search(query, deadline)
        if hit:
            return hit
    return None


def resolve_location(location: str | None, lat: float | None, lon: float | None,
                     timeout_s: float | None = None
                     ) -> tuple[float, float, str]:
    deadline = _deadline_after(timeout_s)
    if lat is not None and lon is not None:
        if not _in_seoul(lat, lon):
            raise CourseError("서울 지역 좌표만 지원해요. 서울 시내 위치로 다시 알려주세요.")
        return lat, lon, location or f"({lat:.4f}, {lon:.4f})"
    if lat is not None or lon is not None:
        raise CourseError("위도와 경도를 함께 알려주세요.")
    if is_citywide_scope(location):
        raise CourseError("어디에서 출발할지 조금 더 좁혀서 알려주세요. 구 이름만 알려주셔도 충분해요.")
    if district_scope(location):
        raise CourseError("구 단위 새 코스는 코스 추천을 요청해 주세요. 출발점 변경에는 역·주소 등 특정 위치가 필요해요.")
    if location:
        key = location.replace(" ", "")
        # Explicit station intent must use the same canonical coordinates as
        # the bundled animal presets, even when a landmark alias also exists.
        station_intent = _looks_like_station_query(location)
        if station_intent:
            station = _offline_station_search(location)
            if station:
                return station
            station = _station_keyword_search(location, deadline)
            if station:
                return station
            # Station intent is fail-closed. In particular, do not continue to
            # generic keyword/address/fuzzy lookup: "부산역" previously became
            # the Seoul shop "부산아지매국밥 명일역점" through that path.
            shown = _echo(location)
            raise CourseError(
                f"'{shown}' 위치를 찾지 못했어요. 현재 서울 지역의 지하철역만 "
                "출발지로 지원해요. 서울의 정확한 역 이름을 알려주세요."
            )
        # ① exact offline landmark aliases kept for backward compatibility.
        for name, (glat, glon) in GAZETTEER.items():
            if name == key:
                return glat, glon, name
        # ② all 289 Seoul Metro station/address rows (no network, instant).
        station = _offline_station_search(location)
        if station:
            return station
        # ③ Kakao API. Address-shaped inputs (관철동 7-14, 테헤란로8길 8)
        # go to the address API first — keyword search treats them as POI
        # names; place-shaped inputs (station/landmark names) go keyword
        # first. Each is the other's fallback.
        is_address = _looks_like_address(location)
        searches = (
            [_run_address_search, _run_keyword_search] if is_address
            else [_run_keyword_search, _run_address_search]
        )
        for search in searches:
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0.02:
                break
            hit = search(location, deadline)
            if hit:
                return hit
        # ④ fuzzy gazetteer LAST, and only for short place-like queries.
        # Running it before the API resolved any address containing "강남"
        # or "은평" to the wrong station.
        if not is_address and 2 <= len(key) <= 10:
            for name, (glat, glon) in GAZETTEER.items():
                if name in key or key in name:
                    return glat, glon, name
        known = "신설동역, 시청, 강남역, 여의도한강공원, 서울숲"
        # Echo the input back so the user sees what we searched for, but cap it:
        # an LLM can pass a very long string and the whole thing would otherwise
        # land in the tool result (PlayMCP asks for minimal result size).
        shown = _echo(location)
        # The missing-API-key state is an operator concern; it is already logged
        # at startup and must not be described to end users.
        raise CourseError(
            f"'{shown}' 위치를 찾지 못했어요. 역 이름, 가게 상호명, 도로명 주소, "
            "지번 주소 모두 쓸 수 있어요. "
            "예: 성수역, 스타벅스 서울숲점, 서울 성동구 아차산로 100, 마포구 상암동 1601. "
            f"근처 역으로 시작하려면: {known}"
        )
    raise CourseError(
        "출발 위치가 필요해요. 역 이름, 가게 상호명, 도로명·지번 주소 중 하나를 "
        "알려주세요. 예: 강남역, 스타벅스 서울숲점, 서울 중구 세종대로 110"
    )
