"""FinAlly FastAPI application."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# load_dotenv must run before any module that reads env vars at import time
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent.parent / ".env")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.db import (  # noqa: E402
    get_cash_balance,
    get_positions,
    get_watchlist_tickers,
    init_db,
    insert_portfolio_snapshot,
)
from app.market import PriceCache, create_market_data_source  # noqa: E402
from app.market.stream import _generate_events  # noqa: E402
from app.routers import chat, health, portfolio, watchlist  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL = 10.0


async def _snapshot_loop(app: FastAPI) -> None:
    """Record portfolio value every SNAPSHOT_INTERVAL seconds."""
    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL)
        try:
            price_cache = app.state.price_cache
            raw_positions = get_positions()
            pv = sum(
                (price_cache.get_price(p["ticker"]) or p["avg_cost"]) * p["quantity"]
                for p in raw_positions
            )
            insert_portfolio_snapshot(round(get_cash_balance() + pv, 2))
        except Exception:
            logger.exception("Portfolio snapshot failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("FinAlly backend starting up")
    init_db()

    price_cache = PriceCache()
    tickers = get_watchlist_tickers()

    market_source = create_market_data_source(price_cache)
    await market_source.start(tickers)

    # Record session open prices (first price seen per ticker) for daily change %
    session_open_prices: dict[str, float] = {
        t: p for t in tickers if (p := price_cache.get_price(t)) is not None
    }

    # Seed initial portfolio snapshot
    raw_positions = get_positions()
    pv = sum(
        (price_cache.get_price(p["ticker"]) or p["avg_cost"]) * p["quantity"]
        for p in raw_positions
    )
    insert_portfolio_snapshot(round(get_cash_balance() + pv, 2))

    app.state.price_cache = price_cache
    app.state.market_source = market_source
    app.state.session_open_prices = session_open_prices

    snapshot_task = asyncio.create_task(_snapshot_loop(app), name="portfolio-snapshot")
    logger.info("FinAlly backend ready — %d tickers tracked", len(tickers))

    yield

    # --- Shutdown ---
    logger.info("FinAlly backend shutting down")
    snapshot_task.cancel()
    try:
        await snapshot_task
    except asyncio.CancelledError:
        pass
    await market_source.stop()
    logger.info("FinAlly backend stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="FinAlly API",
        description="AI-powered trading workstation backend",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(watchlist.router)
    app.include_router(portfolio.router)
    app.include_router(chat.router)

    @app.get("/api/stream/prices", tags=["streaming"])
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(request.app.state.price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Serve Next.js static export when built
    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
        logger.info("Serving frontend from %s", static_dir)

    return app


app = create_app()
