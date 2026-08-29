# Runnywhere data licences and provenance

This notice applies to the bundled files under `data/` and the station catalogue
in `src/runart/stations.py`. The MIT licence in `LICENSE` applies to source code,
not to third-party data. Snapshot details are recorded in `data/snapshot.json`.

## OpenStreetMap-derived databases — ODbL 1.0

`seoul_graph.pkl`, `facilities.pkl`, `infra_points.pkl`,
`animal_station_presets.json.gz`, `standard_course_presets.json.gz`, and
`park_course_presets.json` contain or are derived from OpenStreetMap data.
They are offered under the Open Data Commons Open Database License 1.0 (ODbL):

- © OpenStreetMap contributors
- https://www.openstreetmap.org/copyright
- https://opendatacommons.org/licenses/odbl/1-0/

The public copy of this repository provides the derivative databases themselves
and the corresponding transformation code in `etl/`,
`scripts/build_animal_presets.py`, `scripts/build_standard_presets.py`, and
`scripts/build_park_presets.py`.
A recipient may copy, modify, and redistribute
the databases under ODbL 1.0. Any public use must retain OpenStreetMap attribution
and make the applicable derivative database, or a compliant method of producing
it, available under ODbL. Non-OSM inputs listed below retain their own terms.

The transformations download a Seoul pedestrian graph, retain way geometry and
selected tags, reduce it to a NetworkX graph, add public-data-derived running
scores, extract facilities and infrastructure points, and precompute animal-art and
ordinary running routes. `data_integrity.py` records the distributed artifacts' SHA-256 hashes.

## Seoul public data — Korea Open Government Licence Type 1

The following works are provided with source attribution under 공공누리 제1유형
(KOGL Type 1), which permits commercial use and modification. The bundled data
is filtered, coordinate-converted, joined, scored, or aggregated; it is not an
unaltered official publication. © Seoul Metropolitan Government or the provider
shown on the linked source page.

- 서울시 경사도 (OA-22241), source file/data updated 2025-03-20
  (used in the 2026-07-11 build):
  https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do
- 서울시 가로등 위치 정보 (OA-22205), 2025 data snapshot supplied under
  the legacy filename `서울특별시_가로등 위치 정보_20221108.csv`, accessed
  2026-07-09:
  https://data.seoul.go.kr/dataList/OA-22205/F/1/datasetView.do
- 서울시 보행자 신호등 분포도 현황 (OA-22356), source file updated
  2026-02-13:
  https://data.seoul.go.kr/dataList/OA-22356/F/1/datasetView.do
- 서울시 공중화장실 위치정보 (OA-22586), accessed for the 2026-07-11
  build:
  https://data.seoul.go.kr/dataList/OA-22586/S/1/datasetView.do
- 서울교통공사 역주소 및 전화번호, 2026-02-12 dataset (289 rows),
  KOGL Type 1:
  https://www.data.go.kr/data/15044231/fileData.do
  and https://data.seoul.go.kr/dataList/OA-12035/S/1/datasetView.do

KOGL Type 1 terms: https://www.kogl.or.kr/info/license.do

## Seoul Metro coordinates

The 1–8 line coordinates used to join the station address catalogue correspond
to 서울교통공사_1_8호선 역사 좌표(위경도) 정보, whose official data.go.kr record
states `이용허락범위 제한 없음`:
https://www.data.go.kr/data/15099316/fileData.do

Station names and addresses retain the KOGL Type 1 attribution above. Four
renamed or newly opened station coordinates were checked against current public
station-coordinate records. Coordinates are approximate starting points, not
survey-grade entrance locations.

## Mapzen Terrain Tiles / NASA SRTM

`N37E126.hgt` and `N37E127.hgt` are NASA Shuttle Radar Topography Mission
SRTMGL1/Skadi elevation tiles obtained on 2026-07-09 from the Mapzen Terrain
Tiles `elevation-tiles-prod` bucket listed in the Registry of Open Data on AWS.
The SRTM source data is public-domain U.S. government data with no reuse
restriction. This notice does not describe it as CC0, because public-domain
status and a CC0 dedication are legally distinct. Mapzen/Tilezen requests
attribution for hosted terrain tiles.

- Mapzen Terrain Tiles; SRTM data courtesy of the U.S. Geological Survey
- AWS Open Data registry: https://registry.opendata.aws/terrain-tiles/
- Mapzen/Tilezen attribution: https://github.com/tilezen/joerd/blob/master/docs/attribution.md
- SRTMGL1 v3 dataset citation: https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL1.003

## No endorsement and accuracy

Attribution does not imply endorsement by OpenStreetMap, Seoul Metropolitan
Government, Seoul Metro, NASA, JPL, NGA, Kakao, or any contributor. These
snapshots may be incomplete or stale and must not be treated as authoritative
real-time safety or navigation data.
