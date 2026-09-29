"""
DSE Uptrend Gate -- TRADE / NO-TRADE verdict, live DSE data, many tickers.

The DSE-native sibling of `dse_trend.py`. Same gating philosophy -- a strict
all-or-nothing uptrend confirmation -- but instead of reading a local CSV it
pulls live daily OHLCV straight from the Dhaka Stock Exchange day-end archive
(via `dse_technical.fetch_history`) and accepts a whole watchlist of trading
codes at once.

GATE MODEL (unchanged from dse_trend.py)
----------------------------------------
  * MANDATORY gates -- EVERY one must PASS for a TRADE (uptrend) verdict.
                       One failure -> NO TRADE. No partial credit.
  * OPTIONAL  gates -- confidence boosters only; never change the verdict
                       (60% base for all-mandatory, up to +40% from these).
  * GUARD     gates -- VETO a TRADE if they fail. Only active when you supply
                       their inputs (--min-turnover, --index-symbol).

Standard library only (indicators the pandas original needed -- ADX, OBV, MA
slope, higher-highs/lows -- are ported to pure python here); reuses the fetch
and shared indicators from `dse_technical.py`.

Usage:
    python dse_uptrend.py ACMEPL
    python dse_uptrend.py ACMEPL KBPPWBIL SQURPHARMA --days 730
    python dse_uptrend.py ACMEPL --min-turnover 100000 --index-symbol DS30
    python dse_uptrend.py --from-xlsx "Debt to Equity Ratio.xlsx" --brief
    python dse_uptrend.py ACMEPL --json

This is decision SUPPORT, not investment advice. Every figure traces back to
fetched OHLCV -- nothing is fabricated. The position decision remains yours.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

from dse_technical import (atr, fetch_history, macd, read_tickers_xlsx, rsi,
                           sma)

FETCH_DELAY_SECONDS = 3  # polite pause between successive DSE archive calls
MIN_ROWS = 200           # need >=200 clean bars for the 200-day MA


# --------------------------------------------------------------------------- #
# Extra indicators the pandas original relied on -- ported to pure python.
# (sma/macd/rsi/atr are imported from dse_technical.)
# --------------------------------------------------------------------------- #
def _clean(bars: list[dict]) -> list[dict]:
    """Drop bars with a NaN close (DSE '--' sentinels)."""
    return [b for b in bars if b["close"] == b["close"]]


def _ewm(values: list[float], alpha: float) -> list[float]:
    """Recursive EW mean seeded with the first value (== pandas ewm adjust=False).
    With alpha=1/n this is Wilder smoothing, matching dse_trend's ADX/ATR."""
    if not values:
        return []
    out = [values[0]]
    for v in values[1:]:
        out.append(v * alpha + out[-1] * (1 - alpha))
    return out


def sma_series(values: list[float], period: int) -> list[float]:
    """Rolling SMA as a list (length len(values)-period+1); [] if too short."""
    if len(values) < period:
        return []
    out = []
    window = sum(values[:period])
    out.append(window / period)
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out.append(window / period)
    return out


def _linreg_slope(ys: list[float]) -> float:
    """Least-squares slope of ys against x = 0,1,2,..."""
    n = len(ys)
    xs = range(n)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    return (n * sxy - sx * sy) / denom if denom else 0.0


def slope_positive(series: list[float], n: int) -> bool:
    """True if a linear fit over the last n points has positive slope."""
    y = [v for v in series if v == v]  # drop NaN
    y = y[-n:]
    if len(y) < n:
        return False
    return _linreg_slope(y) > 0


def adx(bars: list[dict], n: int = 14):
    """Return (ADX, +DI, -DI) at the last bar, Wilder-smoothed; None if short."""
    if len(bars) < n + 2:
        return None
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    atr_s = _ewm(trs, 1 / n)
    plus_s = _ewm(plus_dm, 1 / n)
    minus_s = _ewm(minus_dm, 1 / n)
    plus_di = [100 * p / a if a else 0.0 for p, a in zip(plus_s, atr_s)]
    minus_di = [100 * m / a if a else 0.0 for m, a in zip(minus_s, atr_s)]
    dx = []
    for p, m in zip(plus_di, minus_di):
        tot = p + m
        dx.append(100 * abs(p - m) / tot if tot else 0.0)
    adx_s = _ewm(dx, 1 / n)
    return adx_s[-1], plus_di[-1], minus_di[-1]


def obv_series(closes: list[float], volumes: list[float]) -> list[float] | None:
    """On-balance volume series; None if volume is entirely missing."""
    if all(v != v for v in volumes):
        return None
    out = [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        vol = volumes[i] if volumes[i] == volumes[i] else 0.0
        sign = 1.0 if d > 0 else -1.0 if d < 0 else 0.0
        out.append(out[-1] + sign * vol)
    return out


def higher_highs_lows(highs: list[float], lows: list[float],
                      lookback: int = 40) -> bool:
    """Crude structure check: recent swing highs and lows are both rising."""
    if len(highs) < lookback:
        return False
    h = highs[-lookback:]
    l = lows[-lookback:]
    half = lookback // 2
    hh = max(h[half:]) > max(h[:half])
    hl = min(l[half:]) > min(l[:half])
    return bool(hh and hl)


# --------------------------------------------------------------------------- #
# Gate evaluation
# --------------------------------------------------------------------------- #
def evaluate_uptrend(symbol: str, bars: list[dict], index_bars=None,
                     min_avg_vol=None) -> dict:
    """Run the uptrend gates. Returns a structured decision dict; decision is
    one of TRADE (UPTREND) / NO TRADE / NO DATA."""
    bars = _clean(bars)
    if len(bars) < MIN_ROWS:
        return {"symbol": symbol, "decision": "NO DATA",
                "reason": f"only {len(bars)} clean bars (need >={MIN_ROWS} for "
                          "the 200-day MA) -- illiquid / thin / bad code",
                "mandatory": {}, "optional": {}, "guards": {},
                "snapshot": None, "confidence": None}

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]
    price = closes[-1]

    ma20, ma50, ma200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    ma200_ser = sma_series(closes, 200)
    ma50_ser = sma_series(closes, 50)
    r = rsi(closes, 14)
    m = macd(closes)                       # (line, signal, hist) or None
    a = adx(bars)                          # (adx, +di, -di) or None
    obv = obv_series(closes, vols)

    adx_val, plus_di, minus_di = a if a else (float("nan"),) * 3
    macd_line, macd_sig = (m[0], m[1]) if m else (float("nan"), float("nan"))

    # ---- MANDATORY gates (all must pass for TRADE) ----
    mandatory = {
        "Price > MA20":           bool(ma20 and price > ma20),
        "Price > MA50":           bool(ma50 and price > ma50),
        "Price > MA200":          bool(ma200 and price > ma200),
        "MA50 > MA200 (Golden)":  bool(ma50 and ma200 and ma50 > ma200),
        "MA20 > MA50 (aligned)":  bool(ma20 and ma50 and ma20 > ma50),
        "MA200 sloping up":       slope_positive(ma200_ser, 20),
        "ADX > 25 & +DI > -DI":   bool(a) and adx_val > 25 and plus_di > minus_di,
        "RSI in 50-72 band":      r is not None and 50 <= r <= 72,
        "MACD > signal & > 0":    bool(m) and macd_line > macd_sig and macd_line > 0,
    }

    # ---- OPTIONAL gates (confidence boosters) ----
    optional = {
        "MA50 sloping up":         slope_positive(ma50_ser, 15),
        "Higher highs & lows":     higher_highs_lows(highs, lows),
        "OBV rising":              slope_positive(obv, 20) if obv else False,
        "RSI not overbought(<72)": r is not None and r < 72,
    }

    # ---- GUARD gates (VETO a TRADE if they fail) ----
    guards = {}
    if min_avg_vol is not None:
        recent = [v for v in vols[-20:] if v == v]
        avg_vol = sum(recent) / len(recent) if recent else 0.0
        guards["Liquidity: 20d avg vol >= {:,.0f}".format(min_avg_vol)] = \
            avg_vol >= min_avg_vol
    if index_bars is not None:
        iclose = [b["close"] for b in _clean(index_bars)]
        if len(iclose) >= 50:
            isma50 = sma(iclose, 50)
            guards["Market: index > 50-day MA"] = bool(isma50 and iclose[-1] > isma50)

    mand_pass = all(mandatory.values())
    guard_pass = all(guards.values()) if guards else True
    opt_score = sum(1 for v in optional.values() if v)
    opt_total = len(optional)
    is_trade = mand_pass and guard_pass

    return {
        "symbol": symbol,
        "decision": "TRADE (UPTREND)" if is_trade else "NO TRADE",
        "reason": None if mand_pass else "failed mandatory: "
                  + next(k for k, v in mandatory.items() if not v),
        "mandatory": mandatory,
        "optional": optional,
        "guards": guards,
        "mandatory_passed": mand_pass,
        "guards_passed": guard_pass,
        "optional_score": f"{opt_score}/{opt_total}",
        "confidence": round(60 + 40 * (opt_score / opt_total), 1) if is_trade else None,
        "snapshot": {
            "date": str(bars[-1]["date"]),
            "close": round(float(price), 2),
            "MA20": round(float(ma20), 2) if ma20 else None,
            "MA50": round(float(ma50), 2) if ma50 else None,
            "MA200": round(float(ma200), 2) if ma200 else None,
            "RSI14": round(float(r), 1) if r is not None else None,
            "ADX": round(float(adx_val), 1) if a else None,
            "+DI": round(float(plus_di), 1) if a else None,
            "-DI": round(float(minus_di), 1) if a else None,
            "MACD": round(float(macd_line), 3) if m else None,
            "MACD_signal": round(float(macd_sig), 3) if m else None,
        },
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_report(res: dict) -> None:
    print(f"\n{'=' * 56}\n  DSE UPTREND GATE   {res['symbol']}\n{'=' * 56}")
    if res["decision"] == "NO DATA":
        print(f"  {res['reason']}")
        return

    def line(name, ok):
        return f"    [{'PASS' if ok else 'FAIL'}] {name}"

    print("\n  MANDATORY GATES (all required for TRADE):")
    for k, v in res["mandatory"].items():
        print(line(k, v))
    print("\n  OPTIONAL GATES (confidence only):")
    for k, v in res["optional"].items():
        print(line(k, v))
    if res["guards"]:
        print("\n  GUARD GATES (veto if fail):")
        for k, v in res["guards"].items():
            print(line(k, v))

    print("\n  SNAPSHOT:")
    for k, v in res["snapshot"].items():
        print(f"    {k:<12}: {v}")

    print("\n  " + "-" * 52)
    print(f"    VERDICT      : {res['decision']}"
          + (f"  ({res['reason']})" if res["reason"] else ""))
    print(f"    Mandatory    : {'ALL PASS' if res['mandatory_passed'] else 'FAILED'}")
    print(f"    Guards       : {'PASS' if res['guards_passed'] else 'FAILED'}")
    print(f"    Optional     : {res['optional_score']}")
    if res["confidence"] is not None:
        print(f"    Confidence   : {res['confidence']}%")
    print("  " + "-" * 52)
    print("\n  NOTE: technical gate only -- combine with fundamentals & your own "
          "risk limits. Not investment advice.")


def brief_row(res: dict) -> dict:
    """Condense a full evaluation into a one-line screening summary."""
    if res["decision"] == "NO DATA":
        return {"symbol": res["symbol"], "rank": -1, "decision": "NO DATA",
                "note": res["reason"]}
    passed = sum(1 for v in res["mandatory"].values() if v)
    total = len(res["mandatory"])
    opt = int(res["optional_score"].split("/")[0])
    s = res["snapshot"]
    actionable = res["decision"].startswith("TRADE")
    rank = (1000 if actionable else 0) + passed * 10 + opt
    return {
        "symbol": res["symbol"], "rank": rank, "decision": res["decision"],
        "price": s["close"], "rsi": s["RSI14"], "adx": s["ADX"],
        "passed": passed, "total": total, "optional_score": res["optional_score"],
        "confidence": res["confidence"], "fail": res["reason"],
    }


def print_brief_table(rows: list[dict]) -> None:
    """Ranked one-line-per-stock screen (actionable TRADEs first)."""
    ranked = sorted((r for r in rows if r["rank"] >= 0),
                    key=lambda r: r["rank"], reverse=True)
    skipped = [r for r in rows if r["rank"] < 0]

    print(f"\n{'SYMBOL':<12}{'DECISION':<16}{'PRICE':>9}{'RSI':>6}{'ADX':>6}"
          f"{'MAND':>7}{'OPT':>6}{'CONF':>7}  BLOCKER")
    print("-" * 96)
    for r in ranked:
        rsi_s = f"{r['rsi']:.0f}" if r["rsi"] is not None else "-"
        adx_s = f"{r['adx']:.0f}" if r["adx"] is not None else "-"
        conf_s = f"{r['confidence']:.0f}%" if r["confidence"] is not None else "-"
        blocker = "" if r["decision"].startswith("TRADE") else (r["fail"] or "")
        print(f"{r['symbol']:<12}{r['decision']:<16}{r['price']:>9g}{rsi_s:>6}"
              f"{adx_s:>6}{str(r['passed'])+'/'+str(r['total']):>7}"
              f"{r['optional_score']:>6}{conf_s:>7}  {blocker}")
    for r in skipped:
        print(f"{r['symbol']:<12}{'NO DATA':<16}{r['note']}")
    print("-" * 96)
    trades = sum(1 for r in ranked if r["decision"].startswith("TRADE"))
    print(f"{len(ranked)} screened, {trades} TRADE (UPTREND), {len(skipped)} skipped.  "
          "Deep-dive the trades without --brief for full gate detail.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="DSE uptrend TRADE/NO-TRADE gate over live DSE data (many tickers).")
    ap.add_argument("symbols", nargs="*", help="DSE trading codes, e.g. ACMEPL KBPPWBIL")
    ap.add_argument("--from-xlsx", metavar="PATH",
                    help="Read the ticker list from an .xlsx (e.g. 'Debt to Equity Ratio.xlsx')")
    ap.add_argument("--col", default="Code",
                    help="Header of the ticker column in --from-xlsx (default 'Code')")
    ap.add_argument("--days", type=int, default=730,
                    help="History window in calendar days (default 730; keep >=~400 for MA200)")
    ap.add_argument("--min-turnover", type=float, default=None, dest="min_turnover",
                    help="Liquidity guard: min avg 20-day volume (shares). Omit to disable.")
    ap.add_argument("--index-symbol", default=None,
                    help="Market guard: fetch this index code and require it > its 50-day MA "
                         "(e.g. DS30). Omit to disable.")
    ap.add_argument("--brief", action="store_true",
                    help="One-line ranked screen per stock (use for long watchlists)")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON (list of results) instead of text reports")
    args = ap.parse_args(argv)

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
        symbols = symbols + from_file  # CLI symbols first, then file list

    if not symbols:
        ap.error("no tickers given -- pass trading codes and/or --from-xlsx PATH")

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    # Fetch the market index once (guard is the same for every ticker).
    index_bars = None
    if args.index_symbol:
        try:
            index_bars = fetch_history(args.index_symbol.upper(), start, end)
            if not index_bars:
                print(f"warning: index {args.index_symbol} returned no data -- "
                      "market guard disabled.", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: index {args.index_symbol} fetch failed ({exc}) -- "
                  "market guard disabled.", file=sys.stderr)
            index_bars = None

    brief_rows: list[dict] = []
    json_out: list[dict] = []
    fetched_any = False
    for sym in symbols:
        sym = sym.upper()
        if fetched_any:
            time.sleep(FETCH_DELAY_SECONDS)  # don't hammer the DSE archive
        try:
            bars = fetch_history(sym, start, end)
            fetched_any = True
        except Exception as exc:  # noqa: BLE001 - surface fetch/parse failures clearly
            print(f"{sym}: failed to fetch data ({exc})", file=sys.stderr)
            if args.brief:
                brief_rows.append({"symbol": sym, "rank": -1, "decision": "NO DATA",
                                   "note": "fetch failed"})
            continue
        if not bars:
            res = {"symbol": sym, "decision": "NO DATA",
                   "reason": "no data returned (check the trading code / date range).",
                   "mandatory": {}, "optional": {}, "guards": {},
                   "snapshot": None, "confidence": None}
        else:
            res = evaluate_uptrend(sym, bars, index_bars=index_bars,
                                   min_avg_vol=args.min_turnover)

        if args.json:
            json_out.append(res)
        elif args.brief:
            brief_rows.append(brief_row(res))
        else:
            print_report(res)

    if args.json:
        print(json.dumps(json_out, indent=2))
    elif args.brief:
        print_brief_table(brief_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
