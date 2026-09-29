"""
dse_claude -- a clean-slate DSE swing mechanic.

Prior gate/tech scripts in this workspace "fired but lost money": they produced
BUY signals with no real edge. dse_claude is built against the four structural
reasons a fixed swing ruleset bleeds on the Dhaka Stock Exchange:

    1. Chasing        -> anti-extension + no-blow-off-bar gates.
    2. Bad stops      -> stop sits below real pullback support, not a round ATR.
    3. Fantasy targets-> target is a structural prior high, RR must clear 2.0.
    4. Counter-trend  -> entries only inside a confirmed, rising uptrend.

It reads ticker(s), a list, or an .xlsx watchlist and prints ONLY tradeable
setups as a table with exactly these columns:

    SYMBOL  UPTREND  RR  ENTRY  EXIT  STOP

The setup is a pullback-to-support inside an uptrend: wait for a shallow dip
toward SMA20 (staying above SMA50), then a turn-up confirmation bar. Entry near
support gives a tight stop and room to the prior high -- naturally high RR.

Data layer and indicators are reused from dse_technical.py; standard library only.

This is decision SUPPORT, not investment advice. Every number traces to price
history. The position decision and its risk remain yours.

Usage:
    python dse_claude.py ACMEPL KBPPWBIL --days 400
    python dse_claude.py --from-xlsx "Debt to Equity Ratio.xlsx"
    python dse_claude.py ACMEPL --csv        # dump shown rows to dse_claude_signals.csv
    python dse_claude.py ACMEPL --verbose    # per-ticker PASS/FAIL gate ledger
    python dse_claude.py ACMEPL --no-live     # day-end archive only
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time

from dse_technical import (
    atr,
    ema_series,
    fetch_history,
    fetch_live_snapshot,
    merge_live_bar,
    read_tickers_xlsx,
    rsi,
    sma,
)

FETCH_DELAY_SECONDS = 3  # polite pause between successive DSE archive calls

# Every threshold lives here so logic can be tuned without editing rules.
DEFAULTS = {
    "min_rr": 2.0,                  # target must be >= 2x the risk to show a row
    "ext_above_sma20_max_pct": 6.0,  # anti-chase: not more than this % above SMA20
    "day_gain_max_pct": 5.0,        # anti-chase: signal bar gain ceiling (no blow-off)
    "rsi_min": 40.0,                # confirmation RSI recovery band (lower)
    "rsi_max": 68.0,                # confirmation RSI recovery band (upper)
    "pullback_lookback": 10,        # bars to search for the shallow dip
    "slope_lookback": 10,           # bars back to measure SMA50 slope
    "resistance_lookback": 40,      # bars over which the target (prior high) is found
    "stop_atr_buffer": 0.25,        # ATR buffer placed below the pullback low
    "min_risk_atr": 1.0,            # floor the stop distance at this many ATR
    "min_days": 20,                 # liquidity/data floor (trading days)
    "min_avg_vol": 100_000,         # liquidity floor (avg 20d volume)
    "atr_period": 14,
}


# --------------------------------------------------------------------------- #
# Small indicator helpers layered on dse_technical's primitives
# --------------------------------------------------------------------------- #
def macd_hist_pair(closes: list[float], fast=12, slow=26, signal=9):
    """Return (macd_line, signal_line, hist_now, hist_prev) or None if too short.

    dse_technical.macd() only exposes the latest triple; the pullback rule needs
    to know whether the histogram is *rising*, so recompute the series here.
    """
    if len(closes) < slow + signal + 1:
        return None
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_e, slow_e)]
    signal_line = ema_series(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line[-1], signal_line[-1], hist[-1], hist[-2]


def _finite(x) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


# --------------------------------------------------------------------------- #
# Core evaluation: the pullback-in-uptrend swing rule
# --------------------------------------------------------------------------- #
def evaluate(symbol: str, bars: list[dict], live: bool = False,
             cfg: dict | None = None) -> dict:
    """Evaluate one stock. Returns a result dict:

    Tradeable   -> {symbol, tradeable=True, uptrend, rr, entry, exit, stop, ledger, live}
    Not tradeable-> {symbol, tradeable=False, reason, ledger, live}

    `ledger` is the ordered list of "PASS/FAIL  text" strings (auditability).
    """
    cfg = {**DEFAULTS, **(cfg or {})}
    ledger: list[str] = []

    def gate(ok: bool, text: str) -> bool:
        ledger.append(f"{'PASS' if ok else 'FAIL'}  {text}")
        return ok

    def skip(reason: str) -> dict:
        return {"symbol": symbol, "tradeable": False, "reason": reason,
                "ledger": ledger, "live": live}

    bars = [b for b in bars if _finite(b["close"])]  # drop NaN closes

    # --- Liquidity / data floor ------------------------------------------- #
    if len(bars) < cfg["min_days"]:
        return skip(f"insufficient data ({len(bars)}d)")
    recent_vol = [b["volume"] for b in bars[-20:] if _finite(b["volume"])]
    avg_vol = sum(recent_vol) / len(recent_vol) if recent_vol else 0.0
    if not gate(avg_vol >= cfg["min_avg_vol"],
                f"liquidity: avg20d vol {avg_vol:,.0f} >= {cfg['min_avg_vol']:,.0f}"):
        return skip(f"illiquid (avg20d vol {avg_vol:,.0f})")

    closes = [b["close"] for b in bars]
    price = closes[-1]
    prev_close = closes[-2]
    last = bars[-1]

    # Need >=60 bars to measure the SMA50 slope over `slope_lookback`.
    s20, s50 = sma(closes, 20), sma(closes, 50)
    if s20 is None or s50 is None or len(closes) < 50 + cfg["slope_lookback"]:
        return skip(f"need >= {50 + cfg['slope_lookback']} days for uptrend read "
                    f"(have {len(closes)})")
    s50_prev = sma(closes[:-cfg["slope_lookback"]], 50)
    s200 = sma(closes, 200)
    a = atr(bars, cfg["atr_period"])
    if a is None or a <= 0:
        return skip("ATR unavailable")

    # --- Uptrend gate ------------------------------------------------------ #
    up_price = gate(price > s50, f"price {price:g} > SMA50 {s50:.2f}")
    up_struct = gate(s20 > s50, f"SMA20 {s20:.2f} > SMA50 {s50:.2f}")
    up_slope = gate(s50 > s50_prev,
                    f"SMA50 rising ({s50:.2f} > {s50_prev:.2f} {cfg['slope_lookback']}d ago)")
    if not (up_price and up_struct and up_slope):
        return skip("not in a confirmed uptrend")
    grade = "Strong" if (s200 is not None and price > s200) else "Moderate"
    ledger.append(f"INFO  uptrend grade: {grade}"
                  + (f" (price > SMA200 {s200:.2f})" if grade == "Strong"
                     else " (not clearly above SMA200)"))

    # --- Pullback: shallow dip toward SMA20, staying above SMA50 ---------- #
    win = bars[-cfg["pullback_lookback"]:]
    lows = [b["low"] for b in win if _finite(b["low"])]
    if not lows:
        return skip("no valid lows in pullback window")
    pullback_low = min(lows)
    dipped = gate(pullback_low <= s20 + a,
                  f"pullback dipped toward SMA20 (low {pullback_low:g} <= SMA20+ATR {s20 + a:.2f})")
    shallow = gate(pullback_low > s50,
                   f"pullback shallow (low {pullback_low:g} > SMA50 {s50:.2f})")
    if not (dipped and shallow):
        return skip("no shallow pullback toward SMA20")

    # --- Turn-up confirmation on the latest bar --------------------------- #
    up_close = gate(price > prev_close,
                    f"confirm: close {price:g} > prev close {prev_close:g}")
    # Live-merged bars carry no open -> skip the bullish-bar test rather than fail.
    if _finite(last["open"]):
        bullish = gate(price > last["open"],
                       f"confirm: bullish bar (close {price:g} > open {last['open']:g})")
    else:
        bullish = True
        ledger.append("INFO  bullish-bar test skipped (no open on live bar)")
    reclaim = gate(price >= s20, f"confirm: reclaimed SMA20 (close {price:g} >= {s20:.2f})")
    r = rsi(closes)
    rsi_ok = gate(r is not None and cfg["rsi_min"] <= r <= cfg["rsi_max"],
                  f"confirm: RSI {r:.1f} in [{cfg['rsi_min']:.0f},{cfg['rsi_max']:.0f}]"
                  if r is not None else "confirm: RSI unavailable")
    if not (up_close and bullish and reclaim and rsi_ok):
        return skip("no turn-up confirmation")

    # --- Momentum turning -------------------------------------------------- #
    mh = macd_hist_pair(closes)
    if mh is None:
        return skip("MACD unavailable (too few bars)")
    macd_line, macd_sig, hist_now, hist_prev = mh
    mom_ok = gate(macd_line > macd_sig or hist_now > hist_prev,
                  f"momentum turning (MACD {macd_line:+.3f} vs sig {macd_sig:+.3f}; "
                  f"hist {hist_prev:+.3f}->{hist_now:+.3f})")
    if not mom_ok:
        return skip("momentum not turning up")

    # --- Anti-chase -------------------------------------------------------- #
    ext_pct = (price - s20) / s20 * 100
    not_extended = gate(ext_pct <= cfg["ext_above_sma20_max_pct"],
                        f"anti-chase: {ext_pct:.1f}% above SMA20 <= {cfg['ext_above_sma20_max_pct']:.0f}%")
    day_gain = (price / prev_close - 1) * 100
    no_blowoff = gate(day_gain <= cfg["day_gain_max_pct"],
                      f"anti-chase: signal-bar gain {day_gain:.1f}% <= {cfg['day_gain_max_pct']:.0f}%")
    if not (not_extended and no_blowoff):
        return skip("over-extended / blow-off bar (would be chasing)")

    # --- Levels & RR ------------------------------------------------------- #
    entry = price
    stop = pullback_low - cfg["stop_atr_buffer"] * a
    if entry - stop < cfg["min_risk_atr"] * a:      # floor the stop distance
        stop = entry - cfg["min_risk_atr"] * a
    risk = entry - stop
    if risk <= 0:
        return skip("non-positive risk (stop >= entry)")

    res_win = bars[-cfg["resistance_lookback"]:]
    highs = [b["high"] for b in res_win if _finite(b["high"])]
    resistance = max(highs) if highs else float("nan")
    if not _finite(resistance) or resistance <= entry:
        return skip("no overhead resistance for a target")
    target = resistance
    rr = (target - entry) / risk
    rr_ok = gate(rr >= cfg["min_rr"],
                 f"RR {rr:.2f} >= {cfg['min_rr']:.1f} (entry {entry:g}, target {target:g}, stop {stop:.2f})")
    if not rr_ok:
        return skip(f"RR {rr:.2f} < {cfg['min_rr']:.1f}")

    return {
        "symbol": symbol, "tradeable": True, "uptrend": grade,
        "rr": rr, "entry": entry, "exit": target, "stop": stop,
        "ledger": ledger, "live": live, "date": last["date"],
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_verbose(res: dict) -> None:
    tag = "  [LIVE]" if res.get("live") else ""
    print(f"\n{'=' * 60}\n{res['symbol']}{tag}\n{'=' * 60}")
    for line in res["ledger"]:
        print(f"  {line}")
    if res["tradeable"]:
        print(f"  >>> TRADEABLE  uptrend={res['uptrend']}  RR={res['rr']:.2f}  "
              f"entry={res['entry']:g}  exit={res['exit']:g}  stop={res['stop']:.2f}")
    else:
        print(f"  >>> skipped: {res['reason']}")


def print_table(results: list[dict]) -> None:
    shown = sorted((r for r in results if r["tradeable"]),
                   key=lambda r: r["rr"], reverse=True)
    skipped = [r for r in results if not r["tradeable"]]

    print(f"\n{'SYMBOL':<12}{'UPTREND':<10}{'RR':>6}{'ENTRY':>10}"
          f"{'EXIT':>10}{'STOP':>10}")
    print("-" * 58)
    if not shown:
        print("  (no tradeable pullback setups in this list)")
    for r in shown:
        live_tag = " *" if r.get("live") else ""
        print(f"{r['symbol'] + live_tag:<12}{r['uptrend']:<10}{r['rr']:>6.2f}"
              f"{r['entry']:>10.2f}{r['exit']:>10.2f}{r['stop']:>10.2f}")
    print("-" * 58)
    print(f"{len(shown)} tradeable, {len(skipped)} skipped.  "
          "Pullback-to-support swing setups; RR >= 2.0.  Not investment advice.")
    if shown and any(r.get("live") for r in shown):
        print("  * = includes a live/intraday bar (provisional).")

    if skipped:
        print("\nSkipped:")
        for r in sorted(skipped, key=lambda r: r["symbol"]):
            print(f"  {r['symbol']:<12} {r['reason']}")


def write_csv(results: list[dict], path: str) -> int:
    shown = sorted((r for r in results if r["tradeable"]),
                   key=lambda r: r["rr"], reverse=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "uptrend", "rr", "entry", "exit", "stop", "live"])
        for r in shown:
            w.writerow([r["symbol"], r["uptrend"], f"{r['rr']:.2f}",
                        f"{r['entry']:.2f}", f"{r['exit']:.2f}", f"{r['stop']:.2f}",
                        int(bool(r.get("live")))])
    return len(shown)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="DSE swing pullback-to-support signal (uptrend, RR, entry, exit, stop).")
    ap.add_argument("symbols", nargs="*", help="DSE trading codes, e.g. ACMEPL KBPPWBIL")
    ap.add_argument("--from-xlsx", metavar="PATH",
                    help="Read the ticker list from an .xlsx (e.g. 'Debt to Equity Ratio.xlsx')")
    ap.add_argument("--col", default="Code",
                    help="Header of the ticker column in --from-xlsx (default 'Code')")
    ap.add_argument("--days", type=int, default=400,
                    help="History window in calendar days (default 400)")
    ap.add_argument("--min-rr", type=float, default=DEFAULTS["min_rr"],
                    help=f"Minimum risk/reward to show a row (default {DEFAULTS['min_rr']})")
    ap.add_argument("--max-ext", type=float, default=DEFAULTS["ext_above_sma20_max_pct"],
                    help="Anti-chase: max %% above SMA20 (default %(default)s)")
    ap.add_argument("--max-day-gain", type=float, default=DEFAULTS["day_gain_max_pct"],
                    help="Anti-chase: max signal-bar gain %% (default %(default)s)")
    ap.add_argument("--csv", action="store_true",
                    help="Also write shown rows to dse_claude_signals.csv")
    ap.add_argument("--verbose", action="store_true",
                    help="Print the per-ticker PASS/FAIL gate ledger")
    ap.add_argument("--no-live", action="store_true",
                    help="Skip the live intraday snapshot; use the day-end archive only")
    args = ap.parse_args(argv)

    cfg = {**DEFAULTS, "min_rr": args.min_rr,
           "ext_above_sma20_max_pct": args.max_ext,
           "day_gain_max_pct": args.max_day_gain}

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

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    live_snapshot: dict[str, dict] = {}
    session_date: dt.date | None = None
    if not args.no_live:
        try:
            live_snapshot, session_date = fetch_live_snapshot()
            print(f"Live snapshot: {len(live_snapshot)} tickers as of {session_date}.")
        except Exception as exc:  # noqa: BLE001 - degrade to archive-only on any failure
            print(f"live snapshot unavailable ({exc}); using archive only", file=sys.stderr)

    results: list[dict] = []
    for i, sym in enumerate(symbols):
        sym = sym.upper()
        if i > 0:
            time.sleep(FETCH_DELAY_SECONDS)  # don't hammer the DSE archive
        try:
            bars = fetch_history(sym, start, end)
        except Exception as exc:  # noqa: BLE001 - surface any fetch/parse failure clearly
            print(f"{sym}: failed to fetch data ({exc})", file=sys.stderr)
            results.append({"symbol": sym, "tradeable": False,
                            "reason": "fetch failed", "ledger": [], "live": False})
            continue
        live = merge_live_bar(bars, live_snapshot.get(sym), session_date)
        if not bars:
            results.append({"symbol": sym, "tradeable": False,
                            "reason": "no data (check code / date range)",
                            "ledger": [], "live": False})
            continue
        res = evaluate(sym, bars, live, cfg)
        results.append(res)
        if args.verbose:
            print_verbose(res)

    print_table(results)
    if args.csv:
        n = write_csv(results, "dse_claude_signals.csv")
        print(f"\nWrote {n} rows -> dse_claude_signals.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
