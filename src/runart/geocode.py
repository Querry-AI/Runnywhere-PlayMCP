"""Start-location resolution (PRD §7.3 fallback chain).

Order: ① direct coordinates ② offline gazetteer of Seoul running spots
(no network, keeps avg latency low) ③ Kakao Local keyword/address search when
`KAKAO_REST_API_KEY` is set (1s timeout, LRU-cached) ④ actionable guidance.

The API key never appears in logs, responses, or errors (PRD §8).
"""

import json
import os
import re
import ssl
import urllib.parse
import urllib.request

from .course import CourseError

_LOCAL_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
_ADDRESS_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/address.json"
# 1s was too tight for Kakao's p95 and made lookups fail intermittently.
_TIMEOUT_S = 2.5
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
_ROAD_DISTRICT_HINTS = {
    "테헤란로": "강남구",
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


def _in_seoul(lat: float, lon: float) -> bool:
    lat_lo, lat_hi, lon_lo, lon_hi = _SEOUL_BOUNDS
    return lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi


# Success-only caches. functools.lru_cache would also cache a None produced
# by one transient timeout, permanently killing that query until restart.
_SEARCH_CACHE: dict[tuple[str, str], tuple[float, float, str]] = {}
_SEARCH_CACHE_MAX = 2048


def _cache_get(kind: str, query: str):
    return _SEARCH_CACHE.get((kind, query))


def _cache_ok(kind: str, query: str, hit: tuple[float, float, str]) -> None:
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[(kind, query)] = hit


def _kakao_get(url: str, params: dict) -> list[dict]:
    """One Kakao Local API call with a single retry on transient failure.
    The key is read here and never leaves this function."""
    key = os.environ.get("KAKAO_REST_API_KEY")
    if not key:
        return []
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"KakaoAK {key}"})
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as r:
                return json.load(r).get("documents", [])
        except Exception:  # noqa: BLE001 — timeout/HTTP/parse all fall through
            if attempt:
                return []
    return []


def _keyword_search(query: str) -> tuple[float, float, str] | None:
    """Kakao Local keyword search, restricted to Seoul via `rect`."""
    hit = _cache_get("kw", query)
    if hit:
        return hit
    docs = _kakao_get(_LOCAL_SEARCH_URL, {
        "query": query, "size": 5, "rect": _SEOUL_RECT,
    })
    for doc in docs:
        lat, lon = float(doc["y"]), float(doc["x"])
        if _in_seoul(lat, lon):
            found = (lat, lon, doc.get("place_name") or query)
            _cache_ok("kw", query, found)
            return found
    return None


def _address_search(query: str) -> tuple[float, float, str] | None:
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
    })
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

    variants = [q, compact_road]
    has_seoul = q.startswith(("서울 ", "서울시", "서울특별시"))
    has_district = any(d in q for d in _SEOUL_DISTRICTS)
    for road, district in _ROAD_DISTRICT_HINTS.items():
        if road in compact_road and not has_district:
            variants.append(f"{district} {compact_road}")
            variants.append(f"서울특별시 {district} {compact_road}")
            break
    if not has_seoul:
        variants.append(f"서울특별시 {compact_road}")

    out = []
    seen = set()
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _run_keyword_search(location: str) -> tuple[float, float, str] | None:
    return _keyword_search(location.strip())


def _run_address_search(location: str) -> tuple[float, float, str] | None:
    for query in _address_query_variants(location):
        hit = _address_search(query)
        if hit:
            return hit
    return None


def resolve_location(location: str | None, lat: float | None, lon: float | None,
                     ) -> tuple[float, float, str]:
    if lat is not None and lon is not None:
        if not _in_seoul(lat, lon):
            raise CourseError("서울 지역 좌표만 지원해요. 서울 시내 위치로 다시 알려주세요.")
        return lat, lon, location or f"({lat:.4f}, {lon:.4f})"
    if location:
        key = location.replace(" ", "")
        # ① exact offline gazetteer hit (no network, instant)
        for name, (glat, glon) in GAZETTEER.items():
            if name == key:
                return glat, glon, name
        # ② Kakao API. Address-shaped inputs (관철동 7-14, 테헤란로8길 8)
        # go to the address API first — keyword search treats them as POI
        # names; place-shaped inputs (station/landmark names) go keyword
        # first. Each is the other's fallback.
        is_address = _looks_like_address(location)
        searches = (
            [_run_address_search, _run_keyword_search] if is_address
            else [_run_keyword_search, _run_address_search]
        )
        for search in searches:
            hit = search(location)
            if hit:
                return hit
        # ③ fuzzy gazetteer LAST, and only for short place-like queries.
        # Running it before the API resolved any address containing "강남"
        # or "은평" to the wrong station.
        if not is_address and 2 <= len(key) <= 10:
            for name, (glat, glon) in GAZETTEER.items():
                if name in key or key in name:
                    return glat, glon, name
        known = "시청, 강남역, 여의도한강공원, 서울숲, 석촌호수, 올림픽공원, 서울시 중구 세종대로 110"
        no_key_hint = (
            "" if os.environ.get("KAKAO_REST_API_KEY")
            else " (서버에 KAKAO_REST_API_KEY가 설정되지 않아 지도 검색 없이 동작 중이에요.)"
        )
        raise CourseError(
            f"'{location}' 위치를 찾지 못했어요. 좌표(위도/경도)로 알려주시거나 "
            f"더 잘 알려진 지명으로 시도해 주세요. 예: {known}{no_key_hint}"
        )
    raise CourseError("출발 위치가 필요해요. 지명(예: 시청, 강남역)이나 좌표를 알려주세요.")
