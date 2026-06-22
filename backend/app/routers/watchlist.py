"""Watchlist API endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db import (
    add_watchlist_ticker,
    get_watchlist_tickers,
    remove_watchlist_ticker,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddTickerRequest(BaseModel):
    ticker: str


def _enrich_ticker(ticker: str, price_cache, session_open_prices: dict) -> dict:
    """Return watchlist entry enriched with live price data."""
    update = price_cache.get(ticker)
    if update is None:
        return {"ticker": ticker, "price": None, "previous_price": None,
                "change": None, "change_percent": None, "direction": None,
                "daily_change": None, "daily_change_percent": None}

    open_price = session_open_prices.get(ticker, update.price)
    daily_change = round(update.price - open_price, 4)
    daily_change_pct = round((daily_change / open_price) * 100, 4) if open_price else 0.0

    return {
        "ticker": ticker,
        "price": update.price,
        "previous_price": update.previous_price,
        "change": update.change,
        "change_percent": update.change_percent,
        "direction": update.direction,
        "daily_change": daily_change,
        "daily_change_percent": daily_change_pct,
    }


@router.get("")
def get_watchlist(request: Request) -> list[dict]:
    price_cache = request.app.state.price_cache
    session_open_prices = request.app.state.session_open_prices
    tickers = get_watchlist_tickers()
    return [_enrich_ticker(t, price_cache, session_open_prices) for t in tickers]


@router.post("", status_code=201)
async def add_ticker(body: AddTickerRequest, request: Request) -> dict:
    ticker = body.ticker.upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker cannot be empty")

    added = add_watchlist_ticker(ticker)
    if not added:
        raise HTTPException(status_code=409, detail=f"{ticker} is already in your watchlist")

    # Register with market data source and record session open price
    market_source = request.app.state.market_source
    price_cache = request.app.state.price_cache
    session_open_prices = request.app.state.session_open_prices

    await market_source.add_ticker(ticker)

    # Record the open price for daily change calculation
    price = price_cache.get_price(ticker)
    if price is not None and ticker not in session_open_prices:
        session_open_prices[ticker] = price

    logger.info("Watchlist: added %s", ticker)
    return _enrich_ticker(ticker, price_cache, session_open_prices)


@router.delete("/{ticker}")
async def remove_ticker(ticker: str, request: Request) -> dict:
    ticker = ticker.upper().strip()
    removed = remove_watchlist_ticker(ticker)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{ticker} not found in watchlist")

    market_source = request.app.state.market_source
    await market_source.remove_ticker(ticker)

    # Clean up session open price tracking
    request.app.state.session_open_prices.pop(ticker, None)

    logger.info("Watchlist: removed %s", ticker)
    return {"ticker": ticker, "removed": True}
