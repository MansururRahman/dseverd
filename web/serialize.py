"""Make engine results JSON-safe: the engines use NaN for "missing" and date
objects for bar dates, neither of which JSON (or FastAPI's encoder) accepts."""
import datetime as dt
import math


def to_jsonable(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dt.date):  # datetime is a date subclass
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
