# Market Simulator — Design and Code Structure

The FinAlly market simulator generates realistic-looking stock price movements without any external API dependency. It is the default data source when `MASSIVE_API_KEY` is not set.

---

## Goals

- Prices move continuously, every 500ms, creating a genuine "live" feel in the UI.
- Price movements are correlated across related stocks (tech stocks move together, finance stocks move together), which looks realistic.
- Occasional random "shock" events create dramatic spikes/drops for demo interest.
- Starting prices are close to real-world values so the UI looks plausible.
- Stateless restarts: each run starts from seed prices; no persistence needed.

---

## Algorithm: Geometric Brownian Motion (GBM)

The simulator uses GBM, the standard mathematical model for stock price evolution. Each tick, a price is updated according to:

```
S(t+dt) = S(t) × exp((μ - σ²/2) × dt + σ × √dt × Z)
```

Where:
- `S(t)` — current price
- `μ` (mu) — annualized expected return (drift)
- `σ` (sigma) — annualized volatility
- `dt` — time step as a fraction of a trading year
- `Z` — standard normal random variable (correlated across tickers via Cholesky)

### Why GBM?

GBM has two key properties that make it appropriate here:

1. **Prices stay positive.** The exponential function ensures `S(t+dt) > 0` always.
2. **Log-returns are normally distributed.** This matches the behavior of real equity prices over short time horizons.

The academic alternative (mean-reverting Ornstein-Uhlenbeck) would cause prices to drift back to their seed values over time, which looks artificial in a demo. GBM lets prices wander freely.

### Time step

Each tick is 500ms. Expressed as a fraction of a trading year (252 days × 6.5 hours/day × 3600 sec/hour = 5,896,800 seconds):

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR    # ≈ 8.48e-8
```

This tiny `dt` produces sub-cent moves per tick, which accumulate naturally to produce realistic intraday price paths.

---

## Correlated Moves via Cholesky Decomposition

Real stocks don't move independently — tech stocks tend to rise and fall together. The simulator captures this using a Cholesky decomposition of a correlation matrix.

### Correlation structure

```python
CORRELATION_GROUPS = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Between sectors, or for unknown tickers
TSLA_CORR          = 0.3   # TSLA is in tech but behaves independently
```

### How Cholesky produces correlated normals

1. Build an n×n correlation matrix `Σ` where `Σ[i,j] = ρ(ticker_i, ticker_j)`.
2. Compute the Cholesky factor `L` such that `L @ L.T = Σ`.
3. Each tick: draw n independent standard normals `z`, then apply `Z = L @ z`.
4. The resulting `Z` vector has the covariance structure of `Σ`.

```python
# Build the correlation matrix
corr = np.eye(n)
for i in range(n):
    for j in range(i + 1, n):
        rho = _pairwise_correlation(tickers[i], tickers[j])
        corr[i, j] = rho
        corr[j, i] = rho

# Decompose once; reuse on every tick
cholesky = np.linalg.cholesky(corr)

# Per-tick: generate correlated draws
z_independent = np.random.standard_normal(n)
z_correlated  = cholesky @ z_independent
```

The Cholesky matrix is recomputed only when tickers are added or removed — it is constant between those events, so the per-tick cost is just one matrix-vector multiply.

---

## Random Shock Events

Beyond normal GBM diffusion, there is a small probability per tick per ticker of a sudden price shock:

```python
EVENT_PROBABILITY = 0.001   # 0.1% per tick per ticker

if random.random() < self._event_prob:
    magnitude = random.uniform(0.02, 0.05)   # 2%–5% move
    direction = random.choice([-1, 1])
    price *= (1 + magnitude * direction)
```

With 10 tickers at 2 ticks/second:
- Expected events per second: `10 × 2 × 0.001 = 0.02`
- Expected interval between events: ~50 seconds

This creates the occasional dramatic spike or crash that makes the demo more visually interesting.

---

## Seed Prices and Per-Ticker Parameters

Defined in `seed_prices.py`. Starting prices approximate real-world values at project creation. Per-ticker `sigma` and `mu` reflect each stock's historical character:

```python
SEED_PRICES = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM":  195.00,
    "V":    280.00,
    "NFLX": 600.00,
}

TICKER_PARAMS = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},   # Moderate volatility
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},   # Lower volatility (mature)
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility, lower drift
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High vol, strong upward drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low vol (payments network)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}  # For dynamically-added tickers
```

Tickers added dynamically (not in `TICKER_PARAMS`) receive `DEFAULT_PARAMS` and a random seed price between $50–$300.

---

## Class Structure

### `GBMSimulator` (pure math, no async)

Responsible only for the price math. Has no knowledge of asyncio, FastAPI, or the cache.

```
GBMSimulator
├── __init__(tickers, dt, event_probability)
│     └─ calls _add_ticker_internal() for each, then _rebuild_cholesky()
│
├── step() → dict[str, float]
│     └─ the hot path: one Cholesky multiply + GBM formula per ticker per tick
│
├── add_ticker(ticker)
│     └─ _add_ticker_internal() → _rebuild_cholesky()
│
├── remove_ticker(ticker)
│     └─ removes from _tickers/_prices/_params → _rebuild_cholesky()
│
├── get_price(ticker) → float | None
│
├── get_tickers() → list[str]
│
├── _add_ticker_internal(ticker)   [private]
│     └─ looks up SEED_PRICES + TICKER_PARAMS, sets initial state
│
├── _rebuild_cholesky()            [private]
│     └─ builds correlation matrix, calls np.linalg.cholesky()
│
└── _pairwise_correlation(t1, t2)  [static, private]
      └─ returns ρ based on CORRELATION_GROUPS membership
```

**Internal state:**

```python
_tickers: list[str]               # ordered list (index == matrix row)
_prices:  dict[str, float]        # current prices
_params:  dict[str, dict]         # {sigma, mu} per ticker
_cholesky: np.ndarray | None      # L matrix, None for n <= 1
_dt: float                        # time step (constant)
_event_prob: float                # shock probability (constant)
```

### `SimulatorDataSource` (async wrapper)

Bridges `GBMSimulator` to the `MarketDataSource` interface. Owns an asyncio task that calls `GBMSimulator.step()` on a timer and writes results to `PriceCache`.

```
SimulatorDataSource (implements MarketDataSource)
├── start(tickers)
│     ├─ creates GBMSimulator(tickers)
│     ├─ seeds PriceCache with initial prices (so SSE has data immediately)
│     └─ creates asyncio.Task(_run_loop)
│
├── stop()
│     └─ cancels and awaits the task
│
├── add_ticker(ticker)
│     ├─ sim.add_ticker(ticker)
│     └─ seeds cache with immediate price
│
├── remove_ticker(ticker)
│     ├─ sim.remove_ticker(ticker)
│     └─ cache.remove(ticker)
│
├── get_tickers() → list[str]
│     └─ delegates to sim.get_tickers()
│
└── _run_loop()   [private async task]
      └─ loop: sim.step() → cache.update() for each ticker → asyncio.sleep(0.5)
```

---

## Tick Timing

The background loop runs `asyncio.sleep(0.5)` between ticks. This means:

- **Update rate:** 2 Hz (500ms per tick)
- **SSE cadence:** The SSE endpoint also reads the cache at ~500ms intervals (matching the production of new data)
- **Latency from simulation to browser:** < 1 second typically

The loop does not attempt to compensate for drift (i.e., the time taken by `step()` is not subtracted from the sleep). This is intentional simplicity — `step()` runs in microseconds and the demo does not require precise timing.

---

## Full Tick Sequence (per 500ms)

```
1. asyncio.sleep(0.5) completes in _run_loop
2. _sim.step() called
   a. Generate n independent standard normals: np.random.standard_normal(n)
   b. Apply Cholesky: z_correlated = L @ z_independent
   c. For each ticker:
      i.  Compute GBM: price *= exp(drift + diffusion * z_correlated[i])
      ii. Optionally apply random shock (0.1% chance)
      iii. Round to 2 decimal places
   d. Return {ticker: new_price} dict
3. For each (ticker, price) in result:
   cache.update(ticker, price)
   → creates PriceUpdate(ticker, price, previous_price, timestamp=now)
   → increments cache.version
4. SSE endpoint detects version change, sends event to all connected clients
5. asyncio.sleep(0.5) begins again
```

---

## Adding a New Ticker at Runtime

When the user adds a ticker via the watchlist API:

```python
await source.add_ticker("PYPL")
```

Inside `SimulatorDataSource.add_ticker()`:
1. `GBMSimulator.add_ticker("PYPL")` is called.
2. If `PYPL` is not in `SEED_PRICES`, a random seed price ($50–$300) is assigned.
3. If `PYPL` is not in `TICKER_PARAMS`, `DEFAULT_PARAMS` is used.
4. `_rebuild_cholesky()` is called to incorporate the new ticker into the correlation matrix.
5. `SimulatorDataSource` seeds the cache immediately with the initial price — the SSE stream includes PYPL on the very next tick.

---

## Testing the Simulator

Key test scenarios in `backend/tests/market/test_simulator.py`:

| Test | What it verifies |
|------|-----------------|
| Prices always positive | GBM cannot produce negative prices |
| Price changes are small | `abs(new - old) / old < 0.01` per tick (no teleporting) |
| All tickers updated | `step()` returns a value for every tracked ticker |
| Add/remove ticker | Works mid-simulation; Cholesky rebuilt correctly |
| Correlation direction | When one tech stock gets a positive shock, others are more likely to move up |
| Shock events fire | Forcing `event_probability=1.0` confirms shock logic executes |
| Single ticker | `cholesky` is `None` for n=1; no crash |
| Unknown tickers | `DEFAULT_PARAMS` applied; random seed price in valid range |

---

## Limitations and Known Behavior

- **No intraday mean reversion.** Prices drift away from seed values indefinitely. Over a multi-hour session, TSLA might reach $400 or $80. This is acceptable for a demo.
- **No market hours.** The simulator runs 24/7; there is no "market open/close" concept. Prices move on weekends too.
- **No bid/ask spread.** Every fill is at the exact simulated mid price. This is intentional simplicity — fees and spread are out of scope.
- **Daily change % is relative to session start.** Since there is no concept of a previous close, `change_percent` in the UI should be computed as `(current - first_seen) / first_seen * 100` on the frontend from SSE data.
- **Restart resets prices.** Each container start begins from `SEED_PRICES`. There is no price history continuity across restarts. The `portfolio_snapshots` table preserves portfolio value history, but not raw price history.
