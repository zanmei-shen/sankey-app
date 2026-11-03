"""
app.py — FastAPI web application for SEC EDGAR financial data retrieval.

Run (development):
    uvicorn app:app --reload

Run (production):
    uvicorn app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

import sec_client
import sankey_data
import sankey_render

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CURRENT_YEAR = datetime.now().year
# Allowed characters for a US exchange ticker: letters, digits, dot, hyphen
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


async def _warm_tickers() -> None:
    """Load the SEC ticker list in the background so the first search is instant."""
    try:
        await asyncio.to_thread(sec_client.load_tickers)
    except Exception as exc:  # best effort; the first search retries
        log.warning("Could not pre-load the ticker list: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    if not ua:
        log.warning(
            "SEC_USER_AGENT is not set — all data requests will fail. "
            'Add SEC_USER_AGENT="Your Name your@email.com" to .env'
        )
    else:
        log.info("SEC_USER_AGENT configured.")
        asyncio.create_task(_warm_tickers())
    yield


app = FastAPI(
    title="SEC EDGAR Financial Explorer",
    version="1.0.0",
    lifespan=lifespan,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Optionally mount static files if the directory exists
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _validate(ticker: str, start_year: int, end_year: int) -> str:
    """Sanitise and validate request params; raise HTTPException on bad input."""
    ticker = ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(
            status_code=422,
            detail="Invalid ticker. Use letters, digits, dots, or hyphens (max 10 chars).",
        )
    if not (1993 <= start_year <= CURRENT_YEAR):
        raise HTTPException(
            status_code=422,
            detail=f"start_year must be between 1993 and {CURRENT_YEAR}.",
        )
    if not (1993 <= end_year <= CURRENT_YEAR):
        raise HTTPException(
            status_code=422,
            detail=f"end_year must be between 1993 and {CURRENT_YEAR}.",
        )
    if start_year > end_year:
        raise HTTPException(status_code=422, detail="start_year must be ≤ end_year.")
    return ticker


async def _run(fn, *args) -> object:
    """Run a blocking SEC call in a thread pool and map its errors to HTTP errors."""
    try:
        return await asyncio.to_thread(fn, *args)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except EnvironmentError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


async def _fetch(ticker: str, start_year: int, end_year: int) -> dict:
    """Run the blocking SEC fetch in a thread pool to avoid blocking the event loop."""
    return await _run(sec_client.fetch_financials, ticker, start_year, end_year)  # type: ignore[return-value]


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"current_year": CURRENT_YEAR},
    )


@app.get("/api/fetch")
async def api_fetch(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AMZN"),
    start_year: int = Query(..., description="Start fiscal year, e.g. 2020"),
    end_year: int = Query(..., description="End fiscal year, e.g. 2025"),
):
    """Return annual revenue data as JSON."""
    ticker = _validate(ticker, start_year, end_year)
    result = await _fetch(ticker, start_year, end_year)

    rows = result["rows"]
    for r in rows:
        # Ensure period_end is JSON-serialisable
        if r.get("period_end") is not None:
            r["period_end"] = str(r["period_end"])

    return {
        "company": result["company"],
        "ticker": result["ticker"],
        "cik": result["cik"],
        "concept_used": result["concept_used"],
        "segments_found": result["segments_found"],
        "row_count": len(rows),
        "rows": rows,
    }


@app.get("/api/download")
async def api_download(
    ticker: str = Query(..., description="Stock ticker symbol"),
    start_year: int = Query(..., description="Start fiscal year"),
    end_year: int = Query(..., description="End fiscal year"),
):
    """Return data as a CSV file download."""
    ticker = _validate(ticker, start_year, end_year)
    result = await _fetch(ticker, start_year, end_year)

    df = pd.DataFrame(result["rows"], columns=sec_client.OUTPUT_COLUMNS)
    df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df.sort_values(["period_end", "metric", "segment"], inplace=True, ignore_index=True)

    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)

    # Sanitise ticker for use in filename
    safe_ticker = re.sub(r"[^\w.\-]", "", ticker)[:10]
    filename = f"{safe_ticker}_{start_year}_{end_year}_sec.csv"

    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/sankey")
async def api_sankey(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AMZN"),
    year: int = Query(..., description="Fiscal year, e.g. 2025"),
    period: str = Query("FY", description="FY, Q1, Q2, Q3 or Q4"),
    mode: str = Query("income", description="income or segments"),
    categories: bool = Query(True, description="income mode: show revenue categories on the left"),
):
    """Return a Sankey diagram (SVG) for one fiscal year and period."""
    if not (1993 <= year <= CURRENT_YEAR):
        raise HTTPException(status_code=422, detail=f"year must be between 1993 and {CURRENT_YEAR}.")
    ticker = _validate(ticker, year, year)
    period = period.strip().upper()
    mode = mode.strip().lower()
    if period not in sankey_data.PERIODS:
        raise HTTPException(status_code=422, detail=f"period must be one of {', '.join(sankey_data.PERIODS)}.")
    if mode not in sankey_data.MODES:
        raise HTTPException(status_code=422, detail=f"mode must be one of {', '.join(sankey_data.MODES)}.")

    model = await _run(sankey_data.build, ticker, year, period, mode, categories)
    svg = sankey_render.render_svg(model)  # type: ignore[arg-type]
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


@app.get("/api/tickers")
async def api_tickers(
    q: str = Query(..., min_length=1, max_length=40, description="Partial ticker or company name"),
    limit: int = Query(8, ge=1, le=20),
):
    """Suggest tickers for a partial ticker symbol or company name."""
    results = await _run(sec_client.search_tickers, q, limit)
    return {"query": q, "results": results}


@app.get("/api/health")
async def health():
    """Simple health-check endpoint."""
    ua_set = bool(os.getenv("SEC_USER_AGENT", "").strip())
    return {"status": "ok", "sec_user_agent_configured": ua_set}
