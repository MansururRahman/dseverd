"""
DSE three-stage shortlister: swing screen -> dual backtest -> uptrend gate.
==========================================================================

A single command that runs the whole funnel we discussed:

  STAGE 1  Run the dse_swing_signal BUY flowchart against a watchlist (CLI codes
           and/or an .xlsx) and keep the names that come back CONDITIONAL BUY --
           i.e. every automated gate passed *today*, with sizing.

  STAGE 2  For each survivor, validate the NAME (not just today's setup) with two
           independent backtests on the SAME history already fetched:
             * dse_swing_signal --backtest  (your actual strategy; scaling exits)
             * dse_gate_strategy            (independent mechanical trend regime)
           A name with positive expectancy in BOTH is robust across methods, not
           a curve-fit of one ruleset.

  STAGE 3  Run the stage-2 ROBUST names through the dse_uptrend TRADE/NO-TRADE
           gate (again on the cached bars). This is the strictest filter -- every
           mandatory MA / ADX / RSI / MACD gate must pass -- so a name that comes
           out TRADE (UPTREND) here has a validated historical edge AND a
           confirmed trend structure today.

  STAGE 4  Independently of stages 1-3, run the dse_claude pullback-to-support
           swing mechanic across EVERY fetched ticker (not just the survivors) and
           print its uptrend / RR / entry / exit / stop table. This is a parallel
           read, not a filter of any earlier stage: it answers "across the whole
           watchlist, which names are clean pullback-to-support setups right now,
           and at what levels?" It runs even when stage 1 produced no survivors.

Each ticker is fetched from the DSE archive exactly ONCE (3s polite delay between
fetches); stages 2, 3 and 4 reuse the cached bars, so there is no second round of
per-ticker network calls. Standard library only -- it just orchestrates the
existing modules.

Usage:
    python dse_shortlist.py ACMEPL KBPPWBIL --days 730
    python dse_shortlist.py --from-xlsx "Debt to Equity Ratio.xlsx" --days 730
    python dse_shortlist.py --from-xlsx "Debt to Equity Ratio.xlsx" --col Code --capital 2000000
    python dse_shortlist.py ACMEPL KBPPWBIL --index-symbol DS30 --min-turnover 100000

Decision SUPPORT only -- not investment advice. Stage-2 backtests are AUTOMATED
gates only (manual confirmations assumed), so they are an optimistic upper bound.
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
from dse_technical import fetch_history, read_tickers_xlsx

FETCH_DELAY_SECONDS = 3  # polite pause between successive DSE archive calls


def _expectancy(trades: list[dict]) -> tuple:
    """Return (expectancy, n_trades, win_rate) from a list of trade dicts."""
    rets = [t["ret"] for t in trades]
    if not rets:
        return None, 0, 0.0
    win_rate = sum(1 for r in rets if r > 0) / len(rets)
    return sum(rets) / len(rets), len(rets), win_rate


def run_stage4(all_fetched: list[dict]) -> None:
    """STAGE 4: dse_claude pullback-to-support signal across EVERY fetched ticker.

    Operates on the bars already cached in stage 1 (no re-fetch) and prints the
    dse_claude uptrend / RR / entry / exit / stop table. Independent of the
    stage 1-3 filtering -- it reads the whole watchlist, not just the survivors.
    """
    print(f"\n{'=' * 78}\nSTAGE 4 -- DSE_CLAUDE PULLBACK SIGNAL ON ALL "
          f"{len(all_fetched)} FETCHED TICKER(S)\n{'=' * 78}")
    cc_results = [dse_claude.evaluate(it["symbol"], it["bars"], live=False)
                  for it in all_fetched]
    dse_claude.print_table(cc_results)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="DSE two-stage shortlister: swing screen -> dual backtest.")
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
    # STAGE 1: swing screen. Fetch each ticker once (~3s polite delay between);
    # cache bars for stage 2.
    # --------------------------------------------------------------------- #
    print(f"\n{'=' * 78}\nSTAGE 1 -- SWING SCREEN ({len(symbols)} tickers, "
          f"~{FETCH_DELAY_SECONDS}s/ticker)\n{'=' * 78}")
    print(f"{'SYMBOL':<12}{'DECISION':<16}{'SETUP':<10}{'PRICE':>9}{'RSI':>6}"
          f"{'GATES':>8}{'RR':>7}  BLOCKER")
    print("-" * 78)

    passed: list[dict] = []       # survivors: {symbol, bars, res}
    all_fetched: list[dict] = []  # every ticker with data: {symbol, bars} (stage 4)
    n_screened = n_skipped = 0
    for i, sym in enumerate(symbols):
        if i > 0:
            time.sleep(FETCH_DELAY_SECONDS)
        try:
            bars = fetch_history(sym, start, end)
        except Exception as exc:  # noqa: BLE001
            print(f"{sym:<12}{'NO DATA':<16}fetch failed ({exc})")
            n_skipped += 1
            continue
        if not bars:
            print(f"{sym:<12}{'NO DATA':<16}no data returned")
            n_skipped += 1
            continue

        all_fetched.append({"symbol": sym, "bars": bars})  # cache for stage 4
        res = swing.evaluate_entry(sym, bars, p)
        n_screened += 1
        if res["decision"] == "NO DATA":
            print(f"{sym:<12}{'NO DATA':<16}{res['reason']}")
            n_skipped += 1
            continue

        s, z = res["snapshot"], res["sizing"]
        gates_passed = sum(1 for g in res["gates"] if g["ok"])
        gates_total = len(res["gates"])
        rr = f"{z['rr']:.2f}" if z and z["rr"] is not None else "-"
        rsi_s = f"{s['rsi']:.0f}" if s["rsi"] is not None else "-"
        blocker = "" if res["decision"] == "CONDITIONAL BUY" else (res["reason"] or "")
        print(f"{sym:<12}{res['decision']:<16}{res['setup']:<10}{s['price']:>9g}"
              f"{rsi_s:>6}{str(gates_passed) + '/' + str(gates_total):>8}{rr:>7}  {blocker}")

        if res["decision"] == "CONDITIONAL BUY":
            passed.append({"symbol": sym, "bars": bars, "res": res})

    print("-" * 78)
    print(f"{n_screened} screened, {len(passed)} CONDITIONAL BUY, {n_skipped} skipped.")

    if not passed:
        print("\nNo CONDITIONAL BUYs today -- nothing to backtest in stages 2-3. "
              "(Swing gates on TODAY's bar; a low-volume day alone can block a name.)")
        run_stage4(all_fetched)  # stage 4 reads the whole watchlist regardless
        return 0

    # --------------------------------------------------------------------- #
    # STAGE 2: dual backtest on the cached bars (no re-fetch).
    # --------------------------------------------------------------------- #
    print(f"\n{'=' * 78}\nSTAGE 2 -- DUAL BACKTEST OF THE {len(passed)} SURVIVOR(S)\n{'=' * 78}")
    print(f"{'SYMBOL':<12}{'SWING_EXP':>10}{'SW_WR':>7}{'SW_N':>6}"
          f"{'GATE_EXP':>10}{'GT_WR':>7}{'GT_N':>6}   VERDICT")
    print("-" * 78)

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

    print("-" * 78)
    if robust:
        print(f"ROBUST (positive expectancy in BOTH backtests): {', '.join(robust)}")
        print("  -> these are today's CONDITIONAL BUYs that also backtested positive "
              "under two independent rulesets.")
    else:
        print("No survivor was positive in both backtests -- today's setups pass the "
              "gates but lack a validated historical edge on these names.")

    # --------------------------------------------------------------------- #
    # STAGE 3: uptrend gate on the stage-2 ROBUST names (cached bars, no
    # re-fetch). Strictest filter -- every mandatory MA/ADX/RSI/MACD gate must
    # pass -- so a TRADE (UPTREND) here means validated edge + confirmed trend.
    # --------------------------------------------------------------------- #
    print(f"\n{'=' * 78}\nSTAGE 3 -- UPTREND GATE ON THE {len(robust)} ROBUST NAME(S)\n{'=' * 78}")
    if not robust:
        print("No ROBUST names from stage 2 -- nothing to run through the uptrend gate.")
    else:
        # Optional market guard: fetch the index once (same for every ticker).
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

        bars_by_sym = {item["symbol"]: item["bars"] for item in passed}
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
            print("  -> today's CONDITIONAL BUYs that backtested positive under two "
                  "rulesets AND currently pass the full uptrend gate.")
        else:
            print("\nNo ROBUST name currently passes the uptrend gate -- the backtested "
                  "edge is there but today's trend structure isn't confirmed.")

    # STAGE 4: dse_claude pullback signal across EVERY fetched ticker (cached
    # bars, no re-fetch) -- a whole-watchlist read, independent of stages 1-3.
    run_stage4(all_fetched)

    print("\nNOTE: stage-2 backtests use AUTOMATED gates only (manual confirmations "
          "assumed) -- an optimistic upper bound. Decision support, not advice.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
