"""Offline test data: real DSE bars recorded by tests/record_fixtures.py, plus
fake fetchers and patch helpers so no test ever touches the network."""
from __future__ import annotations

import datetime as dt
import functools
import json
from pathlib import Path

import dse_gate_strategy
import dse_swing_signal

ROOT = Path(__file__).resolve().parent.parent
BARS_DIR = Path(__file__).resolve().parent / "fixtures" / "bars"
NAN = float("nan")


@functools.cache
def _load(symbol: str) -> tuple:
    rows = json.loads((BARS_DIR / f"{symbol}.json").read_text(encoding="utf-8"))
    return tuple({**r, "date": dt.date.fromisoformat(r["date"])} for r in rows)


def load_bars(symbol: str) -> list[dict]:
    """A fresh list of fresh dicts every call -- callers may mutate it."""
    return [dict(b) for b in _load(symbol)]


def fixture_symbols() -> list[str]:
    return sorted(p.stem for p in BARS_DIR.glob("*.json"))


def fake_fetch_history(symbol: str, start, end) -> list[dict]:
    if symbol == "BADCODE":
        raise OSError("simulated timeout")
    if not (BARS_DIR / f"{symbol}.json").exists():
        return []
    return load_bars(symbol)


def _last_clean(symbol: str) -> dict:
    return next(b for b in reversed(_load(symbol)) if b["close"] == b["close"])


def live_next_day() -> tuple[dict, dt.date]:
    """A live page one day after the newest recorded bar: every ticker merges."""
    syms = fixture_symbols()
    session = max(_load(s)[-1]["date"] for s in syms) + dt.timedelta(days=1)
    snapshot = {}
    for s in syms:
        c = _last_clean(s)["close"]
        snapshot[s] = {"open": NAN, "high": c * 1.02, "low": c * 0.98, "close": c * 1.005,
                       "volume": max(_last_clean(s)["volume"], 1000.0),
                       "trades": _last_clean(s)["trades"]}
    return snapshot, session


def live_today() -> tuple[dict, dt.date]:
    snapshot, _ = live_next_day()
    return snapshot, dt.date.today()


def live_outage():
    raise OSError("simulated live outage")


LIVE = {"next_day": live_next_day, "today": live_today, "outage": live_outage}


def patch_shortlist_env(monkeypatch, modules, live: str = "outage", frac: float = 0.83) -> None:
    """Point each shortlist module at the fixtures, remove the delay, freeze the clock."""
    for mod in modules:
        monkeypatch.setattr(mod, "fetch_history", fake_fetch_history)
        monkeypatch.setattr(mod, "fetch_live_snapshot", LIVE[live])
        monkeypatch.setattr(mod, "FETCH_DELAY_SECONDS", 0)
        monkeypatch.setattr(mod, "session_elapsed_fraction", lambda now=None: frac)


def _fingerprint(bars: list[dict]) -> tuple:
    return (bars[0]["date"], bars[0]["close"], len(bars))


def force_survivors(monkeypatch) -> list[str]:
    """Make every evaluated ticker a CONDITIONAL BUY and pin both backtests, so the
    funnel reaches stages 2-3 (ROBUST / MIXED / WEAK) whatever the recorded data says.
    Patches the shared engine modules, so old and new shortlist code both see it."""
    syms = fixture_symbols()
    real_entry = dse_swing_signal.evaluate_entry
    swing_rets = {syms[0]: [0.02, -0.01], syms[1]: [0.03], syms[2]: [-0.01]}
    gate_rets = {syms[0]: [0.01], syms[1]: [-0.02], syms[2]: []}
    by_fp = {_fingerprint(load_bars(s)): s for s in syms}
    assert len(by_fp) == len(syms), "fixture fingerprints collide"

    def entry(symbol, bars, p):
        res = real_entry(symbol, bars, p)
        if res["decision"] == "NO DATA":
            return res
        return {**res, "decision": "CONDITIONAL BUY", "reason": None}

    def swing_backtest(symbol, bars, p):
        return {"symbol": symbol,
                "trades": [{"ret": r} for r in swing_rets.get(symbol, [-0.005])]}

    def gate_backtest(bars, cfg):
        sym = by_fp.get(_fingerprint(bars))
        return [{"ret": r} for r in gate_rets.get(sym, [])], len(bars)

    monkeypatch.setattr(dse_swing_signal, "evaluate_entry", entry)
    monkeypatch.setattr(dse_swing_signal, "run_backtest", swing_backtest)
    monkeypatch.setattr(dse_gate_strategy, "backtest", gate_backtest)
    return syms
