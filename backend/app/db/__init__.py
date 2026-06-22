"""Database layer for FinAlly."""

from .database import (
    DEFAULT_WATCHLIST,
    add_watchlist_ticker,
    delete_position,
    get_cash_balance,
    get_chat_history,
    get_portfolio_history,
    get_positions,
    get_watchlist_tickers,
    init_db,
    insert_chat_message,
    insert_portfolio_snapshot,
    insert_trade,
    remove_watchlist_ticker,
    update_cash_balance,
    upsert_position,
)

__all__ = [
    "DEFAULT_WATCHLIST",
    "init_db",
    "get_cash_balance",
    "update_cash_balance",
    "get_watchlist_tickers",
    "add_watchlist_ticker",
    "remove_watchlist_ticker",
    "get_positions",
    "upsert_position",
    "delete_position",
    "insert_trade",
    "insert_portfolio_snapshot",
    "get_portfolio_history",
    "get_chat_history",
    "insert_chat_message",
]
