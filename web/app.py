"""FastAPI app serving the DSE tools. From the repo root:

    python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import dse_shortlist
from web import models, services
from web.cache import BarCache
from web.jobs import JobRunner
from web.serialize import to_jsonable

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "Debt to Equity Ratio.xlsx"
STATIC = Path(__file__).resolve().parent / "static"


def create_app(cache: BarCache | None = None, runner: JobRunner | None = None,
               watchlist_path: Path = WATCHLIST) -> FastAPI:
    cache = cache or BarCache()
    runner = runner or JobRunner()
    app = FastAPI(title="DSE tools")

    @app.exception_handler(services.FetchError)
    async def _fetch_failed(request: Request, exc: services.FetchError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.get("/api/watchlist")
    def watchlist():
        return {"symbols": services.read_watchlist(watchlist_path)}

    @app.get("/api/defaults")
    def defaults():
        return models.defaults()

    @app.post("/api/swing/entry")
    def swing_entry(req: models.SwingEntryRequest):
        return services.swing_entry(cache, **req.model_dump())

    @app.post("/api/swing/exit")
    def swing_exit(req: models.SwingExitRequest):
        return services.swing_exit(cache, **req.model_dump())

    @app.post("/api/claude")
    def claude(req: models.ClaudeRequest):
        return services.claude(cache, **req.model_dump())

    @app.post("/api/uptrend")
    def uptrend(req: models.UptrendRequest):
        return services.uptrend_gate(cache, **req.model_dump())

    @app.post("/api/technical")
    def technical(req: models.TechnicalRequest):
        return services.technical(cache, **req.model_dump())

    @app.post("/api/gate")
    def gate_strategy(req: models.GateRequest):
        return services.gate_strategy(cache, **req.model_dump())

    @app.post("/api/shortlist")
    def shortlist(req: models.ShortlistRequest):
        symbols = list(dict.fromkeys(req.symbols))
        if req.use_watchlist:
            symbols += [s for s in services.read_watchlist(watchlist_path) if s not in symbols]
        if not symbols:
            raise HTTPException(422, "no tickers: pass symbols, or use_watchlist with a "
                                     "non-empty watchlist")
        opts = dse_shortlist.ShortlistOptions(
            days=req.days, capital=req.capital, risk=req.risk, score_gate=req.score_gate,
            min_turnover=req.min_turnover, index_symbol=req.index_symbol,
            no_live=not req.live, raw_volume=req.raw_volume)

        def run(emit):
            # delay=0: the cache throttles every archive fetch itself.
            return dse_shortlist.run_shortlist(symbols, opts, emit=emit,
                                               fetch=cache.fetcher(req.days),
                                               fetch_live=cache.live_snapshot, delay=0)

        return {"job_id": runner.submit("shortlist", req.model_dump(), run)}

    @app.get("/api/jobs/latest")  # declared before /api/jobs/{job_id} so it wins
    def latest_job():
        job = runner.latest("shortlist")
        if job is None:
            raise HTTPException(404, "no shortlist job yet")
        return to_jsonable(job)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = runner.get(job_id)
        if job is None:
            raise HTTPException(404, "no job with that id")
        return to_jsonable(job)

    @app.post("/api/jobs/{job_id}/cancel", status_code=202)
    def cancel_job(job_id: str):
        if not runner.cancel(job_id):
            raise HTTPException(404, "no active job with that id")
        return {"cancelled": job_id}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


app = create_app()
