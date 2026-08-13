"""Build a browser harness for the course detail page's editing UI.

pytest can only assert that strings are present in the rendered page. It cannot
tell "the page says X" from "the page says X where nobody can read it" -- the
sr-only edit bar passed every string assertion for two commits while showing
users nothing (see design-plans/2026-08-13-...-review-and-plan.md, F-01).

This produces a self-contained HTML file that runs the *production* editing
code against a minimal Kakao Maps double, so `fetch` can be mocked and the real
DOM state observed. Stdlib only -- no new dependency.

    .venv/bin/python tests/browser/build_harness.py [outdir]

Then serve the outdir and open harness.html / harness_animal.html, and paste
tests/browser/edit_scenarios.js into the console (or run it via any browser
automation) -- it returns a result object, one key per scenario.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from runart.course import generate_course  # noqa: E402
from runart.facilities import facilities_along  # noqa: E402
from runart.models import CourseParams  # noqa: E402
from runart.render import preview_html, route_points  # noqa: E402

CITY_HALL = dict(lat=37.5665, lon=126.9780, location_name="서울시청")

# Only the Kakao surface preview_html() actually touches. The projection is a
# deliberate identity-ish stand-in: these scenarios exercise the feedback and
# request state machine, not map projection accuracy.
KAKAO_DOUBLE = """<script>
(function(){
  function LatLng(lat, lon){ this._lat = lat; this._lng = lon; }
  LatLng.prototype.getLat = function(){ return this._lat; };
  LatLng.prototype.getLng = function(){ return this._lng; };
  function Point(x, y){ this.x = x; this.y = y; }
  function Polyline(){} Polyline.prototype.setMap=function(){}; Polyline.prototype.setPath=function(){};
  function CustomOverlay(o){ this._o = o || {}; }
  CustomOverlay.prototype.setMap = function(m){
    if (!m || !this._o.content) return;
    var host = document.getElementById('map');
    if (typeof this._o.content === 'string') {
      var d = document.createElement('div'); d.innerHTML = this._o.content; host.appendChild(d.firstChild);
    } else host.appendChild(this._o.content);
  };
  CustomOverlay.prototype.setPosition = function(){};
  function Circle(){} Circle.prototype.setMap=function(){};
  Circle.prototype.setPosition=function(){}; Circle.prototype.setRadius=function(){};
  function LatLngBounds(){} LatLngBounds.prototype.extend = function(){};
  function Map(node, opts){ this._node=node; this._opts=opts; this.draggable=true; this.zoomable=true;
    this.controls=[]; window.__map=this; }
  Map.prototype.addControl=function(c){ this.controls.push(c); };
  Map.prototype.removeControl=function(c){ this.controls = this.controls.filter(x => x !== c); };
  Map.prototype.setBounds=function(){};
  Map.prototype.setDraggable=function(v){ this.draggable=v; };
  Map.prototype.setZoomable=function(v){ this.zoomable=v; };
  Map.prototype.panBy=function(x,y){ this.lastPan=[x,y]; this.panCount=(this.panCount||0)+1; };
  Map.prototype.getProjection=function(){ return {
    containerPointFromCoords: ll => new Point((ll.getLng()-126.97)*100000, (37.57-ll.getLat())*100000),
    coordsFromContainerPoint: p => new LatLng(37.57 - p.y/100000, 126.97 + p.x/100000) }; };
  window.kakao = { maps: { load: fn => fn(), Map, LatLng, Point, Polyline, CustomOverlay,
    Circle, LatLngBounds, ZoomControl: function(){}, ControlPosition: { LEFT: 'LEFT' },
    event: { addListener: function(){}, preventMap: function(){ window.__preventMapCount=(window.__preventMapCount||0)+1; } }, services: { Status: { OK: 'OK' } } } };
})();
</script>
"""

SCRIPT_MARKER = "<script>\n const segs ="


def _page(shape: str | None) -> str:
    course = generate_course(CourseParams(**CITY_HALL, distance_km=5.0))
    if shape:
        course.params = course.params.model_copy(update={"shape": shape})
    facilities = facilities_along(route_points(course),
                                  ["convenience_store", "restroom"], limit=80)
    return preview_html(course, facilities, "https://runnywhere.example")


def _harness(page: str) -> str:
    if SCRIPT_MARKER not in page:
        raise SystemExit("page script marker not found -- preview_html layout changed")
    return page.replace(SCRIPT_MARKER, KAKAO_DOUBLE + SCRIPT_MARKER, 1)


def main() -> None:
    outdir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    outdir.mkdir(parents=True, exist_ok=True)
    for name, shape in (("harness.html", None), ("harness_animal.html", "dog")):
        target = outdir / name
        target.write_text(_harness(_page(shape)), encoding="utf-8")
        print(f"{target} ({target.stat().st_size:,}B)")


if __name__ == "__main__":
    main()
