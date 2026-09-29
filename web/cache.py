"""Bar cache for the web layer.

* Archive bars: one fetch per (symbol, days), reused until the Asia/Dhaka date
  changes or `bars_ttl` passes (so a mid-session fetch picks up the close later).
* Live snapshot: one all-ticker page, reused for `live_ttl` seconds.
* Throttle: every archive fetch -- shortlist job or single-stock request -- goes
  through one lock and waits until `min_interval` has passed since the previous
  one, keeping the CLI's politeness gap toward old.dsebd.org.

Callers always get a new list, so appending a live bar never touches the cache.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

import dse_technical

DHAKA_TZ = dt.timezone(dt.timedelta(hours=6))


def dhaka_today() -> dt.date:
    return dt.datetime.now(DHAKA_TZ).date()


class BarCache:
    def __init__(self, fetch=None, fetch_live=None,
                 min_interval: float = dse_technical.FETCH_DELAY_SECONDS,
                 bars_ttl: float = 1800.0, live_ttl: float = 300.0,
                 clock=time.monotonic, sleep=time.sleep, today=dhaka_today):
        self._fetch = fetch
        self._fetch_live = fetch_live
        self.min_interval = min_interval
        self.bars_ttl = bars_ttl
        self.live_ttl = live_ttl
        self.clock = clock
        self.sleep = sleep
        self.today = today
        self._bars: dict[tuple[str, int], tuple[dt.date, float, list[dict]]] = {}
        self._live: tuple | None = None
        self._lock = threading.Lock()       # guards the dicts
        self._net_lock = threading.Lock()   # serializes archive fetches
        self._last_net: float | None = None

    def bars(self, symbol: str, days: int) -> list[dict]:
        key = (symbol.upper(), int(days))
        with self._lock:
            hit = self._bars.get(key)
        if hit and self._fresh(hit[0], hit[1], self.bars_ttl):
            return list(hit[2])
        end = dt.date.today()
        start = end - dt.timedelta(days=key[1])
        fetch = self._fetch or dse_technical.fetch_history
        bars = self._throttled(fetch, key[0], start, end)
        with self._lock:
            self._bars[key] = (self.today(), self.clock(), bars)
        return list(bars)

    def live_snapshot(self) -> tuple[dict, dt.date | None]:
        with self._lock:
            hit = self._live
        if hit and self._fresh(hit[0], hit[1], self.live_ttl):
            return hit[2], hit[3]
        fetch_live = self._fetch_live or dse_technical.fetch_live_snapshot
        snapshot, session_date = fetch_live()
        with self._lock:
            self._live = (self.today(), self.clock(), snapshot, session_date)
        return snapshot, session_date

    def fetcher(self, days: int):
        """A fetch_history-compatible callable for run_shortlist. The range is
        implied by `days`, which is also the cache key."""
        return lambda symbol, start, end: self.bars(symbol, days)

    def _fresh(self, day: dt.date, fetched_at: float, ttl: float) -> bool:
        return day == self.today() and self.clock() - fetched_at < ttl

    def _throttled(self, fn, *args):
        with self._net_lock:
            if self._last_net is not None:
                wait = self._last_net + self.min_interval - self.clock()
                if wait > 0:
                    self.sleep(wait)
            try:
                return fn(*args)
            finally:
                self._last_net = self.clock()
