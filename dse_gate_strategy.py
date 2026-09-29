"""
Multi-Gate Confirmation Strategy for DSE -- Screener + Backtester
=================================================================

A DSE-native port of `gate_strategy.py`. A trade fires ONLY when every enabled
"gate" passes (logical AND): SMA trend, SMA crossover, MACD, RSI, Volume, ATR
volatility. The backtest then reports the REAL expectancy, win rate, and max
drawdown the gates produced on historical Dhaka Stock Exchange prices.

What changed vs the original gate_strategy.py
---------------------------------------------
* Data source is the official DSE day-end archive (via `dse_technical.fetch_history`),
  NOT Yahoo Finance -- DSE tickers are not on yfinance.
* Standard library ONLY. No pandas / numpy / yfinance to install. Indicators are
  computed as pure-python rolling series aligned bar-by-bar.
* Everything else -- the six gates, next-open execution, intrabar stop-then-target
  exit, ATR stop, take-profit %, cost model, and the summary stats -- mirrors the
  original so a backtest here answers: "do these gates have positive expectancy
  on DSE data?"

IMPORTANT HONEST NOTE
---------------------
This does NOT guarantee any fixed profit-per-trade. The gates raise signal
quality; the BACKTEST tells you the real expectancy, win rate, and max drawdown.
Take-profit / stop-loss are configurable so you can test a target directly and
see what actually happens. Decision SUPPORT, not investment advice.

Usage:
    python dse_gate_strategy.py ACMEPL
    python dse_gate_strategy.py ACMEPL KBPPWBIL --days 730
    python dse_gate_strategy.py ACMEPL --tp 0.03 --stop-mult 2.0 --cost 0.005
    python dse_gate_strategy.py ACMEPL --no-volume --no-atr        # loosen gates
    python dse_gate_strategy.py --from-xlsx "Debt to Equity Ratio.xlsx" --days 730
    python dse_gate_strategy.py ACMEPL --trades                    # list every trade
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from dse_technical import ema_series, fetch_history, read_tickers_xlsx

FETCH_DELAY_SECONDS = 3  # polite pause between successive DSE archive calls
NaN = float("nan")


def _isnan(x) -> bool:
    return x != x


# --------------------------------------------------------------------------- #
# Rolling-series indicators (pure python; aligned index-for-index with `bars`).
# Each returns a list the same length as its input, with None where undefined
# (warm-up period). This is the series analogue of dse_technical's scalar
# sma/rsi/macd/atr -- a backtest needs the indicator value at *every* bar.
# --------------------------------------------------------------------------- #
def sma_series(values: list[float], period: int) -> list:
    out = [None] * len(values)
    if period <= 0:
        return out
    run = 0.0
    window: list[float] = []
    for i, v in enumerate(values):
        window.append(v)
        run += v
        if len(window) > period:
            run -= window.pop(0)
        if len(window) == period:
            out[i] = run / period
    return out


def rsi_series(closes: list[float], period: int = 14) -> list:
    """Wilder-smoothed RSI at each bar (matches dse_technical.rsi seeding)."""
    n = len(closes)
    out: list = [None] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # gains[k] corresponds to closes[k+1]; first RSI lands on closes[period].
    out[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for k in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[k]) / period
        avg_loss = (avg_loss * (period - 1) + losses[k]) / period
        rsi_val = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        out[k + 1] = rsi_val
    return out


def macd_series(closes: list[float], fast=12, slow=26, signal=9) -> tuple:
    """Return (macd_line, signal_line, hist) as three same-length series.

    Uses ema_series (adjust=False, seeded from the first value) exactly like the
    original strategy's pandas ewm(adjust=False)."""
    if not closes:
        return [], [], []
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_e, slow_e)]
    signal_line = ema_series(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def atr_series(bars: list[dict], period: int = 14) -> list:
    """Wilder-smoothed ATR at each bar. out[i] is None during warm-up."""
    n = len(bars)
    out: list = [None] * n
    if n < period + 1:
        return out
    tr = [None]  # no TR for the first bar
    for i in range(1, n):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    seed = sum(tr[1:period + 1]) / period      # average of first `period` TRs
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


# --------------------------------------------------------------------------- #
# Data hygiene: drop bars with a NaN in any OHLC field (DSE '--' sentinels);
# a NaN volume is treated as 0 (a no-trade day), which the volume gate rejects.
# --------------------------------------------------------------------------- #
def _clean_bars(bars: list[dict]) -> list[dict]:
    out = []
    for b in bars:
        if any(_isnan(b[k]) for k in ("open", "high", "low", "close")):
            continue
        vol = b["volume"]
        out.append({**b, "volume": 0.0 if _isnan(vol) else vol})
    return out


# --------------------------------------------------------------------------- #
# GATES -- each yields a boolean per bar (True = gate passes that bar).
# Mirrors evaluate_gates() in the original. A bar with any required indicator
# still in warm-up (None) fails the AND automatically.
# --------------------------------------------------------------------------- #
def evaluate_gates(bars: list[dict], cfg: dict) -> list[bool]:
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    n = len(bars)

    sma_fast = sma_series(closes, cfg["sma_fast"])
    sma_slow = sma_series(closes, cfg["sma_slow"])
    sma_trend = sma_series(closes, cfg["sma_trend"])
    _, _, macd_hist = macd_series(closes, cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"])
    rsi_vals = rsi_series(closes, cfg["rsi_period"])
    vol_avg = sma_series(vols, cfg["vol_period"])
    atr_vals = atr_series(bars, cfg["atr_period"])

    all_pass = [False] * n
    for i in range(n):
        ok = True
        if cfg["use_trend"]:
            ok = ok and sma_trend[i] is not None and closes[i] > sma_trend[i]
        if cfg["use_sma_cross"]:
            ok = ok and (sma_fast[i] is not None and sma_slow[i] is not None
                         and sma_fast[i] > sma_slow[i])
        if cfg["use_macd"]:
            ok = ok and macd_hist[i] > 0  # macd_hist is defined from bar 0 (ema seed)
        if cfg["use_rsi"]:
            r = rsi_vals[i]
            ok = ok and r is not None and cfg["rsi_min"] < r < cfg["rsi_max"]
        if cfg["use_volume"]:
            va = vol_avg[i]
            ok = ok and va is not None and vols[i] > va * cfg["vol_mult"]
        if cfg["use_atr"]:
            a = atr_vals[i]
            ok = ok and a is not None and closes[i] > 0 and (a / closes[i]) < cfg["atr_max_pct"]
        all_pass[i] = ok
    return all_pass, atr_vals


# --------------------------------------------------------------------------- #
# CURRENT GATE STATUS -- the same six gates evaluated on the most recent bar,
# reported PASS/FAIL with the numbers, plus the blocking factor (first gate that
# fails). This mirrors the live gate readout in dse_swing_signal.py: a signal
# fires only when EVERY enabled gate passes; otherwise the blocker is what to
# watch. The latest bar's signal would execute at the next session's open.
# --------------------------------------------------------------------------- #
def gate_snapshot(bars: list[dict], cfg: dict) -> dict | None:
    bars = _clean_bars(bars)
    n = len(bars)
    if n == 0:
        return None
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    i = n - 1

    sma_fast = sma_series(closes, cfg["sma_fast"])[i]
    sma_slow = sma_series(closes, cfg["sma_slow"])[i]
    sma_trend = sma_series(closes, cfg["sma_trend"])[i]
    macd_hist = macd_series(closes, cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"])[2][i]
    r = rsi_series(closes, cfg["rsi_period"])[i]
    vol_avg = sma_series(vols, cfg["vol_period"])[i]
    atr_val = atr_series(bars, cfg["atr_period"])[i]
    price = closes[i]

    gates: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        gates.append({"name": name, "ok": bool(ok), "detail": detail})

    if cfg["use_trend"]:
        add(f"Price > SMA{cfg['sma_trend']} (trend)",
            sma_trend is not None and price > sma_trend,
            f"close {price:g} vs SMA{cfg['sma_trend']} {sma_trend:.2f}"
            if sma_trend is not None else f"SMA{cfg['sma_trend']} n/a (warm-up)")
    if cfg["use_sma_cross"]:
        add(f"SMA{cfg['sma_fast']} > SMA{cfg['sma_slow']} (cross)",
            sma_fast is not None and sma_slow is not None and sma_fast > sma_slow,
            f"SMA{cfg['sma_fast']} {sma_fast:.2f} vs SMA{cfg['sma_slow']} {sma_slow:.2f}"
            if sma_fast is not None and sma_slow is not None else "SMAs n/a (warm-up)")
    if cfg["use_macd"]:
        add("MACD histogram > 0", macd_hist > 0, f"hist {macd_hist:+.3f}")
    if cfg["use_rsi"]:
        add(f"RSI in ({cfg['rsi_min']:.0f}, {cfg['rsi_max']:.0f})",
            r is not None and cfg["rsi_min"] < r < cfg["rsi_max"],
            f"RSI(14) {r:.1f}" if r is not None else "RSI n/a (warm-up)")
    if cfg["use_volume"]:
        add(f"Volume > {cfg['vol_mult']:g}x 20d avg",
            vol_avg is not None and vols[i] > vol_avg * cfg["vol_mult"],
            f"today {vols[i]:,.0f} vs {cfg['vol_mult']:g}x avg {vol_avg * cfg['vol_mult']:,.0f}"
            if vol_avg is not None else "vol avg n/a (warm-up)")
    if cfg["use_atr"]:
        atr_pct = (atr_val / price) if (atr_val is not None and price > 0) else None
        add(f"ATR% < {cfg['atr_max_pct']:.0%}",
            atr_pct is not None and atr_pct < cfg["atr_max_pct"],
            f"ATR(14) {atr_val:.2f} = {atr_pct:.1%} of price"
            if atr_pct is not None else "ATR n/a (warm-up)")

    all_pass = all(g["ok"] for g in gates)
    blocker = next((g["name"] for g in gates if not g["ok"]), None)
    return {"date": bars[i]["date"], "price": price, "gates": gates,
            "all_pass": all_pass, "blocker": blocker}


def print_gate_snapshot(snap: dict) -> None:
    print(f"\n  CURRENT GATE STATUS (as of {snap['date']}, close {snap['price']:g}):")
    for g in snap["gates"]:
        print(f"    [{'PASS' if g['ok'] else 'FAIL'}] {g['name']:<26} {g['detail']}")
    if snap["all_pass"]:
        print("    >>> ALL GATES PASS -- signal ARMED (would enter at next session's open)")
    else:
        print(f"    >>> NO SIGNAL -- blocking factor: {snap['blocker']}")


# --------------------------------------------------------------------------- #
# VERDICT -- fuse the live gate status (is a signal armed today?) with the
# backtested edge (did these gates make money on this name?). This is what
# dse_gate_strategy can say that a single-snapshot tool cannot: not just
# "is it a signal" but "is it a signal in a regime that historically paid".
# --------------------------------------------------------------------------- #
def strategy_verdict(snap: dict | None, trades: list[dict]) -> tuple[str, str]:
    if snap is None:
        return "NO DATA", "no clean bars to evaluate"
    passed = sum(1 for g in snap["gates"] if g["ok"])
    total = len(snap["gates"])
    armed = snap["all_pass"]
    exp = (sum(t["ret"] for t in trades) / len(trades)) if trades else None

    if exp is None:
        edge, exp_s = "untested", "no historical trades"
    elif exp > 0:
        edge, exp_s = "positive", f"{exp:+.2%}/trade"
    else:
        edge, exp_s = "negative", f"{exp:+.2%}/trade"

    if armed and edge == "positive":
        return ("BUY",
                f"all {total} gates pass AND backtested edge is positive ({exp_s})")
    if armed:
        return ("ARMED BUT NO EDGE",
                f"all {total} gates pass, but backtested edge is {edge} ({exp_s}) "
                "-- mechanically valid setup, not historically profitable here")
    if total - passed == 1 and edge == "positive":
        return ("WATCH",
                f"1 gate from arming (blocker: {snap['blocker']}); "
                f"backtested edge positive ({exp_s})")
    return ("NO TRADE",
            f"{passed}/{total} gates pass (blocker: {snap['blocker']}); "
            f"edge {edge} ({exp_s})")


# --------------------------------------------------------------------------- #
# BACKTEST -- long-only, one position at a time, next-open execution.
# Intrabar exits assume the worst case: stop is checked before target.
# Identical trade mechanics to the original gate_strategy.py.
# --------------------------------------------------------------------------- #
def backtest(bars: list[dict], cfg: dict) -> list[dict]:
    bars = _clean_bars(bars)
    if len(bars) < 2:
        return [], 0
    entry_ok, atr_vals = evaluate_gates(bars, cfg)

    trades: list[dict] = []
    in_pos = False
    entry_price = stop_price = target_price = NaN
    entry_date = None

    for i in range(len(bars) - 1):
        nxt = bars[i + 1]
        if not in_pos:
            if entry_ok[i] and atr_vals[i] is not None:
                entry_price = nxt["open"]
                stop_price = entry_price - cfg["atr_stop_mult"] * atr_vals[i]
                target_price = entry_price * (1 + cfg["take_profit_pct"])
                entry_date = nxt["date"]
                in_pos = True
        else:
            hi, lo = nxt["high"], nxt["low"]
            exit_price = exit_reason = None
            if lo <= stop_price:                       # worst-case: stop first
                exit_price, exit_reason = stop_price, "stop"
            elif hi >= target_price:
                exit_price, exit_reason = target_price, "target"
            if exit_price is not None:
                ret = (exit_price / entry_price - 1) - cfg["cost_pct"]
                trades.append(dict(entry_date=entry_date, exit_date=nxt["date"],
                                   entry=entry_price, exit=exit_price,
                                   reason=exit_reason, ret=ret))
                in_pos = False

    return trades, len(bars)


# --------------------------------------------------------------------------- #
# STATS -- expectancy, win rate, drawdown, streak (mirrors summarize()).
# --------------------------------------------------------------------------- #
def summarize(trades: list[dict], account: float = 2_000_000.0) -> str:
    if not trades:
        return "No trades generated. Loosen gates (--no-volume/--no-atr), " \
               "lengthen --days, or relax the RSI band."

    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    win_rate = len(wins) / len(rets)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = sum(rets) / len(rets)

    # Equity curve: simplistic full-notional compounding (same as original).
    equity = account
    curve = [equity]
    for r in rets:
        equity *= (1 + r)
        curve.append(equity)
    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)

    streak = mx = 0
    for r in rets:
        streak = streak + 1 if r <= 0 else 0
        mx = max(mx, streak)

    n_stop = sum(1 for t in trades if t["reason"] == "stop")
    n_target = sum(1 for t in trades if t["reason"] == "target")

    return "\n".join([
        f"Trades:                {len(trades)}   (target {n_target} / stop {n_stop})",
        f"Win rate:              {win_rate:6.1%}",
        f"Avg win:               {avg_win:+6.2%}",
        f"Avg loss:              {avg_loss:+6.2%}",
        f"Expectancy/trade:      {expectancy:+6.2%}  <-- the number that matters",
        f"Total return:          {curve[-1] / account - 1:+6.1%}",
        f"Max drawdown:          {max_dd:6.1%}",
        f"Longest losing streak: {mx} trades",
    ])


def print_trades(symbol: str, trades: list[dict]) -> None:
    if not trades:
        return
    print(f"\n  TRADE LOG ({symbol}):")
    print(f"    {'ENTRY DATE':<12}{'EXIT DATE':<12}{'ENTRY':>10}{'EXIT':>10}"
          f"{'RET':>9}  REASON")
    for t in trades:
        print(f"    {str(t['entry_date']):<12}{str(t['exit_date']):<12}"
              f"{t['entry']:>10.2f}{t['exit']:>10.2f}{t['ret']:>+9.2%}  {t['reason']}")


# --------------------------------------------------------------------------- #
# CONFIG (defaults mirror the original; tune on the CLI)
# --------------------------------------------------------------------------- #
DEFAULT_CFG = dict(
    sma_fast=20, sma_slow=50, sma_trend=200,
    macd_fast=12, macd_slow=26, macd_signal=9,
    rsi_period=14, rsi_min=50, rsi_max=72,
    vol_period=20, vol_mult=1.0,
    atr_period=14, atr_max_pct=0.05,
    use_trend=True, use_sma_cross=True, use_macd=True,
    use_rsi=True, use_volume=True, use_atr=True,
    take_profit_pct=0.03,     # gross take-profit target
    atr_stop_mult=2.0,        # stop = entry - 2*ATR
    cost_pct=0.005,           # 0.5% round-trip costs (DSE commission+levy realistic)
)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="DSE multi-gate confirmation strategy: screener + backtester "
                    "(standard library only).")
    ap.add_argument("symbols", nargs="*", help="DSE trading codes, e.g. ACMEPL KBPPWBIL")
    ap.add_argument("--from-xlsx", metavar="PATH",
                    help="Read the ticker list from an .xlsx (e.g. 'Debt to Equity Ratio.xlsx')")
    ap.add_argument("--col", default="Code",
                    help="Header of the ticker column in --from-xlsx (default 'Code')")
    ap.add_argument("--days", type=int, default=730,
                    help="History window in calendar days (default 730; SMA200 needs a long window)")
    ap.add_argument("--capital", type=float, default=2_000_000.0,
                    help="Account size for the equity curve (default 2,000,000 BDT)")
    ap.add_argument("--tp", type=float, default=DEFAULT_CFG["take_profit_pct"],
                    help="Take-profit fraction (default 0.03 = 3%%)")
    ap.add_argument("--stop-mult", type=float, default=DEFAULT_CFG["atr_stop_mult"],
                    help="ATR stop multiple (default 2.0)")
    ap.add_argument("--cost", type=float, default=DEFAULT_CFG["cost_pct"],
                    help="Round-trip cost fraction (default 0.005 = 0.5%%)")
    ap.add_argument("--rsi-min", type=float, default=DEFAULT_CFG["rsi_min"])
    ap.add_argument("--rsi-max", type=float, default=DEFAULT_CFG["rsi_max"])
    for g in ("trend", "sma-cross", "macd", "rsi", "volume", "atr"):
        ap.add_argument(f"--no-{g}", action="store_true", help=f"disable the {g} gate")
    ap.add_argument("--trades", action="store_true", help="print every individual trade")
    args = ap.parse_args(argv)

    cfg = dict(DEFAULT_CFG)
    cfg["take_profit_pct"] = args.tp
    cfg["atr_stop_mult"] = args.stop_mult
    cfg["cost_pct"] = args.cost
    cfg["rsi_min"] = args.rsi_min
    cfg["rsi_max"] = args.rsi_max
    cfg["use_trend"] = not args.no_trend
    cfg["use_sma_cross"] = not args.no_sma_cross
    cfg["use_macd"] = not args.no_macd
    cfg["use_rsi"] = not args.no_rsi
    cfg["use_volume"] = not args.no_volume
    cfg["use_atr"] = not args.no_atr

    symbols = list(args.symbols)
    if args.from_xlsx:
        try:
            from_file = read_tickers_xlsx(args.from_xlsx, args.col)
        except Exception as exc:  # noqa: BLE001 - surface file/parse failures clearly
            print(f"failed to read tickers from {args.from_xlsx} ({exc})", file=sys.stderr)
            return 2
        if not from_file:
            print(f"no tickers found in {args.from_xlsx} (column '{args.col}')", file=sys.stderr)
            return 2
        print(f"Loaded {len(from_file)} tickers from {args.from_xlsx} (column '{args.col}').")
        symbols = symbols + from_file

    if not symbols:
        ap.error("no tickers given -- pass trading codes and/or --from-xlsx PATH")

    active = [g for g, on in (("trend", cfg["use_trend"]), ("sma_cross", cfg["use_sma_cross"]),
                              ("macd", cfg["use_macd"]), ("rsi", cfg["use_rsi"]),
                              ("volume", cfg["use_volume"]), ("atr", cfg["use_atr"])) if on]
    print(f"Gates ON: {', '.join(active)}")
    print(f"Trade rules: take-profit {cfg['take_profit_pct']:.1%}, "
          f"stop {cfg['atr_stop_mult']:g}xATR, cost {cfg['cost_pct']:.2%} round-trip")

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    all_trades: list[dict] = []
    for i, sym in enumerate(symbols):
        sym = sym.upper()
        if i > 0:
            time.sleep(FETCH_DELAY_SECONDS)  # don't hammer the DSE archive
        print(f"\n{'=' * 64}\n{sym}  --  GATED-STRATEGY BACKTEST\n{'=' * 64}")
        try:
            bars = fetch_history(sym, start, end)
        except Exception as exc:  # noqa: BLE001 - surface fetch/parse failures clearly
            print(f"  failed to fetch data ({exc})")
            continue
        if not bars:
            print("  no data returned (check the trading code / date range).")
            continue
        trades, n_clean = backtest(bars, cfg)
        window = f"{bars[0]['date']} -> {bars[-1]['date']}" if bars else "n/a"
        print(f"  {n_clean} clean trading days  ({window})")
        if n_clean < cfg["sma_trend"] + cfg["macd_slow"]:
            print(f"  [WARNING: fewer than SMA{cfg['sma_trend']} + MACD warm-up days -- "
                  "trend gate may never arm; results thin]")
        snap = gate_snapshot(bars, cfg)
        if snap:
            print_gate_snapshot(snap)
        print()
        print(summarize(trades, account=args.capital))
        if args.trades:
            print_trades(sym, trades)
        label, reason = strategy_verdict(snap, trades)
        print(f"\n  >>> VERDICT: {label}  ({reason})")
        all_trades.extend(trades)

    if len([s for s in symbols]) > 1:
        print(f"\n{'=' * 64}\nPORTFOLIO (all symbols pooled)\n{'=' * 64}")
        print(summarize(all_trades, account=args.capital))

    print("\nNOTE: rule-based backtest for decision support only -- not investment "
          "advice. Past expectancy does not guarantee future results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
