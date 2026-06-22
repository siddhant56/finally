"""LiteLLM integration for the FinAlly AI assistant."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

from litellm import completion
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}


class TradeAction(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float


class WatchlistChange(BaseModel):
    ticker: str
    action: Literal["add", "remove"]


class LLMChatResponse(BaseModel):
    message: str
    trades: list[TradeAction] = []
    watchlist_changes: list[WatchlistChange] = []


_MOCK_RESPONSE = LLMChatResponse(
    message=(
        "I'm FinAlly, your AI trading assistant! I can analyze your portfolio, "
        "suggest and execute trades, and manage your watchlist. What would you like to do?"
    ),
    trades=[],
    watchlist_changes=[],
)


def _build_system_prompt(portfolio_context: dict) -> str:
    cash = portfolio_context["cash_balance"]
    total = portfolio_context["total_value"]
    positions = portfolio_context["positions"]
    watchlist = portfolio_context["watchlist"]

    pos_lines = "\n".join(
        f"  - {p['ticker']}: {p['quantity']} shares @ avg ${p['avg_cost']:.2f}, "
        f"current ${p['current_price']:.2f}, P&L ${p['unrealized_pnl']:+.2f} ({p['unrealized_pnl_percent']:+.1f}%)"
        for p in positions
    ) or "  (no open positions)"

    watch_lines = "\n".join(
        f"  - {w['ticker']}: ${w['price']:.2f}" if w.get("price") else f"  - {w['ticker']}: (price loading)"
        for w in watchlist
    ) or "  (empty)"

    return f"""You are FinAlly, an AI trading assistant for a simulated portfolio with virtual money.

Current Portfolio:
- Cash Balance: ${cash:,.2f}
- Total Portfolio Value: ${total:,.2f}

Open Positions:
{pos_lines}

Watchlist:
{watch_lines}

Guidelines:
- Be concise, direct, and data-driven. Respond in 1-3 sentences unless analysis is requested.
- Execute trades when the user asks — no confirmation needed (simulated money, zero risk).
- For buy orders, check that cash >= quantity * current_price.
- For sell orders, check that the user holds enough shares.
- Suggest tickers and watchlist additions proactively when relevant.
- Always respond with valid JSON. The "message" field is the only text shown to the user."""


async def call_llm(messages: list[dict], portfolio_context: dict) -> LLMChatResponse:
    """Call the LLM or return a deterministic mock (when LLM_MOCK=true)."""
    if os.environ.get("LLM_MOCK", "false").lower() == "true":
        return _MOCK_RESPONSE

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY not set — returning mock response")
        return _MOCK_RESPONSE

    system_msg = {"role": "system", "content": _build_system_prompt(portfolio_context)}
    full_messages = [system_msg] + messages

    def _call_sync() -> LLMChatResponse:
        response = completion(
            model=MODEL,
            messages=full_messages,
            response_format=LLMChatResponse,
            reasoning_effort="low",
            extra_body=EXTRA_BODY,
            api_key=api_key,
        )
        raw = response.choices[0].message.content
        return LLMChatResponse.model_validate_json(raw)

    try:
        return await asyncio.to_thread(_call_sync)
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        return LLMChatResponse(
            message="I'm having trouble connecting right now. Please try again in a moment.",
            trades=[],
            watchlist_changes=[],
        )
