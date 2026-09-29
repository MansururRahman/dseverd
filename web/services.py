"""Web-facing wrappers around the DSE engines: fetch bars through the cache,
call the engine exactly as its CLI does, and wrap the result in a JSON-safe
envelope {symbol, live_merged, note, fetched_at, result}."""
from __future__ import annotations

import datetime as dt

import dse_claude
import dse_gate_strategy as gate
import dse_swing_signal as swing
import dse_technical
import dse_uptrend as uptrend
from web.serialize import to_jsonable

GATE_NAMES = ("trend", "sma_cross", "macd", "rsi", "volume", "atr")


class FetchError(Exception):
    """The DSE archive could not be fetched for a symbol (-> HTTP 502)."""


def read_watchlist(path) -> list[str]:
    try:
        return dse_technical.read_tickers_xlsx(str(path), "Code")
    except Exception:  # noqa: BLE001 - a missing/unreadable sheet just means no watchlist
        return []


def _bars(cache, symbol: str, days: int) -> list[dict]:
    try:
        return cache.bars(symbol, days)
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"fetch failed for {symbol}: {exc}") from exc


def _merge_live(cache, symbol: str, bars: list[dict]) -> tuple[list[dict], bool, str | None]:
    """Append today's live bar to a COPY of `bars` (merge_live_bar mutates)."""
    try:
        snapshot, session_date = cache.live_snapshot()
    except Exception as exc:  # noqa: BLE001 - degrade to archive-only, like the CLIs
        return bars, False, f"live snapshot unavailable ({exc}); using archive only"
    bars = list(bars)
    merged = dse_technical.merge_live_bar(bars, snapshot.get(symbol), session_date)
    return bars, merged, None


def _envelope(symbol: str, result, merged: bool = False, note: str | None = None) -> dict:
    return to_jsonable({"symbol": symbol, "live_merged": merged, "note": note,
                        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                        "result": result})


def trade_stats(trades: list[dict]) -> dict:
    rets = [t["ret"] for t in trades]
    if not rets:
        return {"n": 0, "win_rate": None, "expectancy": None, "total_pnl": None}
    pnl = [t["pnl_bdt"] for t in trades if "pnl_bdt" in t]
    return {"n": len(rets), "win_rate": sum(1 for r in rets if r > 0) / len(rets),
            "expectancy": sum(rets) / len(rets), "total_pnl": sum(pnl) if pnl else None}


def swing_entry(cache, symbol, days, capital, risk, min_rr, score_gate, backtest) -> dict:
    bars = _bars(cache, symbol, days)
    p = {**swing.DEFAULTS, "capital": capital, "risk_pct": risk, "min_rr": min_rr,
         "score_gate": score_gate}
    bt = None
    if backtest:
        run = swing.run_backtest(symbol, bars, p)
        bt = {**run, "stats": trade_stats(run["trades"])}
    return _envelope(symbol, {"entry": swing.evaluate_entry(symbol, bars, p), "backtest": bt})


def swing_exit(cache, symbol, days, entry, stop, target) -> dict:
    bars = _bars(cache, symbol, days)
    return _envelope(symbol, swing.evaluate_exit(symbol, bars, entry, stop, target))


def claude(cache, symbol, days, live, min_rr, max_ext, max_day_gain) -> dict:
    bars = _bars(cache, symbol, days)
    merged, note = False, None
    if live:
        bars, merged, note = _merge_live(cache, symbol, bars)
    cfg = {**dse_claude.DEFAULTS, "min_rr": min_rr, "ext_above_sma20_max_pct": max_ext,
           "day_gain_max_pct": max_day_gain}
    if not bars:
        result = {"symbol": symbol, "tradeable": False,
                  "reason": "no data (check code / date range)", "ledger": [], "live": False}
    else:
        result = dse_claude.evaluate(symbol, bars, merged, cfg)
    return _envelope(symbol, result, merged, note)


def uptrend_gate(cache, symbol, days, min_turnover, index_symbol) -> dict:
    index_bars, note = None, None
    if index_symbol:
        try:
            index_bars = cache.bars(index_symbol, days)
            if not index_bars:
                note = f"warning: index {index_symbol} returned no data -- market guard disabled."
        except Exception as exc:  # noqa: BLE001
            note = (f"warning: index {index_symbol} fetch failed ({exc}) -- "
                    "market guard disabled.")
            index_bars = None
    bars = _bars(cache, symbol, days)
    if not bars:
        result = {"symbol": symbol, "decision": "NO DATA",
                  "reason": "no data returned (check the trading code / date range).",
                  "mandatory": {}, "optional": {}, "guards": {},
                  "snapshot": None, "confidence": None}
    else:
        result = uptrend.evaluate_uptrend(symbol, bars, index_bars=index_bars,
                                          min_avg_vol=min_turnover)
    return _envelope(symbol, result, False, note)


def technical(cache, symbol, days, live) -> dict:
    bars = _bars(cache, symbol, days)
    merged, note = False, None
    if live:
        bars, merged, note = _merge_live(cache, symbol, bars)
    brief = dse_technical.brief_row(symbol, bars, merged)
    clean = [b for b in bars if b["close"] == b["close"]]
    verdict, score, reasons = None, None, []
    if len(clean) >= 20:
        verdict, score, reasons = dse_technical.technical_verdict(clean)
    return _envelope(symbol, {"brief": brief, "verdict": verdict, "score": score,
                              "reasons": reasons}, merged, note)


def gate_strategy(cache, symbol, days, capital, tp, stop_mult, cost, rsi_min, rsi_max,
                  disabled_gates) -> dict:
    bars = _bars(cache, symbol, days)
    cfg = {**gate.DEFAULT_CFG, "take_profit_pct": tp, "atr_stop_mult": stop_mult,
           "cost_pct": cost, "rsi_min": rsi_min, "rsi_max": rsi_max}
    for g in GATE_NAMES:
        cfg[f"use_{g}"] = g not in disabled_gates
    trades, n_clean = gate.backtest(bars, cfg)
    snap = gate.gate_snapshot(bars, cfg)
    label, reason = gate.strategy_verdict(snap, trades)
    window = f"{bars[0]['date']} -> {bars[-1]['date']}" if bars else "n/a"
    note = None
    if n_clean < cfg["sma_trend"] + cfg["macd_slow"]:
        note = (f"WARNING: fewer than SMA{cfg['sma_trend']} + MACD warm-up days -- "
                "trend gate may never arm; results thin")
    return _envelope(symbol, {"snapshot": snap, "trades": trades, "n_clean": n_clean,
                              "window": window, "verdict": label, "verdict_reason": reason,
                              "stats": trade_stats(trades),
                              "summary_text": gate.summarize(trades, account=capital)},
                     False, note)
