"""Record real DSE day-end bars for the offline tests. Needs the network (~40 s).

    python -m tests.record_fixtures

Keeps the first N_TICKERS watchlist names that have at least MIN_BARS bars, so
every fixture is long enough for the swing engine (60) and SMA200.
"""
import datetime as dt
import json
import time

from dse_technical import FETCH_DELAY_SECONDS, fetch_history, read_tickers_xlsx
from tests.fixtures import BARS_DIR, ROOT

N_TICKERS = 8
MIN_BARS = 250


def main() -> None:
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    watchlist = read_tickers_xlsx(str(ROOT / "Debt to Equity Ratio.xlsx"))
    end = dt.date.today()
    start = end - dt.timedelta(days=730)
    kept = 0
    for i, sym in enumerate(watchlist):
        if kept == N_TICKERS:
            break
        if i:
            time.sleep(FETCH_DELAY_SECONDS)
        bars = fetch_history(sym, start, end)
        if len(bars) < MIN_BARS:
            print(f"{sym}: {len(bars)} bars -- skipped")
            continue
        rows = [{**b, "date": b["date"].isoformat()} for b in bars]
        (BARS_DIR / f"{sym}.json").write_text(json.dumps(rows), encoding="utf-8")
        kept += 1
        print(f"{sym}: {len(bars)} bars")


if __name__ == "__main__":
    main()
