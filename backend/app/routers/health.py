"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(request: Request) -> dict:
    source = request.app.state.market_source
    tickers = source.get_tickers() if source else []
    return {
        "status": "ok",
        "market_source": type(source).__name__ if source else "none",
        "tracked_tickers": len(tickers),
    }
