"""API request bodies. Defaults come straight from the engines' own DEFAULTS so
the web UI and the CLI agree on every knob; /api/defaults serves them."""
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

import dse_claude
import dse_gate_strategy as gate
import dse_swing_signal as swing

Symbol = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True,
                                          pattern=r"^[A-Za-z0-9&._-]{1,20}$")]
Days = Annotated[int, Field(ge=20, le=3650)]
Positive = Annotated[float, Field(gt=0)]
NonNegative = Annotated[float, Field(ge=0)]
GateName = Literal["trend", "sma_cross", "macd", "rsi", "volume", "atr"]


class SwingEntryRequest(BaseModel):
    symbol: Symbol
    days: Days = 730
    capital: Positive = swing.DEFAULTS["capital"]
    risk: Positive = swing.DEFAULTS["risk_pct"]
    min_rr: NonNegative = swing.DEFAULTS["min_rr"]
    score_gate: int = swing.DEFAULTS["score_gate"]
    backtest: bool = False


class SwingExitRequest(BaseModel):
    symbol: Symbol
    days: Days = 730
    entry: Positive
    stop: Positive
    target: Positive


class ClaudeRequest(BaseModel):
    symbol: Symbol
    days: Days = 400
    live: bool = True
    min_rr: NonNegative = dse_claude.DEFAULTS["min_rr"]
    max_ext: Positive = dse_claude.DEFAULTS["ext_above_sma20_max_pct"]
    max_day_gain: Positive = dse_claude.DEFAULTS["day_gain_max_pct"]


class UptrendRequest(BaseModel):
    symbol: Symbol
    days: Days = 730
    min_turnover: Positive | None = None
    index_symbol: Symbol | None = None


class TechnicalRequest(BaseModel):
    symbol: Symbol
    days: Days = 730
    live: bool = True


class GateRequest(BaseModel):
    symbol: Symbol
    days: Days = 730
    capital: Positive = 2_000_000.0
    tp: Positive = gate.DEFAULT_CFG["take_profit_pct"]
    stop_mult: Positive = gate.DEFAULT_CFG["atr_stop_mult"]
    cost: NonNegative = gate.DEFAULT_CFG["cost_pct"]
    rsi_min: float = gate.DEFAULT_CFG["rsi_min"]
    rsi_max: float = gate.DEFAULT_CFG["rsi_max"]
    disabled_gates: list[GateName] = []


class ShortlistRequest(BaseModel):
    symbols: list[Symbol] = []
    use_watchlist: bool = False
    days: Days = 730
    live: bool = True
    raw_volume: bool = False
    capital: Positive = swing.DEFAULTS["capital"]
    risk: Positive = swing.DEFAULTS["risk_pct"]
    score_gate: int = swing.DEFAULTS["score_gate"]
    min_turnover: Positive | None = None
    index_symbol: Symbol | None = None


TOOL_MODELS = {
    "swing_entry": SwingEntryRequest, "swing_exit": SwingExitRequest,
    "claude": ClaudeRequest, "uptrend": UptrendRequest, "technical": TechnicalRequest,
    "gate": GateRequest, "shortlist": ShortlistRequest,
}


def defaults() -> dict:
    """{tool: {field: default}} for every optional field (prefills the UI forms)."""
    return {tool: {name: f.default for name, f in model.model_fields.items()
                   if not f.is_required()}
            for tool, model in TOOL_MODELS.items()}
