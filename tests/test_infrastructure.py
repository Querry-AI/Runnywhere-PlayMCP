import networkx as nx

from runart import infrastructure
from runart.geo import to_latlon


def _cross_graph(center=(37.5, 127.0)):
    """A + intersection: north-south route through an east-west street."""
    g = nx.Graph()
    lat0, lon0 = center
    nodes = {
        "c": (0.0, 0.0),
        "n": (0.0, 200.0),
        "s": (0.0, -200.0),
        "e": (200.0, 0.0),
        "w": (-200.0, 0.0),
    }
    for name, (x, y) in nodes.items():
        lat, lon = to_latlon(x, y, lat0, lon0)
        g.add_node(name, lat=lat, lon=lon)
    for other in ("n", "s", "e", "w"):
        g.add_edge("c", other, length=200.0)
    return g, lat0, lon0


def _patch_signals(monkeypatch, signals):
    monkeypatch.setattr(
        infrastructure,
        "get_infra_points",
        lambda: {"pedestrian_signal": signals, "streetlight": []},
    )
    infrastructure._infra_buckets.cache_clear()


def test_signal_counted_when_route_crosses_straight_through(monkeypatch):
    g, lat0, lon0 = _cross_graph()
    at_corner = to_latlon(12.0, 12.0, lat0, lon0)
    _patch_signals(monkeypatch, [at_corner])

    # s -> c -> n goes straight through the intersection: the runner crosses
    # the east-west street at its signalized crosswalk.
    assert infrastructure.pedestrian_signals_crossed(g, ["s", "c", "n"]) == 1


def test_signal_not_counted_when_route_turns_at_corner(monkeypatch):
    g, lat0, lon0 = _cross_graph()
    at_corner = to_latlon(12.0, 12.0, lat0, lon0)
    _patch_signals(monkeypatch, [at_corner])

    # s -> c -> e turns the corner: the runner stays on the same block side
    # and never enters a crosswalk, so the signal must not count.
    assert infrastructure.pedestrian_signals_crossed(g, ["s", "c", "e"]) == 0


def test_signal_not_counted_when_merely_running_past(monkeypatch):
    g, lat0, lon0 = _cross_graph()
    mid_block = to_latlon(12.0, 100.0, lat0, lon0)  # beside the s-c leg
    _patch_signals(monkeypatch, [mid_block])

    assert infrastructure.pedestrian_signals_crossed(g, ["s", "c", "n"]) == 0
