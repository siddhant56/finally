# Backend — Developer Guide

## Project Setup

```bash
cd backend
uv sync --extra dev   # Install all dependencies including test/lint tools
```

## Market Data API

The market data subsystem lives in `app/market/`. Use these imports:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source
```

### Core Types

- **`PriceUpdate`** — Immutable dataclass: `ticker`, `price`, `previous_price`, `timestamp`, plus properties `change`, `change_percent`, `direction` ("up"/"down"/"flat"), and `to_dict()` for JSON serialization.

- **`PriceCache`** — Thread-safe in-memory store. Key methods:
  - `update(ticker, price, timestamp=None) -> PriceUpdate`
  - `get(ticker) -> PriceUpdate | None`
  - `get_price(ticker) -> float | None`
  - `get_all() -> dict[str, PriceUpdate]`
  - `remove(ticker)`
  - `version` property — monotonic counter, increments on every update (for SSE change detection)

- **`MarketDataSource`** — Abstract interface implemented by `SimulatorDataSource` and `MassiveDataSource`. Lifecycle: `start(tickers)` -> `add_ticker()` / `remove_ticker()` -> `stop()`.

- **`create_market_data_source(cache)`** — Factory. Returns `MassiveDataSource` if `MASSIVE_API_KEY` is set, otherwise `SimulatorDataSource`.

### SSE Streaming

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)  # Returns FastAPI APIRouter
# Endpoint: GET /api/stream/prices (text/event-stream)
```

### Seed Data

Default tickers: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX. Seed prices and per-ticker volatility/drift params are in `app/market/seed_prices.py`.

## Running Tests

```bash
uv run --extra dev pytest -v              # All tests
uv run --extra dev pytest --cov=app       # With coverage
uv run --extra dev ruff check app/ tests/ # Lint
```

## Demo

```bash
uv run market_data_demo.py   # Live terminal dashboard with simulated prices
```

---

## Running the Backend

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8000
```

The server reads `.env` from the project root automatically. Visit `http://localhost:8000/api/health` to verify.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | (required for chat) | OpenRouter key for LLM |
| `MASSIVE_API_KEY` | (empty) | Polygon.io key; if absent, uses GBM simulator |
| `DB_PATH` | `../db/finally.db` | Override SQLite path (Docker: `/app/db/finally.db`) |
| `LLM_MOCK` | `false` | Set `true` for deterministic mock LLM (tests/dev) |

## App Architecture

```
app/
├── main.py              FastAPI app + lifespan (startup/shutdown, snapshot task)
├── db/
│   ├── schema.sql       SQLite DDL (6 tables)
│   └── database.py      All DB query functions (auto-seeds on first run)
├── routers/
│   ├── health.py        GET /api/health
│   ├── watchlist.py     GET/POST /api/watchlist, DELETE /api/watchlist/{ticker}
│   ├── portfolio.py     GET /api/portfolio, POST /api/portfolio/trade,
│   │                    GET /api/portfolio/history, GET /api/portfolio/prices/{ticker}
│   └── chat.py          POST /api/chat, GET /api/chat/history
├── llm/
│   └── chat_llm.py      LiteLLM → OpenRouter/Cerebras, structured output
└── market/              (complete — see MARKET_DATA_SUMMARY.md)
```

### App State (via `request.app.state`)

| Key | Type | Description |
|-----|------|-------------|
| `price_cache` | `PriceCache` | Live price cache, read by all routes |
| `market_source` | `MarketDataSource` | Simulator or Massive; add/remove tickers dynamically |
| `session_open_prices` | `dict[str, float]` | First price seen per ticker (for daily change %) |

### Key Behaviors

- **Database**: lazily initialized on first startup; tables created + seeded if the SQLite file is absent
- **Portfolio snapshots**: recorded every 10 seconds by a background task, and immediately after every trade
- **Daily change %**: computed as `(current_price - session_open_price) / session_open_price * 100` where `session_open_price` is the first price seen at app startup
- **SSE stream**: dynamic — adding/removing watchlist tickers is reflected on the next SSE tick
- **LLM chat**: structured output (Pydantic) → auto-executes trades + watchlist changes; falls back to mock response if `OPENROUTER_API_KEY` is missing

### LLM Structured Output

```python
class LLMChatResponse(BaseModel):
    message: str                          # shown to user
    trades: list[TradeAction] = []        # auto-executed
    watchlist_changes: list[WatchlistChange] = []  # auto-executed
```
