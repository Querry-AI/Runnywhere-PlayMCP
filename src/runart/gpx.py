"""GPX export."""

from xml.sax.saxutils import escape


def to_gpx(name: str, points: list[tuple[float, float]]) -> str:
    pts = "\n".join(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}"/>' for lat, lon in points)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Runnywhere" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>{escape(name)}</name>
    <trkseg>
{pts}
    </trkseg>
  </trk>
</gpx>
"""
