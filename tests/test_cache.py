import datetime as dt
import threading
import time
from types import SimpleNamespace

import pytest

from web.cache import BarCache

DAY = dt.date(2026, 9, 28)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def env():
    calls, live_calls, day, clock = [], [], [DAY], FakeClock()

    def fetch(symbol, start, end):
        calls.append((symbol, (end - start).days))
        return [{"date": DAY, "close": 1.0}]

    def fetch_live():
        live_calls.append(1)
        return {"X": {"close": 1.0}}, DAY

    cache = BarCache(fetch=fetch, fetch_live=fetch_live, min_interval=3, bars_ttl=1800,
                     live_ttl=300, clock=clock, sleep=clock.sleep, today=lambda: day[0])
    return SimpleNamespace(cache=cache, calls=calls, live_calls=live_calls, day=day, clock=clock)


def test_second_request_is_a_cache_hit(env):
    env.cache.bars("ACMEPL", 730)
    env.cache.bars("ACMEPL", 730)
    assert env.calls == [("ACMEPL", 730)]


def test_symbol_is_uppercased(env):
    env.cache.bars("acmepl", 730)
    env.cache.bars("ACMEPL", 730)
    assert env.calls == [("ACMEPL", 730)]


def test_returned_list_is_a_copy(env):
    env.cache.bars("A", 730).append("junk")
    assert len(env.cache.bars("A", 730)) == 1


def test_days_is_part_of_the_key(env):
    env.cache.bars("A", 730)
    env.cache.bars("A", 400)
    assert env.calls == [("A", 730), ("A", 400)]


def test_new_dhaka_day_refetches(env):
    env.cache.bars("A", 730)
    env.day[0] = DAY + dt.timedelta(days=1)
    env.cache.bars("A", 730)
    assert len(env.calls) == 2


def test_bars_expire_after_ttl(env):
    env.cache.bars("A", 730)
    env.clock.t += 1799
    env.cache.bars("A", 730)
    assert len(env.calls) == 1
    env.clock.t += 2
    env.cache.bars("A", 730)
    assert len(env.calls) == 2


def test_network_fetches_are_spaced_by_min_interval(env):
    env.cache.bars("A", 730)
    env.cache.bars("A", 730)       # hit: no sleep
    env.cache.bars("B", 730)
    assert env.clock.sleeps == [3.0]


def test_fetch_errors_are_not_cached():
    attempts = []

    def flaky(symbol, start, end):
        attempts.append(symbol)
        if len(attempts) == 1:
            raise OSError("timed out")
        return [{"close": 1.0}]

    cache = BarCache(fetch=flaky, min_interval=0)
    with pytest.raises(OSError):
        cache.bars("A", 730)
    assert cache.bars("A", 730) == [{"close": 1.0}]


def test_live_snapshot_cached_for_ttl(env):
    env.cache.live_snapshot()
    env.clock.t += 299
    env.cache.live_snapshot()
    assert len(env.live_calls) == 1
    env.clock.t += 2
    assert env.cache.live_snapshot() == ({"X": {"close": 1.0}}, DAY)
    assert len(env.live_calls) == 2


def test_fetcher_uses_days_as_key_and_ignores_range(env):
    fetch = env.cache.fetcher(730)
    fetch("A", dt.date(2000, 1, 1), dt.date(2000, 1, 2))
    env.cache.bars("A", 730)
    assert env.calls == [("A", 730)]


def test_concurrent_fetches_are_serialized_and_spaced():
    stamps = []

    def fetch(symbol, start, end):
        stamps.append(time.monotonic())
        return [{"close": 1.0}]

    cache = BarCache(fetch=fetch, min_interval=0.2)
    threads = [threading.Thread(target=cache.bars, args=(s, 730)) for s in ("A", "B", "C")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    assert len(stamps) == 3
    assert all(b - a >= 0.19 for a, b in zip(stamps, stamps[1:]))
