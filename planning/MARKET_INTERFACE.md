# Market Data Interface — Unified Python API Design

This document describes the unified market data interface used in FinAlly's backend. The design decouples all price-consuming code from the underlying data source, allowing the simulator and the Massive API client to be swapped transparently via an environment variable.

---

## Design Goals

1. **Source agnosticism** — portfolio valuation, SSE streaming, and trade execution never know or care whether prices come from the simulator or the Massive API.
2. **Single producer, many consumers** — one background task writes to a shared `PriceCache`; all other code reads from the cache.
3. **Dynamic watchlist** — tickers can be added or removed at runtime without restarting the data source.
4. **Async-friendly** — lifecycle methods are async; the synchronous Massive REST client runs in a thread pool to avoid blocking the event loop.

---

## Architecture

```
Environment Variable
  MASSIVE_API_KEY set? ──Yes──▶ MassiveDataSource  ──▶┐
                │                                      │
                └──No───▶ SimulatorDataSource ─────────┤
                                                       ▼
                                                 PriceCache (thread-safe)
                                                       │
                                    ┌──────────────────┼──────────────────┐
                                    ▼                  ▼                  ▼
                             SSE stream          Portfolio           Trade
                             /api/stream/prices  valuation           execution
```

### Module layout

```
backend/app/market/
├── __init__.py         # Public exports
├── models.py           # PriceUpdate dataclass
├── interface.py        # MarketDataSource ABC
├── cache.py            # PriceCache (thread-safe dict + version counter)
├── seed_prices.py      # Seed prices and GBM parameters
├── simulator.py        # GBMSimulator + SimulatorDataSource
├── massive_client.py   # MassiveDataSource (Polygon.io REST poller)
├── factory.py          # create_market_data_source() — env-based selection
└── stream.py           # FastAPI SSE endpoint factory
```

---

## Core Data Model — `PriceUpdate`

Defined in `models.py`. Frozen (immutable) dataclass; safe to share across threads.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float      # Unix seconds

    @property
    def change(self) -> float: ...         # price - previous_price
    @property
    def change_percent(self) -> float: ... # (change / previous_price) * 100
    @property
    def direction(self) -> str: ...        # 'up', 'down', or 'flat'

    def to_dict(self) -> dict: ...         # JSON-serializable for SSE/API
```

---

## Abstract Interface — `MarketDataSource`

Defined in `interface.py`. Every data source must implement all five methods.

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Start the background update task for the given ticker list."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. Effective on the next update cycle."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from PriceCache immediately."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of tracked tickers (synchronous)."""
```

**Lifecycle contract:**
- `start()` is called once at application startup with the initial watchlist.
- `stop()` is called once at application shutdown.
- `add_ticker()` / `remove_ticker()` may be called at any time after `start()`.

---

## Price Cache — `PriceCache`

Defined in `cache.py`. The single shared state store between producers and consumers.

```python
class PriceCache:
    def update(self, ticker: str, price: float,
               timestamp: float | None = None) -> PriceUpdate: ...
    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_all(self) -> dict[str, PriceUpdate]: ...   # shallow copy
    def get_price(self, ticker: str) -> float | None: ...
    def remove(self, ticker: str) -> None: ...

    @property
    def version(self) -> int: ...  # Monotonic counter; bumped on every update
```

**Thread safety:** A `threading.Lock` protects all reads and writes. The version counter is used by the SSE endpoint for efficient change detection (compare `last_seen_version` before sending an event).

---

## Implementations

### SimulatorDataSource

Wraps a `GBMSimulator` in an asyncio background task. Updates the cache every 500ms. Full details in `MARKET_SIMULATOR.md`.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,    # seconds between ticks
        event_probability: float = 0.001, # random shock probability per tick
    ) -> None: ...
```

### MassiveDataSource

Polls `GET /v2/snapshot/locale/us/markets/stocks/tickers` for all watched tickers in a single API call. Runs the synchronous Massive REST client in `asyncio.to_thread()` to avoid blocking.

```python
class MassiveDataSource(MarketDataSource):
    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,  # seconds; 15s = safe for free tier (5 req/min)
    ) -> None: ...
```

**Polling strategy:**
- An immediate poll on `start()` fills the cache before the first SSE tick.
- Subsequent polls happen every `poll_interval` seconds.
- Between polls, the SSE stream re-sends cached prices (same price, no direction change). This keeps the "live" feel even when underlying data is slower.
- If a poll fails (network error, 429, etc.), the error is logged and the loop continues — the cache retains stale-but-valid data.

---

## Factory — `create_market_data_source()`

Defined in `factory.py`. The only place where the environment variable is read.

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """
    Returns MassiveDataSource if MASSIVE_API_KEY is set and non-empty.
    Returns SimulatorDataSource otherwise.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

---

## Usage — Application Startup and Shutdown

```python
from app.market import PriceCache, create_market_data_source

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]

# --- FastAPI lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(DEFAULT_TICKERS)

    app.state.price_cache = cache
    app.state.market_source = source

    yield  # app runs here

    await source.stop()
```

---

## Usage — Reading Prices (Consumers)

All price consumers read from `PriceCache`. They never call the data source directly.

```python
# Single ticker
update: PriceUpdate | None = cache.get("AAPL")
if update:
    print(f"${update.price:.2f} ({update.direction})")

# All tickers (for SSE broadcast)
all_prices: dict[str, PriceUpdate] = cache.get_all()

# Just the float (for trade execution)
price: float | None = cache.get_price("TSLA")
if price is None:
    raise ValueError("TSLA not in price cache")

# Version-based change detection (SSE endpoint)
last_version = 0
while True:
    if cache.version != last_version:
        last_version = cache.version
        yield cache.get_all()   # send SSE event
    await asyncio.sleep(0.1)
```

---

## Usage — Dynamic Watchlist

```python
# Add a ticker (watchlist API handler)
async def add_to_watchlist(ticker: str):
    await source.add_ticker(ticker)          # updates simulator / Massive poller
    # cache will have a price on the next update cycle

# Remove a ticker (watchlist API handler)
async def remove_from_watchlist(ticker: str):
    await source.remove_ticker(ticker)       # removes from cache immediately
```

---

## Public Exports (`__init__.py`)

```python
from .models import PriceUpdate
from .cache import PriceCache
from .interface import MarketDataSource
from .factory import create_market_data_source

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
]
```

Downstream modules import from `app.market` only — never from submodules directly.

```python
from app.market import PriceCache, create_market_data_source, PriceUpdate
```

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `PriceCache` as single point of truth | Decouples producers from consumers; no direct coupling between data source and SSE/portfolio code |
| Frozen `PriceUpdate` dataclass | Immutable snapshots are safe to share across threads without copying |
| Version counter on `PriceCache` | Cheap change detection for SSE — one integer comparison per tick instead of diffing price dicts |
| `asyncio.to_thread()` for Massive | The `massive` Python client is synchronous; wrapping in a thread avoids blocking the FastAPI event loop |
| Immediate first poll in `MassiveDataSource.start()` | Ensures SSE has real data within milliseconds of startup, not 15 seconds later |
| Strategy pattern (ABC + factory) | The data source can be swapped for testing, mocking, or a future WebSocket-based source with zero downstream changes |
