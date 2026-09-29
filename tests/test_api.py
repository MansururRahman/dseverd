import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests import fixtures as fx
from web.app import create_app
from web.cache import BarCache


def make_cache(fetch=fx.fake_fetch_history, live=fx.live_next_day):
    return BarCache(fetch=fetch, fetch_live=live, min_interval=0)


@pytest.fixture
def cache():
    return make_cache()


@pytest.fixture
def client(cache):
    return TestClient(create_app(cache=cache))


def sym():
    return fx.fixture_symbols()[0]


def wait_job(client, job_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


# --- meta ------------------------------------------------------------------ #
def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>DSE Screener</title>" in r.text


def test_watchlist_reads_the_xlsx(client):
    symbols = client.get("/api/watchlist").json()["symbols"]
    assert sym() in symbols and len(symbols) >= len(fx.fixture_symbols())


def test_watchlist_missing_file_is_empty(cache, tmp_path):
    c = TestClient(create_app(cache=cache, watchlist_path=tmp_path / "none.xlsx"))
    assert c.get("/api/watchlist").json() == {"symbols": []}


def test_defaults_mirror_engine_defaults(client):
    d = client.get("/api/defaults").json()
    assert d["swing_entry"]["capital"] == 2_000_000.0
    assert d["claude"]["days"] == 400 and d["claude"]["min_rr"] == 2.0
    assert d["gate"]["disabled_gates"] == []
    assert "entry" not in d["swing_exit"] and "symbol" not in d["technical"]


# --- single-stock tools ------------------------------------------------------ #
def test_swing_entry(client):
    j = client.post("/api/swing/entry", json={"symbol": sym()}).json()
    assert j["symbol"] == sym() and j["live_merged"] is False
    assert j["result"]["entry"]["decision"] in ("CONDITIONAL BUY", "NO TRADE")
    assert j["result"]["backtest"] is None


def test_swing_entry_with_backtest(client):
    j = client.post("/api/swing/entry", json={"symbol": sym(), "backtest": True}).json()
    bt = j["result"]["backtest"]
    assert set(bt["stats"]) == {"n", "win_rate", "expectancy", "total_pnl"}
    assert isinstance(bt["trades"], list)


def test_symbol_is_normalized(client):
    j = client.post("/api/swing/entry", json={"symbol": f"  {sym().lower()} "}).json()
    assert j["symbol"] == sym()


def test_swing_exit_requires_levels(client):
    r = client.post("/api/swing/exit", json={"symbol": sym(), "entry": 10, "target": 11})
    assert r.status_code == 422
    assert any(e["loc"][-1] == "stop" for e in r.json()["detail"])


def test_swing_exit(client):
    last = fx.load_bars(sym())[-1]["close"]
    j = client.post("/api/swing/exit", json={"symbol": sym(), "entry": last,
                                             "stop": last * 0.95, "target": last * 1.05}).json()
    assert isinstance(j["result"]["action"], str)


def test_claude_live_merge_does_not_touch_cache(client, cache):
    before = len(cache.bars(sym(), 400))
    j = client.post("/api/claude", json={"symbol": sym()}).json()
    assert j["live_merged"] is True and j["result"]["live"] is True
    assert len(cache.bars(sym(), 400)) == before


def test_claude_live_outage_falls_back(cache):
    c = TestClient(create_app(cache=make_cache(live=fx.live_outage)))
    j = c.post("/api/claude", json={"symbol": sym()}).json()
    assert j["live_merged"] is False
    assert j["note"] == "live snapshot unavailable (simulated live outage); using archive only"


def test_claude_live_off(client):
    j = client.post("/api/claude", json={"symbol": sym(), "live": False}).json()
    assert j["live_merged"] is False and j["note"] is None


def test_uptrend_no_data(client):
    j = client.post("/api/uptrend", json={"symbol": "EMPTY"}).json()
    assert j["result"]["decision"] == "NO DATA"


def test_uptrend_index_warnings(client):
    j = client.post("/api/uptrend", json={"symbol": sym(), "index_symbol": "BADCODE"}).json()
    assert j["note"] == ("warning: index BADCODE fetch failed (simulated timeout) "
                         "-- market guard disabled.")
    j = client.post("/api/uptrend", json={"symbol": sym(), "index_symbol": "EMPTY"}).json()
    assert j["note"] == "warning: index EMPTY returned no data -- market guard disabled."


def test_technical(client):
    j = client.post("/api/technical", json={"symbol": sym()}).json()
    assert isinstance(j["result"]["verdict"], str)
    assert isinstance(j["result"]["brief"]["score"], int)
    assert all(isinstance(x, str) for x in j["result"]["reasons"])


def test_technical_no_data(client):
    j = client.post("/api/technical", json={"symbol": "EMPTY", "live": False}).json()
    assert j["result"]["verdict"] is None and j["result"]["brief"]["score"] is None


def test_gate_disabling_a_gate_removes_it(client):
    full = client.post("/api/gate", json={"symbol": sym()}).json()["result"]
    fewer = client.post("/api/gate", json={"symbol": sym(),
                                           "disabled_gates": ["volume"]}).json()["result"]
    assert len(fewer["snapshot"]["gates"]) == len(full["snapshot"]["gates"]) - 1
    assert isinstance(full["verdict"], str) and isinstance(full["summary_text"], str)


def test_gate_rejects_unknown_gate(client):
    r = client.post("/api/gate", json={"symbol": sym(), "disabled_gates": ["nope"]})
    assert r.status_code == 422


@pytest.mark.parametrize("path", ["/api/swing/entry", "/api/claude", "/api/uptrend",
                                  "/api/technical", "/api/gate"])
def test_fetch_failure_is_502(client, path):
    r = client.post(path, json={"symbol": "BADCODE"})
    assert r.status_code == 502
    assert r.json() == {"detail": "fetch failed for BADCODE: simulated timeout"}


@pytest.mark.parametrize("body", [{"symbol": ""}, {"symbol": "AC ME"},
                                  {"symbol": "ACMEPL", "days": 5},
                                  {"symbol": "ACMEPL", "capital": -1}])
def test_bad_input_is_422(client, body):
    assert client.post("/api/swing/entry", json=body).status_code == 422


# --- shortlist jobs ------------------------------------------------------------ #
def test_shortlist_job_lifecycle(client):
    symbols = fx.fixture_symbols()[:3] + ["BADCODE"]
    job_id = client.post("/api/shortlist", json={"symbols": symbols, "live": False}).json()["job_id"]
    job = wait_job(client, job_id)
    assert job["status"] == "done", job["error"]
    assert [r["symbol"] for r in job["result"]["stage1"]["rows"]] == symbols
    assert job["partial"]["stage1"]["n_screened"] == 3
    assert client.get("/api/jobs/latest").json()["id"] == job_id


def test_shortlist_dedupes_symbols(client):
    s = sym()
    job_id = client.post("/api/shortlist",
                         json={"symbols": [s.lower(), s, f" {s} "], "live": False}).json()["job_id"]
    job = wait_job(client, job_id)
    assert [r["symbol"] for r in job["result"]["stage1"]["rows"]] == [s]


def test_shortlist_needs_tickers(client, cache, tmp_path):
    assert client.post("/api/shortlist", json={}).status_code == 422
    c = TestClient(create_app(cache=cache, watchlist_path=tmp_path / "none.xlsx"))
    assert c.post("/api/shortlist", json={"use_watchlist": True}).status_code == 422


def test_unknown_jobs_are_404(client):
    assert client.get("/api/jobs/latest").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.post("/api/jobs/nope/cancel").status_code == 404


def _slow_client():
    def slow(symbol, start, end):
        time.sleep(0.2)
        return fx.fake_fetch_history(symbol, start, end)
    return TestClient(create_app(cache=make_cache(fetch=slow)))


def test_latest_job_while_running():
    c = _slow_client()
    job_id = c.post("/api/shortlist", json={"symbols": fx.fixture_symbols(),
                                            "live": False}).json()["job_id"]
    deadline = time.monotonic() + 5
    while c.get(f"/api/jobs/{job_id}").json()["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    latest = c.get("/api/jobs/latest").json()
    assert latest["id"] == job_id and latest["status"] == "running"
    c.post(f"/api/jobs/{job_id}/cancel")
    wait_job(c, job_id)


def test_cancel_shortlist_job():
    c = _slow_client()
    job_id = c.post("/api/shortlist", json={"symbols": fx.fixture_symbols(),
                                            "live": False}).json()["job_id"]
    deadline = time.monotonic() + 5
    while c.get(f"/api/jobs/{job_id}").json()["progress"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 202
    assert wait_job(c, job_id)["status"] == "cancelled"
