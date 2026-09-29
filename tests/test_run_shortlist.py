import pytest

import dse_shortlist
from dse_shortlist import ShortlistOptions, run_shortlist
from tests import fixtures as fx


def _symbols():
    return fx.fixture_symbols() + ["BADCODE", "EMPTY"]


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(dse_shortlist, "session_elapsed_fraction", lambda now=None: 0.83)


def _run(symbols=None, **kw):
    kw.setdefault("fetch", fx.fake_fetch_history)
    kw.setdefault("delay", 0)
    opts = kw.pop("opts", ShortlistOptions(no_live=True))
    return run_shortlist(symbols or _symbols(), opts, **kw)


def test_stage1_rows_cover_every_symbol_including_failures():
    res = _run()
    rows = {r["symbol"]: r for r in res["stage1"]["rows"]}
    assert list(rows) == _symbols()
    assert rows["BADCODE"] == {"symbol": "BADCODE", "src": "-", "decision": "NO DATA",
                               "reason": "fetch failed (simulated timeout)"}
    assert rows["EMPTY"]["reason"] == "no data returned"
    assert res["live"] == {"enabled": False, "n_tickers": 0, "session_date": None, "error": None}
    assert [r["symbol"] for r in res["stage4"]["rows"]] == fx.fixture_symbols()


def test_evaluated_rows_carry_the_engine_result():
    row = next(r for r in _run()["stage1"]["rows"] if r["symbol"] == fx.fixture_symbols()[0])
    assert row["gates_total"] == len(row["result"]["gates"])
    assert row["decision"] == row["result"]["decision"]


def test_live_outage_is_recorded_not_raised():
    res = _run(fx.fixture_symbols()[:1], opts=ShortlistOptions(), fetch_live=fx.live_outage)
    assert res["live"]["error"] == "simulated live outage"
    assert res["stage1"]["n_live"] == 0


def test_live_bar_merged_from_injected_snapshot():
    res = _run(fx.fixture_symbols(), opts=ShortlistOptions(), fetch_live=fx.live_next_day)
    assert res["stage1"]["n_live"] == len(fx.fixture_symbols())
    assert all(r["src"] == "LIVE" for r in res["stage1"]["rows"])
    assert any("VOLUME is PROJECTED" in line for line in res["stage1"]["notes"])


def test_events_arrive_in_stage_order():
    events = []
    _run(emit=lambda e, d: events.append(e))
    names = [e for e in events if e not in ("ticker", "stage1_row", "stage2_row")]
    assert names[:3] == ["live", "stage1_start", "stage1_done"]
    assert names[-2:] == ["stage4_done", "done"]
    assert events.count("ticker") >= len(_symbols())


def test_exception_from_emit_aborts_the_run():
    class Stop(Exception):
        pass

    seen = []

    def emit(event, data):
        if event == "ticker":
            seen.append(data["symbol"])
            if data["i"] == 1:
                raise Stop

    with pytest.raises(Stop):
        _run(emit=emit)
    assert seen == _symbols()[:2]


def test_delay_is_slept_between_tickers(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dse_shortlist.time, "sleep", sleeps.append)
    _run(fx.fixture_symbols()[:3], delay=1.5)
    assert sleeps == [1.5, 1.5]


def test_forced_survivors_fill_stages_2_and_3(monkeypatch):
    syms = fx.force_survivors(monkeypatch)
    res = _run(fx.fixture_symbols(), opts=ShortlistOptions(no_live=True, min_turnover=100000))
    verdicts = {r["symbol"]: r["verdict"] for r in res["stage2"]["rows"]}
    assert verdicts[syms[0]] == "ROBUST (both +)"
    assert verdicts[syms[1]] == "MIXED (one +)"
    assert verdicts[syms[2]] == "WEAK (neither +)"
    assert res["stage2"]["robust"] == [syms[0]]
    assert res["stage3"]["n_robust"] == 1
    assert [r["symbol"] for r in res["stage3"]["rows"]] == [syms[0]]


def test_index_fetch_failure_becomes_a_stage3_warning(monkeypatch):
    fx.force_survivors(monkeypatch)
    res = _run(fx.fixture_symbols(), opts=ShortlistOptions(no_live=True, index_symbol="BADCODE"))
    assert res["stage3"]["warning"] == ("warning: index BADCODE fetch failed (simulated timeout) "
                                        "-- market guard disabled.")


def test_no_survivors_skips_stages_2_and_3(monkeypatch):
    def no_trade(symbol, bars, p):
        return {"symbol": symbol, "decision": "NO TRADE", "setup": "NONE",
                "reason": "failed gate: x", "gates": [], "manual": [], "sizing": None,
                "snapshot": {"date": "d", "price": 1.0, "rsi": None, "atr_pct": 1.0,
                             "structure": 0}}
    monkeypatch.setattr(dse_shortlist.swing, "evaluate_entry", no_trade)
    res = _run()
    assert res["stage2"] is None and res["stage3"] is None
    assert res["stage4"] is not None
