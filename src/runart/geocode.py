"""Start-location resolution (PRD §7.3 fallback chain).

Order: ① direct coordinates ② offline gazetteer of Seoul running spots
(no network, keeps avg latency low) ③ Kakao Local keyword/address search when
`KAKAO_REST_API_KEY` is set (1s timeout, LRU-cached) ④ actionable guidance.

The API key never appears in logs, responses, or errors (PRD §8).
"""

import functools
import json
import os
import ssl
import urllib.parse
import urllib.request

from .course import CourseError

_LOCAL_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
_ADDRESS_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/address.json"
_TIMEOUT_S = 1.0
_SEOUL_BOUNDS = (37.4, 37.72, 126.76, 127.19)

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
}


def _in_seoul(lat: float, lon: float) -> bool:
    lat_lo, lat_hi, lon_lo, lon_hi = _SEOUL_BOUNDS
    return lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi


@functools.lru_cache(maxsize=1024)
def _keyword_search(query: str) -> tuple[float, float, str] | None:
    """Kakao Local keyword search. Returns None on any failure (fallback
    chain continues); the key is read here and never leaves this function."""
    key = os.environ.get("KAKAO_REST_API_KEY")
    if not key:
        return None
    params = urllib.parse.urlencode({
        "query": query, "size": 3,
        # Bias results to Seoul center; we still validate bounds after.
        "x": "126.9780", "y": "37.5665", "radius": 20000,
    })
    req = urllib.request.Request(
        f"{_LOCAL_SEARCH_URL}?{params}", headers={"Authorization": f"KakaoAK {key}"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as r:
            docs = json.load(r).get("documents", [])
    except Exception:  # noqa: BLE001 — timeout/HTTP/parse all fall through
        return None
    for doc in docs:
        lat, lon = float(doc["y"]), float(doc["x"])
        if _in_seoul(lat, lon):
            return lat, lon, doc.get("place_name") or query
    return None


@functools.lru_cache(maxsize=1024)
def _address_search(query: str) -> tuple[float, float, str] | None:
    """Kakao Local address search for user-entered current/home addresses.

    Returns None on any failure and never exposes the API key. We only accept
    Seoul-bound results because the route graph is Seoul-only.
    """
    key = os.environ.get("KAKAO_REST_API_KEY")
    if not key:
        return None
    params = urllib.parse.urlencode({"query": query, "size": 3})
    req = urllib.request.Request(
        f"{_ADDRESS_SEARCH_URL}?{params}", headers={"Authorization": f"KakaoAK {key}"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_SSL_CTX) as r:
            docs = json.load(r).get("documents", [])
    except Exception:  # noqa: BLE001 — timeout/HTTP/parse all fall through
        return None
    for doc in docs:
        lat, lon = float(doc["y"]), float(doc["x"])
        if _in_seoul(lat, lon):
            name = (
                doc.get("road_address", {}).get("address_name")
                or doc.get("address", {}).get("address_name")
                or query
            )
            return lat, lon, name
    return None


def resolve_location(location: str | None, lat: float | None, lon: float | None,
                     ) -> tuple[float, float, str]:
    if lat is not None and lon is not None:
        if not _in_seoul(lat, lon):
            raise CourseError("서울 지역 좌표만 지원해요. 서울 시내 위치로 다시 알려주세요.")
        return lat, lon, location or f"({lat:.4f}, {lon:.4f})"
    if location:
        key = location.replace(" ", "")
        for name, (glat, glon) in GAZETTEER.items():
            if name == key or (len(key) >= 2 and (name in key or key in name)):
                return glat, glon, name
        hit = _keyword_search(location.strip())
        if hit:
            return hit
        hit = _address_search(location.strip())
        if hit:
            return hit
        known = "시청, 강남역, 여의도한강공원, 서울숲, 석촌호수, 올림픽공원, 서울시 중구 세종대로 110"
        raise CourseError(
            f"'{location}' 위치를 찾지 못했어요. 좌표(위도/경도)로 알려주시거나 "
            f"더 잘 알려진 지명으로 시도해 주세요. 예: {known}"
        )
    raise CourseError("출발 위치가 필요해요. 지명(예: 시청, 강남역)이나 좌표를 알려주세요.")
