"""
DSE rule-based SWING / short-term-position signal engine.

Implements the Rule-Based Trading System (2,000,000 BDT capital, ~2.5% net
target per trade, 1%-1.5% capital risk) as a deterministic, auditable checklist:

  * ENTRY mode (default)   -- runs the full BUY flowchart gate-by-gate and, if it
                              passes, sizes the position (shares, stop, target,
                              risk-reward) using risk-based position sizing.
  * EXIT  mode (--exit)    -- runs the SELL flowchart against an already-open
                              position (entry/stop/target you pass in) and prints
                              the recommended action.

It reuses the fetch + indicators from `dse_technical.py` (standard library only,
no third-party deps). Every decision is rule-based and self-documenting: each
gate prints PASS/FAIL with the numbers it was decided on. Gates that CANNOT be
computed from single-stock OHLCV (DSEX market direction, sector strength,
news/event calendar, institutional flow) are NOT faked -- they are surfaced as
explicit MANUAL confirmations the trader must tick before acting.

Usage:
    python dse_swing_signal.py ACMEPL
    python dse_swing_signal.py ACMEPL KBPPWBIL --days 730
    python dse_swing_signal.py ACMEPL --capital 2000000 --risk 1.0
    python dse_swing_signal.py ACMEPL --exit --entry 50 --stop 49 --target 51.5

This is decision SUPPORT, not investment advice. Not fabricated: every figure
traces back to fetched OHLCV. The position decision and its risk remain yours.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
import time

from dse_technical import (atr, fetch_history, macd,
                           read_tickers_xlsx, rsi, sma)

FETCH_DELAY_SECONDS = 3  # polite pause between successive DSE archive calls

# --------------------------------------------------------------------------- #
# Strategy parameters (defaults match the system spec; override on the CLI).
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "capital": 2_000_000.0,   # starting capital (BDT)
    "risk_pct": 1.0,          # capital risked per trade (%)  -> 1.0%-1.5%
    "gross_target_pct": 3.0,  # gross price rise to net ~2.5% after ~0.5% costs
    "max_alloc_pct": 20.0,    # max capital in a single position (%)
    "min_rr": 0.0,            # RR floor; 0 = gate DISABLED (RR still shown for info)
    "min_turnover": 5_000_000.0,   # min avg 20d turnover (BDT) for liquidity
    "rsi_lo": 50.0, "rsi_hi": 72.0,   # RSI entry band (upper raised 68->72 to allow momentum)
    "atr_pct_max": 4.0,       # max ATR% (volatility ceiling)
    "res_headroom_pct": 3.5,  # min room to nearest resistance (breakouts exempt)
    "vol_spike_mult": 1.5,    # breakout-day volume vs 20d avg
    "score_gate": 2,          # min structural score to allow entry
    # Anti-chasing: refuse entries that already ran (buying the local-top spike
    # is the classic next-day-reversion trap on a thin market like DSE).
    "ext_above_sma20_max_pct": 8.0,  # max % price may sit above SMA20
    "day_gain_max_pct": 6.0,         # max single-day % gain on the signal bar
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _clean(bars: list[dict]) -> list[dict]:
    """Drop bars with a NaN close (DSE '--' sentinels)."""
    return [b for b in bars if b["close"] == b["close"]]


def _avg(xs: list[float]) -> float:
    xs = [x for x in xs if x == x]
    return sum(xs) / len(xs) if xs else 0.0


def _is_bullish_candle(bar: dict, prev: dict | None) -> tuple[bool, str]:
    """Detect a bullish confirmation candle (engulfing / marubozu / hammer)."""
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    if any(v != v for v in (o, h, l, c)):
        return False, "no OHLC"
    rng = h - l
    if rng <= 0:
        return False, "flat bar"
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    # Bullish engulfing vs previous red candle.
    if prev is not None and prev["close"] == prev["close"]:
        if prev["close"] < prev["open"] and c > o and c >= prev["open"] and o <= prev["close"]:
            return True, "bullish engulfing"
    # Green marubozu: big green body, small wicks.
    if c > o and body >= 0.7 * rng:
        return True, "green marubozu"
    # Hammer: small body up top, long lower wick, closed green-ish.
    if lower >= 2 * body and upper <= body and c >= o:
        return True, "hammer"
    return False, "no bullish pattern"


# --------------------------------------------------------------------------- #
# BUY flowchart -- gate-by-gate evaluation
# --------------------------------------------------------------------------- #
def evaluate_entry(symbol: str, bars: list[dict], p: dict,
                   market_ok: bool | None = None, market_note: str = "") -> dict:
    """Run the full BUY flowchart. Returns a structured decision dict.

    market_ok: run-level market-regime verdict (True/False) supplied by an
    orchestrator (e.g. dse_shortlist's breadth proxy). None => no market gate
    (standalone single-stock runs are unaffected). run_backtest never passes it,
    so historical backtests stay per-name and look-ahead-free."""
    bars = _clean(bars)
    gates: list[dict] = []          # automated hard gates (all must PASS)
    structure = 0                   # structural score (needs >= score_gate)
    struct_notes: list[str] = []

    def gate(name: str, ok: bool, detail: str) -> None:
        gates.append({"name": name, "ok": bool(ok), "detail": detail})

    if len(bars) < 60:
        return {"symbol": symbol, "decision": "NO DATA",
                "reason": f"only {len(bars)} trading days (need >=60) -- illiquid / bad code",
                "gates": [], "manual": [], "sizing": None}

    closes = [b["close"] for b in bars]
    last, prev = bars[-1], bars[-2]
    price = closes[-1]

    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    atr_val = atr(bars)
    atr_pct = (atr_val / price * 100) if atr_val else float("nan")
    r = rsi(closes)
    m = macd(closes)

    # --- Market regime (run-level; None when running a stock in isolation) ---
    if market_ok is not None:
        gate("Market regime risk-on", bool(market_ok),
             market_note or ("risk-on" if market_ok else "risk-off"))

    # --- Liquidity (turnover approximated as close x volume) ---
    turnover_20 = _avg([b["close"] * b["volume"] for b in bars[-20:]])
    gate("Liquidity (20d avg turnover >= {:,.0f})".format(p["min_turnover"]),
         turnover_20 >= p["min_turnover"], f"turnover ~= {turnover_20:,.0f} BDT")

    # --- 20d average volume (used by the setup-aware volume gate below) ---
    avg_vol = _avg([b["volume"] for b in bars[-20:]])

    # --- Price above key MAs ---
    gate("Price > SMA20 and SMA50",
         bool(s20 and s50 and price > s20 and price > s50),
         f"price {price:g} | SMA20 {s20:.2f} | SMA50 {s50:.2f}" if s20 and s50 else "MAs n/a")

    # --- MA stack (structural score) ---
    if s20 and s50 and s200 and s20 > s50 > s200:
        structure += 2; struct_notes.append("+2 SMA20>SMA50>SMA200 (full stack)")
    elif s20 and s50 and s20 > s50:
        structure += 1; struct_notes.append("+1 SMA20>SMA50")
    else:
        struct_notes.append("+0 no bullish MA stack")

    # --- Relative-strength proxy: stock's own 3-month return positive ---
    ret_3m = (price / closes[-67] - 1) * 100 if len(closes) > 67 else float("nan")
    gate("3-month return > 0 (RS proxy)", ret_3m == ret_3m and ret_3m > 0,
         f"3m return {ret_3m:+.1f}% (true RS vs DSEX = MANUAL)")

    # --- Breakout vs pullback (structural score) ---
    prior_high_20 = max(b["high"] for b in bars[-21:-1])  # excludes today
    if price > prior_high_20:
        structure += 2; struct_notes.append(f"+2 breakout > 20d high {prior_high_20:g}")
        setup = "BREAKOUT"
    elif s20 and price >= s20 and price <= s20 * 1.02 and last["close"] > last["open"]:
        structure += 1; struct_notes.append("+1 pullback bounce off SMA20")
        setup = "PULLBACK"
    else:
        struct_notes.append("+0 no breakout / pullback setup")
        setup = "NONE"

    # --- Candlestick: no longer a hard gate; contributes +1 to the structure
    #     score as confirmation (a bullish candle is confirmation, not edge). ---
    bull, why = _is_bullish_candle(last, prev)
    if bull:
        structure += 1; struct_notes.append(f"+1 bullish candle ({why})")

    # --- Single, setup-aware volume gate (no contradictory double-gating):
    #       BREAKOUT -> spike, today >= 1.5x avg (confirms the break)
    #       PULLBACK -> dry,   today <= avg      (healthy pullbacks dry up)
    #       NONE     -> participation, today >= avg (trend must have life) ---
    if setup == "BREAKOUT":
        gate("Breakout volume >= {}x avg".format(p["vol_spike_mult"]),
             avg_vol > 0 and last["volume"] >= p["vol_spike_mult"] * avg_vol,
             f"today {last['volume']:,.0f} vs {p['vol_spike_mult']}x avg {p['vol_spike_mult']*avg_vol:,.0f}")
    elif setup == "PULLBACK":
        gate("Volume <= 20d avg (dry pullback)", avg_vol > 0 and last["volume"] <= avg_vol,
             f"today {last['volume']:,.0f} vs avg {avg_vol:,.0f}")
    else:  # NONE
        gate("Volume >= 20d avg (participation)", last["volume"] >= avg_vol,
             f"today {last['volume']:,.0f} vs avg {avg_vol:,.0f}")

    # --- RSI band ---
    gate(f"RSI in [{p['rsi_lo']:.0f}, {p['rsi_hi']:.0f}]",
         r is not None and p["rsi_lo"] <= r <= p["rsi_hi"],
         f"RSI(14) = {r:.1f}" if r is not None else "RSI n/a")

    # --- MACD bullish ---
    gate("MACD bullish (line > signal, hist > 0)",
         bool(m and m[0] > m[1] and m[2] > 0),
         f"line {m[0]:+.3f} signal {m[1]:+.3f} hist {m[2]:+.3f}" if m else "MACD n/a")

    # --- ATR volatility ceiling ---
    gate(f"ATR% < {p['atr_pct_max']:.0f}%", atr_pct == atr_pct and atr_pct < p["atr_pct_max"],
         f"ATR(14) {atr_val:.2f} = {atr_pct:.1f}% of price" if atr_val else "ATR n/a")

    # --- Resistance headroom (room to run toward target).
    #     Confirmed breakouts are EXEMPT: a breakout is meant to clear overhead
    #     resistance, so measuring room to the 60d high double-penalises it. ---
    res_60 = max(b["high"] for b in bars[-60:])
    if price >= res_60:                    # already at/above 60d high -> open sky
        headroom = float("inf"); head_detail = "at/above 60d high (open room)"
    else:
        headroom = (res_60 - price) / price * 100
        head_detail = f"{headroom:.1f}% to 60d resistance {res_60:g}"
    if setup == "BREAKOUT":
        gate(f"Resistance headroom >= {p['res_headroom_pct']:.1f}%", True,
             f"breakout exempt ({head_detail})")
    else:
        gate(f"Resistance headroom >= {p['res_headroom_pct']:.1f}%",
             headroom >= p["res_headroom_pct"], head_detail)

    # --- Structural score gate ---
    gate(f"Structure score >= {p['score_gate']}", structure >= p["score_gate"],
         f"score {structure}  [{'; '.join(struct_notes)}]")

    # --- Anti-chasing: don't buy an already-extended bar. Compatible with
    #     BREAKOUT setups -- clearing the 20d high does NOT imply price is far
    #     above SMA20 or up huge today, so a clean breakout still passes. ---
    ext_pct = (price / s20 - 1) * 100 if s20 else float("nan")
    gate(f"Not over-extended (<= {p['ext_above_sma20_max_pct']:.0f}% above SMA20)",
         ext_pct == ext_pct and ext_pct <= p["ext_above_sma20_max_pct"],
         f"{ext_pct:+.1f}% vs SMA20" if ext_pct == ext_pct else "SMA20 n/a")

    pc = prev["close"]
    day_gain = (price / pc - 1) * 100 if pc == pc and pc else float("nan")
    gate(f"No blow-off day (today gain <= {p['day_gain_max_pct']:.0f}%)",
         day_gain == day_gain and day_gain <= p["day_gain_max_pct"],
         f"today {day_gain:+.1f}%" if day_gain == day_gain else "prev close n/a")

    # --- Position sizing. RR is always computed and shown; it only GATES when a
    #     positive floor is set (--min-rr / DEFAULTS['min_rr']). 0 = disabled. ---
    sizing = _size_position(price, atr_val, bars, p)
    if p["min_rr"] > 0:
        gate(f"Risk-Reward >= {p['min_rr']:.1f}",
             sizing["rr"] is not None and sizing["rr"] >= p["min_rr"],
             f"RR = {sizing['rr']:.2f}" if sizing["rr"] is not None else "RR n/a")

    # --- Manual (non-automatable) confirmations ---
    manual = [
        # --- Disabled for now (per user request) -- re-enable by uncommenting: ---
        # "DSEX Index > 50MA AND rising (market uptrend)",   # STEP 3  DSE Index Direction
        # "Sector in DSE top-5 by 20d return",               # STEP 3  Sector Strength
        # "No earnings / dividend / record-date / price-sensitive notice in next 2 sessions",  # STEP 16 News/Event Filter
        "Institutional / block-trade flow supportive",       # STEP 17 Institutional Activity
    ]

    all_pass = all(g["ok"] for g in gates)
    first_fail = next((g["name"] for g in gates if not g["ok"]), None)
    decision = "CONDITIONAL BUY" if all_pass else "NO TRADE"

    return {
        "symbol": symbol, "decision": decision, "setup": setup,
        "reason": None if all_pass else f"failed gate: {first_fail}",
        "gates": gates, "manual": manual, "sizing": sizing,
        "snapshot": {"date": str(last["date"]), "price": price,
                     "rsi": r, "atr_pct": atr_pct, "structure": structure},
    }


def _size_position(price: float, atr_val: float | None, bars: list[dict], p: dict) -> dict:
    """Risk-based position sizing + stop/target/RR (BUY execution block)."""
    if not atr_val:
        return {"entry": price, "stop": None, "target": None, "shares": 0,
                "rr": None, "risk_amount": 0, "allocation": 0, "risk_actual_pct": 0}
    entry = price
    sup_20 = min(b["low"] for b in bars[-20:])          # nearest support
    stop_atr = entry - 1.5 * atr_val
    stop_sup = sup_20 * 0.997                            # just under support
    stop = max(stop_atr, stop_sup)                       # tighter of the two
    # Floor the stop distance at 1x ATR: a "support" only a fraction of an ATR
    # below price is noise, not a level -- honouring it produces an
    # unrealistically tight stop that inflates RR and share count and gets
    # whipsawed out. Never risk less than one ATR of breathing room.
    min_room_stop = entry - atr_val
    stop = min(stop, min_room_stop)
    if stop >= entry:                                    # safety fallback
        stop = stop_atr
    risk_per_share = entry - stop
    target = entry * (1 + p["gross_target_pct"] / 100)
    reward_per_share = target - entry
    rr = reward_per_share / risk_per_share if risk_per_share > 0 else None

    risk_amount = p["capital"] * p["risk_pct"] / 100
    shares = math.floor(risk_amount / risk_per_share) if risk_per_share > 0 else 0
    alloc_cap = p["capital"] * p["max_alloc_pct"] / 100
    if shares * entry > alloc_cap:                       # apply 20% allocation cap
        shares = math.floor(alloc_cap / entry)
    allocation = shares * entry
    risk_actual = shares * risk_per_share
    return {
        "entry": entry, "stop": stop, "target": target, "shares": shares,
        "risk_per_share": risk_per_share, "reward_per_share": reward_per_share,
        "rr": rr, "risk_amount": risk_amount, "allocation": allocation,
        "risk_actual": risk_actual,
        "risk_actual_pct": (risk_actual / p["capital"] * 100) if p["capital"] else 0,
        "support": sup_20,
    }


# --------------------------------------------------------------------------- #
# SELL flowchart -- evaluate an open position
# --------------------------------------------------------------------------- #
def evaluate_exit(symbol: str, bars: list[dict], entry: float, stop: float,
                  target: float) -> dict:
    bars = _clean(bars)
    if len(bars) < 30:
        return {"symbol": symbol, "action": "NO DATA",
                "signals": [f"only {len(bars)} days"], "price": None}
    closes = [b["close"] for b in bars]
    last, prev = bars[-1], bars[-2]
    price = closes[-1]
    s20 = sma(closes, 20)
    s20_prev = sma(closes[:-1], 20)
    r = rsi(closes)
    m = macd(closes)
    avg_vol = _avg([b["volume"] for b in bars[-20:]])
    vol_3d = _avg([b["volume"] for b in bars[-3:]])
    pnl_pct = (price / entry - 1) * 100

    signals: list[dict] = []

    def sig(fire: bool, action: str, why: str) -> None:
        if fire:
            signals.append({"action": action, "why": why})

    # Priority order matches the SELL flowchart.
    sig(price <= stop, "FULL EXIT", f"stop hit: price {price:g} <= stop {stop:g}")
    sig(last["open"] == last["open"] and last["open"] <= prev["close"] * 0.98,
        "FULL EXIT", f"gap down: open {last['open']:g} <= -2% of prev close {prev['close']:g}")
    sig(price >= target, "PARTIAL EXIT 70%", f"target hit: price {price:g} >= target {target:g}")
    sig(bool(s20 and s20_prev and price < s20 and s20 < s20_prev),
        "EXIT 50%", "trend reversal: close < falling SMA20")
    sig(m is not None and m[0] < m[1] and m[2] < 0, "EXIT 50%", "MACD bearish cross")
    sig(r is not None and r > 75, "EXIT 50%", f"RSI {r:.0f} > 75 overbought")
    sig(avg_vol > 0 and vol_3d < 0.5 * avg_vol, "EXIT", "volume dry-up (3d < 0.5x 20d avg)")

    # Trailing-stop suggestion once in profit.
    trail = None
    atr_val = atr(bars)
    if pnl_pct >= 1.5 and atr_val:
        trail = max(stop, price - atr_val, entry)     # never below breakeven

    if any(s["action"] in ("FULL EXIT",) for s in signals):
        action = "FULL EXIT NOW"
    elif any(s["action"] == "PARTIAL EXIT 70%" for s in signals):
        action = "BOOK 70% AT TARGET, TRAIL THE REST"
    elif signals:
        action = signals[0]["action"]
    else:
        action = "HOLD"

    return {"symbol": symbol, "action": action, "price": price, "pnl_pct": pnl_pct,
            "signals": signals, "trail": trail,
            "snapshot": {"date": str(last["date"]), "rsi": r,
                         "macd_hist": m[2] if m else None}}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_entry_report(res: dict) -> None:
    print(f"\n{'=' * 64}\n{res['symbol']}  --  BUY SIGNAL EVALUATION\n{'=' * 64}")
    if res["decision"] == "NO DATA":
        print(f"  {res['reason']}")
        return
    s = res["snapshot"]
    print(f"  As of {s['date']}   Close: {s['price']:g}   "
          f"RSI: {s['rsi']:.1f}   ATR%: {s['atr_pct']:.1f}   "
          f"Setup: {res['setup']}   Structure score: {s['structure']}")

    print("\n  AUTOMATED GATES (all must PASS):")
    for g in res["gates"]:
        mark = "PASS" if g["ok"] else "FAIL"
        print(f"    [{mark}] {g['name']:<42} {g['detail']}")

    z = res["sizing"]
    if z and z["stop"] is not None:
        print("\n  POSITION SIZING / RISK DASHBOARD:")
        print(f"    Entry            : {z['entry']:.2f}")
        print(f"    Stop             : {z['stop']:.2f}  (support {z['support']:.2f})")
        print(f"    Target (+{DEFAULTS['gross_target_pct']:.0f}% gross ~2.5% net): {z['target']:.2f}")
        print(f"    Risk / share     : {z['risk_per_share']:.2f}")
        print(f"    Reward / share   : {z['reward_per_share']:.2f}")
        print(f"    Risk-Reward (RR) : {z['rr']:.2f}" if z["rr"] else "    RR: n/a")
        print(f"    Shares           : {z['shares']:,}")
        print(f"    Allocation       : {z['allocation']:,.0f} BDT")
        print(f"    Capital at risk  : {z['risk_actual']:,.0f} BDT ({z['risk_actual_pct']:.2f}%)")

    print(f"\n  >>> DECISION: {res['decision']}"
          + (f"  ({res['reason']})" if res["reason"] else ""))
    if res["decision"] == "CONDITIONAL BUY":
        print("      Automated gates cleared. CONFIRM these MANUAL gates before ordering:")
        for man in res["manual"]:
            print(f"        [ ] {man}")
    print("\n  NOTE: rule-based decision support only -- not investment advice.")


def brief_row(res: dict) -> dict:
    """Condense an entry evaluation into a one-line screening summary."""
    if res["decision"] == "NO DATA":
        return {"symbol": res["symbol"], "rank": -1, "decision": "NO DATA",
                "note": res["reason"]}
    passed = sum(1 for g in res["gates"] if g["ok"])
    total = len(res["gates"])
    s, z = res["snapshot"], res["sizing"]
    # Rank: actionable buys first, then by gates-passed then structure score.
    actionable = res["decision"] == "CONDITIONAL BUY"
    rank = (1000 if actionable else 0) + passed * 10 + s["structure"]
    return {
        "symbol": res["symbol"], "rank": rank, "decision": res["decision"],
        "setup": res["setup"], "price": s["price"], "rsi": s["rsi"],
        "structure": s["structure"], "passed": passed, "total": total,
        "rr": z["rr"] if z else None, "fail": res["reason"],
    }


def print_brief_table(rows: list[dict]) -> None:
    """Ranked one-line-per-stock screen (actionable buys first)."""
    ranked = sorted((r for r in rows if r["rank"] >= 0),
                    key=lambda r: r["rank"], reverse=True)
    skipped = [r for r in rows if r["rank"] < 0]

    print(f"\n{'SYMBOL':<12}{'DECISION':<16}{'SETUP':<10}{'PRICE':>8}{'RSI':>6}"
          f"{'STRUCT':>7}{'GATES':>8}{'RR':>7}  BLOCKER")
    print("-" * 96)
    for r in ranked:
        rsi_s = f"{r['rsi']:.0f}" if r["rsi"] is not None else "-"
        rr_s = f"{r['rr']:.2f}" if r["rr"] is not None else "-"
        blocker = "" if r["decision"] == "CONDITIONAL BUY" else (r["fail"] or "")
        print(f"{r['symbol']:<12}{r['decision']:<16}{r['setup']:<10}"
              f"{r['price']:>8g}{rsi_s:>6}{r['structure']:>7}"
              f"{str(r['passed'])+'/'+str(r['total']):>8}{rr_s:>7}  {blocker}")
    for r in skipped:
        print(f"{r['symbol']:<12}{'NO DATA':<16}{r['note']}")
    print("-" * 96)
    buys = sum(1 for r in ranked if r["decision"] == "CONDITIONAL BUY")
    print(f"{len(ranked)} screened, {buys} CONDITIONAL BUY, {len(skipped)} skipped.  "
          "Deep-dive the buys without --brief for full sizing.")


def print_exit_report(res: dict) -> None:
    print(f"\n{'=' * 64}\n{res['symbol']}  --  EXIT EVALUATION\n{'=' * 64}")
    if res["action"] == "NO DATA":
        print(f"  {res['signals'][0]}")
        return
    print(f"  As of {res['snapshot']['date']}   Price: {res['price']:g}   "
          f"Unrealized P&L: {res['pnl_pct']:+.2f}%")
    print("\n  SELL SIGNALS FIRING:")
    if res["signals"]:
        for s in res["signals"]:
            print(f"    - {s['action']:<16} {s['why']}")
    else:
        print("    - none")
    if res["trail"] is not None:
        print(f"\n  Trailing stop suggestion (>=1.5% profit): raise stop to {res['trail']:.2f} "
              "(never below breakeven)")
    print(f"\n  >>> ACTION: {res['action']}")
    print("\n  NOTE: rule-based decision support only -- not investment advice.")


# --------------------------------------------------------------------------- #
# BACKTEST -- walk the automated BUY gates forward, manage with the SELL engine
# (added as a pure extension; the live ENTRY/EXIT code above is reused unchanged)
# --------------------------------------------------------------------------- #
def _full_exit_fill(bar: dict, prev_close: float, stop: float) -> float:
    """Fill price for a FULL EXIT: gap-down -> open, stop-hit -> stop, else close."""
    o, c = bar["open"], bar["close"]
    if prev_close == prev_close and o == o and o <= prev_close * 0.98:
        return o
    if c <= stop:
        return stop
    return c


def run_backtest(symbol: str, bars: list[dict], p: dict) -> dict:
    """Re-run evaluate_entry() on every historical window; when it says
    CONDITIONAL BUY, enter at the next bar's open and hand the position to
    evaluate_exit() bar-by-bar (partial/trailing exits) until it closes.

    FIDELITY NOTE: only the AUTOMATED gates are testable historically. The MANUAL
    confirmations (institutional flow, etc.) are assumed to pass, so the numbers
    are an OPTIMISTIC upper bound vs. live trading. Exits are close-based, exactly
    as the live SELL engine decides them."""
    bars = _clean(bars)
    n = len(bars)
    cost = 0.005  # ~0.5% round-trip: the same spread the 3% gross / 2.5% net spec assumes
    if n < 60:
        return {"symbol": symbol, "trades": [], "days": n,
                "note": f"only {n} clean days (need >=60 for the entry gates)"}

    trades: list[dict] = []
    i = 59  # first window with >= 60 bars
    while i < n - 1:
        res = evaluate_entry(symbol, bars[:i + 1], p)
        if res["decision"] != "CONDITIONAL BUY":
            i += 1
            continue
        z = res["sizing"]
        if not z or z.get("stop") is None or z.get("shares", 0) <= 0:
            i += 1
            continue

        entry_idx = i + 1
        entry_fill = bars[entry_idx]["open"]
        if entry_fill != entry_fill:            # NaN open -> fall back to close
            entry_fill = bars[entry_idx]["close"]
        stop, target = z["stop"], z["target"]
        remaining, legs, target_booked = 1.0, [], False

        exit_index = None
        j = entry_idx
        while j < n:
            ex = evaluate_exit(symbol, bars[:j + 1], entry_fill, stop, target)
            if ex["trail"] is not None and ex["trail"] > stop:
                stop = ex["trail"]              # ratchet the trailing stop up
            action = ex["action"]
            close_j = bars[j]["close"]
            prev_close = bars[j - 1]["close"] if j > 0 else close_j

            if action == "FULL EXIT NOW":
                legs.append((remaining, _full_exit_fill(bars[j], prev_close, stop)))
                remaining = 0.0
            elif action == "BOOK 70% AT TARGET, TRAIL THE REST" and not target_booked:
                book = min(0.70, remaining)
                legs.append((book, target))
                remaining -= book
                target_booked = True
                target = float("inf")           # target taken; trail the rest
            elif action == "EXIT 50%":
                sell = min(0.50, remaining)
                legs.append((sell, close_j))
                remaining -= sell
            elif action == "EXIT":
                legs.append((remaining, close_j))
                remaining = 0.0

            if remaining <= 1e-9:
                exit_index = j
                break
            j += 1

        if exit_index is None:                  # data ran out while still holding
            legs.append((remaining, bars[n - 1]["close"]))
            exit_index = n - 1

        gross = sum(f * (px / entry_fill - 1) for f, px in legs if px == px)
        ret = gross - cost
        trades.append({
            "entry_date": bars[entry_idx]["date"], "exit_date": bars[exit_index]["date"],
            "entry": entry_fill, "ret": ret,
            "pnl_bdt": ret * z["shares"] * entry_fill,
            "hold": (bars[exit_index]["date"] - bars[entry_idx]["date"]).days,
            "legs": len(legs), "setup": res.get("setup"),
        })
        i = exit_index + 1                      # one position at a time

    window = f"{bars[0]['date']} -> {bars[-1]['date']}" if bars else "n/a"
    return {"symbol": symbol, "trades": trades, "days": n, "window": window}


def print_backtest_report(bt: dict) -> None:
    print(f"\n{'=' * 64}\n{bt['symbol']}  --  SWING BACKTEST (automated gates)\n{'=' * 64}")
    if bt.get("note"):
        print(f"  {bt['note']}")
        return
    print(f"  {bt['days']} clean trading days  ({bt['window']})")
    trades = bt["trades"]
    if not trades:
        print("  No trades generated (the automated gates never all-passed in-window).")
        print("\n  NOTE: automated gates only -- manual confirmations assumed. Not advice.")
        return

    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    win_rate = len(wins) / len(rets)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = sum(rets) / len(rets)

    equity = 1.0
    curve = [1.0]
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

    total_bdt = sum(t["pnl_bdt"] for t in trades)
    avg_hold = sum(t["hold"] for t in trades) / len(trades)

    print()
    print(f"  Trades:                {len(trades)}")
    print(f"  Win rate:              {win_rate:6.1%}")
    print(f"  Avg win:               {avg_win:+6.2%}")
    print(f"  Avg loss:              {avg_loss:+6.2%}")
    print(f"  Expectancy/trade:      {expectancy:+6.2%}  <-- the number that matters")
    print(f"  Total return (compnd): {curve[-1] - 1:+6.1%}")
    print(f"  Max drawdown:          {max_dd:6.1%}")
    print(f"  Longest losing streak: {mx} trades")
    print(f"  Avg hold:              {avg_hold:.0f} calendar days")
    print(f"  Sum P&L (per-trade 1% risk sizing): {total_bdt:+,.0f} BDT")
    print("\n  NOTE: AUTOMATED gates only -- the MANUAL confirmations (institutional")
    print("        flow, etc.) are assumed to pass, so this is an OPTIMISTIC upper")
    print("        bound. Exits are close-based (matching the live SELL engine).")
    print("        Rule-based decision support only -- not investment advice.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="DSE rule-based swing signal engine.")
    ap.add_argument("symbols", nargs="*", help="DSE trading codes, e.g. ACMEPL KBPPWBIL")
    ap.add_argument("--from-xlsx", metavar="PATH",
                    help="Read the ticker list from an .xlsx (e.g. 'Debt to Equity Ratio.xlsx')")
    ap.add_argument("--col", default="Code",
                    help="Header of the ticker column in --from-xlsx (default 'Code')")
    ap.add_argument("--days", type=int, default=730, help="History window (calendar days, default 730)")
    ap.add_argument("--capital", type=float, default=DEFAULTS["capital"], help="Capital in BDT (default 2,000,000)")
    ap.add_argument("--risk", type=float, default=DEFAULTS["risk_pct"], help="Risk %% per trade (default 1.0)")
    ap.add_argument("--min-rr", type=float, default=DEFAULTS["min_rr"],
                    help="Risk-reward floor gate; 0 disables it (default 0 = off, RR still shown)")
    ap.add_argument("--score-gate", type=int, default=DEFAULTS["score_gate"],
                    help="Minimum structural score to allow entry (default 2)")
    ap.add_argument("--max-ext", type=float, default=DEFAULTS["ext_above_sma20_max_pct"],
                    dest="max_ext",
                    help="Anti-chasing: max %% price may sit above SMA20 (default 8)")
    ap.add_argument("--max-day-gain", type=float, default=DEFAULTS["day_gain_max_pct"],
                    dest="max_day_gain",
                    help="Anti-chasing: max single-day %% gain on the signal bar (default 6)")
    ap.add_argument("--brief", action="store_true",
                    help="One-line ranked screen per stock (use for long watchlists)")
    ap.add_argument("--backtest", action="store_true",
                    help="Backtest the automated BUY gates + SELL engine over history")
    ap.add_argument("--exit", action="store_true", help="Evaluate an OPEN position (SELL flowchart) instead of entry")
    ap.add_argument("--entry", type=float, help="[exit mode] your entry price")
    ap.add_argument("--stop", type=float, help="[exit mode] your stop price")
    ap.add_argument("--target", type=float, help="[exit mode] your target price")
    args = ap.parse_args(argv)

    p = dict(DEFAULTS)
    p["capital"] = args.capital
    p["risk_pct"] = args.risk
    p["min_rr"] = args.min_rr
    p["score_gate"] = args.score_gate
    p["ext_above_sma20_max_pct"] = args.max_ext
    p["day_gain_max_pct"] = args.max_day_gain

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
        print(f"Loaded {len(from_file)} tickers from {args.from_xlsx} "
              f"(column '{args.col}').")
        symbols = symbols + from_file  # CLI symbols first, then file list

    if not symbols:
        print("no tickers given -- pass trading codes and/or --from-xlsx PATH",
              file=sys.stderr)
        return 2

    if args.exit:
        if len(symbols) != 1 or args.entry is None or args.stop is None or args.target is None:
            print("exit mode needs exactly ONE symbol plus --entry --stop --target",
                  file=sys.stderr)
            return 2

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    brief_rows: list[dict] = []
    for i, sym in enumerate(symbols):
        sym = sym.upper()
        if i > 0:
            time.sleep(FETCH_DELAY_SECONDS)  # don't hammer the DSE archive
        try:
            bars = fetch_history(sym, start, end)
        except Exception as exc:  # noqa: BLE001 - surface fetch/parse failures clearly
            print(f"{sym}: failed to fetch data ({exc})", file=sys.stderr)
            if args.brief:
                brief_rows.append({"symbol": sym, "rank": -1, "decision": "NO DATA",
                                   "note": "fetch failed"})
            continue
        if not bars:
            msg = "no data returned (check the trading code / date range)."
            if args.brief:
                brief_rows.append({"symbol": sym, "rank": -1, "decision": "NO DATA",
                                   "note": msg})
            else:
                print(f"\n{sym}: {msg}")
            continue

        if args.backtest:
            print_backtest_report(run_backtest(sym, bars, p))
            continue

        if args.exit:
            print_exit_report(evaluate_exit(sym, bars, args.entry, args.stop, args.target))
        elif args.brief:
            brief_rows.append(brief_row(evaluate_entry(sym, bars, p)))
        else:
            print_entry_report(evaluate_entry(sym, bars, p))

    if args.brief:
        print_brief_table(brief_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
