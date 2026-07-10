import networkx as nx

from runart import infrastructure
from runart.geo import to_latlon


def test_pedestrian_signal_count_uses_one_meter_route_edge_radius(monkeypatch):
    g = nx.Graph()
    a = (37.5, 127.0)
    b = to_latlon(100.0, 0.0, a[0], a[1])
    near = to_latlon(50.0, 0.8, a[0], a[1])
    far = to_latlon(50.0, 2.0, a[0], a[1])

    g.add_node("a", lat=a[0], lon=a[1])
    g.add_node("b", lat=b[0], lon=b[1])
    g.add_edge("a", "b", length=100.0)

    monkeypatch.setattr(
        infrastructure,
        "get_infra_points",
        lambda: {"pedestrian_signal": [near, far], "streetlight": []},
    )
    infrastructure._infra_buckets.cache_clear()

    assert infrastructure.infra_count_crossed_by_path(
        g, ["a", "b"], "pedestrian_signal"
    ) == 1
