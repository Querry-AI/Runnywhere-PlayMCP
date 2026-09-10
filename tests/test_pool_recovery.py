"""A dead pool worker must not be a permanent, invisible degradation."""
import asyncio
import concurrent.futures
import json
import time

import pytest

from runart import server


class _FakePool:
    """An executor that only has to look alive to the revival code."""

    def __init__(self):
        self.shutdown_called = False

    def submit(self, fn, *args):
        future = concurrent.futures.Future()
        future.set_result(True)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_called = True


@pytest.fixture(autouse=True)
def _isolated_pool(monkeypatch):
    """Every case starts from a clean, fast-retrying pool state."""
    monkeypatch.setattr(server, "_POOL", None)
    monkeypatch.setattr(server, "_POOL_FAILURES", 0)
    monkeypatch.setattr(server, "_POOL_REVIVING", False)
    monkeypatch.setattr(server, "_POOL_RETRY_AT", 0.0)
    monkeypatch.setattr(server, "POOL_RETRY_BASE_S", 0.01)
    yield
    server._POOL = None
    server._POOL_FAILURES = 0
    server._POOL_REVIVING = False


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_broken_pool_comes_back_on_its_own(monkeypatch):
    """It used to latch for the life of the process: every generation ran
    inline until a redeploy, with /healthz still reporting ok."""
    monkeypatch.setattr(server, "_new_pool", _FakePool)
    dead = _FakePool()
    server._POOL = dead

    server._mark_pool_broken("test")

    assert server._POOL is None, "고장 직후에는 풀을 쓰지 않는다"
    assert dead.shutdown_called
    assert _wait_for(lambda: server._POOL is not None), "스스로 되살아나야 한다"
    assert server.pool_state() == "ok"
    assert server._POOL_FAILURES == 0


def test_the_request_path_never_waits_for_the_new_pool(monkeypatch):
    """A fresh worker loads its own graph copy -- 21.9s for eight concurrent
    generations when cold -- so no runner may be behind that."""
    started = []

    class _SlowPool(_FakePool):
        def __init__(self):
            super().__init__()
            started.append(time.monotonic())
            time.sleep(0.3)

    monkeypatch.setattr(server, "_new_pool", _SlowPool)
    server._mark_pool_broken("test")

    began = time.monotonic()
    for _ in range(5):
        assert server._get_pool() is None      # 되살아나기 전에는 inline으로 간다
    assert time.monotonic() - began < 0.1, "요청 경로가 풀 생성을 기다렸다"
    assert _wait_for(lambda: server._POOL is not None)


def test_repeated_failures_back_off_and_finally_stop(monkeypatch):
    """If the cause was memory, retrying fast just repeats the kill."""
    def _always_fails():
        raise OSError("no memory")

    monkeypatch.setattr(server, "_new_pool", _always_fails)
    server._mark_pool_broken("test")

    assert _wait_for(lambda: server._POOL_FAILURES >= server.POOL_MAX_FAILURES, timeout=10)
    assert server.pool_state() == "off"
    assert server._POOL is None
    assert _wait_for(lambda: server._POOL_REVIVING is False, timeout=5)


def test_healthz_says_which_state_the_pool_is_in(monkeypatch):
    monkeypatch.setattr(server, "_new_pool", _FakePool)

    body = json.loads(asyncio.run(server.healthz(None)).body)
    assert body["pool"] in {"ok", "starting", "reviving", "off"}

    server._POOL_FAILURES = server.POOL_MAX_FAILURES
    assert server.pool_state() == "off"
    assert json.loads(asyncio.run(server.healthz(None)).body)["pool"] == "off"
