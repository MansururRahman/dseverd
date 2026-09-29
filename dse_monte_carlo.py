"""
DSE Monte Carlo per-setup simulation.
=====================================

Answers ONE question about a setup the swing engine has already sized: given how
this particular stock actually moves, how often does the TARGET get reached
before the STOP?

The deterministic reward/risk ratio in `dse_swing_signal._size_position` is
arithmetic on two chosen levels -- it says nothing about how likely either level
is to be hit first. Two setups with identical RR can have very different odds if
one name is a quiet 1.2%-ATR drifter and the other gaps 4% overnight. This module
supplies the missing number.

HOW
    1. Turn the stock's own history into RELATIVE bar shapes (O/H/L/C vs the
       previous close, volume vs its trailing average).
    2. Draw contiguous BLOCKS of those shapes (circular bootstrap) to build a
       synthetic future -- blocks, not IID draws, because volatility clustering
       and gap-then-drift sequences are exactly what decides whether a stop or a
       target is hit first.
    3. Replay the position through the REAL SELL engine on that synthetic future
       (`dse_swing_signal._run_position`) -- trailing stop, 70% partial at
       target, MACD/RSI/volume signals and all.
    4. Repeat N times and report the distribution.

Resampling the stock's own bars means the simulation inherits DSE's fat tails,
overnight gaps, price-limit behaviour and thin-trading volume patterns without
modelling any of them.

WHAT THIS IS NOT
    Not a gate, not a veto, not a ranking key. It reports alongside the swing
    engine's verdict; the CONDITIONAL BUY / NO TRADE decision stays decided
    purely by that engine's hard gates. Monte Carlo creates no information -- it
    quantifies the consequences of an ASSUMED process. The assumption (that the
    next N bars resemble the trailing history) is unverified on DSE data, which
    is why every figure ships with its provenance.

Usage:
    python dse_monte_carlo.py ACMEPL
    python dse_monte_carlo.py ACMEPL KBPPWBIL LHB --days 730
    python dse_monte_carlo.py --from-xlsx "Debt to Equity Ratio.xlsx" --brief
    python dse_monte_carlo.py ACMEPL --paths 5000 --horizon 15 --block 3

Decision SUPPORT only -- not investment advice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import random
import sys
import time
import zlib

import dse_swing_signal as swing
from dse_technical import fetch_history, read_tickers_xlsx

FETCH_DELAY_SECONDS = 3   # polite pause between successive DSE archive calls
DEEP_DIVE_MAX = 12        # above this many tickers, screen instead of deep-diving
VOL_AVG_WINDOW = 20   # bars of trailing volume the volume ratio is measured against
MIN_EXIT_BARS = 30    # evaluate_exit() returns NO DATA below this many bars

# Simulation defaults. `seed = None` means "derive it from the symbol".
CFG = {
    "paths": 1000,     # simulated futures per setup
    "horizon": 10,     # synthetic bars per future
    "block": 5,        # bootstrap block length (bars)
    "warmup": 250,     # real bars handed to evaluate_exit for indicator warm-up
    "seed": None,
}


def bar_shapes(bars: list[dict]) -> list[tuple]:
    """History -> list of relative (open, high, low, close, volume) shapes.

    Each shape describes bar i as multiples of the PREVIOUS close (so it can be
    replayed from any price level), with volume as a multiple of the trailing
    average volume BEFORE bar i (so it can be replayed at any liquidity level).

    A pair is dropped when the previous close or any of the bar's own O/H/L/C is
    NaN or non-positive -- dividing by a DSE '--' sentinel or a zero close would
    poison every path that sampled it. NaN volume is treated as zero (untraded),
    not as a reason to discard otherwise-valid price data.
    """
    shapes: list[tuple] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1]["close"]
        if prev_close != prev_close or prev_close <= 0:
            continue
        o, h, l, c = (bars[i]["open"], bars[i]["high"],
                      bars[i]["low"], bars[i]["close"])
        if any(v != v or v <= 0 for v in (o, h, l, c)):
            continue

        window = bars[max(0, i - VOL_AVG_WINDOW):i]        # excludes bar i itself
        vols = [b["volume"] for b in window if b["volume"] == b["volume"]]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        v = bars[i]["volume"]
        if v != v:
            v = 0.0
        v_rel = (v / avg_vol) if avg_vol > 0 else 1.0

        shapes.append((o / prev_close, h / prev_close,
                       l / prev_close, c / prev_close, v_rel))
    return shapes


def sample_blocks(shapes: list[tuple], horizon: int, block: int,
                  rng: random.Random) -> list[tuple]:
    """Draw `horizon` shapes as contiguous CIRCULAR blocks of length `block`.

    Blocks rather than independent draws, because IID resampling destroys the
    very structure that decides whether a stop or a target is reached first:
    volatility clustering, gap-then-drift sequences, consecutive limit-locked
    runs. Wrapping around the end of the series (circular) rather than stopping
    short keeps the most recent bars from being under-weighted relative to the
    middle of the sample.
    """
    n = len(shapes)
    out: list[tuple] = []
    while len(out) < horizon:
        start = rng.randrange(n)
        out.extend(shapes[(start + k) % n] for k in range(block))
    return out[:horizon]


def synth_bars(shapes: list[tuple], last_close: float, last_date: dt.date,
               avg_vol: float) -> list[dict]:
    """Replay relative shapes forward from `last_close` into real OHLCV bars.

    High/low are clamped to bracket open/close: the relation holds by
    construction for clean input, but a bad DSE row can carry a high below its
    own close, and an inverted bar would make `atr` and the gap-down check
    nonsense for every path that sampled it.

    Dates advance one CALENDAR day per bar. The simulation reports its horizon in
    BARS, never in days, so no trading-calendar fidelity is needed -- the dates
    exist only because `evaluate_exit` carries one into its snapshot.
    """
    bars: list[dict] = []
    close, date = last_close, last_date
    for o_rel, h_rel, l_rel, c_rel, v_rel in shapes:
        o = close * o_rel
        new_close = close * c_rel
        high = max(close * h_rel, o, new_close)
        low = min(close * l_rel, o, new_close)
        date = date + dt.timedelta(days=1)
        bars.append({"date": date, "open": o, "high": high, "low": low,
                     "close": new_close, "volume": max(0.0, v_rel * avg_vol)})
        close = new_close
    return bars


def default_seed(symbol: str) -> int:
    """Per-symbol seed that is STABLE ACROSS PROCESSES.

    Deliberately not `hash(symbol)`: Python salts string hashing per interpreter
    unless PYTHONHASHSEED is pinned, so a hash-derived seed would make the same
    command print different probabilities on different runs -- unusable when
    every cited figure has to trace back to reproducible script output.
    """
    return zlib.crc32(symbol.encode("utf-8"))


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated quantile of an already-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_vals[int(pos)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def simulate_setup(symbol: str, bars: list[dict], sizing: dict | None,
                   p: dict, cfg: dict | None = None) -> dict:
    """Simulate `paths` futures for one sized setup and report the distribution.

    `sizing` is a `dse_swing_signal._size_position` result (entry/stop/target/
    shares). The entry FILL is each path's first synthetic bar open -- not
    today's close -- because that is the live sequence: levels are computed at
    the close, the order fills next open. A useful consequence is that
    `p_stop_first` genuinely includes overnight gap risk.

    Returns the four outcome probabilities, the net-return distribution, and the
    provenance needed to reproduce the run. On refusal, `note` is set and every
    probability is None -- silence is never used to mean "fine".
    """
    c = dict(CFG)
    if cfg:
        c.update(cfg)
    paths, horizon, block = c["paths"], c["horizon"], c["block"]
    warmup = c["warmup"]
    seed = c["seed"] if c["seed"] is not None else default_seed(symbol)

    out = {"symbol": symbol,
           "p_target_first": None, "p_stop_first": None,
           "p_signal_exit": None, "p_unresolved": None,
           "e_net": None, "p_profit": None,
           "pct5": None, "pct50": None, "pct95": None,
           "paths": paths, "horizon": horizon, "block": block,
           "warmup": warmup, "seed": seed, "n_hist_shapes": 0, "note": None}

    if sizing is None or sizing.get("stop") is None or not sizing.get("shares"):
        out["note"] = "no sized position (missing stop or zero shares)"
        return out

    clean = swing._clean(bars)
    shapes = bar_shapes(clean)
    out["n_hist_shapes"] = len(shapes)
    if len(shapes) < block * 4:
        out["note"] = (f"only {len(shapes)} usable history bars -- need >= {block * 4} "
                       f"for {block}-bar blocks")
        return out

    hist = clean[-warmup:]
    if len(hist) < MIN_EXIT_BARS:
        out["note"] = (f"only {len(hist)} warm-up bars -- evaluate_exit needs "
                       f">= {MIN_EXIT_BARS}")
        return out

    vols = [b["volume"] for b in hist[-VOL_AVG_WINDOW:] if b["volume"] == b["volume"]]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    last_close, last_date = hist[-1]["close"], hist[-1]["date"]
    stop, target = sizing["stop"], sizing["target"]
    cost = p.get("round_trip_cost", swing.DEFAULTS["round_trip_cost"])

    rng = random.Random(seed)
    entry_index = len(hist)          # the first synthetic bar
    n_target = n_stop = n_signal = n_unresolved = 0
    rets: list[float] = []

    for _ in range(paths):
        future = synth_bars(sample_blocks(shapes, horizon, block, rng),
                            last_close, last_date, avg_vol)
        pos = swing._run_position(symbol, hist + future, entry_index,
                                  future[0]["open"], stop, target, cost)
        rets.append(pos["ret"])
        # Priority order: booking the target counts as a win even if the trailing
        # 30% is later stopped out -- the target WAS reached first.
        if pos["target_booked"]:
            n_target += 1
        elif pos["full_exit"]:
            n_stop += 1
        elif pos["resolved"]:
            n_signal += 1            # closed by EXIT / EXIT 50% signals
        else:
            n_unresolved += 1        # horizon expired still holding

    ordered = sorted(rets)
    out.update({
        "p_target_first": n_target / paths,
        "p_stop_first": n_stop / paths,
        "p_signal_exit": n_signal / paths,
        "p_unresolved": n_unresolved / paths,
        "e_net": sum(rets) / len(rets),
        "p_profit": sum(1 for r in rets if r > 0) / len(rets),
        "pct5": _quantile(ordered, 0.05),
        "pct50": _quantile(ordered, 0.50),
        "pct95": _quantile(ordered, 0.95),
    })
    return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def format_provenance(sim: dict) -> str:
    """The mandatory audit trail. This is what separates a MODEL ESTIMATE from a
    fabricated number, so it is never optional detail."""
    return (f"{sim['paths']} paths, {sim['horizon']}-bar horizon, "
            f"{sim['block']}-bar blocks, {sim['warmup']} warm-up bars, "
            f"{sim['n_hist_shapes']} history bars sampled, seed {sim['seed']}")


def brief_row(symbol: str, decision: str, price: float | None, sim: dict) -> dict:
    """Condense one simulated setup into a screening row.

    Ranking puts the swing engine's CONDITIONAL BUYs above everything else, and
    only sorts by simulated odds WITHIN a decision. That ordering is deliberate:
    the hard gates decide whether a setup is tradeable, so a NO TRADE with
    flattering simulated odds must never be presented above an approved one.
    """
    if sim["note"] is not None:
        return {"symbol": symbol, "rank": -1.0, "decision": decision,
                "price": price, "note": sim["note"], "sim": sim}
    rank = (1000.0 if decision == "CONDITIONAL BUY" else 0.0) + sim["p_target_first"]
    return {"symbol": symbol, "rank": rank, "decision": decision,
            "price": price, "note": None, "sim": sim}


def print_report(symbol: str, decision: str, reason: str | None,
                 sizing: dict | None, sim: dict) -> None:
    """Per-stock deep-dive: the engine's verdict, then the simulated odds."""
    print(f"\n{'=' * 72}\n{symbol}  --  MONTE CARLO SETUP SIMULATION\n{'=' * 72}")
    print(f"  Swing engine verdict: {decision}"
          + (f"   ({reason})" if reason else ""))

    if sizing and sizing.get("stop") is not None:
        rr = f"{sizing['rr']:.2f}" if sizing.get("rr") is not None else "-"
        print(f"  Levels: entry {sizing['entry']:g}   stop {sizing['stop']:.2f}   "
              f"target {sizing['target']:.2f}   RR {rr}   shares {sizing['shares']:,}")

    if sim["note"] is not None:
        print(f"\n  NOT SIMULATED: {sim['note']}")
        print("\n  NOTE: informational simulation only -- not investment advice.")
        return

    print("\n  SIMULATED OUTCOMES (share of paths):")
    print(f"    target first   {sim['p_target_first']:6.1%}"
          f"      stop first     {sim['p_stop_first']:6.1%}")
    print(f"    signal exit    {sim['p_signal_exit']:6.1%}"
          f"      unresolved     {sim['p_unresolved']:6.1%}")
    print("\n  SIMULATED NET RETURN (after "
          f"{swing.DEFAULTS['round_trip_cost']:.1%} round-trip cost):")
    print(f"    expected {sim['e_net']:+.2%}    profitable {sim['p_profit']:6.1%}")
    print(f"    p5 {sim['pct5']:+.2%}   median {sim['pct50']:+.2%}   "
          f"p95 {sim['pct95']:+.2%}")
    print(f"\n    ({format_provenance(sim)})")

    print("\n  NOTE: these are MODEL ESTIMATES from resampling this stock's own")
    print("        history -- not observed outcomes, and NOT a gate. The BUY/NO")
    print("        TRADE decision above comes from the swing engine's hard gates")
    print("        alone. Decision support only -- not investment advice.")


def print_brief_table(rows: list[dict]) -> None:
    """Ranked one-line-per-stock screen."""
    ranked = sorted((r for r in rows if r["rank"] >= 0),
                    key=lambda r: r["rank"], reverse=True)
    skipped = [r for r in rows if r["rank"] < 0]

    print(f"\n{'SYMBOL':<12}{'DECISION':<16}{'PRICE':>9}{'TGT1st':>8}{'STP1st':>8}"
          f"{'SIGEX':>7}{'UNRES':>7}{'E[NET]':>9}{'PROFIT':>8}{'P5':>8}{'P95':>8}")
    print("-" * 102)
    for r in ranked:
        s = r["sim"]
        print(f"{r['symbol']:<12}{r['decision']:<16}{r['price']:>9g}"
              f"{s['p_target_first']:>8.1%}{s['p_stop_first']:>8.1%}"
              f"{s['p_signal_exit']:>7.1%}{s['p_unresolved']:>7.1%}"
              f"{s['e_net']:>+9.2%}{s['p_profit']:>8.1%}"
              f"{s['pct5']:>+8.2%}{s['pct95']:>+8.2%}")
    for r in skipped:
        print(f"{r['symbol']:<12}{r['decision']:<16}{'-':>9}  {r['note']}")
    print("-" * 102)

    buys = sum(1 for r in ranked if r["decision"] == "CONDITIONAL BUY")
    print(f"{len(ranked)} simulated, {buys} CONDITIONAL BUY, {len(skipped)} not simulated.")
    if ranked:
        print(f"({format_provenance(ranked[0]['sim'])}; seed is per-symbol)")
    print("CONDITIONAL BUYs rank first -- the hard gates decide, the simulation")
    print("only informs. MODEL ESTIMATES, not observed outcomes. Not advice.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Monte Carlo simulation of DSE swing setups "
                    "(single ticker, a list, or a spreadsheet column).")
    ap.add_argument("symbols", nargs="*",
                    help="DSE trading codes, e.g. ACMEPL KBPPWBIL")
    ap.add_argument("--from-xlsx", metavar="PATH",
                    help="Read the ticker list from an .xlsx (e.g. 'Debt to Equity Ratio.xlsx')")
    ap.add_argument("--col", default="Code",
                    help="Header of the ticker column in --from-xlsx (default 'Code')")
    ap.add_argument("--days", type=int, default=730,
                    help="History window in calendar days (default 730)")
    ap.add_argument("--brief", action="store_true",
                    help=f"Force the ranked one-line screen (automatic above "
                         f"{DEEP_DIVE_MAX} tickers or with --from-xlsx)")
    ap.add_argument("--paths", type=int, default=CFG["paths"],
                    help=f"Simulated futures per setup (default {CFG['paths']})")
    ap.add_argument("--horizon", type=int, default=CFG["horizon"],
                    help=f"Synthetic bars per future (default {CFG['horizon']})")
    ap.add_argument("--block", type=int, default=CFG["block"],
                    help=f"Bootstrap block length in bars (default {CFG['block']})")
    ap.add_argument("--warmup", type=int, default=CFG["warmup"],
                    help=f"Real bars of indicator warm-up (default {CFG['warmup']})")
    ap.add_argument("--seed", type=int, default=None,
                    help="Override the per-symbol seed (for reproducibility checks)")
    ap.add_argument("--capital", type=float, default=swing.DEFAULTS["capital"],
                    help="Capital in BDT (default 2,000,000) -- affects share count")
    ap.add_argument("--risk", type=float, default=swing.DEFAULTS["risk_pct"],
                    help="Risk %% per trade (default 1.0)")
    ap.add_argument("--score-gate", type=int, default=swing.DEFAULTS["score_gate"],
                    help="Minimum structural score for the swing verdict (default 2)")
    args = ap.parse_args(argv)

    for name, val in (("--paths", args.paths), ("--horizon", args.horizon),
                      ("--block", args.block)):
        if val < 1:
            ap.error(f"{name} must be >= 1")
    if args.warmup < MIN_EXIT_BARS:
        ap.error(f"--warmup must be >= {MIN_EXIT_BARS} (evaluate_exit needs it)")

    symbols = [s.upper() for s in args.symbols]
    if args.from_xlsx:
        try:
            from_file = read_tickers_xlsx(args.from_xlsx, args.col)
        except Exception as exc:  # noqa: BLE001 - surface file/parse failures clearly
            print(f"failed to read tickers from {args.from_xlsx} ({exc})", file=sys.stderr)
            return 2
        if not from_file:
            print(f"no tickers found in {args.from_xlsx} (column '{args.col}')",
                  file=sys.stderr)
            return 2
        print(f"Loaded {len(from_file)} tickers from {args.from_xlsx} "
              f"(column '{args.col}').")
        symbols = symbols + [s for s in from_file if s not in symbols]

    if not symbols:
        ap.error("no tickers given -- pass trading codes and/or --from-xlsx PATH")

    brief = args.brief or bool(args.from_xlsx) or len(symbols) > DEEP_DIVE_MAX

    p = dict(swing.DEFAULTS)
    p["capital"] = args.capital
    p["risk_pct"] = args.risk
    p["score_gate"] = args.score_gate
    c = {"paths": args.paths, "horizon": args.horizon, "block": args.block,
         "warmup": args.warmup, "seed": args.seed}

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    print(f"Simulating {len(symbols)} ticker(s), {args.paths} paths x "
          f"{args.horizon} bars each (~{FETCH_DELAY_SECONDS}s/ticker to fetch).")

    rows: list[dict] = []
    for i, sym in enumerate(symbols):
        if i > 0:
            time.sleep(FETCH_DELAY_SECONDS)
        try:
            bars = fetch_history(sym, start, end)
        except Exception as exc:  # noqa: BLE001
            msg = f"fetch failed ({exc})"
            if brief:
                rows.append({"symbol": sym, "rank": -1.0, "decision": "NO DATA",
                             "price": None, "note": msg, "sim": None})
            else:
                print(f"\n{sym}: {msg}")
            continue
        if not bars:
            msg = "no data returned"
            if brief:
                rows.append({"symbol": sym, "rank": -1.0, "decision": "NO DATA",
                             "price": None, "note": msg, "sim": None})
            else:
                print(f"\n{sym}: {msg}")
            continue

        res = swing.evaluate_entry(sym, bars, p)
        sizing = res.get("sizing")
        sim = simulate_setup(sym, bars, sizing, p, c)
        price = res.get("snapshot", {}).get("price") if res.get("snapshot") else None

        if brief:
            rows.append(brief_row(sym, res["decision"], price, sim))
        else:
            print_report(sym, res["decision"], res.get("reason"), sizing, sim)

    if brief:
        print_brief_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
