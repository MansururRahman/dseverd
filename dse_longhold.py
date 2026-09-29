"""
DSE LONG-HOLD position picker -- rank tickers for a "trade rarely, hold longer"
book, the approach the swing backtests actually supported (see
SWING_SIGNAL_REVIEW_2026-07-23.md and .claude/commands/swing.md).

WHY THIS EXISTS
    Short-term technical swing signals showed no edge on DSE and bled to costs;
    plain buy-and-hold beat them (+27% over 2024-07..2026-07). The two things
    that DO travel on a long horizon are (a) durable trend / long-horizon
    relative strength and (b) low leverage. This tool ranks on exactly those and
    rebalances infrequently, so cost drag is minimal.

WHAT IT IS / IS NOT
    It is transparent decision SUPPORT: an auditable, additive score (each part
    prints its own reason), gated on a long-term uptrend + liquidity + balance-
    sheet safety. It is NOT a proven alpha engine -- the free old.dsebd.org archive
    only serves ~2 years, so a real multi-regime, out-of-sample proof is not
    possible here. The built-in --backtest checks the *technical* rotation rule
    (causal) against buy-and-hold; the leverage filter is a current-snapshot
    quality overlay for the LIVE pick only (using it historically would look
    ahead). Cross-check leverage with /dse-de before committing real money.

    Standard library only. Reuses fetch + indicators from dse_technical.

Usage:
    python dse_longhold.py --from-xlsx "Debt to Equity Ratio.xlsx"      # rank all
    python dse_longhold.py ACMEPL NAVANAPHAR MARICO                     # rank a few
    python dse_longhold.py --from-xlsx "Debt to Equity Ratio.xlsx" --backtest --top 5
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

from dse_technical import atr, fetch_history, read_tickers_xlsx, sma

FETCH_DELAY_SECONDS = 3
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# --------------------------------------------------------------------------- #
# Tunables (deliberately few -- a long-hold screen should be robust, not fitted)
# --------------------------------------------------------------------------- #
MIN_TURNOVER = 5_000_000.0   # 20d avg turnover (BDT) for tradeable liquidity
DE_UNSAFE = 2.0              # D/E at/above this -> excluded as over-leveraged
DE_GOOD, DE_OK = 0.30, 0.75  # D/E scoring tiers
REBAL_EVERY = 21             # backtest rebalance cadence (~1 month of trading days)
COST = 0.005                 # ~0.5% round-trip applied to backtest turnover


# --------------------------------------------------------------------------- #
# Generic .xlsx reader (stdlib) -> list of {header: cell} dicts
# --------------------------------------------------------------------------- #
def _read_xlsx(path: str) -> list[dict]:
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))

    def txt(c) -> str:
        v = c.find(f"{_NS}v")
        if v is None or v.text is None:
            return ""
        return shared[int(v.text)] if c.get("t") == "s" else (v.text or "")

    def col(r: str) -> str:
        return "".join(ch for ch in (r or "") if ch.isalpha())

    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = sheet.find(f"{_NS}sheetData").findall(f"{_NS}row")
    if not rows:
        return []
    header = {col(c.get("r")): txt(c).strip() for c in rows[0]}
    out = []
    for row in rows[1:]:
        rec = {}
        for c in row:
            h = header.get(col(c.get("r")))
            if h:
                rec[h] = txt(c).strip()
        if any(rec.values()):
            out.append(rec)
    return out


def _f(x) -> float:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return float("nan")


def load_fundamentals() -> dict[str, dict]:
    """{CODE: {de, equity, debt}} from the Debt-to-Equity sheet. EPS not used."""
    fund: dict[str, dict] = {}
    for path in glob.glob("Debt to Equity Ratio.xlsx"):
        for r in _read_xlsx(path):
            code = (r.get("Code") or "").strip().upper()
            if code:
                fund[code] = {"de": _f(r.get("Ratio")), "equity": _f(r.get("Equity")),
                              "debt": _f(r.get("Debt"))}
    return fund


# --------------------------------------------------------------------------- #
# Technical metrics (causal: computed from bars up to the last one supplied)
# --------------------------------------------------------------------------- #
def _clean(bars: list[dict]) -> list[dict]:
    return [b for b in bars if b["close"] == b["close"]]


def _ret(closes: list[float], n: int) -> float:
    return (closes[-1] / closes[-1 - n] - 1) * 100 if len(closes) > n else float("nan")


def compute_metrics(bars: list[dict]) -> dict | None:
    """Long-horizon technical read at the last bar. None if too little data."""
    bars = _clean(bars)
    if len(bars) < 130:                     # need ~6 months for RS + a trend read
        return None
    closes = [b["close"] for b in bars]
    price = closes[-1]
    s200 = sma(closes, 200)
    s100 = sma(closes, 100)
    s200_prev = sma(closes[:-REBAL_EVERY], 200)
    s100_prev = sma(closes[:-REBAL_EVERY], 100)
    # Durable uptrend: above the long MA AND that MA rising. Fall back to SMA100
    # when <200 bars (flagged), so young listings aren't silently dropped.
    if s200 is not None and s200_prev is not None:
        trend_ma, trend_prev, ma_label = s200, s200_prev, "SMA200"
    else:
        trend_ma, trend_prev, ma_label = s100, s100_prev, "SMA100"
    trend_up = bool(trend_ma and trend_prev and price > trend_ma and trend_ma > trend_prev)
    above_pct = (price / trend_ma - 1) * 100 if trend_ma else float("nan")
    turnover20 = sum(b["close"] * b["volume"] for b in bars[-20:]
                     if b["volume"] == b["volume"]) / 20
    a = atr(bars)
    return {
        "price": price, "ma": trend_ma, "ma_label": ma_label, "trend_up": trend_up,
        "above_pct": above_pct, "rs_6m": _ret(closes, 126), "rs_3m": _ret(closes, 63),
        "rs_12m": _ret(closes, 252), "turnover": turnover20,
        "atr_pct": (a / price * 100) if a else float("nan"),
    }


# --------------------------------------------------------------------------- #
# Scoring -- transparent additive tally (matches the project's audit style)
# --------------------------------------------------------------------------- #
def score_ticker(m: dict, f: dict) -> dict:
    """Gate + score a candidate. Hard gates decide eligibility; the score ranks
    the survivors. Every component records its own reason string."""
    notes: list[str] = []
    flags: list[str] = []
    score = 0

    def add(pts: int, why: str) -> None:
        nonlocal score
        score += pts
        notes.append(f"{pts:+d} {why}")

    # ---- Hard gates (eligibility to HOLD) ----
    gates: list[tuple[str, bool, str]] = []
    gates.append(("liquidity", m["turnover"] >= MIN_TURNOVER,
                  f"turnover {m['turnover']:,.0f}"))
    gates.append(("long-term uptrend", m["trend_up"],
                  f"price {'>' if m['price'] > m['ma'] else '<='} {m['ma_label']} "
                  f"{m['ma']:.2f} & {'rising' if m['trend_up'] else 'flat/falling'}"))
    equity = f.get("equity", float("nan"))
    de = f.get("de", float("nan"))
    solvent = not (equity == equity and equity <= 0)
    not_overlev = not (de == de and de >= DE_UNSAFE)
    gates.append(("solvent (equity>0)", solvent,
                  f"equity {equity:,.0f}" if equity == equity else "equity n/a"))
    gates.append((f"leverage < {DE_UNSAFE:g}", not_overlev,
                  f"D/E {de:.2f}" if de == de else "D/E n/a"))
    eligible = all(ok for _, ok, _ in gates)
    blocker = next((n for n, ok, _ in gates if not ok), None)

    # ---- Score (long-horizon RS is primary) ----
    rs = m["rs_6m"]
    if rs == rs:
        if rs > 25: add(3, f"6m RS +{rs:.0f}% (strong)")
        elif rs > 10: add(2, f"6m RS +{rs:.0f}%")
        elif rs > 0: add(1, f"6m RS +{rs:.0f}%")
        else: add(-1, f"6m RS {rs:.0f}% (lagging)")
    if m["rs_3m"] == m["rs_3m"] and m["rs_3m"] > 0:
        add(1, f"3m RS +{m['rs_3m']:.0f}%")
    # Trend quality: reward being above the long MA, but penalise blow-off extension.
    ap = m["above_pct"]
    if ap == ap:
        if ap > 60: add(-1, f"{ap:.0f}% over {m['ma_label']} (over-extended)")
        elif ap <= 25: add(1, f"{ap:.0f}% over {m['ma_label']} (healthy)")
    # Leverage quality.
    if de == de:
        if de < DE_GOOD: add(2, f"D/E {de:.2f} (very low)")
        elif de < DE_OK: add(1, f"D/E {de:.2f} (low)")
        elif de >= 1.5: add(-1, f"D/E {de:.2f} (high)")
    if m["ma_label"] == "SMA100":
        flags.append("<200d history (SMA100 trend)")

    return {"eligible": eligible, "blocker": blocker, "score": score,
            "notes": notes, "flags": flags, "gates": gates}


# --------------------------------------------------------------------------- #
# Reporting -- current ranked picks
# --------------------------------------------------------------------------- #
def print_ranking(rows: list[dict], top: int) -> None:
    picks = sorted((r for r in rows if r["res"]["eligible"]),
                   key=lambda r: r["res"]["score"], reverse=True)
    rejected = [r for r in rows if not r["res"]["eligible"]]

    print(f"\n{'='*94}\nDSE LONG-HOLD RANKING  --  {len([r for r in rows])} screened, "
          f"{len(picks)} eligible to HOLD\n{'='*94}")
    print(f"{'#':>2} {'SYMBOL':<11}{'PRICE':>9}{'>MA%':>7}{'RS6m':>7}{'RS3m':>7}"
          f"{'D/E':>6}{'SCORE':>6}  FLAGS")
    print("-" * 94)
    for i, r in enumerate(picks[:top] if top else picks, 1):
        m, f, res = r["m"], r["f"], r["res"]
        de = f.get("de", float("nan"))
        print(f"{i:>2} {r['sym']:<11}{m['price']:>9g}{m['above_pct']:>6.0f}%"
              f"{m['rs_6m']:>+6.0f}%{m['rs_3m']:>+6.0f}%"
              f"{(f'{de:.2f}' if de == de else '-'):>6}{res['score']:>+6}  "
              f"{', '.join(res['flags'])}")
    print("-" * 94)
    if picks:
        print("\nTop pick rationale (auditable score breakdown):")
        for r in picks[:min(top or 5, 5)]:
            print(f"  {r['sym']:<11} score {r['res']['score']:+d}: "
                  f"{'; '.join(r['res']['notes'])}")
    if rejected:
        from collections import Counter
        why = Counter(r["res"]["blocker"] for r in rejected)
        print("\nNot eligible (blocking gate): "
              + ", ".join(f"{k} x{v}" for k, v in why.most_common()))
    print("\nHOLD RULE  : hold each pick while it stays above its long MA "
          f"({picks[0]['m']['ma_label'] if picks else 'SMA200'}). Sell only when it "
          "closes below it or D/E deteriorates. Re-run ~monthly -- do NOT churn.")
    print("NOTE       : decision support, not advice. Fundamentals are a recent "
          "snapshot; verify leverage with /dse-de before committing.")


# --------------------------------------------------------------------------- #
# Backtest -- monthly trend/RS rotation (TECHNICAL ONLY -> causal, no look-ahead)
# --------------------------------------------------------------------------- #
def backtest(data: dict[str, list[dict]], top: int) -> None:
    close_by = {s: {b["date"]: b["close"] for b in _clean(bars)} for s, bars in data.items()}
    dates = sorted({d for m in close_by.values() for d in m})

    def close_asof(sym: str, d: dt.date):
        m = close_by[sym]
        if d in m:
            return m[d]
        prior = [x for x in m if x <= d]
        return m[max(prior)] if prior else None

    # Rebalance points along the shared calendar (need >=200 bars of lead-in).
    rebals = list(range(200, len(dates) - 1, REBAL_EVERY))
    if len(rebals) < 2:
        print("\nBacktest: not enough history for a monthly rotation."); return

    equity, curve = 1.0, [1.0]
    bench, bcurve = 1.0, [1.0]
    held: set[str] = set()
    turnovers = 0
    for k in range(len(rebals) - 1):
        d0, d1 = dates[rebals[k]], dates[rebals[k + 1]]
        # Rank eligible tickers using ONLY data up to d0.
        ranked = []
        for s, bars in data.items():
            upto = [b for b in _clean(bars) if b["date"] <= d0]
            m = compute_metrics(upto)
            if not m or not m["trend_up"] or m["turnover"] < MIN_TURNOVER:
                continue
            if m["rs_6m"] == m["rs_6m"]:
                ranked.append((m["rs_6m"], s))
        ranked.sort(reverse=True)
        newhold = {s for _, s in ranked[:top]}
        turnovers += len(newhold ^ held)            # names changed = round trips
        held = newhold

        # Portfolio return over [d0, d1], equal-weight the held names.
        rets = []
        for s in held:
            c0, c1 = close_asof(s, d0), close_asof(s, d1)
            if c0 and c1:
                rets.append(c1 / c0 - 1)
        port = sum(rets) / len(rets) if rets else 0.0
        equity *= (1 + port)                        # gross; cost applied via turnover below
        curve.append(equity)

        # Benchmark: equal-weight ALL tickers with data, same period.
        brets = []
        for s in data:
            c0, c1 = close_asof(s, d0), close_asof(s, d1)
            if c0 and c1:
                brets.append(c1 / c0 - 1)
        bench *= (1 + (sum(brets) / len(brets) if brets else 0.0))
        bcurve.append(bench)

    # Apply total cost drag from turnover (round trips * per-side cost).
    total_cost = turnovers * (COST / 2)
    net_equity = curve[-1] * (1 - total_cost)

    def mdd(c):
        peak, dd = c[0], 0.0
        for v in c:
            peak = max(peak, v); dd = min(dd, v / peak - 1)
        return dd

    print(f"\n{'='*72}\nLONG-HOLD ROTATION BACKTEST (top {top}, ~monthly, technical-only)")
    print(f"{'='*72}")
    print(f"  Window            : {dates[rebals[0]]} -> {dates[rebals[-1]]}  "
          f"({len(rebals)-1} rebalances)")
    print(f"  Avg names changed : {turnovers/(len(rebals)-1):.1f}/rebalance "
          f"(low churn = low cost)")
    print(f"  Strategy return   : {(curve[-1]-1)*100:+.1f}% gross | "
          f"{(net_equity-1)*100:+.1f}% net of ~{total_cost*100:.1f}% cost")
    print(f"  Strategy max DD   : {mdd(curve)*100:.1f}%")
    print(f"  Buy&hold (bench)  : {(bcurve[-1]-1)*100:+.1f}%   max DD {mdd(bcurve)*100:.1f}%")
    print(f"{'='*72}")
    print("  The bar: match/beat buy-and-hold, ideally with a shallower drawdown.")
    print("  Fundamental overlay is applied to the LIVE pick only (snapshot data);")
    print("  using it historically would look ahead, so the backtest is trend/RS only.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="DSE long-hold position picker.")
    ap.add_argument("symbols", nargs="*", help="DSE trading codes")
    ap.add_argument("--from-xlsx", metavar="PATH", help="Read ticker list from an .xlsx")
    ap.add_argument("--col", default="Code", help="Ticker column header (default 'Code')")
    ap.add_argument("--days", type=int, default=1000, help="History window (calendar days)")
    ap.add_argument("--top", type=int, default=10, help="Show/hold top N (default 10)")
    ap.add_argument("--backtest", action="store_true",
                    help="Backtest the monthly trend/RS rotation vs buy-and-hold")
    args = ap.parse_args(argv)

    symbols = list(args.symbols)
    if args.from_xlsx:
        try:
            symbols += read_tickers_xlsx(args.from_xlsx, args.col)
        except Exception as exc:  # noqa: BLE001
            print(f"failed to read {args.from_xlsx} ({exc})", file=sys.stderr); return 2
    if not symbols:
        print("no tickers -- pass codes and/or --from-xlsx PATH", file=sys.stderr); return 2

    fund = load_fundamentals()
    print(f"Loaded fundamentals for {len(fund)} codes. Fetching {len(symbols)} tickers...")
    end = dt.date.today(); start = end - dt.timedelta(days=args.days)

    data: dict[str, list[dict]] = {}
    for i, sym in enumerate(symbols):
        sym = sym.upper()
        if i:
            time.sleep(FETCH_DELAY_SECONDS)
        try:
            bars = fetch_history(sym, start, end)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: fetch failed ({exc})", file=sys.stderr); continue
        if bars:
            data[sym] = bars

    if args.backtest:
        backtest(data, args.top)
        return 0

    rows = []
    for sym, bars in data.items():
        m = compute_metrics(bars)
        if not m:
            continue
        f = fund.get(sym, {})
        rows.append({"sym": sym, "m": m, "f": f, "res": score_ticker(m, f)})
    print_ranking(rows, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
