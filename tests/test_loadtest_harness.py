"""Deterministic contract tests for the contest latency harness."""

import importlib.util
from collections import Counter
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "runart_loadtest", Path(__file__).parents[1] / "scripts" / "loadtest.py")
assert _SPEC and _SPEC.loader
loadtest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(loadtest)


def test_nearest_rank_percentile_uses_the_ceil_rank():
    values = list(range(1, 101))
    assert loadtest._nearest_rank(values, 0.50) == 50
    assert loadtest._nearest_rank(values, 0.99) == 99
    assert loadtest._nearest_rank(values, 1.00) == 100


def test_nearest_rank_rejects_invalid_samples():
    with pytest.raises(ValueError):
        loadtest._nearest_rank([], 0.99)
    with pytest.raises(ValueError):
        loadtest._nearest_rank([1], 0)


def test_partition_indices_covers_every_request_exactly_once():
    partitions = loadtest._partition_indices(1_003, 10)
    flattened = [index for partition in partitions for index in partition]
    assert sorted(flattened) == list(range(1_003))
    assert len(flattened) == len(set(flattened))


def test_request_corpus_is_deterministic_and_contains_both_workloads():
    first = [loadtest._request_for(index) for index in range(200)]
    second = [loadtest._request_for(index) for index in range(200)]
    assert first == second
    assert {group for group, _ in first} == {"course", "art"}
    for _, arguments in first:
        assert arguments["location"] in loadtest.SPOTS
        assert arguments["course_type"]


def test_product_level_is_error_is_reported_not_a_transport_failure():
    """A valid no-course result must remain in latency and outcome evidence."""
    outcomes = Counter(mcp_error=3, constraint_mismatch=3)
    assert outcomes["mcp_error"] == 3
    assert not loadtest._promotion_failed(
        complete=True, outcomes=outcomes, avg_ms=50, p99_ms=500)
    outcomes["timeout"] = 1
    assert loadtest._promotion_failed(
        complete=True, outcomes=outcomes, avg_ms=50, p99_ms=500)
