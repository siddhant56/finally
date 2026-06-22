"""Portfolio and trade API endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db import (
    delete_position,
    get_cash_balance,
    get_portfolio_history,
    get_positions,
    insert_portfolio_snapshot,
    insert_trade,
    update_cash_balance,
    upsert_position,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class TradeRequest(BaseModel):
    ticker: str
    quantity: float
    side: str  # "buy" or "sell"


def _build_position_view(pos: dict, price_cache) -> dict:
    """Enrich a raw DB position row with live price and P&L."""
    ticker = pos["ticker"]
    quantity = pos["quantity"]
    avg_cost = pos["avg_cost"]

    current_price = price_cache.get_price(ticker) or avg_cost
    market_value = round(current_price * quantity, 2)
    cost_basis = round(avg_cost * quantity, 2)
    unrealized_pnl = round(market_value - cost_basis, 2)
    unrealized_pnl_pct = round((unrealized_pnl / cost_basis) * 100, 4) if cost_basis else 0.0

    return {
        "ticker": ticker,
        "quantity": quantity,
        "avg_cost": avg_cost,
        "current_price": current_price,
        "market_value": market_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_percent": unrealized_pnl_pct,
    }


@router.get("")
def get_portfolio(request: Request) -> dict:
    price_cache = request.app.state.price_cache
    raw_positions = get_positions()
    cash = get_cash_balance()

    positions = [_build_position_view(p, price_cache) for p in raw_positions]
    positions_value = sum(p["market_value"] for p in positions)
    total_value = round(cash + positions_value, 2)
    total_pnl = round(sum(p["unrealized_pnl"] for p in positions), 2)

    return {
        "cash_balance": cash,
        "total_value": total_value,
        "positions": positions,
        "total_unrealized_pnl": total_pnl,
    }


@router.post("/trade")
async def execute_trade(body: TradeRequest, request: Request) -> dict:
    ticker = body.ticker.upper().strip()
    side = body.side.lower().strip()
    quantity = body.quantity

    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be positive")

    price_cache = request.app.state.price_cache
    price = price_cache.get_price(ticker)
    if price is None:
        raise HTTPException(status_code=400, detail=f"Price not available for {ticker}. Add it to your watchlist first.")

    cash = get_cash_balance()
    raw_positions = get_positions()
    position_map = {p["ticker"]: p for p in raw_positions}

    if side == "buy":
        total_cost = round(price * quantity, 2)
        if total_cost > cash:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient cash. Need ${total_cost:,.2f}, have ${cash:,.2f}."
            )
        new_cash = round(cash - total_cost, 2)
        existing = position_map.get(ticker)
        if existing:
            old_qty = existing["quantity"]
            old_cost = existing["avg_cost"]
            new_qty = old_qty + quantity
            new_avg_cost = round((old_qty * old_cost + quantity * price) / new_qty, 4)
        else:
            new_qty = quantity
            new_avg_cost = round(price, 4)

        update_cash_balance(new_cash)
        upsert_position(ticker, new_qty, new_avg_cost)

    else:  # sell
        existing = position_map.get(ticker)
        if not existing or existing["quantity"] < quantity:
            held = existing["quantity"] if existing else 0
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient shares. Have {held}, trying to sell {quantity}."
            )
        proceeds = round(price * quantity, 2)
        new_cash = round(cash + proceeds, 2)
        new_qty = round(existing["quantity"] - quantity, 8)
        update_cash_balance(new_cash)
        if new_qty <= 1e-8:
            delete_position(ticker)
            new_qty = 0.0
        else:
            upsert_position(ticker, new_qty, existing["avg_cost"])

    trade_id = insert_trade(ticker, side, quantity, price)

    # Record portfolio snapshot after trade
    updated_positions = get_positions()
    price_cache_snapshot = price_cache.get_all()
    positions_value = sum(
        (price_cache_snapshot.get(p["ticker"]).price if price_cache_snapshot.get(p["ticker"]) else p["avg_cost"])
        * p["quantity"]
        for p in updated_positions
    )
    insert_portfolio_snapshot(round(new_cash + positions_value, 2))

    # Build enriched position view for response
    pos_row = {"ticker": ticker, "quantity": new_qty, "avg_cost": new_avg_cost if side == "buy" else (existing["avg_cost"] if new_qty > 0 else 0.0)}
    position_view = _build_position_view(pos_row, price_cache) if new_qty > 0 else None

    return {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "price": price,
        "cash_balance": new_cash,
        "position": position_view,
    }


@router.get("/history")
def get_history() -> dict:
    snapshots = get_portfolio_history()
    return {"snapshots": snapshots}


@router.get("/prices/{ticker}")
def get_ticker_price(ticker: str, request: Request) -> dict:
    """Get the current price for a single ticker (used by the trade bar)."""
    ticker = ticker.upper().strip()
    price_cache = request.app.state.price_cache
    update = price_cache.get(ticker)
    if update is None:
        raise HTTPException(status_code=404, detail=f"Price not available for {ticker}")
    return update.to_dict()
