# DSE Web UI — Design

Date: 2026-09-28 · Status: awaiting review

## Goal

Run the existing DSE shortlist funnel and single-stock tools from a browser instead of the CLI, with structured, readable results. The user runs it alone on this PC for now. Keep internet deployment possible later without restructuring (it would need auth and a persistent job store at that point, both out of scope here).

## Scope

In v1:
- Shortlist funnel (`dse_shortlist.py`, 4 stages).
- Single-stock tools: swing entry and exit (`dse_swing_signal`), `dse_claude`, uptrend (`dse_uptrend`), technical read (`dse_technical`), gate strategy (`dse_gate_strategy`).

Out of v1:
- Hold pickers.
- Monte Carlo.
- Variant scripts (`_strict`, `_Bak`, `_nolive`, `_intraday`, `_no_false_positive`).
- `gate_strategy.py` and `downloadimage.py`.
- Auth.
- Run history kept across server restarts.
- JS test framework.

## Constraints

- The CLI scripts stay standard-library only. FastAPI and uvicorn are dependencies of `web/` alone.
- The CLI's behaviour and printed output don't change.
- One DSE archive fetch at a time, with `FETCH_DELAY_SECONDS` (3s) between tickers. A 68-ticker watchlist run takes about 4 minutes.
- Launched from the repo root, because the engines import each other as sibling modules and resolve `Debt to Equity Ratio.xlsx` by relative path.
- Binds to `127.0.0.1` by default.

## Architecture

```
web/
  __init__.py
  app.py            FastAPI app factory create_app(): API routes + serves static/ at /
  models.py         pydantic request models (defaults = the engines' DEFAULTS)
  services.py       one function per tool: fetch (via cache) -> engine -> to_jsonable()
  serialize.py      to_jsonable()
  jobs.py           in-memory job registry + single worker thread (shortlist only)
  cache.py          bar cache + network throttle
  static/           index.html, app.js, style.css  (no build step)
  requirements.txt  fastapi, uvicorn, httpx (for TestClient), pytest
tests/
  fixtures.py       recorded real bars for 8 watchlist tickers (tests/fixtures/bars/*.json) + fakes
  record_fixtures.py  one-off network script that records those bars
  legacy_shortlist.py frozen verbatim copy of the pre-refactor dse_shortlist.py (test oracle)
  test_shortlist_equivalence.py, test_run_shortlist.py, test_cache.py, test_jobs.py,
  test_serialize.py, test_api.py
dse_shortlist.py    main() split into run_shortlist() + CliPrinter
```

### Refactor of `dse_shortlist.py`

- `run_shortlist(symbols, opts, on_progress=None) -> dict` holds all the fetch and stage logic that `main()` has now.
  - `opts` has the same fields as the argparse namespace.
  - It returns `{live: {enabled, session_date, n_tickers, error}, stage1: [...], stage2: [...], robust: [...], stage3: [...], confirmed: [...], stage4: [...], notes: [str], summary: {...}}`.
  - Every `print` that isn't a table row becomes an entry in `notes` or `summary`, keeping its wording.
- Progress is reported through a single `emit(event, data)` callback. It fires before each ticker (`ticker`) and at each stage boundary (`live`, `stageN_start`, `stageN_row`, `stageN_done`, `done`). An exception raised from `emit`, such as `Cancelled`, aborts the run.
- `CliPrinter` is an `emit` target that reproduces the current stdout and stderr byte for byte as events arrive. That keeps the CLI's streaming behaviour: stage 1 rows still print one ticker at a time during a 4-minute run.
- `main()` becomes: parse args → load tickers → `run_shortlist(symbols, opts, emit=CliPrinter())`.
- `run_shortlist` also accepts `fetch`, `fetch_live` and `delay`, defaulting to the DSE functions and `FETCH_DELAY_SECONDS`. The web job passes the cache's fetchers and `delay=0`, because the cache does the throttling itself.
- Behaviour doesn't change. That includes the rule that stages 2 and 4 use archive-only bars while stages 1 and 3 use the separate live-merged list.

No other engine file is modified. The web layer calls these existing functions:

| Tool | Engine calls |
|---|---|
| Swing entry | `dse_swing_signal.evaluate_entry`, optional `run_backtest` |
| Swing exit | `dse_swing_signal.evaluate_exit(entry, stop, target)` |
| dse_claude | `dse_claude.evaluate(symbol, bars, live, cfg)` |
| Uptrend | `dse_uptrend.evaluate_uptrend(symbol, bars, index_bars, min_avg_vol)` |
| Technical | `dse_technical.brief_row` + `technical_verdict` |
| Gate strategy | `dse_gate_strategy.gate_snapshot` + `backtest`, summary numbers derived the same way as `summarize` |

Each tool handles the live bar the same way its CLI does:
- Only `dse_claude` and `dse_technical` have a `--no-live` flag. Their endpoints take `live` (default true) and merge through `dse_technical.merge_live_bar`.
- Swing entry and exit, uptrend and gate strategy are archive-only in the CLI, so their endpoints take no `live` parameter and always return `live_merged=false`.
- The shortlist uses its own `merge_live_bar_synth`, unchanged.

### Bar cache (`cache.py`)

- **Archive bars:** key `(symbol, days)`, value the bars list plus when it was fetched. Entries expire when the Asia/Dhaka calendar date changes, or 30 minutes after fetching, so bars fetched mid-session pick up the day's close once the archive publishes it.
- **Network throttle:** every archive fetch, whether from a shortlist job or a single-stock request, goes through one lock and waits until at least `FETCH_DELAY_SECONDS` have passed since the previous fetch.
- **Live snapshot:** a single entry, expiring after 5 minutes.
- Live merging happens at request time, on a copy of the cached archive bars. Callers never receive the cached list itself, so the shortlist's archive list and live list stay distinct.
- The shortlist job fills the cache for every ticker it fetches. Clicking a row afterwards costs nothing.

### Jobs (`jobs.py`)

- `Job{id, kind, params, status: queued|running|done|failed|cancelled, progress{stage,i,n,symbol}, partial{stage1..}, result, error, created}`.
- One worker thread and a FIFO queue, so only one shortlist job fetches at a time.
- Cancellation sets a flag. The `on_progress` callback raises `Cancelled` at the next ticker boundary.
- `partial` fills in as each stage completes, so the UI can show stage 1 while stage 2 is still running. For this, `run_shortlist` accepts an optional `on_stage(stage_no, rows)` callback.
- On failure, `error` holds the final line of the traceback, and the full traceback is logged to the console.
- Only the most recent 20 jobs are kept. The most recent shortlist job, whatever its status, is served at `GET /api/jobs/latest`. A page reload restores the result, or resumes polling if the job is still running.

### API

All requests and responses are JSON. Parameters default to each engine's `DEFAULTS` or `CFG`.

| Method | Path | Body / notes |
|---|---|---|
| GET | `/api/watchlist` | `{symbols: [...]}` from `Debt to Equity Ratio.xlsx` (`Code` column) |
| GET | `/api/defaults` | each tool's default parameters, used to prefill the Advanced forms |
| POST | `/api/swing/entry` | `{symbol, days=730, capital, risk, min_rr, score_gate, backtest=false}` |
| POST | `/api/swing/exit` | `{symbol, days=730, entry, stop, target}`. All three are required and must be > 0 |
| POST | `/api/claude` | `{symbol, days=400, live=true, min_rr, max_ext, max_day_gain}` |
| POST | `/api/uptrend` | `{symbol, days=730, min_turnover?, index_symbol?}` |
| POST | `/api/technical` | `{symbol, days=730, live=true}` |
| POST | `/api/gate` | `{symbol, days=730, capital, tp, stop_mult, cost, rsi_min, rsi_max, disabled_gates: []}` |
| POST | `/api/shortlist` | `{symbols?: [...], use_watchlist=false, days=730, live=true, raw_volume=false, capital, risk, score_gate, min_turnover?, index_symbol?}` → `{job_id}` |
| GET | `/api/jobs/{id}` | job record |
| GET | `/api/jobs/latest` | latest shortlist job, or `404` |
| POST | `/api/jobs/{id}/cancel` | `202` |

Every single-stock response contains `{symbol, live_merged: bool, fetched_at, result: <engine dict>}`.

`to_jsonable()` converts NaN and ±inf to `null`, `date`/`datetime` to ISO strings, and tuples to lists, recursively.

## UI

This is a single page, `static/index.html`, with plain JS and no framework.

**Layout:** a left sidebar with two groups, "Shortlist" (the funnel) and "Single stock" (the six tools), and a main panel on the right. Light and dark themes follow `prefers-color-scheme`, and numbers are set in monospace.

**Single-stock view:**
- **Inputs:**
  - A symbol box with a datalist from `/api/watchlist`. Any code is accepted.
  - Days, and a live toggle for tools that support it.
  - A collapsible "Advanced" section with that tool's parameters, prefilled from `/api/defaults`.
- **Result, top to bottom:**
  - A header line: symbol, date, and a `LIVE (provisional)` badge when `live_merged`.
  - A verdict card with the decision chip and key numbers:
    - Swing entry: setup, entry, stop, target, shares, RR.
    - Swing exit: action, P&L %, trail.
    - dse_claude: uptrend grade, RR, entry, exit, stop.
    - Uptrend: decision, confidence, optional score.
    - Technical: verdict, score.
    - Gate strategy: all-pass or the blocking gate, plus the strategy verdict.
  - A gate or ledger table with a PASS/FAIL icon, the name, and the engine's own detail text word for word.
  - Manual confirmations, shown as display-only checkboxes (swing entry).
  - Snapshot key-value pairs (RSI, ATR%, moving averages, and so on).
  - A backtest table (swing entry with backtest ticked, and gate strategy): the trades, plus a summary of trade count, win rate, expectancy and total P&L.

**Shortlist view:**
- **Inputs:** ticker source (watchlist xlsx, or pasted codes separated by spaces or commas), days, live, raw volume, index symbol, minimum turnover, and Advanced swing parameters.
- **Run** starts a job and polls `/api/jobs/{id}` every second. There's a progress bar ("Stage 1 · 23/68 · BATBC") and a Cancel button.
- **Four stage tables** appear as `partial` fills in:
  1. Swing screen: sortable, with decisions as colored chips.
  2. Dual backtest: ROBUST rows highlighted.
  3. Uptrend: CONFIRMED rows highlighted.
  4. dse_claude tradeables.
- A notes panel shows `notes` word for word.
- Clicking a symbol in any table opens Swing entry for that symbol, using the cached bars.
- On page load, `/api/jobs/latest` restores the last result.

**Errors:** fetch and server errors appear in a red box inside the result area. Validation errors highlight the offending fields.

## Error handling

| Case | Behaviour |
|---|---|
| DSE fetch fails (timeout, HTTP error) | Single-stock: `502 {detail: "fetch failed for SYM: <exc>"}`. Shortlist: a `NO DATA` row and the run continues, same as the CLI |
| Engine returns NO DATA / NO TRADE / not tradeable | Normal `200` result, shown as the engine reports it |
| Invalid input | `422` from pydantic validation |
| Live snapshot unavailable | Fall back to archive only, add a note, `live_merged=false` |
| Unhandled exception in a job | Status `failed`, `error` = last traceback line, server keeps running |
| Cancel | Stops at the next ticker boundary, status `cancelled`, partial results kept |
| Watchlist xlsx missing | `/api/watchlist` returns `{symbols: []}` and the UI hides the watchlist option |

## Testing

pytest. No test touches the network: `fetch_history` and `fetch_live_snapshot` are patched with `tests/fixtures.py`.

1. **Equivalence against a frozen copy, set up before the refactor.**
   - Copy the unmodified `dse_shortlist.py` verbatim to `tests/legacy_shortlist.py`.
   - The test runs `legacy_shortlist.main(argv)` and the new `dse_shortlist.main(argv)` under identical patches: fixture fetches, `FETCH_DELAY_SECONDS = 0`, and a fixed `session_elapsed_fraction`.
   - It asserts that stdout, stderr and the return code are identical.
   - This works on any date, unlike saved output files, which would contain today's date.
   - Variants cover:
     - no-live
     - live projected
     - live raw-volume
     - early session
     - live outage
     - "forced survivors", which patch the shared engine functions so stages 2 and 3 run whatever the recorded data is, including index-symbol and min-turnover warnings.
2. **Real check:** a `--no-live` CLI run on 3 real tickers before and after the refactor, compared with a diff. This is manual and needs the network.
3. **Unit tests:**
   - `to_jsonable`: NaN, inf, dates, nested structures.
   - Cache: hit, miss, Dhaka date rollover, live 5-minute expiry, returns a copy.
   - Jobs: queued → running → done, failed, cancelled, FIFO order.
4. **API tests** (`TestClient`, fixture data): the success shape for each endpoint, `502` on a fetch error, `422` on bad input, the shortlist job lifecycle through to `done`, and cancel.
5. **Frontend:** a manual smoke test in a real browser. One single-stock tool on a real ticker, a 3-ticker shortlist, and a row click through to Swing entry.

## Running

```
pip install -r web/requirements.txt
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
# open http://localhost:8000
python -m pytest tests
```
