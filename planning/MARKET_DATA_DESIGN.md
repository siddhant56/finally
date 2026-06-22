# Market Data Backend — Complete Design Document

This document is the authoritative implementation reference for FinAlly's market data subsystem. It covers the unified interface, the GBM simulator, the Massive (Polygon.io) API client, the SSE streaming endpoint, and how everything wires into FastAPI. All code shown here reflects the actual implementation in `backend/app/market/`.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Module Layout](#2-module-layout)
3. [Data Model — `PriceUpdate`](#3-data-model--priceupdate)
4. [Price Cache — `PriceCache`](#4-price-cache--pricecache)
5. [Unified Interface — `MarketDataSource`](#5-unified-interface--marketdatasource)
6. [GBM Simulator](#6-gbm-simulator)
7. [Massive API Client](#7-massive-api-client)
8. [Factory — Selecting a Source at Runtime](#8-factory--selecting-a-source-at-runtime)
9. [SSE Streaming Endpoint](#9-sse-streaming-endpoint)
10. [FastAPI Integration (Lifespan)](#10-fastapi-integration-lifespan)
11. [Dynamic Watchlist Operations](#11-dynamic-watchlist-operations)
12. [Consuming Prices in API Routes](#12-consuming-prices-in-api-routes)
13. [Testing Strategy](#13-testing-strategy)
14. [Design Decision Log](#14-design-decision-log)

---

## 1. Architecture Overview

```
Environment variable
  MASSIVE_API_KEY set? ──Yes──▶ MassiveDataSource  ─────▶┐
                  │             (REST poller,              │
                  │              15s interval)             │
                  └──No───▶ SimulatorDataSource ──────────▶┤
                             (GBM, 500ms ticks)            │
                                                           ▼
                                                   PriceCache  (thread-safe, in-memory)
                                                     version: int  ← bumped on every write
                                                           │
                             ┌─────────────────────────────┼──────────────────────┐
                             ▼                             ▼                      ▼
                    SSE /api/stream/prices       Portfolio valuation        Trade execution
                    (version change detection)   (cache.get_price())       (cache.get_price())
```

**Key invariants:**
- There is exactly **one** data source running at any time (either simulator or Massive — never both).
- All price consumers read exclusively from `PriceCache`. They never call the data source directly.
- The cache is the single source of truth for the current price of every ticker.

---

## 2. Module Layout

```
backend/app/market/
├── __init__.py          Public exports (PriceUpdate, PriceCache, MarketDataSource,
│                        create_market_data_source)
├── models.py            PriceUpdate frozen dataclass
├── interface.py         MarketDataSource abstract base class
├── cache.py             PriceCache — thread-safe dict + version counter
├── seed_prices.py       Seed prices, per-ticker GBM params, correlation groups
├── simulator.py         GBMSimulator (math) + SimulatorDataSource (async wrapper)
├── massive_client.py    MassiveDataSource — Polygon.io REST poller
├── factory.py           create_market_data_source() — env-based selection
└── stream.py            FastAPI SSE router factory
```

Downstream modules import only from `app.market` — never from submodules:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source
```

---

## 3. Data Model — `PriceUpdate`

**File:** `backend/app/market/models.py`

A frozen (immutable) dataclass that represents a single price snapshot for one ticker at one point in time. Immutable so it is safe to pass across threads without copying.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change: price - previous_price."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous_price."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict — used by SSE and REST endpoints."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

**Wire format example** (what the frontend receives over SSE):

```json
{
  "ticker": "AAPL",
  "price": 191.34,
  "previous_price": 191.21,
  "timestamp": 1719000000.123,
  "change": 0.13,
  "change_percent": 0.0679,
  "direction": "up"
}
```

---

## 4. Price Cache — `PriceCache`

**File:** `backend/app/market/cache.py`

The shared state store between the data source (producer) and all consumers (SSE, portfolio, trades). Protected by a single `threading.Lock`.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every write; used for SSE change detection

    def update(
        self, ticker: str, price: float, timestamp: float | None = None
    ) -> PriceUpdate:
        """Record a new price. Returns the PriceUpdate created.

        On the first write for a ticker, previous_price == price (direction='flat').
        On subsequent writes, previous_price is the last known price.
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices — returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: return just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker — called when removed from watchlist."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonically increasing counter. Incremented on every update."""
        return self._version
```

### Version counter

The `version` property is the key to efficient SSE. The SSE generator stores `last_version` and compares it to `cache.version` each loop iteration:

```python
last_version = -1
while True:
    current_version = cache.version
    if current_version != last_version:
        last_version = current_version
        yield cache.get_all()   # new data available — send it
    await asyncio.sleep(0.1)
```

This avoids serializing identical payloads 5× per second when the cache hasn't changed (e.g., between Massive API polls).

---

## 5. Unified Interface — `MarketDataSource`

**File:** `backend/app/market/interface.py`

Abstract base class that every data source must implement. The five methods form the complete contract.

```python
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])   # once at startup
        # ... app running ...
        await source.add_ticker("TSLA")               # dynamic watchlist
        await source.remove_ticker("GOOGL")
        # ... shutting down ...
        await source.stop()                           # once at shutdown
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Called once. Starts a background task."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from PriceCache immediately."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return current tracked tickers. Synchronous."""
```

**Lifecycle contract:**
- `start()` — called once during FastAPI lifespan startup with the initial watchlist from the DB.
- `stop()` — called once during lifespan shutdown.
- `add_ticker()` / `remove_ticker()` — called by the watchlist API routes at any time after `start()`.
- `get_tickers()` — may be called at any time, synchronously.

---

## 6. GBM Simulator

**File:** `backend/app/market/simulator.py` and `backend/app/market/seed_prices.py`

### 6.1 Seed Data

**File:** `backend/app/market/seed_prices.py`

Starting prices and per-ticker GBM parameters. `sigma` is annualized volatility; `mu` is annualized expected return (drift). TSLA gets high sigma (volatile); MSFT and V get low sigma (stable). JPM and V are grouped as "finance"; everyone else is "tech".

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT":  420.00,
    "AMZN":  185.00,
    "TSLA":  250.00,
    "NVDA":  800.00,
    "META":  500.00,
    "JPM":   195.00,
    "V":     280.00,
    "NFLX":  600.00,
}

TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},   # Lower vol (mature)
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High vol, lower drift
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High vol, strong upward drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR   = 0.3
TSLA_CORR          = 0.3   # TSLA is nominally tech but behaves independently
```

Tickers not in `SEED_PRICES` get a random seed between $50–$300. Tickers not in `TICKER_PARAMS` get `DEFAULT_PARAMS`.

### 6.2 The GBM Formula

Each tick, every price is updated using the closed-form solution to Geometric Brownian Motion:

```
S(t+dt) = S(t) × exp((μ - σ²/2) × dt + σ × √dt × Z)
```

Where:
- `μ` — annualized drift (expected return, e.g. 0.05 = 5%/year)
- `σ` — annualized volatility (e.g. 0.25 = 25%/year)
- `dt` — time step as a fraction of a trading year (500ms / 5,896,800s/year ≈ 8.48e-8)
- `Z` — correlated standard normal random variable

The exponential form guarantees prices are always positive. With `dt ≈ 8.48e-8`, moves per tick are sub-cent, accumulating naturally over time.

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.48e-8

# Per-tick price update:
drift     = (mu - 0.5 * sigma**2) * dt
diffusion = sigma * math.sqrt(dt) * z_correlated[i]
new_price = old_price * math.exp(drift + diffusion)
```

### 6.3 Correlated Moves via Cholesky Decomposition

Stocks in the same sector tend to move together. The simulator captures this with a Cholesky decomposition of a correlation matrix.

**Step 1 — Build the correlation matrix:**

```python
n = len(tickers)
corr = np.eye(n)
for i in range(n):
    for j in range(i + 1, n):
        rho = _pairwise_correlation(tickers[i], tickers[j])
        corr[i, j] = rho
        corr[j, i] = rho
```

`_pairwise_correlation` returns:
- 0.6 if both are tech stocks (not TSLA)
- 0.5 if both are finance stocks
- 0.3 otherwise (cross-sector, TSLA, or unknown tickers)

**Step 2 — Decompose once, reuse every tick:**

```python
# Computed in _rebuild_cholesky() — O(n^3), only runs when tickers change
cholesky = np.linalg.cholesky(corr)  # L such that L @ L.T == corr
```

**Step 3 — Each tick, produce correlated draws:**

```python
z_independent = np.random.standard_normal(n)  # n iid N(0,1)
z_correlated  = cholesky @ z_independent       # now correlated per corr matrix
```

The result: if AAPL's draw is positive, MSFT and GOOGL are more likely to be positive too — but not always. TSLA's draw is only weakly correlated (0.3) with anyone.

### 6.4 Random Shock Events

```python
EVENT_PROBABILITY = 0.001  # 0.1% per tick per ticker

# Applied after the GBM step:
if random.random() < self._event_prob:
    magnitude = random.uniform(0.02, 0.05)   # 2-5% sudden move
    direction = random.choice([-1, 1])
    price *= (1 + magnitude * direction)
```

With 10 tickers at 2 ticks/second: expected events per second = `10 × 2 × 0.001 = 0.02`, or roughly one event every 50 seconds. This creates the occasional dramatic spike or crash that makes the demo visually interesting.

### 6.5 `GBMSimulator` — Pure Math Class

Responsible only for price math. No asyncio, no cache, no FastAPI.

```python
class GBMSimulator:
    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        # Internal state
        self._tickers: list[str] = []
        self._prices:  dict[str, float] = {}
        self._params:  dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None
        self._dt = dt
        self._event_prob = event_probability

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all prices by one tick. Returns {ticker: new_price}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated  = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift     = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)
```

`_rebuild_cholesky()` is only called when tickers are added or removed — not on every tick. Per-tick cost is one matrix-vector multiply (O(n²) for n < 50 — microseconds).

### 6.6 `SimulatorDataSource` — Async Wrapper

Implements `MarketDataSource`. Owns the asyncio background task that calls `GBMSimulator.step()` every 500ms and writes to `PriceCache`.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache immediately so SSE has data before the first tick completes
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)  # available immediately

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

**Tick sequence (every 500ms):**

```
1. asyncio.sleep(0.5) completes
2. _sim.step() called
   a. np.random.standard_normal(n)         → n independent draws
   b. cholesky @ z_independent             → n correlated draws
   c. For each ticker:
      - GBM formula applied
      - 0.1% chance: random shock (2-5%)
      - Round to 2 decimal places
   d. Returns {ticker: price} dict
3. For each (ticker, price):
   cache.update(ticker, price)
   → creates PriceUpdate with previous_price from prior cache entry
   → increments cache.version
4. SSE endpoint detects version change on next poll → sends event
5. asyncio.sleep(0.5) begins again
```

---

## 7. Massive API Client

**File:** `backend/app/market/massive_client.py`

Polls `GET /v2/snapshot/locale/us/markets/stocks/tickers` via the `massive` Python client (Polygon.io). The synchronous REST client runs in `asyncio.to_thread()` to avoid blocking the event loop.

### 7.1 Full Implementation

```python
import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """Polls Massive (Polygon.io) REST API for real stock prices.

    Rate limits:
      Free:    5 req/min → poll every 15s (safe margin)
      Starter: unlimited → poll every 5-10s
      Advanced: unlimited → poll every 2-5s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()   # Fill cache immediately — don't wait 15s
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            # Price will appear on the next poll cycle

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    ts    = snap.last_trade.timestamp / 1000.0  # ms → seconds
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=ts)
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s",
                                   getattr(snap, "ticker", "???"), e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Keep running — cache retains last known prices

    def _fetch_snapshots(self) -> list:
        """Runs in a thread — synchronous Massive REST call."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### 7.2 Behavior Between Polls

Between polls (up to 15 seconds), the SSE stream continues to send the same cached prices on every tick. The `direction` will be `"flat"` (same price twice in a row). This is intentional: it maintains the "live" connection feel even when actual market data is stale. The frontend's flash animation is triggered only when `direction != 'flat'`.

### 7.3 Error Handling

| HTTP Status | Cause | Behavior |
|-------------|-------|----------|
| `401 Unauthorized` | Bad API key | Logged; loop continues with stale cache |
| `403 Forbidden` | Feature not on plan | Logged; loop continues |
| `404 Not Found` | Unknown ticker | Snapshot skipped; others succeed |
| `429 Too Many Requests` | Rate limit | Logged; next poll at next interval |
| `5xx` | Server error | Logged; next poll at next interval |
| Network error | Timeout / DNS | Logged; next poll at next interval |

The poll loop never crashes the application — it logs and retries.

### 7.4 API Response Parsing

The Massive snapshot for a single ticker looks like:

```json
{
  "ticker": "AAPL",
  "lastTrade": {
    "p": 190.85,
    "s": 100,
    "t": 1700000000123456789
  },
  "todaysChange": 1.85,
  "todaysChangePerc": 0.98
}
```

Via the Python client, accessed as:
```python
snap.ticker            # "AAPL"
snap.last_trade.price  # 190.85
snap.last_trade.timestamp  # Unix nanoseconds (divide by 1000 to get seconds)
```

---

## 8. Factory — Selecting a Source at Runtime

**File:** `backend/app/market/factory.py`

The only place in the codebase where `MASSIVE_API_KEY` is read. Everything else is agnostic to which source is active.

```python
import os
import logging

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Return a MassiveDataSource if MASSIVE_API_KEY is set, else SimulatorDataSource.

    The returned source is unstarted. Call await source.start(tickers) before use.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

To force the simulator even with an API key set (useful in tests):

```python
from app.market import PriceCache
from app.market.simulator import SimulatorDataSource

cache = PriceCache()
source = SimulatorDataSource(price_cache=cache)
await source.start(["AAPL", "TSLA"])
```

---

## 9. SSE Streaming Endpoint

**File:** `backend/app/market/stream.py`

The SSE endpoint pushes all cached prices to every connected browser client. The push cadence matches the simulator's tick rate (~500ms).

### 9.1 Router Factory

```python
import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Factory: inject PriceCache into the SSE endpoint without a global."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",   # Prevent nginx from buffering SSE
            },
        )

    return router
```

### 9.2 Event Generator

```python
async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Yields SSE events while the client is connected."""

    # Instruct the browser to reconnect after 1s if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1

    try:
        while True:
            if await request.is_disconnected():
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
```

### 9.3 SSE Wire Format

Each event is a single line prefixed with `data: `, terminated by two newlines:

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 191.34, "previous_price": 191.21, "timestamp": 1719000000.123, "change": 0.13, "change_percent": 0.068, "direction": "up"}, "GOOGL": {"ticker": "GOOGL", ...}, ...}

data: {"AAPL": {...}, "GOOGL": {...}, ...}

```

### 9.4 Frontend Usage (EventSource)

```typescript
const es = new EventSource('/api/stream/prices');

es.onmessage = (event) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(event.data);
  for (const [ticker, update] of Object.entries(prices)) {
    dispatch(updatePrice(update));
    if (update.direction !== 'flat') {
      flashTicker(ticker, update.direction);  // trigger CSS animation
    }
  }
};

es.onerror = () => {
  // EventSource reconnects automatically after the retry: 1000 directive
  setConnectionStatus('reconnecting');
};
```

### 9.5 Version-Based Change Detection

The `version` counter means the SSE generator only serializes and sends data when something actually changed. With the Massive API (15s poll), this prevents 30 identical events between polls:

```
tick 1: version 5 → same as last_version 5 → skip
tick 2: version 5 → skip
...
tick 30: version 6 → send event (Massive poll just returned new data)
```

With the simulator (500ms ticks), the version advances on every tick, so every interval produces an event.

---

## 10. FastAPI Integration (Lifespan)

**File:** `backend/app/main.py` (to be created by the Backend API agent)

The market data system integrates into FastAPI via a lifespan context manager. App state carries both the cache and the source so API routes can access them via `request.app.state`.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source
from app.market.stream import create_stream_router

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(DEFAULT_TICKERS)

    app.state.price_cache = cache
    app.state.market_source = source

    yield  # Application runs here

    # --- Shutdown ---
    await source.stop()


app = FastAPI(lifespan=lifespan)

# Register SSE router (inject cache reference)
app.include_router(create_stream_router(app.state.price_cache))
```

> **Note:** `app.state.price_cache` is not accessible at module load time (before lifespan runs). The `create_stream_router` call should be moved inside the lifespan, or the router should pull the cache from `request.app.state` at request time. The cleanest pattern:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(DEFAULT_TICKERS)

    app.state.price_cache = cache
    app.state.market_source = source

    # Register the stream router after state is set
    app.include_router(create_stream_router(cache))

    yield

    await source.stop()
```

---

## 11. Dynamic Watchlist Operations

The watchlist API routes call `add_ticker` and `remove_ticker` on the source after retrieving it from app state. The source updates both its internal ticker list and the cache atomically (or as close to atomic as the async model allows).

```python
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddTickerRequest(BaseModel):
    ticker: str


@router.post("")
async def add_ticker(body: AddTickerRequest, request: Request):
    ticker = body.ticker.upper().strip()
    source = request.app.state.market_source
    cache  = request.app.state.price_cache

    await source.add_ticker(ticker)
    # Price may be None for the first milliseconds after add (before next tick)
    price = cache.get_price(ticker)
    return {"ticker": ticker, "price": price}


@router.delete("/{ticker}")
async def remove_ticker(ticker: str, request: Request):
    ticker = ticker.upper().strip()
    source = request.app.state.market_source
    await source.remove_ticker(ticker)
    return {"ticker": ticker, "removed": True}
```

**What happens on `add_ticker("PYPL")`:**

1. `source.add_ticker("PYPL")` is called.
2. **Simulator path:** `GBMSimulator.add_ticker("PYPL")` runs — looks up seed price (random $50-300 for unknown ticker), assigns `DEFAULT_PARAMS`, rebuilds Cholesky. Then `cache.update("PYPL", seed_price)` is called immediately. PYPL is live in the SSE stream within milliseconds.
3. **Massive path:** `"PYPL"` is appended to `self._tickers`. It will appear in the next poll (up to `poll_interval` seconds later). Until then, `cache.get_price("PYPL")` returns `None`.

**What happens on `remove_ticker("AAPL")`:**

1. `source.remove_ticker("AAPL")` is called.
2. Both paths: `cache.remove("AAPL")` is called immediately. AAPL disappears from the SSE stream on the next tick.
3. Simulator: `GBMSimulator.remove_ticker("AAPL")` removes AAPL from the internal list and rebuilds Cholesky.
4. Massive: AAPL is removed from `self._tickers`; it won't be included in the next poll.

---

## 12. Consuming Prices in API Routes

All price consumers read from `PriceCache`. They never touch the data source.

### Portfolio valuation

```python
@router.get("/portfolio")
async def get_portfolio(request: Request):
    cache = request.app.state.price_cache
    # Load positions from DB, then price each one:
    for position in positions:
        current_price = cache.get_price(position.ticker)
        if current_price is None:
            current_price = position.avg_cost  # fallback: use cost basis
        unrealized_pnl = (current_price - position.avg_cost) * position.quantity
```

### Trade execution

```python
@router.post("/portfolio/trade")
async def execute_trade(body: TradeRequest, request: Request):
    cache = request.app.state.price_cache
    price = cache.get_price(body.ticker)
    if price is None:
        raise HTTPException(400, f"No price available for {body.ticker}")
    # ... proceed with trade at price ...
```

### Watchlist endpoint

```python
@router.get("/watchlist")
async def get_watchlist(request: Request):
    cache = request.app.state.price_cache
    # Load tickers from DB, attach latest prices:
    result = []
    for ticker in db_tickers:
        update = cache.get(ticker)
        result.append({
            "ticker": ticker,
            "price": update.price if update else None,
            "change_percent": update.change_percent if update else None,
            "direction": update.direction if update else None,
        })
    return result
```

**Fallback for missing prices:** A ticker just added to the watchlist may not yet have a price in the cache (especially with the Massive API). API routes should handle `None` prices gracefully and not crash.

---

## 13. Testing Strategy

**Test files:** `backend/tests/market/`

### 13.1 Unit Tests

**`test_models.py`** — PriceUpdate correctness:
```python
def test_direction_up():
    u = PriceUpdate("AAPL", price=191.0, previous_price=190.0)
    assert u.direction == "up"
    assert u.change == 1.0
    assert round(u.change_percent, 4) == 0.5263

def test_to_dict_keys():
    u = PriceUpdate("AAPL", 190.0, 190.0, 1234567890.0)
    d = u.to_dict()
    assert set(d.keys()) == {"ticker", "price", "previous_price", "timestamp",
                              "change", "change_percent", "direction"}

def test_frozen():
    u = PriceUpdate("AAPL", 190.0, 190.0)
    with pytest.raises(FrozenInstanceError):
        u.price = 200.0
```

**`test_cache.py`** — thread safety and version counter:
```python
def test_version_increments_on_each_update():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.0)
    assert cache.version == v0 + 1
    cache.update("AAPL", 191.0)
    assert cache.version == v0 + 2

def test_previous_price_tracks_correctly():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    u = cache.update("AAPL", 191.0)
    assert u.previous_price == 190.0
    assert u.direction == "up"

def test_concurrent_writes_are_safe():
    cache = PriceCache()
    import threading
    def writer():
        for i in range(1000):
            cache.update("AAPL", float(i))
    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    # No exception = thread safety holds
```

**`test_simulator.py`** — GBM correctness:
```python
def test_prices_always_positive():
    sim = GBMSimulator(["AAPL", "TSLA", "NVDA"])
    for _ in range(10000):
        prices = sim.step()
        assert all(p > 0 for p in prices.values())

def test_per_tick_move_is_small():
    sim = GBMSimulator(["AAPL"], event_probability=0.0)  # disable shocks
    for _ in range(100):
        old = sim.get_price("AAPL")
        prices = sim.step()
        new = prices["AAPL"]
        assert abs(new - old) / old < 0.01  # < 1% per tick

def test_shock_events_fire_at_probability_1():
    sim = GBMSimulator(["AAPL"], event_probability=1.0)
    old = sim.get_price("AAPL")
    prices = sim.step()
    # With p=1.0, a shock always fires — price changes more than normal GBM
    assert abs(prices["AAPL"] - old) / old > 0.015  # > 1.5% (2-5% shocks)

def test_add_remove_ticker_rebuilds_cholesky():
    sim = GBMSimulator(["AAPL", "GOOGL"])
    sim.add_ticker("TSLA")
    assert "TSLA" in sim.get_tickers()
    prices = sim.step()
    assert "TSLA" in prices
    sim.remove_ticker("TSLA")
    assert "TSLA" not in sim.get_tickers()
    prices = sim.step()
    assert "TSLA" not in prices
```

**`test_factory.py`** — env-based selection:
```python
def test_returns_simulator_without_api_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    source = create_market_data_source(PriceCache())
    assert isinstance(source, SimulatorDataSource)

def test_returns_massive_with_api_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    source = create_market_data_source(PriceCache())
    assert isinstance(source, MassiveDataSource)

def test_returns_simulator_when_key_is_empty_string(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "   ")
    source = create_market_data_source(PriceCache())
    assert isinstance(source, SimulatorDataSource)
```

**`test_massive.py`** — mock HTTP calls:
```python
@pytest.fixture
def mock_source(monkeypatch):
    cache = PriceCache()
    source = MassiveDataSource(api_key="fake", price_cache=cache)
    source._client = MagicMock()
    return source, cache

def test_poll_updates_cache(mock_source):
    source, cache = mock_source
    mock_snap = MagicMock()
    mock_snap.ticker = "AAPL"
    mock_snap.last_trade.price = 190.85
    mock_snap.last_trade.timestamp = 1700000000000  # ms
    source._client.get_snapshot_all.return_value = [mock_snap]
    source._tickers = ["AAPL"]
    asyncio.get_event_loop().run_until_complete(source._poll_once())
    assert cache.get_price("AAPL") == 190.85

def test_poll_failure_does_not_raise(mock_source):
    source, cache = mock_source
    source._client.get_snapshot_all.side_effect = Exception("network error")
    source._tickers = ["AAPL"]
    # Should not raise
    asyncio.get_event_loop().run_until_complete(source._poll_once())
```

### 13.2 Integration Tests

**`test_simulator_source.py`** — full async lifecycle:
```python
@pytest.mark.asyncio
async def test_simulator_source_lifecycle():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
    await source.start(["AAPL", "GOOGL"])

    # Cache seeded immediately on start
    assert cache.get_price("AAPL") is not None
    assert cache.get_price("GOOGL") is not None

    # Prices update after a tick
    v0 = cache.version
    await asyncio.sleep(0.1)
    assert cache.version > v0

    # Add ticker
    await source.add_ticker("TSLA")
    assert cache.get_price("TSLA") is not None

    # Remove ticker
    await source.remove_ticker("GOOGL")
    assert cache.get("GOOGL") is None

    await source.stop()
```

---

## 14. Design Decision Log

| Decision | Alternative Considered | Rationale |
|----------|------------------------|-----------|
| Strategy pattern (ABC + factory) | Conditional `if` branches scattered throughout codebase | Single swap point; downstream code is source-agnostic; testable in isolation |
| `PriceCache` as single truth | Data source writes directly to a dict consumers read | Decouples producer from consumers; consumers never block the write path; thread-safe by design |
| Frozen `PriceUpdate` dataclass | Mutable dict | Immutable snapshots are safe across threads without defensive copying |
| Version counter for SSE | Diff the entire price dict each tick | One integer comparison per tick vs. O(n) dict comparison; avoids sending 30 identical events between Massive polls |
| GBM over mean-reverting (OU) | Ornstein-Uhlenbeck | OU reverts to seed prices — visually artificial for a demo. GBM wanders freely, looks real |
| Cholesky decomposition | Independent random draws | Correlated moves (tech stocks co-moving) look more realistic; O(n²) per-tick cost is negligible for n < 50 |
| `asyncio.to_thread()` for Massive | Re-implementing REST client as async | The `massive` Python client is synchronous; wrapping it avoids blocking the event loop without a rewrite |
| Immediate first poll in Massive `start()` | Wait for first scheduled poll | Without this, SSE has no data for up to 15 seconds after startup — bad UX |
| Shock events (0.1% probability) | Pure GBM only | Drama: pure GBM produces smooth, boring charts; shocks create newsworthy spikes that make the demo more engaging |
| SSE over WebSockets | WebSocket | One-way push is sufficient; EventSource has built-in reconnection; no handshake complexity; works through all proxies |
| 500ms tick rate | 1000ms (1 Hz) | Sub-second updates create a genuine "live" feel; 1Hz feels choppy on smooth monitors |
