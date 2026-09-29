"""
Multi-Gate Confirmation Strategy: Screener + Backtester
=======================================================

A trade fires ONLY when every enabled "gate" passes (logical AND).
Gates included: SMA trend, SMA crossover, MACD, RSI, Volume, ATR volatility.

IMPORTANT HONEST NOTE
---------------------
This does NOT guarantee any fixed profit-per-trade (e.g. 3%). The gates
raise signal quality and win rate; the BACKTEST tells you the real
expectancy, win rate, and max drawdown. Take-profit / stop-loss are
configurable so you can test a 3% target directly and see what happens.

Requires (on your machine):  pip install pandas numpy yfinance
The functions work on any DataFrame with columns:
    ['open','high','low','close','volume']  (DatetimeIndex)
"""

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------
# INDICATORS
# ----------------------------------------------------------------------
def add_indicators(df, cfg):
    d = df.copy()
    c = d["close"]

    # SMAs
    d["sma_fast"] = c.rolling(cfg["sma_fast"]).mean()
    d["sma_slow"] = c.rolling(cfg["sma_slow"]).mean()
    d["sma_trend"] = c.rolling(cfg["sma_trend"]).mean()

    # MACD
    ema_f = c.ewm(span=cfg["macd_fast"], adjust=False).mean()
    ema_s = c.ewm(span=cfg["macd_slow"], adjust=False).mean()
    d["macd"] = ema_f - ema_s
    d["macd_signal"] = d["macd"].ewm(span=cfg["macd_signal"], adjust=False).mean()
    d["macd_hist"] = d["macd"] - d["macd_signal"]

    # RSI (Wilder's smoothing)
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / cfg["rsi_period"], adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / cfg["rsi_period"], adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    d["rsi"] = 100 - (100 / (1 + rs))

    # Volume vs its average
    d["vol_avg"] = d["volume"].rolling(cfg["vol_period"]).mean()

    # ATR (for volatility gate + stop sizing)
    hl = d["high"] - d["low"]
    hc = (d["high"] - c.shift()).abs()
    lc = (d["low"] - c.shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / cfg["atr_period"], adjust=False).mean()

    return d


# ----------------------------------------------------------------------
# GATES  -- each returns a boolean Series (True = gate passes that bar)
# ----------------------------------------------------------------------
def evaluate_gates(d, cfg):
    gates = {}

    # 1. Trend gate: price above long-term SMA
    if cfg["use_trend"]:
        gates["trend"] = d["close"] > d["sma_trend"]

    # 2. SMA crossover gate: fast above slow (momentum regime)
    if cfg["use_sma_cross"]:
        gates["sma_cross"] = d["sma_fast"] > d["sma_slow"]

    # 3. MACD gate: histogram positive (bullish momentum)
    if cfg["use_macd"]:
        gates["macd"] = d["macd_hist"] > 0

    # 4. RSI gate: within a healthy band (not overbought, has momentum)
    if cfg["use_rsi"]:
        gates["rsi"] = (d["rsi"] > cfg["rsi_min"]) & (d["rsi"] < cfg["rsi_max"])

    # 5. Volume gate: today's volume above its average (participation)
    if cfg["use_volume"]:
        gates["volume"] = d["volume"] > d["vol_avg"] * cfg["vol_mult"]

    # 6. Volatility gate: ATR not extreme (avoid chaotic bars)
    if cfg["use_atr"]:
        atr_pct = d["atr"] / d["close"]
        gates["atr"] = atr_pct < cfg["atr_max_pct"]

    gate_df = pd.DataFrame(gates)
    # ENTRY = every enabled gate passes (logical AND across columns)
    all_pass = gate_df.all(axis=1)
    return all_pass, gate_df


# ----------------------------------------------------------------------
# BACKTEST  -- long-only, one position at a time, next-open execution
# ----------------------------------------------------------------------
def backtest(df, cfg):
    d = add_indicators(df, cfg)
    entry_ok, gate_df = evaluate_gates(d, cfg)
    d["entry_signal"] = entry_ok

    trades = []
    in_pos = False
    entry_price = stop_price = target_price = np.nan
    entry_date = None

    idx = d.index
    for i in range(len(d) - 1):
        row = d.iloc[i]
        nxt_open = d["open"].iloc[i + 1]  # execute at next bar's open

        if not in_pos:
            if row["entry_signal"] and not np.isnan(row["atr"]):
                entry_price = nxt_open
                # volatility-based stop, plus configurable take-profit %
                stop_price = entry_price - cfg["atr_stop_mult"] * row["atr"]
                target_price = entry_price * (1 + cfg["take_profit_pct"])
                entry_date = idx[i + 1]
                in_pos = True
        else:
            hi = d["high"].iloc[i + 1]
            lo = d["low"].iloc[i + 1]
            exit_price = exit_reason = None

            # Stop checked first (conservative: assume worst case intrabar)
            if lo <= stop_price:
                exit_price, exit_reason = stop_price, "stop"
            elif hi >= target_price:
                exit_price, exit_reason = target_price, "target"

            if exit_price is not None:
                cost = cfg["cost_pct"]  # round-trip slippage+commission
                ret = (exit_price / entry_price - 1) - cost
                trades.append(
                    dict(entry_date=entry_date, exit_date=idx[i + 1],
                         entry=entry_price, exit=exit_price,
                         reason=exit_reason, ret=ret))
                in_pos = False

    return pd.DataFrame(trades), d, gate_df


# ----------------------------------------------------------------------
# STATS
# ----------------------------------------------------------------------
def summarize(trades, account=2000.0, risk_pct=0.01):
    if len(trades) == 0:
        return "No trades generated. Loosen gates or lengthen the data window."

    wins = trades[trades["ret"] > 0]
    losses = trades[trades["ret"] <= 0]
    win_rate = len(wins) / len(trades)
    avg_win = wins["ret"].mean() if len(wins) else 0
    avg_loss = losses["ret"].mean() if len(losses) else 0
    expectancy = trades["ret"].mean()

    # Equity curve assuming fixed-fractional 1% risk per trade
    equity = account
    curve = [equity]
    for r in trades["ret"]:
        equity *= (1 + r)  # simplistic: full-notional compounding
        curve.append(equity)
    curve = pd.Series(curve)
    peak = curve.cummax()
    dd = (curve / peak - 1)
    max_dd = dd.min()

    # longest losing streak
    streak = mx = 0
    for r in trades["ret"]:
        streak = streak + 1 if r <= 0 else 0
        mx = max(mx, streak)

    lines = [
        f"Trades:            {len(trades)}",
        f"Win rate:          {win_rate:6.1%}",
        f"Avg win:           {avg_win:+6.2%}",
        f"Avg loss:          {avg_loss:+6.2%}",
        f"Expectancy/trade:  {expectancy:+6.2%}  <-- the number that matters",
        f"Total return:      {curve.iloc[-1]/account - 1:+6.1%}",
        f"Max drawdown:      {max_dd:6.1%}",
        f"Longest losing streak: {mx} trades",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
DEFAULT_CFG = dict(
    # indicator periods
    sma_fast=20, sma_slow=50, sma_trend=200,
    macd_fast=12, macd_slow=26, macd_signal=9,
    rsi_period=14, rsi_min=45, rsi_max=70,
    vol_period=20, vol_mult=1.0,
    atr_period=14, atr_max_pct=0.05,
    # which gates are active
    use_trend=True, use_sma_cross=True, use_macd=True,
    use_rsi=True, use_volume=True, use_atr=True,
    # trade management  (test your 3% here)
    take_profit_pct=0.03,     # <-- 3% target
    atr_stop_mult=2.0,        # stop = entry - 2*ATR
    cost_pct=0.002,           # 0.2% round-trip costs
)


# ----------------------------------------------------------------------
# LIVE DATA LOADER (uncomment on your machine)
# ----------------------------------------------------------------------
def load_yf(ticker, start="2015-01-01", end=None):
    import yfinance as yf
    df = yf.download(ticker, start=start, end=end, auto_adjust=True)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]]


# ----------------------------------------------------------------------
# DEMO on synthetic data so it runs anywhere
# ----------------------------------------------------------------------
def synthetic(n=1500, seed=7):
    rng = np.random.default_rng(seed)
    # random walk with mild drift + volatility clustering
    rets = rng.normal(0.0004, 0.012, n)
    price = 50 * np.exp(np.cumsum(rets))
    high = price * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = price * (1 - np.abs(rng.normal(0, 0.006, n)))
    openp = price * (1 + rng.normal(0, 0.003, n))
    vol = rng.integers(5e5, 5e6, n).astype(float)
    dates = pd.date_range("2018-01-01", periods=n, freq="B")
    return pd.DataFrame(dict(open=openp, high=high, low=low,
                             close=price, volume=vol), index=dates)


if __name__ == "__main__":
    df = synthetic()
    trades, d, gates = backtest(df, DEFAULT_CFG)
    print("=== ALL SIX GATES ON (strict) ===")
    print(summarize(trades))

    # show how loosening changes trade count
    cfg2 = {**DEFAULT_CFG, "use_volume": False, "use_atr": False}
    t2, _, _ = backtest(df, cfg2)
    print("\n=== 4 GATES (no volume/atr) ===")
    print(summarize(t2))
