# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A flat folder of standalone Python CLI scripts that screen, score and backtest Dhaka Stock Exchange (DSE) stocks. Every script is decision support: it prints a verdict and the numbers behind it. There is no build and no linter config, but there is now an offline pytest suite, a local git repo, and a `web/` FastAPI package layered on top of the same scripts. Output goes to stdout. A few scripts can also write CSVs (per-symbol OHLCV, `dse_claude_signals.csv`).

## Running

Every `dse_*.py` script uses the same argparse conventions:

```
python dse_<name>.py ACMEPL KBPPWBIL --days 730           # explicit tickers
python dse_<name>.py --from-xlsx "Debt to Equity Ratio.xlsx" [--col Code]
python dse_<name>.py ACMEPL --brief                        # one-line table (where supported)
python dse_<name>.py ACMEPL --backtest                     # swing / hold pickers
python dse_<name>.py ACMEPL --no-live                      # skip the intraday live bar
python dse_swing_signal.py ACMEPL --exit --entry 50 --stop 49 --target 51.5
```

- Each module's docstring has its own usage block and design rationale. Read it before changing a script.
- Runs hit the network live. They make one archive request per ticker and sleep `FETCH_DELAY_SECONDS` (3s) between tickers, so a full watchlist run takes minutes. To verify a change quickly, run a single ticker.
- Run scripts from this directory. They import each other as sibling modules, and the hold pickers find `Debt to Equity Ratio.xlsx` by a relative `glob`.

## Web UI

`web/` is a FastAPI app over the same engines (shortlist funnel + swing entry/exit, dse_claude, uptrend, technical, gate strategy). Its dependencies live only in `web/requirements.txt`; the CLI scripts stay stdlib-only.

```
python -m pip install -r web/requirements.txt
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000   # from the repo root
python -m pytest                                              # offline; fixtures in tests/fixtures/bars
python -m pytest tests/test_api.py::test_swing_entry          # single test
```

- `dse_shortlist.run_shortlist()` returns the funnel as data and reports progress through `emit(event, data)`. `CliPrinter` is the emit target that prints the CLI report. The web layer runs it as a background job (`web/jobs.py`, one worker thread) that the page polls.
- `tests/legacy_shortlist.py` is a **frozen** copy of the pre-refactor script. `tests/test_shortlist_equivalence.py` asserts the CLI output is byte-identical to it. Never edit the legacy copy. If you change shortlist output on purpose, update the legacy copy in the same change and say so.
- `web/cache.py` caches bars per (symbol, days) for the Dhaka day, with a 30-minute expiry, and throttles all archive fetches to `FETCH_DELAY_SECONDS`. This is why the shortlist job passes `delay=0`.
- `dse_technical.merge_live_bar` mutates its list, so `web/services.py` always merges into a copy.
- Refresh the recorded fixtures with `python -m tests.record_fixtures` (needs the network).

## Dependencies

All `dse_*.py` scripts use only the standard library, and the docstrings state this as a design constraint. Don't add pandas, numpy or requests to them. They read `.xlsx` files with `zipfile` + `ElementTree`, not openpyxl. The exceptions are:
- `gate_strategy.py`: an older pandas/numpy/yfinance prototype, not wired into the DSE pipeline.
- `downloadimage.py`: uses `requests` to download chart GIFs from amarstock.com into a `Chart/` folder, which must already exist.

## Architecture

**`dse_technical.py` is the shared data and indicator layer.** The other scripts import it:
- `fetch_history(symbol, start, end)` scrapes and regex-parses the HTML table at `old.dsebd.org/day_end_archive.php`. It returns ascending bars `{date, open, high, low, close, volume, trades}`, and missing values are `float('nan')`. Callers filter NaN with the idiom `b["close"] == b["close"]` (`_clean`).
- `fetch_live_snapshot()` fetches `latest_share_price_scroll_l.php`, a single request covering all tickers. `merge_live_bar()` appends today's bar only if it is newer than the last archived bar, so it self-heals once the archive publishes. The live page has **no open price**, so the live bar's `open` is NaN. `dse_claude` relies on that NaN. `dse_shortlist*.py` instead sets open to the previous close, locally.
- The pure-Python indicators are `sma`, `ema`/`ema_series`, `rsi`, `macd`, `atr` and `read_tickers_xlsx`.
- The free archive only serves about 2 years of history, which limits every backtest.

**Strategy engines** each consume bar lists and expose an evaluate function plus report printers:
- `dse_swing_signal.py`: the rule-based BUY/SELL flowchart. It provides `evaluate_entry`, `evaluate_exit`, `_size_position` (risk-based sizing from `DEFAULTS`: 2,000,000 BDT capital, 1% risk, ~0.5% round-trip cost), `_run_position` (the SELL engine) and `run_backtest`. Gates it cannot compute, such as market direction, sector and news, are surfaced as MANUAL confirmations, never faked.
- `dse_claude.py`: pullback-in-uptrend setups with a structural stop and target, and RR ≥ 2.0.
- `dse_uptrend.py`: uptrend structure checks (ADX, OBV, slopes, higher highs and lows).
- `dse_gate_strategy.py`: AND-of-gates screener and backtester.
- `dse_monte_carlo.py`: block-bootstrap resampling of the stock's own relative bars, replayed through `dse_swing_signal._run_position`. It reports its results only and never gates a trade.

**`dse_shortlist.py` is the orchestrator.** It runs a four-stage funnel and fetches each ticker once:
1. Swing screen on live-merged bars.
2. Dual backtest (swing + gate) on archive-only bars.
3. Uptrend gate on live-merged bars.
4. `dse_claude` across every fetched ticker on archive-only bars.

Stages 2 and 4 must never see the partial live bar, so the archive list and the live-merged list are deliberately separate objects. Volume on the live bar is prorated by session time elapsed (DSE session: 10:00–14:20, Asia/Dhaka UTC+6), and the merge is refused before about 50% of the session has elapsed.

**Hold pickers** (`dse_shorthold.py`, `dse_mediumhold.py`, `dse_longhold.py`) rank tickers on trend or relative strength plus leverage. They load debt-to-equity from `Debt to Equity Ratio.xlsx` (columns `Code, Debt, Equity, Ratio`) through `load_fundamentals()`. Leverage is a current snapshot, so these scripts use it only for the live pick, never in backtests, to avoid look-ahead. The short and medium scripts are near-copies that differ only in their parameter block.

## Variants and duplicates

Scripts with a `_strict`, `_no_false_positive`, `_nolive`, `_intraday` or `_Bak` suffix are forked snapshots of an earlier version. They are not imported by anything. Edit the unsuffixed file unless the user names a variant. Current state:
- `dse_shortlist.py` started as a copy of `dse_shortlist_intraday.py` and has since been refactored into `run_shortlist()` + `CliPrinter` for the web UI.
- `_intraday` is the pre-refactor snapshot.
- Never copy either file over the other.
- `dse_shortlist_Bak.py` is identical to `dse_shortlist_nolive.py`, the pre-live EOD-only version.

## Conventions in this code

- Backtests must be causal: indicators are computed only from bars up to the decision bar, and a half-formed intraday bar never enters a trade simulation.
- Every gate prints PASS/FAIL with the numbers it was decided on. Keep new logic auditable in the same way.
- When data is missing or unreliable, refuse or skip and say so. Don't extrapolate. This is the "refuse rather than fabricate" stance repeated across the docstrings.
- The docstrings reference `SWING_SIGNAL_REVIEW_2026-07-23.md`, `.claude/commands/swing.md` and a `/dse-de` command, but none of these exist in this folder. That review found that short-term swing signals showed no edge on DSE and lost money to costs, while buy-and-hold did better. This is the motivation behind the hold pickers.
