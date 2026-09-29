"""
DSE intraday shortlister: live LTP + day-end archive -> swing screen -> backtest.
=================================================================================

Same four-stage funnel as dse_shortlist.py, but the "today" bar comes from the
LIVE all-ticker price page instead of waiting for the day-end archive to publish.

WHY THIS EXISTS
---------------
`day_end_archive.php` only holds COMPLETED sessions. During trading hours it ends
at yesterday, so dse_shortlist.py run at (say) 13:35 Asia/Dhaka screens yesterday's
close and produces byte-identical output to a run at 10:00. This script fetches
`latest_share_price_scroll_l.php` first -- one request returns every listed ticker
-- and appends that live bar on top of each name's archive history, so the gates
decide on TODAY's price.

    bars used by the gates = day_end_archive (.. -> yesterday) + live bar (today)

WHICH STAGES SEE THE LIVE BAR
-----------------------------
    STAGE 1  swing screen    LIVE  -- the whole point: today's price, today's gates
    STAGE 2  dual backtest   EOD   -- a half-formed bar must not enter a trade
                                      simulation; backtests stay on closed bars
    STAGE 3  uptrend gate    LIVE  -- a "today" structural read, same as stage 1
    STAGE 4  dse_claude      EOD   -- unchanged from dse_shortlist.py

Stage 2 and stage 4 therefore reuse the untouched archive list, while stages 1 and
3 use a separate combined list. They are distinct list objects on purpose.

THE SYNTHESIZED OPEN
--------------------
The live page publishes LTP*/HIGH/LOW/CLOSEP*/YCP*/VOLUME but NO opening price, so
`dse_technical._parse_live_table` correctly reports `open` as NaN. Two checks in
dse_swing_signal read `open`:

    * the PULLBACK test          `last["close"] > last["open"]`   (line ~159)
    * the bullish-candle bonus   `_is_bullish_candle`             (line ~168)

With NaN both go False, so a genuine pullback misclassifies as setup NONE and then
gets tested against the PARTICIPATION volume rule (vol >= avg) instead of the DRY
PULLBACK rule (vol <= avg) -- the opposite test. To avoid that, this script sets

    live bar open := previous session's close

so `close > open` reads as "trading above yesterday's close", which is exactly what
the live page's own CHANGE column expresses. The synthesis is LOCAL to this file:
`dse_technical.merge_live_bar` is not used and not modified, so dse_technical.py,
dse_claude.py and dse_shortlist.py keep their existing NaN behaviour (dse_claude
relies on that NaN to skip its bullish-bar test).

KNOWN LIMITS (documented, not fixed)
------------------------------------
  * One snapshot is taken before the loop; with ~3s/ticker the last name is
    screened minutes after it. Consistent across tickers, slightly stale at the tail.
  * Today's HIGH/LOW/VOLUME are PARTIAL, so ATR%, the 60d-resistance check and every
    volume gate read an unfinished session.
  * Stage-1 sizing comes off a moving LTP -- entry/stop/target shift until the close.
  * Once the archive publishes today, the live session is no longer newer than the
    last archive bar, so nothing is merged and the run silently uses official closes.

Usage:
    python dse_shortlist_intraday.py ACMEPL KBPPWBIL --days 730
    python dse_shortlist_intraday.py --from-xlsx "Debt to Equity Ratio.xlsx" --days 730
    python dse_shortlist_intraday.py --from-xlsx "Debt to Equity Ratio.xlsx" --no-live

Decision SUPPORT only -- not investment advice. Intraday figures are PROVISIONAL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

import dse_claude
import dse_gate_strategy as gate
import dse_swing_signal as swing
import dse_uptrend as uptrend
from dse_technical import fetch_history, fetch_live_snapshot, read_tickers_xlsx

FETCH_DELAY_SECONDS = 3  # polite pause between successive DSE archive calls

# --------------------------------------------------------------------------- #
# DSE trading session (Asia/Dhaka = UTC+6, no DST) -- used to prorate volume.
# --------------------------------------------------------------------------- #
DHAKA_TZ = dt.timezone(dt.timedelta(hours=6))
SESSION_OPEN = dt.time(10, 0)
SESSION_CLOSE = dt.time(14, 20)
# Below this much of the session elapsed, refuse the merge rather than project.
#
# The projection divides by elapsed TIME, so it is only as good as the assumption that
# volume accrues uniformly -- and DSE's does not: the open and close are heavier than
# the middle, so volume-done always runs ahead of time-elapsed and the projection
# OVERSTATES. The multiplier is what sets the size of that error: 1/frac.
#
#     11:00  frac 0.23  ->  4.33x   ~50% overstatement under plausible front-loading
#     12:10  frac 0.50  ->  2.00x   the cap this threshold enforces
#     13:35  frac 0.83  ->  1.21x   residual error only a few percent
#
# 0.5 keeps the multiplier at or below 2x. Before ~12:10 the run declines to guess and
# screens yesterday's close instead -- the same "refuse rather than fabricate" stance
# the zero-bar and pre-open guards take.
MIN_PRORATE_FRACTION = 0.5   # 130 of 260 minutes, i.e. from about 12:10


def session_elapsed_fraction(now: dt.datetime | None = None) -> float:
    """Fraction of the DSE session elapsed, clamped to [0.0, 1.0].

    0.0 before the 10:00 open, 1.0 at or after the 14:20 close. `now` may be naive
    (assumed Dhaka) or aware (converted); defaults to the current Dhaka time.
    """
    now = now or dt.datetime.now(DHAKA_TZ)
    now = now.replace(tzinfo=DHAKA_TZ) if now.tzinfo is None else now.astimezone(DHAKA_TZ)
    open_dt = now.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute,
                          second=0, microsecond=0)
    close_dt = now.replace(hour=SESSION_CLOSE.hour, minute=SESSION_CLOSE.minute,
                           second=0, microsecond=0)
    total = (close_dt - open_dt).total_seconds()
    return max(0.0, min(1.0, (now - open_dt).total_seconds() / total))


# --------------------------------------------------------------------------- #
# Live bar merge (local -- deliberately NOT dse_technical.merge_live_bar)
# --------------------------------------------------------------------------- #
def merge_live_bar_synth(bars: list[dict], live_row: dict | None,
                         session_date: dt.date | None,
                         now: dt.datetime | None = None,
                         prorate_volume: bool = True) -> tuple[list[dict], bool]:
    """Return (combined_bars, merged) with today's live bar appended.

    Never mutates `bars` -- stages 2 and 4 keep the pristine archive list. The
    appended bar's `open` is SYNTHESIZED as the previous session's close, and its
    `volume` is PROJECTED to a full day (see below); `high`, `low`, `close` and
    `trades` are exactly as published on the live page.

    VOLUME PRORATION (prorate_volume=True)
    --------------------------------------
    The live page reports volume session-TO-DATE, but every volume gate compares it
    against a 20-day average of COMPLETED sessions. Left raw, that mismatch biases
    the gates in opposite directions depending on setup:

        BREAKOUT  needs vol >= 1.5x avg  -> false FAIL  (volume hasn't accrued yet)
        PULLBACK  needs vol <= avg       -> false PASS  (the dangerous one: the
                                            earlier you run, the "drier" it looks)
        NONE      needs vol >= avg       -> false FAIL

    So today's volume is divided by the fraction of the session elapsed, projecting
    it to a full-day estimate that IS comparable with the 20-day average. Scaling
    the bar's `volume` field covers every consumer at once -- the turnover gate, the
    20-day average, all three setup volume gates, and evaluate_exit's dry-up check --
    with no change to the engines, which stay pure and clock-free.

    This assumes volume accrues UNIFORMLY through the session. DSE's does not: the
    open and the close are heavier than the middle, so volume-done runs ahead of
    time-elapsed and the projection OVERSTATES. That inverts rather than removes the
    bias -- raw partial volume makes PULLBACKs falsely pass, an early projection makes
    them falsely fail while BREAKOUTs pass too easily. The overstatement shrinks as the
    session progresses (a few percent by 13:35), which is what MIN_PRORATE_FRACTION
    exists to enforce. It is a model estimate, not an observation, which is why the bar
    carries `volume_projected`, `volume_raw` and `session_fraction` for the caller to
    surface. Pass prorate_volume=False to compare raw partial volume instead.

    Properly removing the assumption needs a time-of-day volume baseline ("volume by
    11:00 today vs the median volume by 11:00 over the last 20 sessions"), which needs
    stored intraday snapshots. That belongs to the SaaS ingest layer, not here.

    Refuses -- returning the input list and False -- when the merge would be wrong
    or the row unusable:
      * no archive history to append to, or no live row / session date
      * the live session is not strictly newer than the last archive bar
        (archive already published today, or a stale stamp on a holiday)
      * close / high / low / volume is missing, NaN, or <= 0
      * less than MIN_PRORATE_FRACTION of the session has elapsed (before ~12:10),
        where the projection's multiplier is large enough that the uniformity
        assumption dominates the answer. Only applies when prorate_volume is True --
        the refusal exists to protect the projection, so --raw-volume runs are
        unaffected and still merge from the open.

    The zero rule must reject ZERO as well as NaN. A ticker that has not traded yet
    today comes back as HIGH=LOW=VOLUME=0 with a carried-over LTP, which would
    otherwise merge as the impossible bar {open: 3303, high: 0, low: 0, close: 3322,
    volume: 0} -- high below close -- silently corrupting ATR and every volume gate.
    A one-price session (high == low == close, volume > 0) is legitimate and is
    still accepted.
    """
    if not bars or live_row is None or session_date is None:
        return bars, False
    if session_date <= bars[-1]["date"]:
        return bars, False
    fields = (live_row.get("close"), live_row.get("high"),
              live_row.get("low"), live_row.get("volume"))
    # v != v -> NaN; v <= 0 -> untraded/unpublished. Both mean "not a session".
    if any(v is None or v != v or v <= 0 for v in fields):
        return bars, False

    raw_volume = live_row["volume"]
    volume = raw_volume
    frac = 1.0
    if prorate_volume:
        frac = session_elapsed_fraction(now)
        if frac < MIN_PRORATE_FRACTION:
            return bars, False      # too early for the projection to mean anything
        volume = raw_volume / frac  # frac >= MIN_PRORATE_FRACTION, so never /0

    combined = list(bars)
    combined.append({
        "date": session_date,
        "open": bars[-1]["close"],   # SYNTHESIZED: prior close stands in for open
        "high": live_row["high"],
        "low": live_row["low"],
        "close": live_row["close"],
        "volume": volume,            # PROJECTED to a full day when prorating
        "trades": live_row.get("trades", float("nan")),
        # Provenance for the caller/UI -- these are model estimates, label them.
        "volume_raw": raw_volume,
        "volume_projected": bool(prorate_volume and frac < 1.0),
        "session_fraction": frac,
    })
    return combined, True


def _expectancy(trades: list[dict]) -> tuple:
    """Return (expectancy, n_trades, win_rate) from a list of trade dicts."""
    rets = [t["ret"] for t in trades]
    if not rets:
        return None, 0, 0.0
    win_rate = sum(1 for r in rets if r > 0) / len(rets)
    return sum(rets) / len(rets), len(rets), win_rate


def run_stage4(all_fetched: list[dict]) -> None:
    """STAGE 4: dse_claude pullback signal across EVERY fetched ticker (EOD bars).

    Reads the archive-only list on purpose -- dse_claude has its own live handling
    and this stage is deliberately left as it is in dse_shortlist.py.
    """
    print(f"\n{'=' * 84}\nSTAGE 4 -- DSE_CLAUDE PULLBACK SIGNAL ON ALL "
          f"{len(all_fetched)} FETCHED TICKER(S)  [EOD bars]\n{'=' * 84}")
    cc_results = [dse_claude.evaluate(it["symbol"], it["bars"], live=False)
                  for it in all_fetched]
    dse_claude.print_table(cc_results)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="DSE intraday shortlister: live LTP + archive -> swing screen "
                    "-> dual backtest -> uptrend gate.")
    ap.add_argument("symbols", nargs="*", help="DSE trading codes, e.g. ACMEPL KBPPWBIL")
    ap.add_argument("--from-xlsx", metavar="PATH",
                    help="Read the ticker list from an .xlsx (e.g. 'Debt to Equity Ratio.xlsx')")
    ap.add_argument("--col", default="Code",
                    help="Header of the ticker column in --from-xlsx (default 'Code')")
    ap.add_argument("--days", type=int, default=730,
                    help="History window in calendar days (default 730; SMA200 needs a long window)")
    ap.add_argument("--capital", type=float, default=swing.DEFAULTS["capital"],
                    help="Capital in BDT (default 2,000,000)")
    ap.add_argument("--risk", type=float, default=swing.DEFAULTS["risk_pct"],
                    help="Risk %% per trade (default 1.0)")
    ap.add_argument("--score-gate", type=int, default=swing.DEFAULTS["score_gate"],
                    help="Minimum structural score to allow entry (default 2)")
    ap.add_argument("--min-turnover", type=float, default=None, dest="min_turnover",
                    help="Stage-3 liquidity guard: min avg 20-day volume (shares). Omit to disable.")
    ap.add_argument("--index-symbol", default=None,
                    help="Stage-3 market guard: fetch this index (e.g. DS30) and require it > its "
                         "50-day MA. Omit to disable.")
    ap.add_argument("--no-live", action="store_true",
                    help="Skip the live intraday snapshot; behave like dse_shortlist.py "
                         "(day-end archive only)")
    ap.add_argument("--raw-volume", action="store_true",
                    help="Do NOT project today's partial volume to a full day. Compares "
                         "session-to-date volume against a 20-day average of complete "
                         "sessions, which biases pullbacks to a false PASS.")
    args = ap.parse_args(argv)

    # Swing params (stage 1 + swing backtest) and gate cfg (gate backtest).
    p = dict(swing.DEFAULTS)
    p["capital"] = args.capital
    p["risk_pct"] = args.risk
    p["score_gate"] = args.score_gate
    cfg = dict(gate.DEFAULT_CFG)

    # Assemble the watchlist: CLI codes first, then the xlsx list.
    symbols = [s.upper() for s in args.symbols]
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
        symbols = symbols + [s for s in from_file if s not in symbols]

    if not symbols:
        ap.error("no tickers given -- pass trading codes and/or --from-xlsx PATH")

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    # --------------------------------------------------------------------- #
    # LIVE SNAPSHOT: one request up front returns every listed ticker, so all
    # names share a single consistent timestamp and there is no per-ticker cost.
    # A failure here is non-fatal -- fall back to archive-only.
    # --------------------------------------------------------------------- #
    live_snapshot: dict[str, dict] = {}
    session_date: dt.date | None = None
    if args.no_live:
        print("Live snapshot: DISABLED (--no-live) -- day-end archive only.")
    else:
        try:
            live_snapshot, session_date = fetch_live_snapshot()
            print(f"Live snapshot: {len(live_snapshot)} tickers as of {session_date}.")
        except Exception as exc:  # noqa: BLE001
            print(f"live snapshot unavailable ({exc}); using archive only", file=sys.stderr)

    # --------------------------------------------------------------------- #
    # STAGE 1: swing screen on archive + live bar. Fetch each ticker once
    # (~3s polite delay between); cache BOTH bar lists for the later stages.
    # --------------------------------------------------------------------- #
    print(f"\n{'=' * 84}\nSTAGE 1 -- SWING SCREEN ({len(symbols)} tickers, "
          f"~{FETCH_DELAY_SECONDS}s/ticker)  [LIVE bars where available]\n{'=' * 84}")
    print(f"{'SYMBOL':<12}{'SRC':<6}{'DECISION':<16}{'SETUP':<10}{'PRICE':>9}{'RSI':>6}"
          f"{'GATES':>8}{'RR':>7}  BLOCKER")
    print("-" * 84)

    passed: list[dict] = []       # survivors: {symbol, bars, live_bars, live, res}
    all_fetched: list[dict] = []  # every ticker with data: {symbol, bars} (stage 4)
    n_screened = n_skipped = n_live = 0
    for i, sym in enumerate(symbols):
        if i > 0:
            time.sleep(FETCH_DELAY_SECONDS)
        try:
            bars = fetch_history(sym, start, end)
        except Exception as exc:  # noqa: BLE001
            print(f"{sym:<12}{'-':<6}{'NO DATA':<16}fetch failed ({exc})")
            n_skipped += 1
            continue
        if not bars:
            print(f"{sym:<12}{'-':<6}{'NO DATA':<16}no data returned")
            n_skipped += 1
            continue

        # bars      = archive only  -> stages 2 and 4
        # live_bars = archive + today -> stages 1 and 3
        live_bars, merged = merge_live_bar_synth(bars, live_snapshot.get(sym), session_date,
                                                 prorate_volume=not args.raw_volume)
        if merged:
            n_live += 1
        src = "LIVE" if merged else "EOD"

        all_fetched.append({"symbol": sym, "bars": bars})  # cache for stage 4
        res = swing.evaluate_entry(sym, live_bars, p)
        n_screened += 1
        if res["decision"] == "NO DATA":
            print(f"{sym:<12}{src:<6}{'NO DATA':<16}{res['reason']}")
            n_skipped += 1
            continue

        s, z = res["snapshot"], res["sizing"]
        gates_passed = sum(1 for g in res["gates"] if g["ok"])
        gates_total = len(res["gates"])
        rr = f"{z['rr']:.2f}" if z and z["rr"] is not None else "-"
        rsi_s = f"{s['rsi']:.0f}" if s["rsi"] is not None else "-"
        blocker = "" if res["decision"] == "CONDITIONAL BUY" else (res["reason"] or "")
        print(f"{sym:<12}{src:<6}{res['decision']:<16}{res['setup']:<10}{s['price']:>9g}"
              f"{rsi_s:>6}{str(gates_passed) + '/' + str(gates_total):>8}{rr:>7}  {blocker}")

        if res["decision"] == "CONDITIONAL BUY":
            passed.append({"symbol": sym, "bars": bars, "live_bars": live_bars,
                           "live": merged, "res": res})

    print("-" * 84)
    print(f"{n_screened} screened, {len(passed)} CONDITIONAL BUY, {n_skipped} skipped, "
          f"{n_live} on a LIVE bar.")
    # An all-EOD run mid-morning is the early-session refusal, not a bug. Say so.
    if not n_live and not args.no_live and session_date == end:
        frac_now = session_elapsed_fraction()
        if 0.0 < frac_now < MIN_PRORATE_FRACTION:
            print(f"  NOTE: {frac_now:.0%} of the session elapsed, below the "
                  f"{MIN_PRORATE_FRACTION:.0%} needed to project volume")
            print(f"  (x{1 / frac_now:.1f} uplift would let the uniformity assumption "
                  "dominate). Screening yesterday's close")
            print("  instead. Re-run after ~12:10 Asia/Dhaka, or pass --raw-volume to "
                  "use partial volume as-is.")
        elif frac_now <= 0.0:
            print("  NOTE: session has not opened (or has no volume yet) -- screening "
                  "the last completed session.")
    if n_live:
        frac = session_elapsed_fraction()
        print(f"  LIVE = archive history + today's ({session_date}) live bar. Its OPEN is "
              f"SYNTHESIZED as the prior close.")
        if args.raw_volume:
            print(f"  VOLUME is RAW session-to-date ({frac:.0%} of the session elapsed) "
                  "compared against a 20-day average of")
            print("  COMPLETE sessions -- this biases PULLBACK setups toward a false PASS. "
                  "Drop --raw-volume to project it.")
        else:
            print(f"  VOLUME is PROJECTED to a full day: session-to-date / {frac:.2f} "
                  f"elapsed = x{1 / frac:.2f} uplift, so it is")
            print("  comparable with the 20-day average of complete sessions. That assumes "
                  "volume accrues uniformly;")
            print("  DSE's open and close are heavier, so the projection tends to run HIGH. "
                  "A MODEL ESTIMATE, not an")
            print("  observation -- volume-dependent gates carry more uncertainty than the "
                  "price/trend gates.")
        print("  HIGH/LOW are also partial -- prices, gates and sizing are PROVISIONAL "
              "until the close.")

    if not passed:
        print("\nNo CONDITIONAL BUYs -- nothing to backtest in stages 2-3. "
              "(Swing gates on the latest bar; a low-volume session alone can block a name.)")
        run_stage4(all_fetched)  # stage 4 reads the whole watchlist regardless
        return 0

    # --------------------------------------------------------------------- #
    # STAGE 2: dual backtest on the ARCHIVE bars (no re-fetch, no live bar).
    # A partially-formed session must never enter a trade simulation: its close
    # is a moving LTP and its open is synthesized, both of which would feed
    # run_backtest's entry fill and _full_exit_fill.
    # --------------------------------------------------------------------- #
    print(f"\n{'=' * 84}\nSTAGE 2 -- DUAL BACKTEST OF THE {len(passed)} SURVIVOR(S)  "
          f"[EOD bars]\n{'=' * 84}")
    print(f"{'SYMBOL':<12}{'SWING_EXP':>10}{'SW_WR':>7}{'SW_N':>6}"
          f"{'GATE_EXP':>10}{'GT_WR':>7}{'GT_N':>6}   VERDICT")
    print("-" * 84)

    robust: list[str] = []
    for item in passed:
        sym, bars = item["symbol"], item["bars"]

        sw = swing.run_backtest(sym, bars, p)
        sw_trades = sw.get("trades", [])
        sw_exp, sw_n, sw_wr = _expectancy(sw_trades)

        gt_trades, _ = gate.backtest(bars, cfg)
        gt_exp, gt_n, gt_wr = _expectancy(gt_trades)

        both_pos = (sw_exp is not None and sw_exp > 0 and gt_exp is not None and gt_exp > 0)
        one_pos = (sw_exp is not None and sw_exp > 0) or (gt_exp is not None and gt_exp > 0)
        verdict = "ROBUST (both +)" if both_pos else ("MIXED (one +)" if one_pos else "WEAK (neither +)")
        if both_pos:
            robust.append(sym)

        sw_exp_s = f"{sw_exp:+.2%}" if sw_exp is not None else "n/a"
        gt_exp_s = f"{gt_exp:+.2%}" if gt_exp is not None else "n/a"
        print(f"{sym:<12}{sw_exp_s:>10}{sw_wr:>6.0%}{sw_n:>6}"
              f"{gt_exp_s:>10}{gt_wr:>6.0%}{gt_n:>6}   {verdict}")

    print("-" * 84)
    if robust:
        print(f"ROBUST (positive expectancy in BOTH backtests): {', '.join(robust)}")
        print("  -> these are the CONDITIONAL BUYs that also backtested positive "
              "under two independent rulesets.")
    else:
        print("No survivor was positive in both backtests -- the setups pass the "
              "gates but lack a validated historical edge on these names.")

    # --------------------------------------------------------------------- #
    # STAGE 3: uptrend gate on the stage-2 ROBUST names, using the LIVE bars --
    # like stage 1 this is a "today" structural read (MA/ADX/RSI/MACD).
    # --------------------------------------------------------------------- #
    print(f"\n{'=' * 84}\nSTAGE 3 -- UPTREND GATE ON THE {len(robust)} ROBUST NAME(S)  "
          f"[LIVE bars where available]\n{'=' * 84}")
    if not robust:
        print("No ROBUST names from stage 2 -- nothing to run through the uptrend gate.")
    else:
        # Optional market guard: fetch the index once (same for every ticker).
        # Archive-only: the live page carries no index rows.
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

        bars_by_sym = {item["symbol"]: item["live_bars"] for item in passed}
        stage3_rows: list[dict] = []
        confirmed: list[str] = []
        for sym in robust:
            res = uptrend.evaluate_uptrend(sym, bars_by_sym[sym],
                                           index_bars=index_bars,
                                           min_avg_vol=args.min_turnover)
            stage3_rows.append(uptrend.brief_row(res))
            if res["decision"].startswith("TRADE"):
                confirmed.append(sym)
        uptrend.print_brief_table(stage3_rows)

        if confirmed:
            print(f"\nCONFIRMED (stage 1 + stage 2 + stage 3): {', '.join(confirmed)}")
            print("  -> CONDITIONAL BUYs that backtested positive under two "
                  "rulesets AND currently pass the full uptrend gate.")
        else:
            print("\nNo ROBUST name currently passes the uptrend gate -- the backtested "
                  "edge is there but the trend structure isn't confirmed.")

    # STAGE 4: dse_claude pullback signal across EVERY fetched ticker (archive
    # bars, no re-fetch) -- a whole-watchlist read, independent of stages 1-3.
    run_stage4(all_fetched)

    print("\nNOTE: stage-2 backtests use AUTOMATED gates only (manual confirmations "
          "assumed) -- an optimistic upper bound. Decision support, not advice.")
    if n_live:
        print("NOTE: stages 1 and 3 ran on a PROVISIONAL intraday bar. Re-run after the "
              "close to confirm on official prices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
