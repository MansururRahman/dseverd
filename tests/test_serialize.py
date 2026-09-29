import datetime as dt
import json
import math

from web.serialize import to_jsonable


def test_non_finite_floats_become_none():
    assert to_jsonable([math.nan, math.inf, -math.inf, 1.5]) == [None, None, None, 1.5]


def test_dates_and_datetimes_become_iso():
    out = to_jsonable({"d": dt.date(2026, 9, 28), "t": dt.datetime(2026, 9, 28, 13, 35)})
    assert out == {"d": "2026-09-28", "t": "2026-09-28T13:35:00"}


def test_nested_tuples_dicts_and_keys():
    out = to_jsonable({1: ({"x": math.nan},), "b": True, "n": None, "s": "ok"})
    assert out == {"1": [{"x": None}], "b": True, "n": None, "s": "ok"}
    json.dumps(out, allow_nan=False)
