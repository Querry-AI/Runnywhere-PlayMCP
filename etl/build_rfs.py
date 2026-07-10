"""Offline ETL step 2: enrich the Seoul graph with RFS component scores and
extract facilities (PRD §5.7). Runs after build_graph.py; never at runtime.

Data sources, best-available with recorded coverage (checked 2026-07-09
against data.seoul.go.kr — see runart-mcp-prd/PRD.md §14 for the full audit):

  - Elevation/slope : SRTM 30m (always on, AWS elevation-tiles-prod skadi,
                      public) cross-checked against Seoul Open Data Plaza
                      "서울시 경사도"(OA-22241, 국토지리정보원 수치지형도 기반
                      polygon shapefile, 44~101MB zip, 무인증) when present —
                      see load_slope_polygons(). Disagreement is resolved
                      conservatively (steeper value wins, PRD §7.2).
  - Sidewalk width  : OSM highway-class priors (always on, build_graph.py)
                      refined by "서울시 보도통계자료"(OA-22240, xlsx, 무인증,
                      연1회 갱신) when present — see load_sidewalk_widths().
                      The dataset has no route geometry, only 노선명(route
                      name) + 폭(m), so this is a *name* join against the OSM
                      "name" tag (build_graph.py now keeps it), not a
                      segment-level join — expect partial coverage and some
                      false matches on common route names.
  - Lighting        : OSM lit tag (always on, build_graph.py) blended with
                      "서울시 가로등 위치 정보"(OA-22205, CSV 위경도, 무인증,
                      0.65MB) point density when present — see
                      load_streetlight_points(). "서울시 OO구 보안등 위치정보"
                      (crime-prevention lights, per-district OA-115xx) is a
                      secondary source, not yet joined.

  All three Seoul datasets above require a one-time MANUAL download — the
  portal's own download endpoint (datafile.seoul.go.kr/bigfile/...) returned
  503 on every attempt as of 2026-07-09, on both data.seoul.go.kr and its
  data.go.kr mirror, so this can't be automated right now:
    1. OA-22205 CSV  -> data/streetlights.csv
    2. OA-22241 zip  -> unzip into data/seoul_slope/ (a .shp should exist there)
    3. OA-22240 xlsx -> data/sidewalk_stats.xlsx
  Re-run this script after placing any of them; each is optional and
  independently detected (missing file -> that source's prior stays as-is).
  - CCTV            : ⚠️ "서울시 [자치구] 안심이 CCTV 연계 현황" (per-district,
                      point-level lat/lon) was TERMINATED across all districts
                      around 2025-12 — reason given: "안심주소에 IP 노출로
                      국정원 지적" (security/IP-exposure issue flagged by the
                      National Intelligence Service). No point-level CCTV
                      coordinate dataset is currently available from Seoul
                      Open Data Plaza. "서울시 자치구 (목적별) CCTV 설치현황"
                      (OA-2722, active) only gives district-level aggregate
                      counts by purpose (방범/어린이보호/공원 등) — usable at
                      best as a district-wide prior, not per-edge scoring.
                      Practical source: OSM man_made=surveillance (below).
  - Parks/riverside : OSM polygons (leisure/landuse/natural). "서울시 주요
                      공원현황"(OA-394, xlsx) has park name/address/주요시설
                      text but no drinking-fountain-level granularity or
                      geometry — not a clear upgrade over OSM polygons.
  - Facilities      : OSM shop=convenience, amenity=toilets/drinking_water,
                      leisure=park centroids + local "서울시 공중화장실 위치정보"
                      CSV when present → data/facilities.pkl. No dedicated
                      Seoul 음수대 dataset found.

Principles (PRD): snapshot dates recorded, missing data stays neutral with
rfs_coverage tracking, sanity check on well-known running spots.
"""

import csv
import gzip
import json
import os
import pickle
import ssl
import struct
import sys
import unicodedata
import urllib.request

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    _SSL_CTX = ssl.create_default_context()


def _urlopen(url: str, timeout: int):
    return urllib.request.urlopen(url, timeout=timeout, context=_SSL_CTX)
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
GRAPH = DATA / "seoul_graph.pkl"
FACILITIES = DATA / "facilities.pkl"
INFRA_POINTS = DATA / "infra_points.pkl"
SNAPSHOT = DATA / "snapshot.json"


def _first_existing(*candidates: Path) -> Path | None:
    for p in candidates:
        if p.exists():
            return p
    return None


def _glob_first(pattern: str) -> Path | None:
    hits = sorted(ROOT.glob(pattern)) + sorted(DATA.glob(pattern))
    return hits[0] if hits else None


def _find_dir_by_nfc_name(*names: str) -> Path | None:
    """Find local Korean-named folders even when macOS stores filenames as NFD."""
    wanted = {unicodedata.normalize("NFC", name) for name in names}
    for base in (ROOT, DATA):
        for p in base.iterdir():
            if p.is_dir() and unicodedata.normalize("NFC", p.name) in wanted:
                return p
    return None

SKADI_TILES = ("N37E126", "N37E127")
SKADI_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/N37/{tile}.hgt.gz"

SANITY_SPOTS = {
    "한강공원(반포)": (37.5100, 126.9950),
    "석촌호수": (37.5061, 127.1050),
    "서울숲": (37.5444, 127.0374),
    "남산 순환길": (37.5512, 126.9882),
}


# ---------- elevation (SRTM skadi) ----------

class Srtm:
    SIZE = 3601  # SRTM1: 3601x3601 big-endian int16, row 0 = north edge

    def __init__(self):
        self.tiles: dict[tuple[int, int], bytes] = {}
        for name in SKADI_TILES:
            path = DATA / f"{name}.hgt"
            if not path.exists():
                print(f"Downloading {name}.hgt (SRTM 30m, public S3)...", flush=True)
                with _urlopen(SKADI_URL.format(tile=name), timeout=120) as r:
                    raw = gzip.decompress(r.read())
                path.write_bytes(raw)
            lat0, lon0 = int(name[1:3]), int(name[4:7])
            self.tiles[(lat0, lon0)] = path.read_bytes()

    def _cell(self, tile: bytes, row: int, col: int) -> float | None:
        row = min(max(row, 0), self.SIZE - 1)
        col = min(max(col, 0), self.SIZE - 1)
        (val,) = struct.unpack_from(">h", tile, (row * self.SIZE + col) * 2)
        return None if val == -32768 else float(val)

    def elevation(self, lat: float, lon: float) -> float | None:
        """Bilinear interpolation — nearest-cell sampling quantizes 30m cells
        into spurious slope on short edges."""
        key = (int(lat), int(lon))
        tile = self.tiles.get(key)
        if tile is None:
            return None
        r = (key[0] + 1 - lat) * (self.SIZE - 1)
        c = (lon - key[1]) * (self.SIZE - 1)
        r0, c0 = int(r), int(c)
        fr, fc = r - r0, c - c0
        vals = [self._cell(tile, r0, c0), self._cell(tile, r0, c0 + 1),
                self._cell(tile, r0 + 1, c0), self._cell(tile, r0 + 1, c0 + 1)]
        if any(v is None for v in vals):
            return next((v for v in vals if v is not None), None)
        top = vals[0] * (1 - fc) + vals[1] * fc
        bot = vals[2] * (1 - fc) + vals[3] * fc
        return top * (1 - fr) + bot * fr


# ---------- streetlights ----------
#
# "서울시 가로등 위치 정보" (OA-22205): confirmed live, CSV, 위도/경도 columns,
# 무인증, 0.65MB (https://data.seoul.go.kr/dataList/OA-22205/F/1/datasetView.do).
# The portal's file download is a session-bound click-through (no stable
# direct URL found), so this loads a manually-downloaded CSV instead of
# fetching it automatically. To use it: download the CSV from the page
# above into data/streetlights.csv, then re-run this script.

def _find_col(fieldnames, *keywords) -> str | None:
    """First column whose header contains any keyword — tolerates header
    variants like '위도'/'WGS84위도'/'lat' across dataset revisions."""
    for name in fieldnames:
        low = name.strip().lower()
        if any(kw.lower() in low for kw in keywords):
            return name
    return None


def load_streetlight_points() -> list[tuple[float, float]]:
    """OA-22205 CSV (위도/경도) — looked up by filename pattern in the project
    root and data/; empty otherwise (OSM lit-tag prior still applies)."""
    import csv

    path = _glob_first("*가로등*csv") or _first_existing(DATA / "streetlights.csv")
    if path is None:
        return []
    pts = []
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            with path.open(encoding=enc) as f:
                reader = csv.DictReader(f)
                fields = reader.fieldnames or []
                lat_col = _find_col(fields, "위도", "lat", "y좌표", "ycoord")
                lon_col = _find_col(fields, "경도", "lon", "x좌표", "xcoord")
                if not lat_col or not lon_col:
                    print(f"  streetlights.csv 컬럼 인식 실패 (encoding={enc}): {fields}",
                          flush=True)
                    return []
                transformer = None
                for row in reader:
                    lat, lon = row.get(lat_col), row.get(lon_col)
                    if not lat or not lon:
                        continue
                    try:
                        lat, lon = float(lat), float(lon)
                    except ValueError:
                        continue
                    if 37.3 < lat < 37.8 and 126.6 < lon < 127.3:
                        pts.append((lat, lon))
                        continue
                    # Some district exports label EPSG:5186 projected values
                    # as X/Y coordinates. Convert only plausible projected
                    # pairs; invalid zero/sentinel rows are discarded.
                    if 100_000 < lon < 300_000 and 400_000 < lat < 700_000:
                        if transformer is None:
                            from pyproj import Transformer
                            transformer = Transformer.from_crs(
                                "EPSG:5186", "EPSG:4326", always_xy=True)
                        wgs_lon, wgs_lat = transformer.transform(lon, lat)
                        if 37.3 < wgs_lat < 37.8 and 126.6 < wgs_lon < 127.3:
                            pts.append((wgs_lat, wgs_lon))
            return pts
        except UnicodeDecodeError:
            continue
    print("  streetlights.csv 인코딩 인식 실패 (utf-8-sig/cp949/euc-kr 모두 실패)", flush=True)
    return []


# ---------- government elevation (OA-22241, 국토지리정보원 1:5000) ----------
#
# "서울시 경사도" actually ships as NGII digital-topo layers, not slope
# polygons: 표고 5000 (N3P_*.shp spot-elevation points, HEIGHT column) and
# 등고선 5000 (N3L_*.shp contour polylines, HEIGHT column), CRS embedded
# (EPSG:5174). This builds a high-resolution elevation point cloud from
# both layers (contour vertices are subsampled) and interpolates each graph
# node's elevation by inverse-distance weighting. SRTM stays as the
# fallback wherever the point cloud is sparse.

ELEV_CELL = 0.0005  # ~55m buckets for the elevation point cloud
CONTOUR_VERTEX_STRIDE = 6  # contours are dense; every Nth vertex is plenty


def load_gov_elevation():
    """{cell: [(lat, lon, height), ...]} from 표고+등고선 shapefiles, or None."""
    folder = (
        _first_existing(ROOT / "서울시 경사도", DATA / "seoul_slope")
        or _find_dir_by_nfc_name("서울시 경사도", "seoul_slope")
    )
    if folder is None:
        return None
    spot_shps = sorted(folder.glob("**/N3P*.shp")) + sorted(folder.glob("**/표고*.shp"))
    contour_shps = sorted(folder.glob("**/N3L*.shp")) + sorted(folder.glob("**/등고선*.shp"))
    if not spot_shps and not contour_shps:
        print(f"  경사도 폴더({folder})에서 N3P/N3L shapefile을 찾지 못함", flush=True)
        return None

    import numpy as np
    import pyogrio
    from pyproj import Transformer

    buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    n_pts = 0

    def add_points(xs, ys, hs, transformer):
        nonlocal n_pts
        lons, lats = transformer.transform(xs, ys)
        for lat, lon, h in zip(lats, lons, hs):
            key = (int(lat / ELEV_CELL), int(lon / ELEV_CELL))
            buckets.setdefault(key, []).append((lat, lon, float(h)))
        n_pts += len(hs)

    for shp in spot_shps:
        info = pyogrio.read_info(shp)
        transformer = Transformer.from_crs(info["crs"], "EPSG:4326", always_xy=True)
        gdf = pyogrio.read_dataframe(shp, columns=["HEIGHT"])
        xs = gdf.geometry.x.values
        ys = gdf.geometry.y.values
        add_points(xs, ys, gdf["HEIGHT"].values, transformer)
        print(f"  표고점 {len(gdf):,}개 ({shp.name})", flush=True)

    for shp in contour_shps:
        info = pyogrio.read_info(shp)
        transformer = Transformer.from_crs(info["crs"], "EPSG:4326", always_xy=True)
        total = info["features"]
        chunk = 25_000
        read = 0
        while read < total:
            gdf = pyogrio.read_dataframe(shp, columns=["HEIGHT"],
                                         skip_features=read, max_features=chunk)
            if len(gdf) == 0:
                break
            xs_all, ys_all, hs_all = [], [], []
            for geom, h in zip(gdf.geometry.values, gdf["HEIGHT"].values):
                lines = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
                for line in lines:
                    coords = np.asarray(line.coords)[::CONTOUR_VERTEX_STRIDE]
                    xs_all.append(coords[:, 0])
                    ys_all.append(coords[:, 1])
                    hs_all.append(np.full(len(coords), h))
            if xs_all:
                add_points(np.concatenate(xs_all), np.concatenate(ys_all),
                           np.concatenate(hs_all), transformer)
            read += len(gdf)
        print(f"  등고선 정점 샘플링 완료 ({shp.name})", flush=True)

    print(f"  정부 고도 점군: {n_pts:,}개 포인트", flush=True)
    return buckets if n_pts else None


def gov_elevation_at(buckets, lat: float, lon: float) -> float | None:
    """IDW over points within the surrounding 3x3 cells (~150m); None when
    the cloud has no nearby points (caller falls back to SRTM)."""
    if buckets is None:
        return None
    ci, cj = int(lat / ELEV_CELL), int(lon / ELEV_CELL)
    klon = 111_320.0 * 0.7934  # cos(37.5도) — good enough intra-Seoul
    best: list[tuple[float, float]] = []  # (dist2, height)
    for i in (ci - 1, ci, ci + 1):
        for j in (cj - 1, cj, cj + 1):
            for plat, plon, h in buckets.get((i, j), ()):
                d2 = ((plat - lat) * 110_540.0) ** 2 + ((plon - lon) * klon) ** 2
                best.append((d2, h))
    if not best:
        return None
    best.sort(key=lambda t: t[0])
    best = best[:8]
    if best[0][0] < 4.0:  # a point within 2m — take it directly
        return best[0][1]
    wsum = hsum = 0.0
    for d2, h in best:
        w = 1.0 / d2
        wsum += w
        hsum += w * h
    return hsum / wsum


# ---------- pedestrian signals (crossing friction) ----------
#
# "서울특별시_보행자 신호등 분포도.csv": 자치구/종류/X좌표/Y좌표/주소. The
# X/Y ranges match EPSG:5186 (중부원점, verified against known addresses).
# Signal density feeds crossing_score: more signalized crossings on a
# stretch means more forced stops — worse for uninterrupted running.

def load_signal_points() -> list[tuple[float, float]]:
    import csv

    path = _glob_first("*신호등*csv") or _first_existing(DATA / "pedestrian_signals.csv")
    if path is None:
        return []
    from pyproj import Transformer
    transformer = Transformer.from_crs("EPSG:5186", "EPSG:4326", always_xy=True)
    pts = []
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            with path.open(encoding=enc) as f:
                reader = csv.DictReader(f)
                fields = reader.fieldnames or []
                x_col = _find_col(fields, "x좌표", "x")
                y_col = _find_col(fields, "y좌표", "y")
                if not x_col or not y_col:
                    print(f"  신호등 CSV 컬럼 인식 실패 (encoding={enc}): {fields}", flush=True)
                    return []
                for row in reader:
                    try:
                        x, y = float(row[x_col]), float(row[y_col])
                    except (TypeError, ValueError, KeyError):
                        continue
                    lon, lat = transformer.transform(x, y)
                    if 37.3 < lat < 37.8 and 126.6 < lon < 127.3:
                        pts.append((lat, lon))
            return pts
        except UnicodeDecodeError:
            pts = []
            continue
    return []


# ---------- sidewalk width (OA-22240, 노선명 기반 조인) ----------
#
# "서울시 보도통계자료" ships as xlsx with two sheets: 자치구별 총괄현황(1)
# and 노선별 세부현황(2, 노선명·폭(m) 등). It has no route geometry, so this
# joins on normalized 노선명 against the OSM road "name" tag build_graph now
# preserves — a name-level join, not segment-level, so coverage is partial
# and a route's width applies to every OSM edge sharing that name. Download
# from https://data.seoul.go.kr/dataList/OA-22240/F/1/datasetView.do into
# data/sidewalk_stats.xlsx, then re-run this script.

def _normalize_route_name(name: str) -> str:
    return "".join(name.split()).strip()


def load_sidewalk_widths() -> dict[str, float]:
    """{normalized 노선명: 폭(m)} or {} if the file isn't present."""
    path = DATA / "sidewalk_stats.xlsx"
    if not path.exists():
        return {}
    import pandas as pd

    xl = pd.ExcelFile(path)
    sheet = next((s for s in xl.sheet_names if "세부" in s or "노선" in s),
                xl.sheet_names[-1])
    df = xl.parse(sheet)
    name_col = _find_col(df.columns, "노선명")
    width_col = _find_col(df.columns, "폭")
    if not name_col or not width_col:
        print(f"  sidewalk_stats.xlsx 컬럼 인식 실패 (sheet={sheet}): {list(df.columns)}",
              flush=True)
        return {}
    widths: dict[str, float] = {}
    for _, row in df[[name_col, width_col]].dropna().iterrows():
        try:
            w = float(row[width_col])
        except (TypeError, ValueError):
            continue
        key = _normalize_route_name(str(row[name_col]))
        if key:
            widths[key] = max(widths.get(key, 0.0), w)  # widest reported segment wins
    print(f"  보도폭 데이터: {len(widths):,}개 노선명, 컬럼 '{name_col}'/'{width_col}'", flush=True)
    return widths


def width_to_score(width_m: float) -> float:
    """PRD §5.7: 폭 2m+ 만점, 1m 미만 감점."""
    if width_m >= 2.0:
        return 1.0
    if width_m <= 1.0:
        return 0.3
    return 0.3 + 0.7 * (width_m - 1.0)


# ---------- CCTV ----------
#
# The per-district 안심이 CCTV point datasets (OA-20923 and one per 자치구)
# were terminated by Seoul (~2025-12, "안심주소에 IP 노출로 국정원 지적") —
# confirmed dead on multiple districts as of 2026-07-09. There is currently
# no live point-level CCTV coordinate feed from Seoul Open Data Plaza.
# OA-2722 (자치구 CCTV 설치현황, active) only has district-level aggregate
# counts by purpose, not coordinates — kept as a possible future per-district
# multiplier (SEOUL_CCTV_DISTRICT_COUNTS env, not implemented), not a
# per-edge signal. OSM surveillance points are the only usable source today.

def load_cctv_points() -> tuple[list[tuple[float, float]], str]:
    """CCTV points from OSM (man_made=surveillance). Seoul's own point-level
    안심이 CCTV feed is discontinued — see module docstring."""
    if os.environ.get("RUNART_ETL_LOCAL_ONLY") == "1":
        return [], "preserved-existing"
    try:
        import osmnx as ox
        gdf = ox.features_from_place("Seoul, South Korea", {"man_made": "surveillance"})
        pts = [(geom.y, geom.x) for geom in gdf.geometry.centroid]
        return pts, "osm-surveillance"
    except Exception as e:  # noqa: BLE001
        print(f"  CCTV OSM 조회 실패: {e}", flush=True)
        return [], "none"


# ---------- parks / riverside ----------

def load_green_index():
    """STRtree of park/green/water polygons from OSM."""
    if os.environ.get("RUNART_ETL_LOCAL_ONLY") == "1":
        print("Local-only ETL: preserving existing park/green scores", flush=True)
        return None, []
    import osmnx as ox
    from shapely.strtree import STRtree

    print("Fetching park/green/water polygons from OSM...", flush=True)
    frames = []
    for tags in ({"leisure": ["park", "nature_reserve", "garden"]},
                 {"landuse": ["grass", "forest", "recreation_ground"]},
                 {"natural": ["water"]}):
        try:
            gdf = ox.features_from_place("Seoul, South Korea", tags)
            frames.append(gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])])
        except Exception as e:  # noqa: BLE001
            print(f"  polygon fetch {tags} 실패: {e}", flush=True)
    geoms = [g for f in frames for g in f.geometry.values]
    print(f"  {len(geoms):,} polygons", flush=True)
    if not geoms:
        return None, []
    return STRtree(geoms), geoms


# ---------- point bucket for CCTV density ----------

CELL = 0.001  # ~110m


def bucket_points(pts):
    b: dict[tuple[int, int], int] = {}
    for lat, lon in pts:
        k = (int(lat / CELL), int(lon / CELL))
        b[k] = b.get(k, 0) + 1
    return b


def density_score(b, lat, lon, radius_cells=1, saturate=3):
    k0, k1 = int(lat / CELL), int(lon / CELL)
    n = sum(
        b.get((i, j), 0)
        for i in range(k0 - radius_cells, k0 + radius_cells + 1)
        for j in range(k1 - radius_cells, k1 + radius_cells + 1)
    )
    return min(1.0, n / saturate)


# ---------- facilities ----------

def _load_existing_facilities() -> list[dict]:
    if not FACILITIES.exists():
        return []
    with FACILITIES.open("rb") as f:
        return pickle.load(f)


def build_facilities() -> list[dict]:
    local_restrooms = load_seoul_restrooms()
    if os.environ.get("RUNART_ETL_LOCAL_ONLY") == "1":
        existing = merge_facilities(_load_existing_facilities(), local_restrooms)
        print(f"Local-only ETL: preserving/merging {len(existing):,} facilities", flush=True)
        return existing

    import osmnx as ox

    out = []
    specs = [
        ("convenience_store", {"shop": "convenience"}),
        ("restroom", {"amenity": "toilets"}),
        ("water", {"amenity": "drinking_water"}),
        ("park", {"leisure": "park"}),
    ]
    for kind, tags in specs:
        try:
            gdf = ox.features_from_place("Seoul, South Korea", tags)
        except Exception as e:  # noqa: BLE001
            print(f"  facilities {kind} 실패: {e}", flush=True)
            continue
        cent = gdf.geometry.centroid
        names = gdf.get("name")
        for i, geom in enumerate(cent):
            name = None
            if names is not None:
                raw = names.iloc[i]
                name = raw if isinstance(raw, str) else None
            out.append({"type": kind, "name": name or "", "lat": geom.y, "lon": geom.x})
        print(f"  {kind}: {len(cent):,}", flush=True)
    if not out:
        existing = _load_existing_facilities()
        if existing:
            print(f"  facilities fetch empty; preserving {len(existing):,} existing facilities",
                  flush=True)
            out = existing
    return merge_facilities(out, local_restrooms)


def _clean_facility_text(value: str | None) -> str:
    return (value or "").strip().strip("|")


def load_seoul_restrooms() -> list[dict]:
    path = _glob_first("*공중화장실*위치정보*.csv")
    if path is None:
        return []
    out = []
    with path.open(encoding="cp949", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lon = float(row.get("x 좌표") or "")
                lat = float(row.get("y 좌표") or "")
            except ValueError:
                continue
            if not (37.4 <= lat <= 37.72 and 126.76 <= lon <= 127.19):
                continue
            name = (
                _clean_facility_text(row.get("건물명"))
                or _clean_facility_text(row.get("도로명주소"))
                or _clean_facility_text(row.get("지번주소"))
            )
            out.append({
                "type": "restroom",
                "name": name,
                "lat": lat,
                "lon": lon,
                "source": "seoul-public-restroom",
            })
    print(f"  Seoul public restrooms: {len(out):,} ({path.name})", flush=True)
    return out


def merge_facilities(*groups: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for group in groups:
        for fac in group:
            try:
                lat = float(fac["lat"])
                lon = float(fac["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            key = (fac.get("type"), round(lat, 5), round(lon, 5))
            if key in seen:
                continue
            seen.add(key)
            out.append({**fac, "lat": lat, "lon": lon})
    return out


# ---------- main ----------

def main() -> None:
    if not GRAPH.exists():
        sys.exit("data/seoul_graph.pkl이 없습니다. 먼저 etl/build_graph.py를 실행하세요.")
    with GRAPH.open("rb") as f:
        g = pickle.load(f)
    print(f"Graph: {g.number_of_nodes():,} nodes / {g.number_of_edges():,} edges", flush=True)

    from shapely.geometry import Point

    local_only = os.environ.get("RUNART_ETL_LOCAL_ONLY") == "1"
    if local_only:
        print("RUNART_ETL_LOCAL_ONLY=1: local slope/signal/streetlight refresh only",
              flush=True)

    srtm = Srtm()
    tree, geoms = load_green_index()
    green_src = "preserved-existing" if tree is None else "osm-polygons"
    cctv_pts, cctv_src = load_cctv_points()
    cctv_b = bucket_points(cctv_pts)
    print(f"CCTV points: {len(cctv_pts):,} ({cctv_src})", flush=True)
    light_pts = load_streetlight_points()
    light_b = bucket_points(light_pts)
    light_src = "seoul-oa22205" if light_pts else "none (osm-lit-tag prior only)"
    print(f"Streetlight points: {len(light_pts):,} ({light_src})", flush=True)
    gov_elev = load_gov_elevation()
    elev_src = "seoul-oa22241 (표고+등고선 1:5000, srtm fallback)" if gov_elev else "srtm only"
    slope_src = "seoul-oa22241 + srtm fallback" if gov_elev else "srtm-derived"
    print(f"Elevation source: {elev_src}", flush=True)
    signal_pts = load_signal_points()
    signal_b = bucket_points(signal_pts)
    signal_src = "seoul-pedestrian-signals" if signal_pts else "none (osm highway-class prior only)"
    print(f"Pedestrian signal points: {len(signal_pts):,} ({signal_src})", flush=True)
    sidewalk_widths = load_sidewalk_widths()
    sidewalk_src = "seoul-oa22240" if sidewalk_widths else "none (osm highway-class prior only)"

    # Node elevations — kept as node attrs so the runtime can draw real
    # elevation profiles and compute true cumulative ascent. Government
    # 1:5000 point cloud first, SRTM fallback (PRD §7.2 dual source).
    elev: dict = {}
    elev_is_gov: dict = {}
    n_elev_gov = 0
    for n, d in g.nodes(data=True):
        e = gov_elevation_at(gov_elev, d["lat"], d["lon"])
        if e is not None:
            n_elev_gov += 1
            elev_is_gov[n] = True
        else:
            e = srtm.elevation(d["lat"], d["lon"])
        elev[n] = e
        if e is not None:
            d["elev"] = round(e, 1)
    if gov_elev:
        print(f"  노드 고도: 정부 점군 {n_elev_gov:,} / SRTM 폴백 "
              f"{g.number_of_nodes() - n_elev_gov:,}", flush=True)

    n_park = n_slope = n_slope_gov = n_sidewalk = 0
    n_signal = n_light = 0
    for u, v, attrs in g.edges(data=True):
        mlat = (g.nodes[u]["lat"] + g.nodes[v]["lat"]) / 2
        mlon = (g.nodes[u]["lon"] + g.nodes[v]["lon"]) / 2
        cov = 0.4  # highway priors from build_graph already count for something

        e1, e2 = elev.get(u), elev.get(v)
        if e1 is not None and e2 is not None and attrs["length"] > 0:
            both_gov = elev_is_gov.get(u) and elev_is_gov.get(v)
            # Noise floor per source: 1:5000 topo is far cleaner than SRTM
            # (which reads rooftops in dense city blocks).
            floor = 0.3 if both_gov else 1.5
            dh = max(0.0, abs(e2 - e1) - floor)
            attrs["slope_pct"] = round(min(15.0, dh / attrs["length"] * 100.0), 2)
            cov += 0.35 if both_gov else 0.25
            n_slope += 1
            n_slope_gov += bool(both_gov)

        if signal_pts:
            # Signalized-crossing density → forced stops → lower crossing
            # score. Blended with the highway-class prior, not overwritten.
            dens = density_score(signal_b, mlat, mlon, radius_cells=1, saturate=3)
            attrs["crossing_score"] = round(
                0.5 * attrs.get("crossing_score", 0.5) + 0.5 * (1.0 - 0.7 * dens), 2)
            cov += 0.1
            n_signal += 1

        route_name = _normalize_route_name(attrs.get("name", ""))
        width = sidewalk_widths.get(route_name) if route_name else None
        if width is not None:
            attrs["sidewalk_score"] = width_to_score(width)
            cov += 0.15
            n_sidewalk += 1

        if tree is not None:
            hits = tree.query(Point(mlon, mlat))
            park = 0.0
            for idx in hits:
                if geoms[idx].distance(Point(mlon, mlat)) < 0.0004:  # ~40m
                    park = 1.0
                    break
            attrs["park_score"] = park
            n_park += park > 0
            cov += 0.15
        else:
            n_park += attrs.get("park_score", 0.0) > 0

        if cctv_pts:
            attrs["cctv_score"] = round(0.3 + 0.7 * density_score(cctv_b, mlat, mlon), 2)
            cov += 0.2
        if light_pts:
            # Blend the real streetlight density with build_graph's OSM
            # lit-tag prior rather than overwrite — either source alone can
            # miss (OSM lit tagging is sparse; streetlight points don't
            # capture indoor-lit storefronts along a road).
            density = density_score(light_b, mlat, mlon, radius_cells=1, saturate=2)
            attrs["lighting_score"] = round(
                0.4 * attrs.get("lighting_score", 0.5) + 0.6 * (0.2 + 0.8 * density), 2)
            cov += 0.15
            n_light += 1
        attrs["rfs_coverage"] = round(min(1.0, cov), 2)

    with GRAPH.open("wb") as f:
        pickle.dump(g, f)
    print(f"Enriched: slope(all) {n_slope:,}, slope(seoul gov) {n_slope_gov:,}, "
          f"signals {n_signal:,}, streetlights {n_light:,}, "
          f"sidewalk(seoul gov, name-joined) {n_sidewalk:,}, park/water {n_park:,} edges",
          flush=True)

    facs = build_facilities()
    facility_counts: dict[str, int] = {}
    for fac in facs:
        kind = fac.get("type", "unknown")
        facility_counts[kind] = facility_counts.get(kind, 0) + 1
    facility_src = (
        "preserved-existing + seoul-public-restrooms"
        if local_only else "osm-poi + seoul-public-restrooms"
    )
    with FACILITIES.open("wb") as f:
        pickle.dump(facs, f)
    with INFRA_POINTS.open("wb") as f:
        pickle.dump({"streetlight": light_pts, "pedestrian_signal": signal_pts}, f)

    SNAPSHOT.write_text(json.dumps({
        "date": date.today().isoformat(),
        "sources": {"elevation": "srtm-30m-aws", "slope_gov": slope_src,
                    "green": green_src, "cctv": cctv_src, "lighting": light_src,
                    "sidewalk": sidewalk_src, "facilities": facility_src,
                    "crossing": signal_src},
        "local_only": local_only,
        "counts": {"streetlight_points": len(light_pts),
                   "pedestrian_signal_points": len(signal_pts),
                   "gov_elevation_nodes": n_elev_gov,
                   "slope_edges": n_slope,
                   "signal_scored_edges": n_signal,
                   "streetlight_scored_edges": n_light,
                   "infra_points": {"streetlight": len(light_pts),
                                    "pedestrian_signal": len(signal_pts)},
                   "facilities": facility_counts},
    }, ensure_ascii=False, indent=2))

    # Sanity check (PRD §7.2): running spots should score well.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from runart import graph as graphmod
    from runart.rfs import edge_rfs
    graphmod.get_graph.cache_clear()
    graphmod._node_index.cache_clear()
    all_scores = [edge_rfs(a) for _, _, a in g.edges(data=True)]
    all_scores.sort()
    q80 = all_scores[int(len(all_scores) * 0.8)]
    print("\n=== Sanity check (명소 주변 1km 평균 RFS vs 전체 상위 20% 경계) ===", flush=True)
    for name, (lat, lon) in SANITY_SPOTS.items():
        near = [
            edge_rfs(a) for u, v, a in g.edges(data=True)
            if abs(g.nodes[u]["lat"] - lat) < 0.009 and abs(g.nodes[u]["lon"] - lon) < 0.011
        ]
        mean = sum(near) / len(near) if near else 0
        flag = "PASS" if mean >= q80 * 0.9 else "CHECK"
        print(f"  [{flag}] {name}: {mean:.3f} (edges={len(near):,}, q80={q80:.3f})", flush=True)


if __name__ == "__main__":
    main()
