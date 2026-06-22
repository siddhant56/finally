"""Chat API endpoint with LLM integration."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db import (
    add_watchlist_ticker,
    delete_position,
    get_cash_balance,
    get_chat_history,
    get_positions,
    get_watchlist_tickers,
    insert_chat_message,
    insert_portfolio_snapshot,
    insert_trade,
    remove_watchlist_ticker,
    update_cash_balance,
    upsert_position,
)
from app.llm import call_llm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str


def _build_portfolio_context(price_cache) -> dict:
    """Assemble portfolio context for the LLM system prompt."""
    raw_positions = get_positions()
    cash = get_cash_balance()
    watchlist_tickers = get_watchlist_tickers()

    positions = []
    positions_value = 0.0
    for p in raw_positions:
        current_price = price_cache.get_price(p["ticker"]) or p["avg_cost"]
        market_value = current_price * p["quantity"]
        cost_basis = p["avg_cost"] * p["quantity"]
        unrealized_pnl = market_value - cost_basis
        pnl_pct = (unrealized_pnl / cost_basis * 100) if cost_basis else 0.0
        positions.append({
            "ticker": p["ticker"],
            "quantity": p["quantity"],
            "avg_cost": p["avg_cost"],
            "current_price": current_price,
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pnl_percent": round(pnl_pct, 2),
        })
        positions_value += market_value

    watchlist = []
    for t in watchlist_tickers:
        update = price_cache.get(t)
        watchlist.append({"ticker": t, "price": update.price if update else None})

    return {
        "cash_balance": cash,
        "total_value": round(cash + positions_value, 2),
        "positions": positions,
        "watchlist": watchlist,
    }


async def _execute_trade(ticker: str, side: str, quantity: float, price_cache) -> dict:
    """Execute a single trade. Returns result dict with success/error."""
    ticker = ticker.upper()
    price = price_cache.get_price(ticker)
    if price is None:
        return {"ticker": ticker, "side": side, "quantity": quantity, "success": False,
                "error": f"Price not available for {ticker}"}

    cash = get_cash_balance()
    raw_positions = get_positions()
    position_map = {p["ticker"]: p for p in raw_positions}

    if side == "buy":
        total_cost = round(price * quantity, 2)
        if total_cost > cash:
            return {"ticker": ticker, "side": side, "quantity": quantity, "success": False,
                    "error": f"Insufficient cash (need ${total_cost:,.2f}, have ${cash:,.2f})"}
        existing = position_map.get(ticker)
        new_cash = round(cash - total_cost, 2)
        if existing:
            old_qty = existing["quantity"]
            new_qty = old_qty + quantity
            new_avg = round((old_qty * existing["avg_cost"] + quantity * price) / new_qty, 4)
        else:
            new_qty = quantity
            new_avg = round(price, 4)
        update_cash_balance(new_cash)
        upsert_position(ticker, new_qty, new_avg)

    else:  # sell
        existing = position_map.get(ticker)
        if not existing or existing["quantity"] < quantity:
            held = existing["quantity"] if existing else 0
            return {"ticker": ticker, "side": side, "quantity": quantity, "success": False,
                    "error": f"Insufficient shares (have {held}, selling {quantity})"}
        proceeds = round(price * quantity, 2)
        new_cash = round(cash + proceeds, 2)
        new_qty = round(existing["quantity"] - quantity, 8)
        update_cash_balance(new_cash)
        if new_qty <= 1e-8:
            delete_position(ticker)
        else:
            upsert_position(ticker, new_qty, existing["avg_cost"])

    trade_id = insert_trade(ticker, side, quantity, price)
    return {"ticker": ticker, "side": side, "quantity": quantity, "price": price,
            "trade_id": trade_id, "success": True}


@router.post("")
async def chat(body: ChatRequest, request: Request) -> dict:
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    price_cache = request.app.state.price_cache
    market_source = request.app.state.market_source
    session_open_prices = request.app.state.session_open_prices

    # Save user message
    insert_chat_message("user", body.message)

    # Build context and history for LLM
    portfolio_context = _build_portfolio_context(price_cache)
    history = get_chat_history(limit=20)
    # Convert to LLM message format (exclude the message we just inserted)
    llm_messages = [{"role": m["role"], "content": m["content"]} for m in history[:-1]]
    llm_messages.append({"role": "user", "content": body.message})

    # Call LLM
    llm_response = await call_llm(llm_messages, portfolio_context)

    # Execute trades
    trades_executed = []
    for trade in llm_response.trades:
        result = await _execute_trade(trade.ticker, trade.side, trade.quantity, price_cache)
        trades_executed.append(result)

    # Record portfolio snapshot if any trades executed
    if any(r["success"] for r in trades_executed):
        raw_positions = get_positions()
        pv = sum(
            (price_cache.get_price(p["ticker"]) or p["avg_cost"]) * p["quantity"]
            for p in raw_positions
        )
        insert_portfolio_snapshot(round(get_cash_balance() + pv, 2))

    # Execute watchlist changes
    watchlist_changes_executed = []
    for wc in llm_response.watchlist_changes:
        ticker = wc.ticker.upper()
        if wc.action == "add":
            added = add_watchlist_ticker(ticker)
            if added:
                await market_source.add_ticker(ticker)
                price = price_cache.get_price(ticker)
                if price is not None and ticker not in session_open_prices:
                    session_open_prices[ticker] = price
            watchlist_changes_executed.append({"ticker": ticker, "action": "add", "success": added,
                                               "error": None if added else f"{ticker} already in watchlist"})
        else:  # remove
            removed = remove_watchlist_ticker(ticker)
            if removed:
                await market_source.remove_ticker(ticker)
                session_open_prices.pop(ticker, None)
            watchlist_changes_executed.append({"ticker": ticker, "action": "remove", "success": removed,
                                               "error": None if removed else f"{ticker} not in watchlist"})

    actions = {
        "trades": trades_executed,
        "watchlist_changes": watchlist_changes_executed,
    }

    msg_id = insert_chat_message("assistant", llm_response.message, actions=actions)

    return {
        "id": msg_id,
        "role": "assistant",
        "content": llm_response.message,
        "actions": actions,
    }


@router.get("/history")
def get_history() -> dict:
    return {"messages": get_chat_history(limit=50)}
