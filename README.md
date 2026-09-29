# dseverd

Screening, scoring and backtesting tools for stocks on the Dhaka Stock Exchange (DSE).

Each tool is **decision support, not investment advice**. It prints a verdict and the numbers behind it, and the trading decision stays with you. Every gate prints PASS/FAIL with the values it was decided on. When data is missing or unreliable, the tools skip or refuse rather than guess.

The project has two parts:

- **Command-line scripts** (`dse_*.py`). These use only the Python standard library, so there is nothing to install.
- **A web UI** (`web/`). This is a FastAPI app that runs the same engines in the browser.

## Quick start

```
python dse_swing_signal.py ACMEPL
```

This fetches about two years of daily bars for `ACMEPL` from the DSE day-end archive, runs the swing BUY checklist, and prints each gate's result and a position size.

Run scripts from the repository root. They import each other as sibling modules, and some look for `Debt to Equity Ratio.xlsx` by a relative path.

## Data source

- **History** is scraped from `old.dsebd.org/day_end_archive.php`, with one request per ticker. The free archive holds only about two years of history, which limits every backtest.
- **Live prices** come from `latest_share_price_scroll_l.php`, a single request that covers all tickers. During trading hours (10:00–14:20 Asia/Dhaka), today's partial bar is appended to the archive history. You can turn this off with `--no-live`.
- The scripts wait 3 seconds between tickers to be polite to the server, so a full watchlist run takes several minutes.

## Command-line scripts

All scripts accept the same basic arguments:

```
python dse_<name>.py ACMEPL KBPPWBIL                          # one or more tickers
python dse_<name>.py --from-xlsx "Debt to Equity Ratio.xlsx"  # tickers from a spreadsheet column
python dse_<name>.py --from-xlsx list.xlsx --col Symbol       # column other than "Code"
python dse_<name>.py ACMEPL --days 730                        # history window in calendar days
python dse_<name>.py ACMEPL --no-live                         # archive bars only
```

Run any script with `--help` to see all of its options. The docstring at the top of each file explains its rules in detail.

| Script | What it does | Useful flags |
|---|---|---|
| `dse_technical.py` | Shared data and indicator layer (SMA, EMA, RSI, MACD, ATR). Run it on its own for a plain-language technical read. | `--csv`, `--brief` |
| `dse_swing_signal.py` | Rule-based swing BUY checklist with risk-based position sizing. With `--exit`, it runs the SELL checklist for a position you already hold. | `--backtest`, `--capital`, `--risk`, `--exit --entry --stop --target` |
| `dse_claude.py` | Pullback-in-an-uptrend setups with a structural stop and target. Lists only setups with a reward-to-risk ratio of at least 2.0. | `--min-rr`, `--csv`, `--verbose` |
| `dse_uptrend.py` | Strict all-or-nothing uptrend gate (ADX, OBV, slopes, higher highs and lows) giving a TRADE or NO TRADE verdict. | `--min-turnover`, `--json`, `--brief` |
| `dse_gate_strategy.py` | Screener and backtester that fires only when every enabled gate passes (SMA trend and crossover, MACD, RSI, volume, ATR). | `--tp`, `--stop-mult`, `--cost`, `--no-<gate>`, `--trades` |
| `dse_monte_carlo.py` | Resamples the stock's own price history to estimate how often a sized swing setup hits its target before its stop. It only reports; it never blocks a trade. | `--paths`, `--horizon`, `--seed` |
| `dse_shortlist.py` | Four-stage funnel that fetches each ticker once: swing screen, then a swing and gate backtest, then the uptrend gate, then `dse_claude`. | `--min-turnover`, `--raw-volume` |
| `dse_shorthold.py` | Ranks tickers for a hold of days to about two weeks, on trend, 1-month relative strength and leverage. | `--top`, `--backtest` |
| `dse_mediumhold.py` | Same as above for a hold of about one to three months (SMA50, 3-month relative strength). | `--top`, `--backtest` |
| `dse_longhold.py` | Ranks tickers for a "trade rarely, hold longer" book on long-term trend, relative strength and low leverage. | `--top`, `--backtest` |

### Examples

```
# Screen a watchlist and shortlist the candidates
python dse_shortlist.py --from-xlsx "Debt to Equity Ratio.xlsx"

# Check whether to sell a position you already hold
python dse_swing_signal.py ACMEPL --exit --entry 50 --stop 49 --target 51.5

# Backtest the swing rules on one ticker
python dse_swing_signal.py ACMEPL --backtest

# Pick the top 5 long-hold names from a watchlist
python dse_longhold.py --from-xlsx "Debt to Equity Ratio.xlsx" --top 5 --backtest
```

### A note on results

Backtests with these tools found that short-term swing signals showed no edge on DSE. The roughly 0.5% round-trip trading cost turned them into losses, and plain buy-and-hold did better. The hold pickers exist because of this. Before trusting a strategy, compare its **net** backtest return with buy-and-hold.

## Fundamentals file

The hold pickers read leverage from `Debt to Equity Ratio.xlsx`. It needs the columns `Code`, `Debt`, `Equity` and `Ratio`. Leverage is a current snapshot, so it is used only for today's pick and never inside a backtest.

## Web UI

The web UI runs the shortlist funnel, swing entry and exit, `dse_claude`, the uptrend gate, the technical read and the gate strategy from a browser page.

```
python -m pip install -r web/requirements.txt
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000>.

- The shortlist runs as a background job, and the page polls it for progress. Only one job runs at a time.
- Fetched bars are cached per ticker for the current Dhaka trading day, for up to 30 minutes.
- The JSON API is under `/api/` (for example `POST /api/swing/entry`, `POST /api/shortlist`, `GET /api/jobs/{job_id}`). The routes are defined in `web/app.py`.

## Tests

The tests run offline against recorded price data in `tests/fixtures/bars/`.

```
python -m pip install -r web/requirements.txt   # includes pytest
python -m pytest
```

`tests/test_shortlist_equivalence.py` checks that the `dse_shortlist.py` command-line output matches a frozen copy of the script from before the web refactor (`tests/legacy_shortlist.py`). Don't edit the frozen copy unless you change the shortlist output on purpose.

To re-record the fixtures from the live site (this needs network access):

```
python -m tests.record_fixtures
```

## Other files

- Files ending in `_strict`, `_no_false_positive`, `_nolive`, `_intraday` or `_Bak` are older snapshots kept for reference. Nothing imports them. Change the file without the suffix.
- `gate_strategy.py` is an older prototype that uses pandas, numpy and yfinance. It is not part of the DSE pipeline.
- `downloadimage.py` downloads chart images from amarstock.com into a `Chart/` folder, which you need to create first. It needs `requests`.
