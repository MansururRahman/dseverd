"""dse_shortlist.main() must print exactly what the pre-refactor script printed.

tests/legacy_shortlist.py is a frozen verbatim copy of the original. Both run
under identical patches (fixture data, zero delay, fixed session clock) and
their stdout, stderr and exit code must match for every variant.
"""
import pytest

import dse_shortlist
from tests import fixtures as fx
from tests import legacy_shortlist

XLSX = str(fx.ROOT / "Debt to Equity Ratio.xlsx")

VARIANTS = {
    "no_live": dict(argv=["--no-live"], live="outage", frac=0.83, forced=False),
    "live_projected": dict(argv=[], live="next_day", frac=0.83, forced=False),
    "live_raw_volume": dict(argv=["--raw-volume"], live="next_day", frac=0.3, forced=False),
    "live_early_session": dict(argv=[], live="today", frac=0.3, forced=False),
    "live_before_open": dict(argv=[], live="today", frac=0.0, forced=False),
    "live_outage": dict(argv=[], live="outage", frac=0.83, forced=False),
    "from_xlsx": dict(argv=["--no-live", "--from-xlsx", XLSX], live="outage", frac=0.83,
                      forced=False),
    "forced_no_live": dict(argv=["--no-live", "--index-symbol", "DS30",
                                 "--min-turnover", "100000"],
                           live="outage", frac=0.83, forced=True),
    "forced_live": dict(argv=["--index-symbol", "BADCODE", "--min-turnover", "100000"],
                        live="next_day", frac=0.83, forced=True),
    "forced_live_raw": dict(argv=["--raw-volume"], live="next_day", frac=0.6, forced=True),
}


def _run(module, argv, capsys):
    code = module.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def _setup(name, monkeypatch):
    v = VARIANTS[name]
    fx.patch_shortlist_env(monkeypatch, [dse_shortlist, legacy_shortlist],
                           live=v["live"], frac=v["frac"])
    if v["forced"]:
        fx.force_survivors(monkeypatch)
    return fx.fixture_symbols() + ["BADCODE", "EMPTY"] + v["argv"]


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_main_output_matches_legacy(name, monkeypatch, capsys):
    argv = _setup(name, monkeypatch)
    capsys.readouterr()
    legacy = _run(legacy_shortlist, argv, capsys)
    new = _run(dse_shortlist, argv, capsys)
    assert new == legacy


def test_forced_variant_reaches_stage_3(monkeypatch, capsys):
    """Guards the harness itself: the forced variants must exercise stages 2-3."""
    argv = _setup("forced_live", monkeypatch)
    capsys.readouterr()
    _, out, err = _run(legacy_shortlist, argv, capsys)
    assert "STAGE 2 -- DUAL BACKTEST" in out
    assert "ROBUST (positive expectancy in BOTH backtests)" in out
    assert "STAGE 3 -- UPTREND GATE ON THE 1 ROBUST NAME(S)" in out
    assert "index BADCODE fetch failed" in err
    assert "on a LIVE bar." in out and "VOLUME is PROJECTED" in out
