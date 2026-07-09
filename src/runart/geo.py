"""Local planar projection helpers (equirectangular around a reference point).

Good enough for intra-Seoul distances; avoids heavy geodesy deps at runtime.
"""

import math

M_PER_DEG_LAT = 110_540.0


def m_per_deg_lon(lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(lat))


def to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    return ((lon - lon0) * m_per_deg_lon(lat0), (lat - lat0) * M_PER_DEG_LAT)


def to_latlon(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    return (lat0 + y / M_PER_DEG_LAT, lon0 + x / m_per_deg_lon(lat0))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def polyline_length_m(points: list[tuple[float, float]]) -> float:
    return sum(
        haversine_m(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        for i in range(len(points) - 1)
    )
