"""
DSE technical-analysis helper for position decisions.

Pulls daily OHLCV history for DSE-listed stocks from the official Dhaka Stock
Exchange day-end archive (https://old.dsebd.org/day_end_archive.php), computes
standard technical indicators, and prints a plain-language read per stock.

No third-party dependencies -- standard library only.

Usage:
    python dse_technical.py ACMEPL KBPPWBIL
    python dse_technical.py ACMEPL --days 730
    python dse_technical.py ACMEPL --csv        # also dump raw OHLCV to CSV

This is decision SUPPORT, not investment advice. It reports what the price
history shows; the position decision and its risk remain yours.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ARCHIVE_URL = "https://old.dsebd.org/day_end_archive.php"
LIVE_URL = "https://old.dsebd.org/latest_share_price_scroll_l.php"
USER_AGENT = "Mozilla/5.0 (compatible; dse-technical/1.0)"
FETCH_DELAY_SECONDS = 3  # polite pause between successive DSE archive calls
_XL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# --------------------------------------------------------------------------- #
# Read a ticker list from an .xlsx (stdlib only -- xlsx is a zip of XML)
# --------------------------------------------------------------------------- #
def read_tickers_xlsx(path: str, column: str = "Code") -> list[str]:
    """Return trading codes from `column` (matched by header text) of sheet1.

    Falls back to the first column if the named header is not found. Skips the
    header row and blank cells; de-duplicates while preserving order.
    """
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{_XL_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_XL_NS}t")))

    def cell_text(c) -> str:
        v = c.find(f"{_XL_NS}v")
        if v is None or v.text is None:
            return ""
        return shared[int(v.text)] if c.get("t") == "s" else v.text

    def col_letter(ref: str) -> str:
        return "".join(ch for ch in (ref or "") if ch.isalpha())

    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = sheet.find(f"{_XL_NS}sheetData").findall(f"{_XL_NS}row")
    if not rows:
        return []

    # Locate the target column letter from the header row.
    header = {col_letter(c.get("r")): cell_text(c).strip() for c in rows[0]}
    target = next((letter for letter, name in header.items()
                   if name.lower() == column.lower()), None)
    if target is None:
        target = min(header, default="A")  # fall back to first column

    seen, out = set(), []
    for row in rows[1:]:
        for c in row:
            if col_letter(c.get("r")) == target:
                code = cell_text(c).strip().upper()
                if code and code not in seen:
                    seen.add(code)
                    out.append(code)
                break
    return out


# --------------------------------------------------------------------------- #
# Data fetching & parsing
# --------------------------------------------------------------------------- #
def fetch_history(symbol: str, start: dt.date, end: dt.date) -> list[dict]:
    """Return a list of daily bars (ascending by date) for `symbol`."""
    params = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "inst": symbol,
        "archive": "data",
    }
    url = f"{ARCHIVE_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=45) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    return _parse_archive_table(page, symbol)


def _strip_tags(cell: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()


def _parse_archive_table(page: str, symbol: str) -> list[dict]:
    """Find the OHLCV table (header contains DATE + TRADING CODE) and parse it."""
    bars: list[dict] = []
    for table in re.findall(r"<table.*?</table>", page, re.S):
        rows = re.findall(r"<tr[^>]*>.*?</tr>", table, re.S)
        if not rows:
            continue
        header = [_strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[0], re.S)]
        if "DATE" not in header or "TRADING CODE" not in header:
            continue
        col = {name: i for i, name in enumerate(header)}
        for row in rows[1:]:
            cells = [_strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
            if len(cells) < len(header):
                continue
            try:
                bars.append(
                    {
                        "date": dt.date.fromisoformat(cells[col["DATE"]]),
                        "open": _num(cells[col["OPENP*"]]),
                        "high": _num(cells[col["HIGH"]]),
                        "low": _num(cells[col["LOW"]]),
                        "close": _num(cells[col["CLOSEP*"]]),
                        "volume": _num(cells[col["VOLUME"]]),
                        "trades": _num(cells[col["TRADE"]]),
                    }
                )
            except (KeyError, ValueError):
                continue
        break  # found the data table
    bars.sort(key=lambda b: b["date"])
    return bars


def _num(text: str) -> float:
    """Parse a DSE numeric cell ('1,706,087', '13.4', '--')."""
    text = text.replace(",", "").strip()
    if text in ("", "--", "-", "N/A"):
        return float("nan")
    return float(text)


# --------------------------------------------------------------------------- #
# Live intraday snapshot (one request returns every ticker)
# --------------------------------------------------------------------------- #
def fetch_live_snapshot() -> tuple[dict[str, dict], dt.date | None]:
    """Fetch the live all-ticker price page in a single request.

    The day-end archive only holds data up to the last *completed* session, so
    during trading hours today's bar is missing. This page publishes a live
    snapshot for every listed ticker at once -- one HTTP call, no per-ticker
    delay. Returns ({TRADING_CODE: bar_without_date}, session_date), where
    session_date is parsed from the page's 'as-on' stamp (None if unparseable).
    """
    req = urllib.request.Request(LIVE_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=45) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    return _parse_live_table(page), _parse_live_date(page)


def _parse_live_table(page: str) -> dict[str, dict]:
    """Find the live price table (header has TRADING CODE + LTP*) and parse it.

    Mirrors `_parse_archive_table`. `open` is not published intraday (set NaN --
    no indicator here reads it). Close prefers CLOSEP*; when that is blank or 0
    (session not yet closed) it falls back to LTP*, the live last-trade price.
    """
    out: dict[str, dict] = {}
    for table in re.findall(r"<table.*?</table>", page, re.S):
        rows = re.findall(r"<tr[^>]*>.*?</tr>", table, re.S)
        if not rows:
            continue
        header = [_strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[0], re.S)]
        if "TRADING CODE" not in header or "LTP*" not in header:
            continue
        col = {name: i for i, name in enumerate(header)}
        for row in rows[1:]:
            cells = [_strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
            if len(cells) < len(header):
                continue
            code = cells[col["TRADING CODE"]].strip().upper()
            if not code:
                continue
            close = _num(cells[col["CLOSEP*"]])
            if close != close or close == 0:  # not yet closed -> use last trade
                close = _num(cells[col["LTP*"]])
            out[code] = {
                "open": float("nan"),
                "high": _num(cells[col["HIGH"]]),
                "low": _num(cells[col["LOW"]]),
                "close": close,
                "volume": _num(cells[col["VOLUME"]]),
                "trades": _num(cells[col["TRADE"]]),
            }
        break  # found the data table
    return out


def _parse_live_date(page: str) -> dt.date | None:
    """Parse the 'Mon D, YYYY' session stamp from the live page header."""
    m = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(20\d{2})", page)
    if not m:
        return None
    try:
        return dt.datetime.strptime(" ".join(m.groups()), "%b %d %Y").date()
    except ValueError:
        return None


def merge_live_bar(bars: list[dict], live_row: dict | None,
                   session_date: dt.date | None) -> bool:
    """Append today's live bar to `bars` if it is a newer session than the last
    archived bar. Self-healing: once the archive publishes the session its date
    matches the last bar and nothing is appended, so today is never doubled.
    On a holiday the stale live date equals the last bar's date -> no append.
    Returns True iff a live bar was appended.
    """
    if not bars or live_row is None or session_date is None:
        return False
    if session_date <= bars[-1]["date"]:
        return False
    if live_row["close"] != live_row["close"]:  # NaN -> untraded today
        return False
    bars.append({"date": session_date, **live_row})
    return True


# --------------------------------------------------------------------------- #
# Indicators (pure-python)
# --------------------------------------------------------------------------- #
def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return ema_series(values, period)[-1]


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):  # Wilder smoothing
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(closes: list[float], fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_e, slow_e)]
    signal_line = ema_series(macd_line, signal)
    return macd_line[-1], signal_line[-1], macd_line[-1] - signal_line[-1]


def atr(bars: list[dict], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


# --------------------------------------------------------------------------- #
# Verdict: transparent rules-based aggregation of the signals
# --------------------------------------------------------------------------- #
def technical_verdict(bars: list[dict]) -> tuple[str, int, list[str]]:
    """Return (verdict_label, score, reasons). Score is a bounded point tally
    where each technical signal contributes an explicit, auditable amount."""
    closes = [b["close"] for b in bars]
    price = closes[-1]
    score = 0
    reasons: list[str] = []

    def add(points: int, text: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"{points:+d}  {text}")

    # Long-term trend carries the most weight.
    s200 = sma(closes, 200)
    if s200 is not None:
        add(2, "above SMA200 (long-term uptrend)") if price > s200 \
            else add(-2, "below SMA200 (long-term downtrend)")

    # Medium-term trend structure.
    s20, s50 = sma(closes, 20), sma(closes, 50)
    if s20 and s50:
        add(1, "SMA20 > SMA50") if s20 > s50 else add(-1, "SMA20 < SMA50")
    if s20:
        add(1, "price above SMA20") if price > s20 else add(-1, "price below SMA20")

    # Momentum: MACD histogram direction.
    m = macd(closes)
    if m:
        add(1, "MACD histogram positive") if m[2] > 0 else add(-1, "MACD histogram negative")

    # Momentum: RSI extremes (contrarian on extremes; neutral in the band).
    r = rsi(closes)
    if r is not None:
        if r > 72:
            add(-1, f"RSI {r:.0f} overbought")
        elif r < 30:
            add(1, f"RSI {r:.0f} oversold (bounce potential)")

    # Recent drift.
    if len(closes) > 22:
        add(1, "1-month change positive") if price > closes[-23] \
            else add(-1, "1-month change negative")

    # Map the tally to a verdict band.
    if score >= 4:
        label = "FAVORABLE - technicals support taking / adding a long position"
    elif score >= 2:
        label = "LEAN POSITIVE - constructive, wait for confirmation before entering"
    elif score >= -1:
        label = "NEUTRAL / HOLD - no clear edge; wait for a better setup"
    elif score >= -3:
        label = "LEAN NEGATIVE - caution; not a favorable entry"
    else:
        label = "UNFAVORABLE - downtrend; avoid new longs / consider reducing"

    return label, score, reasons


# --------------------------------------------------------------------------- #
# Analysis & reporting
# --------------------------------------------------------------------------- #
def analyze(symbol: str, bars: list[dict], live: bool = False) -> None:
    print(f"\n{'=' * 60}\n{symbol}\n{'=' * 60}")
    bars = [b for b in bars if b["close"] == b["close"]]  # drop NaN closes
    if len(bars) < 20:
        print(f"  Only {len(bars)} trading days found -- too little data for a "
              "reliable read (likely an illiquid/thinly-traded stock).")
        return

    closes = [b["close"] for b in bars]
    last = bars[-1]
    price = last["close"]

    # Liquidity check
    recent_vol = [b["volume"] for b in bars[-20:] if b["volume"] == b["volume"]]
    avg_vol = sum(recent_vol) / len(recent_vol) if recent_vol else 0
    zero_days = sum(1 for b in bars[-20:] if not b["volume"] or b["volume"] != b["volume"])

    live_tag = "  [LIVE/intraday - provisional close]" if live else ""
    print(f"  As of {last['date']}{live_tag}   Close: {price:g}   "
          f"History: {len(bars)} days ({bars[0]['date']} -> {bars[-1]['date']})")
    print(f"  Avg volume (20d): {avg_vol:,.0f}"
          + (f"   [WARNING: {zero_days}/20 recent days had no trades -> illiquid]"
             if zero_days > 3 else ""))

    # Trend: moving averages
    print("\n  TREND")
    for p in (20, 50, 200):
        m = sma(closes, p)
        if m is not None:
            rel = "above" if price >= m else "below"
            print(f"    SMA{p:<3}: {m:8.2f}   price is {rel}")
        else:
            print(f"    SMA{p:<3}: n/a (need {p} days, have {len(closes)})")
    s20, s50 = sma(closes, 20), sma(closes, 50)
    if s20 and s50:
        print(f"    -> {'Bullish' if s20 > s50 else 'Bearish'} "
              f"(SMA20 {'>' if s20 > s50 else '<'} SMA50)")

    # Momentum: RSI + MACD
    print("\n  MOMENTUM")
    r = rsi(closes)
    if r is not None:
        state = ("OVERBOUGHT (>72)" if r > 72 else
                 "OVERSOLD (<30)" if r < 30 else "neutral")
        print(f"    RSI(14): {r:5.1f}   {state}")
    m = macd(closes)
    if m:
        line, sig, hist = m
        print(f"    MACD: {line:+.3f}  signal: {sig:+.3f}  hist: {hist:+.3f}"
              f"   -> {'bullish' if hist > 0 else 'bearish'} crossover")

    # Levels: support/resistance + ATR-based stop reference
    print("\n  LEVELS")
    win = bars[-60:] if len(bars) >= 60 else bars
    hi = max(b["high"] for b in win)
    lo = min(b["low"] for b in win)
    print(f"    60-day range: {lo:g} (support) -- {hi:g} (resistance)")
    print(f"    Position in range: {(price - lo) / (hi - lo) * 100:4.0f}%"
          if hi > lo else "    Position in range: n/a")
    a = atr(bars)
    if a is not None:
        print(f"    ATR(14): {a:.2f}   (a 2xATR stop ~= {price - 2 * a:.2f})")

    # Change stats
    def pct_ago(n):
        if len(closes) > n:
            return (price / closes[-1 - n] - 1) * 100
        return None
    print("\n  PERFORMANCE")
    for label, n in (("1 week", 5), ("1 month", 22), ("3 months", 66)):
        c = pct_ago(n)
        if c is not None:
            print(f"    {label:<9}: {c:+6.1f}%")

    # Verdict
    label, score, reasons = technical_verdict(bars)
    print("\n  VERDICT")
    print(f"    >>> {label}  (score {score:+d})")
    print("    signals: " + "  ".join(reasons))
    a = atr(bars)
    if a is not None and score >= 2:
        print(f"    if entering: reference stop ~{price - 2 * a:.2f} (2xATR below close)")

    print("\n  NOTE: technical read only -- combine with fundamentals & your own "
          "risk limits. Not investment advice.")


def _short_verdict(score: int) -> str:
    if score >= 4:
        return "FAVORABLE"
    if score >= 2:
        return "LEAN +"
    if score >= -1:
        return "NEUTRAL"
    if score >= -3:
        return "LEAN -"
    return "AVOID"


def brief_row(symbol: str, bars: list[dict], live: bool = False) -> dict:
    """Compute a one-line screening summary for `symbol` (used by --brief)."""
    bars = [b for b in bars if b["close"] == b["close"]]
    if len(bars) < 20:
        return {"symbol": symbol, "score": None,
                "note": f"insufficient data ({len(bars)}d)"}
    closes = [b["close"] for b in bars]
    price = closes[-1]
    _, score, _ = technical_verdict(bars)
    r = rsi(closes)
    win = bars[-60:] if len(bars) >= 60 else bars
    hi = max(b["high"] for b in win)
    lo = min(b["low"] for b in win)
    pos = (price - lo) / (hi - lo) * 100 if hi > lo else float("nan")
    recent_vol = [b["volume"] for b in bars[-20:] if b["volume"] == b["volume"]]
    avg_vol = sum(recent_vol) / len(recent_vol) if recent_vol else 0
    return {"symbol": symbol, "score": score, "verdict": _short_verdict(score),
            "price": price, "rsi": r, "pos": pos, "avg_vol": avg_vol, "live": live}


def print_brief_table(rows: list[dict]) -> None:
    """Print a ranked one-line-per-stock screen (highest score first)."""
    ranked = sorted((r for r in rows if r.get("score") is not None),
                    key=lambda r: r["score"], reverse=True)
    skipped = [r for r in rows if r.get("score") is None]

    print(f"\n{'SYMBOL':<12}{'SCORE':>6}  {'VERDICT':<10}{'PRICE':>9}"
          f"{'RSI':>6}{'RANGE%':>8}{'20D VOL':>12}  FLAGS")
    print("-" * 80)
    for r in ranked:
        rsi_s = f"{r['rsi']:.0f}" if r["rsi"] is not None else "-"
        pos_s = f"{r['pos']:.0f}" if r["pos"] == r["pos"] else "-"
        flags = []
        if r["rsi"] is not None and r["rsi"] > 72:
            flags.append("overbought")
        if r["rsi"] is not None and r["rsi"] < 30:
            flags.append("oversold")
        if r["avg_vol"] < 100_000:
            flags.append("illiquid")
        if r.get("live"):
            flags.append("live")
        print(f"{r['symbol']:<12}{r['score']:>+6}  {r['verdict']:<10}"
              f"{r['price']:>9g}{rsi_s:>6}{pos_s:>8}{r['avg_vol']:>12,.0f}  "
              f"{', '.join(flags)}")
    for r in skipped:
        print(f"{r['symbol']:<12}{'--':>6}  {r['note']}")
    print("-" * 80)
    print(f"{len(ranked)} ranked, {len(skipped)} skipped.  "
          "Screen only -- deep-dive the top names without --brief.")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="DSE technical analysis for position decisions.")
    ap.add_argument("symbols", nargs="*", help="DSE trading codes, e.g. ACMEPL KBPPWBIL")
    ap.add_argument("--from-xlsx", metavar="PATH",
                    help="Read the ticker list from an .xlsx (e.g. 'Debt to Equity Ratio.xlsx')")
    ap.add_argument("--col", default="Code",
                    help="Header of the ticker column in --from-xlsx (default 'Code')")
    ap.add_argument("--days", type=int, default=730, help="History window in calendar days (default 730)")
    ap.add_argument("--csv", default=1, action="store_true", help="Also write raw OHLCV to <SYMBOL>.csv")
    ap.add_argument("--brief", action="store_true",
                    help="One-line ranked screen per stock (use for long watchlists)")
    ap.add_argument("--no-live", action="store_true",
                    help="Skip the live intraday snapshot; use the day-end archive only")
    args = ap.parse_args(argv)

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
        print(f"Loaded {len(from_file)} tickers from {args.from_xlsx} (column '{args.col}').")
        symbols = symbols + from_file  # CLI symbols first, then file list

    if not symbols:
        ap.error("no tickers given -- pass trading codes and/or --from-xlsx PATH")

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    # One request pulls a live bar for every ticker; merged onto each stock's
    # archive history below so an intra-session run isn't a day stale.
    live_snapshot: dict[str, dict] = {}
    session_date: dt.date | None = None
    if not args.no_live:
        try:
            live_snapshot, session_date = fetch_live_snapshot()
            print(f"Live snapshot: {len(live_snapshot)} tickers as of {session_date}.")
        except Exception as exc:  # noqa: BLE001 - degrade to archive-only on any failure
            print(f"live snapshot unavailable ({exc}); using archive only",
                  file=sys.stderr)

    brief_rows: list[dict] = []
    for i, sym in enumerate(symbols):
        sym = sym.upper()
        if i > 0:
            time.sleep(FETCH_DELAY_SECONDS)  # don't hammer the DSE archive
        try:
            bars = fetch_history(sym, start, end)
        except Exception as exc:  # noqa: BLE001 - surface any fetch/parse failure clearly
            print(f"{sym}: failed to fetch data ({exc})", file=sys.stderr)
            if args.brief:
                brief_rows.append({"symbol": sym, "score": None, "note": "fetch failed"})
            continue
        live = merge_live_bar(bars, live_snapshot.get(sym), session_date)
        if not bars:
            msg = "no data returned (check the trading code / date range)."
            if args.brief:
                brief_rows.append({"symbol": sym, "score": None, "note": msg})
            else:
                print(f"\n{sym}: {msg}")
            continue
        if args.csv:
            path = f"{sym}.csv"
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["date", "open", "high", "low", "close", "volume", "trades"])
                for b in bars:
                    w.writerow([b["date"], b["open"], b["high"], b["low"],
                                b["close"], b["volume"], b["trades"]])
            print(f"\n{sym}: wrote {len(bars)} rows -> {path}")
        if args.brief:
            brief_rows.append(brief_row(sym, bars, live))
        else:
            analyze(sym, bars, live)

    if args.brief:
        print_brief_table(brief_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
