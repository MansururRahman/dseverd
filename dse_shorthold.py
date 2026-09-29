"""
DSE SHORT-HOLD position picker -- rank tickers for a SHORT holding period
(days to ~2 weeks). Self-contained; reuses only fetch + indicators from
dse_technical. Companion to dse_mediumhold.py and dse_longhold.py.

HORIZON: SMA20 trend, 1-month relative strength, ~weekly rebalance.

  !!! COST WARNING !!!  A short horizon trades OFTEN. On DSE the ~0.5% round-trip
  cost bled every short-term signal we tested to a loss and buy-and-hold beat them
  (see SWING_SIGNAL_REVIEW_2026-07-23.md). This is the MOST cost-exposed of the
  three horizons -- always run --backtest and compare the NET return (not gross)
  to buy-and-hold before trusting it. Ranks on leverage-safe trend/RS only; EPS
  is intentionally NOT used.

Usage:
    python dse_shorthold.py --from-xlsx "Debt to Equity Ratio.xlsx"
    python dse_shorthold.py ACMEPL NAVANAPHAR --top 5
    python dse_shorthold.py --from-xlsx "Debt to Equity Ratio.xlsx" --backtest --top 5

Decision support, not investment advice. Standard library only.
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
# SHORT-HOLD parameters
# --------------------------------------------------------------------------- #
HORIZON_NAME = "SHORT-HOLD (days to ~2 weeks)"
TREND_P, TREND_NAME = 20, "SMA20"      # uptrend definition
RS1, RS1_NAME = 21, "RS1m"             # primary relative strength (1 month)
RS2, RS2_NAME = 10, "RS2w"             # secondary (2 weeks)
REBAL = 5                              # ~weekly rebalance / slope lookback
RS_STRONG, RS_MID = 10.0, 4.0          # RS score tiers
EXT_HI, EXT_OK = 15.0, 8.0             # over-extension caps (% over trend MA)
MIN_DAYS = 40                          # minimum clean bars for a read

MIN_TURNOVER = 5_000_000.0             # 20d avg turnover (BDT) for liquidity
DE_UNSAFE = 2.0                        # D/E >= this -> excluded (over-leveraged)
DE_GOOD, DE_OK = 0.30, 0.75            # D/E scoring tiers
COST = 0.005                           # ~0.5% round-trip for backtest turnover


# --------------------------------------------------------------------------- #
# Fundamentals (leverage only -- EPS not used)
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
        rec = {header[col(c.get("r"))]: txt(c).strip()
               for c in row if header.get(col(c.get("r")))}
        if any(rec.values()):
            out.append(rec)
    return out


def _f(x) -> float:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return float("nan")


def load_fundamentals() -> dict[str, dict]:
    """{CODE: {de, equity, debt}} from the Debt-to-Equity sheet."""
    fund: dict[str, dict] = {}
    for path in glob.glob("Debt to Equity Ratio.xlsx"):
        for r in _read_xlsx(path):
            code = (r.get("Code") or "").strip().upper()
            if code:
                fund[code] = {"de": _f(r.get("Ratio")), "equity": _f(r.get("Equity")),
                              "debt": _f(r.get("Debt"))}
    return fund


# --------------------------------------------------------------------------- #
# Technical metrics (causal)
# --------------------------------------------------------------------------- #
def _clean(bars: list[dict]) -> list[dict]:
    return [b for b in bars if b["close"] == b["close"]]


def _ret(closes: list[float], n: int) -> float:
    return (closes[-1] / closes[-1 - n] - 1) * 100 if len(closes) > n else float("nan")


def compute_metrics(bars: list[dict]) -> dict | None:
    bars = _clean(bars)
    if len(bars) < MIN_DAYS:
        return None
    closes = [b["close"] for b in bars]
    price = closes[-1]
    ma, ma_prev, label, short_hist = sma(closes, TREND_P), sma(closes[:-REBAL], TREND_P), TREND_NAME, False
    if ma is None:
        tp2 = max(10, TREND_P // 2)
        ma, ma_prev, label, short_hist = sma(closes, tp2), sma(closes[:-REBAL], tp2), f"SMA{tp2}", True
    trend_up = bool(ma and ma_prev and price > ma and ma > ma_prev)
    above_pct = (price / ma - 1) * 100 if ma else float("nan")
    turnover20 = sum(b["close"] * b["volume"] for b in bars[-20:]
                     if b["volume"] == b["volume"]) / 20
    a = atr(bars)
    return {"price": price, "ma": ma, "ma_label": label, "trend_up": trend_up,
            "above_pct": above_pct, "short_hist": short_hist,
            "rs1": _ret(closes, RS1), "rs2": _ret(closes, RS2),
            "turnover": turnover20, "atr_pct": (a / price * 100) if a else float("nan")}


# --------------------------------------------------------------------------- #
# Scoring -- transparent additive tally
# --------------------------------------------------------------------------- #
def score_ticker(m: dict, f: dict) -> dict:
    notes: list[str] = []
    flags: list[str] = []
    score = 0

    def add(pts: int, why: str) -> None:
        nonlocal score
        score += pts
        notes.append(f"{pts:+d} {why}")

    de = f.get("de", float("nan"))
    equity = f.get("equity", float("nan"))
    gates = [
        ("liquidity", m["turnover"] >= MIN_TURNOVER, f"turnover {m['turnover']:,.0f}"),
        ("uptrend", m["trend_up"],
         f"price {'>' if m['price'] > m['ma'] else '<='} {m['ma_label']} {m['ma']:.2f} "
         f"& {'rising' if m['trend_up'] else 'flat/falling'}"),
        ("solvent (equity>0)", not (equity == equity and equity <= 0),
         f"equity {equity:,.0f}" if equity == equity else "equity n/a"),
        (f"leverage < {DE_UNSAFE:g}", not (de == de and de >= DE_UNSAFE),
         f"D/E {de:.2f}" if de == de else "D/E n/a"),
    ]
    eligible = all(ok for _, ok, _ in gates)
    blocker = next((n for n, ok, _ in gates if not ok), None)

    rs = m["rs1"]
    if rs == rs:
        if rs > RS_STRONG: add(3, f"{RS1_NAME} +{rs:.0f}% (strong)")
        elif rs > RS_MID:  add(2, f"{RS1_NAME} +{rs:.0f}%")
        elif rs > 0:       add(1, f"{RS1_NAME} +{rs:.0f}%")
        else:              add(-1, f"{RS1_NAME} {rs:.0f}% (lagging)")
    if m["rs2"] == m["rs2"] and m["rs2"] > 0:
        add(1, f"{RS2_NAME} +{m['rs2']:.0f}%")
    ap = m["above_pct"]
    if ap == ap:
        if ap > EXT_HI:  add(-1, f"{ap:.0f}% over {m['ma_label']} (over-extended)")
        elif ap <= EXT_OK: add(1, f"{ap:.0f}% over {m['ma_label']} (healthy)")
    if de == de:
        if de < DE_GOOD:  add(2, f"D/E {de:.2f} (very low)")
        elif de < DE_OK:  add(1, f"D/E {de:.2f} (low)")
        elif de >= 1.5:   add(-1, f"D/E {de:.2f} (high)")
    if m["short_hist"]:
        flags.append(f"short history ({m['ma_label']} trend)")

    return {"eligible": eligible, "blocker": blocker, "score": score,
            "notes": notes, "flags": flags, "gates": gates}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_ranking(rows: list[dict], top: int) -> None:
    picks = sorted((r for r in rows if r["res"]["eligible"]),
                   key=lambda r: (r["res"]["score"],
                                  r["m"]["rs1"] if r["m"]["rs1"] == r["m"]["rs1"] else -1e9),
                   reverse=True)
    rejected = [r for r in rows if not r["res"]["eligible"]]

    print(f"\n{'='*92}\n{HORIZON_NAME} RANKING  --  {len(rows)} screened, "
          f"{len(picks)} eligible to HOLD\n{'='*92}")
    print(f"{'#':>2} {'SYMBOL':<11}{'PRICE':>9}{'>MA%':>7}{RS1_NAME:>7}{RS2_NAME:>7}"
          f"{'D/E':>6}{'SCORE':>6}  FLAGS")
    print("-" * 92)
    for i, r in enumerate(picks[:top] if top else picks, 1):
        m, f = r["m"], r["f"]
        de = f.get("de", float("nan"))
        print(f"{i:>2} {r['sym']:<11}{m['price']:>9g}{m['above_pct']:>6.0f}%"
              f"{m['rs1']:>+6.0f}%{m['rs2']:>+6.0f}%"
              f"{(f'{de:.2f}' if de == de else '-'):>6}{r['res']['score']:>+6}  "
              f"{', '.join(r['res']['flags'])}")
    print("-" * 92)
    if picks:
        print("\nTop pick rationale (auditable score breakdown):")
        for r in picks[:5]:
            print(f"  {r['sym']:<11} {r['res']['score']:+d}: {'; '.join(r['res']['notes'])}")
    if rejected:
        from collections import Counter
        why = Counter(r["res"]["blocker"] for r in rejected)
        print("\nNot eligible (blocking gate): "
              + ", ".join(f"{k} x{v}" for k, v in why.most_common()))
    print(f"\nHOLD RULE  : hold each pick while it stays above its {TREND_NAME}. Sell on a "
          f"decisive close below it (or if leverage deteriorates). Re-check about every "
          f"{REBAL} trading days.")
    print("WARNING    : short horizon = frequent trading = high cost drag. Confirm the "
          "NET backtest beats buy-and-hold before acting. Not investment advice.")


# --------------------------------------------------------------------------- #
# Backtest -- weekly trend/RS rotation (TECHNICAL ONLY -> causal)
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

    lead = max(TREND_P, MIN_DAYS)
    rebals = list(range(lead, len(dates) - 1, REBAL))
    if len(rebals) < 2:
        print(f"\nBacktest ({HORIZON_NAME}): not enough history."); return

    equity, curve, bench, bcurve = 1.0, [1.0], 1.0, [1.0]
    held: set[str] = set()
    turnovers = 0
    for k in range(len(rebals) - 1):
        d0, d1 = dates[rebals[k]], dates[rebals[k + 1]]
        ranked = []
        for s, bars in data.items():
            upto = [b for b in _clean(bars) if b["date"] <= d0]
            m = compute_metrics(upto)
            if m and m["trend_up"] and m["turnover"] >= MIN_TURNOVER and m["rs1"] == m["rs1"]:
                ranked.append((m["rs1"], s))
        ranked.sort(reverse=True)
        newhold = {s for _, s in ranked[:top]}
        turnovers += len(newhold ^ held)
        held = newhold
        rets = [close_asof(s, d1) / close_asof(s, d0) - 1
                for s in held if close_asof(s, d0) and close_asof(s, d1)]
        equity *= (1 + (sum(rets) / len(rets) if rets else 0.0))
        curve.append(equity)
        brets = [close_asof(s, d1) / close_asof(s, d0) - 1
                 for s in data if close_asof(s, d0) and close_asof(s, d1)]
        bench *= (1 + (sum(brets) / len(brets) if brets else 0.0))
        bcurve.append(bench)

    total_cost = turnovers * (COST / 2)
    net_equity = curve[-1] * (1 - total_cost)

    def mdd(c):
        peak, dd = c[0], 0.0
        for v in c:
            peak = max(peak, v); dd = min(dd, v / peak - 1)
        return dd

    print(f"\n{'='*72}\n{HORIZON_NAME} ROTATION BACKTEST (top {top}, rebal ~{REBAL}d)")
    print(f"{'='*72}")
    print(f"  Window            : {dates[rebals[0]]} -> {dates[rebals[-1]]}  "
          f"({len(rebals)-1} rebalances)")
    print(f"  Avg names changed : {turnovers/(len(rebals)-1):.1f}/rebalance")
    print(f"  Strategy return   : {(curve[-1]-1)*100:+.1f}% gross | "
          f"{(net_equity-1)*100:+.1f}% net of ~{total_cost*100:.1f}% cost")
    print(f"  Strategy max DD   : {mdd(curve)*100:.1f}%")
    print(f"  Buy&hold (bench)  : {(bcurve[-1]-1)*100:+.1f}%   max DD {mdd(bcurve)*100:.1f}%")
    print(f"{'='*72}")
    print("  Watch the gross->net gap: frequent short-hold trading pays a lot of cost.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="DSE SHORT-HOLD position picker.")
    ap.add_argument("symbols", nargs="*", help="DSE trading codes")
    ap.add_argument("--from-xlsx", metavar="PATH", help="Read ticker list from an .xlsx")
    ap.add_argument("--col", default="Code", help="Ticker column header (default 'Code')")
    ap.add_argument("--days", type=int, default=1000, help="History window (calendar days)")
    ap.add_argument("--top", type=int, default=10, help="Show/hold top N (default 10)")
    ap.add_argument("--backtest", action="store_true",
                    help="Backtest the weekly rotation vs buy-and-hold")
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
    print(f"[short] Loaded leverage for {len(fund)} codes. Fetching {len(symbols)} tickers...")
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
        if m:
            rows.append({"sym": sym, "m": m, "f": fund.get(sym, {}),
                         "res": score_ticker(m, fund.get(sym, {}))})
    print_ranking(rows, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
