"""SQLite database access layer."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# DB_PATH: override with env var (e.g., in Docker: /app/db/finally.db)
_DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent.parent.parent / "db" / "finally.db"
)
DB_PATH = os.environ.get("DB_PATH", _DEFAULT_DB_PATH)

DEFAULT_WATCHLIST = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]

USER_ID = "default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@contextmanager
def get_db():
    """Yield a SQLite connection. Auto-commits on success, rolls back on error."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create schema and seed default data if the database is fresh."""
    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()

    with get_db() as conn:
        conn.executescript(schema_sql)

        # Seed default user
        conn.execute(
            "INSERT OR IGNORE INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
            (USER_ID, 10000.0, _now()),
        )

        # Seed default watchlist only if empty
        count = conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE user_id = ?", (USER_ID,)
        ).fetchone()[0]
        if count == 0:
            for ticker in DEFAULT_WATCHLIST:
                conn.execute(
                    "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
                    (_new_id(), USER_ID, ticker, _now()),
                )


# --- User / Cash ---

def get_cash_balance() -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT cash_balance FROM users_profile WHERE id = ?", (USER_ID,)
        ).fetchone()
        return row["cash_balance"] if row else 10000.0


def update_cash_balance(new_balance: float) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users_profile SET cash_balance = ? WHERE id = ?",
            (new_balance, USER_ID),
        )


# --- Watchlist ---

def get_watchlist_tickers() -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY added_at",
            (USER_ID,),
        ).fetchall()
        return [r["ticker"] for r in rows]


def add_watchlist_ticker(ticker: str) -> bool:
    """Add a ticker to the watchlist. Returns True if added, False if already present."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
                (_new_id(), USER_ID, ticker.upper(), _now()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def remove_watchlist_ticker(ticker: str) -> bool:
    """Remove a ticker from the watchlist. Returns True if removed, False if not found."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
            (USER_ID, ticker.upper()),
        )
        return cursor.rowcount > 0


# --- Positions ---

def get_positions() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = ? AND quantity > 0 ORDER BY ticker",
            (USER_ID,),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_position(ticker: str, quantity: float, avg_cost: float) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, ticker) DO UPDATE SET
                   quantity = excluded.quantity,
                   avg_cost = excluded.avg_cost,
                   updated_at = excluded.updated_at""",
            (_new_id(), USER_ID, ticker, quantity, avg_cost, _now()),
        )


def delete_position(ticker: str) -> None:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
            (USER_ID, ticker),
        )


# --- Trades ---

def insert_trade(ticker: str, side: str, quantity: float, price: float) -> str:
    """Insert a trade record. Returns the trade ID."""
    trade_id = _new_id()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_id, USER_ID, ticker, side, quantity, price, _now()),
        )
    return trade_id


# --- Portfolio Snapshots ---

def insert_portfolio_snapshot(total_value: float) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) VALUES (?, ?, ?, ?)",
            (_new_id(), USER_ID, total_value, _now()),
        )


def get_portfolio_history(limit: int = 500) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT total_value, recorded_at FROM portfolio_snapshots WHERE user_id = ? ORDER BY recorded_at DESC LIMIT ?",
            (USER_ID, limit),
        ).fetchall()
        # Return in ascending order for the chart
        return [{"total_value": r["total_value"], "recorded_at": r["recorded_at"]} for r in reversed(rows)]


# --- Chat Messages ---

def get_chat_history(limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, role, content, actions, created_at FROM chat_messages WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (USER_ID, limit),
        ).fetchall()
        result = []
        for r in reversed(rows):
            msg = {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"],
            }
            if r["actions"]:
                msg["actions"] = json.loads(r["actions"])
            result.append(msg)
        return result


def insert_chat_message(role: str, content: str, actions: dict | None = None) -> str:
    """Insert a chat message. Returns the message ID."""
    msg_id = _new_id()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, USER_ID, role, content, json.dumps(actions) if actions else None, _now()),
        )
    return msg_id
