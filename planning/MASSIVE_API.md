# Massive (formerly Polygon.io) REST API — Reference

Massive (rebranded from Polygon.io in October 2025) provides U.S. stock market data through REST APIs, WebSocket streams, and flat files. This document covers the REST endpoints relevant to FinAlly: fetching current prices and OHLC data for multiple tickers.

---

## Authentication

Every request requires an API key, passed either as a query parameter or as a Bearer token in the `Authorization` header.

```python
# Query parameter (simple)
GET https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers?apiKey=YOUR_KEY

# Authorization header (preferred for security)
GET https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers
Authorization: Bearer YOUR_KEY
```

The Python client handles this automatically:

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_MASSIVE_API_KEY")
```

---

## Rate Limits

| Plan | Requests per Minute | Recommended Poll Interval |
|------|---------------------|--------------------------|
| Free | 5 req/min | 15 seconds |
| Starter / Developer | Unlimited | 5–10 seconds |
| Advanced / Business | Unlimited | 2–5 seconds |

When the rate limit is exceeded, the API returns HTTP `429 Too Many Requests`. The client should back off and retry after the reset window.

---

## Key Endpoints

### 1. Multi-Ticker Snapshot — `GET /v2/snapshot/locale/us/markets/stocks/tickers`

Fetches the latest market state for a comma-separated list of tickers (or all tickers if omitted) in a single API call. **This is the primary endpoint for FinAlly's polling loop.**

**Query Parameters**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tickers` | string | No | Comma-separated list, e.g. `AAPL,TSLA,GOOG`. Omit for all tickers. |
| `include_otc` | boolean | No | Include OTC securities. Default: `false` |
| `apiKey` | string | Yes* | API key (*or use Authorization header) |

**Availability:** Stocks Starter+ (not available on free Basic plan)
**Data freshness:** 15-minute delayed (Starter/Developer) or real-time (Advanced/Business)

**Example Request**

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="YOUR_KEY")

# Fetch snapshots for specific tickers
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    ticker = snap.ticker
    price  = snap.last_trade.price
    ts     = snap.last_trade.timestamp  # Unix milliseconds
    print(f"{ticker}: ${price:.2f} at {ts}")
```

**Response Schema**

```json
{
  "status": "OK",
  "count": 2,
  "tickers": [
    {
      "ticker": "AAPL",
      "updated": 1700000000000,
      "todaysChange": 1.85,
      "todaysChangePerc": 0.98,
      "day": {
        "o": 189.00,
        "h": 191.50,
        "l": 188.20,
        "c": 190.85,
        "v": 45123456,
        "vw": 190.12
      },
      "prevDay": {
        "o": 187.50,
        "h": 190.00,
        "l": 186.80,
        "c": 189.00,
        "v": 52000000,
        "vw": 188.75
      },
      "min": {
        "o": 190.60,
        "h": 191.00,
        "l": 190.50,
        "c": 190.85,
        "v": 98000,
        "vw": 190.78,
        "t": 1700000000000
      },
      "lastTrade": {
        "p": 190.85,
        "s": 100,
        "t": 1700000000123456789,
        "x": 4
      },
      "lastQuote": {
        "P": 190.86,
        "S": 2,
        "p": 190.84,
        "s": 3,
        "t": 1700000000123456789
      }
    }
  ]
}
```

**Key response fields**

| Field | Description |
|-------|-------------|
| `tickers[].ticker` | Exchange symbol |
| `tickers[].lastTrade.p` | Last trade price (most current) |
| `tickers[].lastTrade.t` | Last trade timestamp (Unix nanoseconds) |
| `tickers[].lastTrade.s` | Trade size (shares) |
| `tickers[].lastQuote.p` / `.P` | Bid / Ask price |
| `tickers[].day.c` | Today's closing/current price |
| `tickers[].day.o` | Today's open price |
| `tickers[].todaysChangePerc` | Percentage change from previous close |
| `tickers[].prevDay.c` | Previous day's closing price |
| `tickers[].min` | Most recent minute bar |
| `tickers[].updated` | Last updated (Unix milliseconds) |

---

### 2. Unified Multi-Asset Snapshot — `GET /v3/snapshot`

Alternative snapshot endpoint that supports up to 250 tickers across multiple asset classes in a single call. More flexible than v2 for mixed-asset portfolios.

**Query Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `ticker.any_of` | string | Comma-separated list of up to 250 tickers |
| `type` | string | Asset class: `stocks`, `options`, `fx`, `crypto`, `indices` |
| `limit` | integer | Max results (default 10, max 250) |
| `sort` | string | Field to sort by |
| `order` | string | `asc` or `desc` |

**Example Request**

```python
# Using the Python client
results = client.list_snapshot_chain(
    "AAPL",                    # underlying ticker (for options)
    # OR use underlying_asset for multi-stock:
    params={"ticker.any_of": "AAPL,NCLH,TSLA", "limit": 10}
)
```

**Example via HTTP**

```
GET /v3/snapshot?ticker.any_of=AAPL,NCLH,TSLA&limit=10&apiKey=YOUR_KEY
```

**Response Schema**

```json
{
  "status": "OK",
  "request_id": "abc123",
  "results": [
    {
      "ticker": "AAPL",
      "type": "stocks",
      "name": "Apple Inc.",
      "market_status": "open",
      "last_trade": {
        "price": 190.85,
        "size": 100,
        "timestamp": 1700000000123456789
      },
      "last_quote": {
        "bid": 190.84,
        "ask": 190.86,
        "bid_size": 300,
        "ask_size": 200
      },
      "session": {
        "open": 189.00,
        "close": 190.85,
        "high": 191.50,
        "low": 188.20,
        "volume": 45123456,
        "change": 1.85,
        "change_percent": 0.98,
        "previous_close": 189.00
      }
    }
  ],
  "next_url": null
}
```

---

### 3. Previous Day Bar — `GET /v2/aggs/ticker/{ticker}/prev`

Returns the prior trading day's OHLC for a single ticker. Useful for computing "daily change %" as a baseline at startup.

**Parameters**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `stocksTicker` | string | Yes (path) | Case-sensitive ticker symbol |
| `adjusted` | boolean | No | Adjust for splits. Default: `true` |

**Example Request**

```python
prev = client.get_previous_close("AAPL")
print(f"AAPL prev close: ${prev.results[0].c:.2f}")
```

**Response Schema**

```json
{
  "ticker": "AAPL",
  "adjusted": true,
  "resultsCount": 1,
  "status": "OK",
  "results": [
    {
      "T": "AAPL",
      "o": 187.50,
      "h": 190.00,
      "l": 186.80,
      "c": 189.00,
      "v": 52000000,
      "vw": 188.75,
      "n": 412500,
      "t": 1699920000000
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `c` | Close price |
| `o` | Open price |
| `h` / `l` | High / Low |
| `v` | Volume |
| `vw` | Volume-weighted average price (VWAP) |
| `n` | Number of transactions |
| `t` | Unix millisecond timestamp |

---

### 4. Custom Bars (OHLC) — `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

Returns aggregated OHLC bars over a date range. Used for historical charts.

**Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `stocksTicker` | string (path) | Ticker symbol |
| `multiplier` | integer (path) | Size multiplier (e.g., `1` for 1-minute bars) |
| `timespan` | string (path) | `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year` |
| `from` | string (path) | Start date `YYYY-MM-DD` or Unix ms timestamp |
| `to` | string (path) | End date `YYYY-MM-DD` or Unix ms timestamp |
| `adjusted` | boolean | Adjust for splits. Default: `true` |
| `sort` | string | `asc` or `desc` |
| `limit` | integer | Max results (default 5000, max 50000) |

**Example Request — 1-day bars for the last 30 days**

```python
import datetime

today = datetime.date.today()
thirty_days_ago = today - datetime.timedelta(days=30)

aggs = []
for bar in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_=thirty_days_ago.isoformat(),
    to=today.isoformat(),
    limit=50,
):
    aggs.append(bar)

for bar in aggs:
    print(f"Date: {bar.t}, Close: ${bar.c:.2f}, Volume: {bar.v:,}")
```

**Example Request — 1-minute bars for intraday**

```python
aggs = []
for bar in client.list_aggs(
    ticker="TSLA",
    multiplier=1,
    timespan="minute",
    from_="2024-01-15",
    to="2024-01-15",
    limit=1000,
):
    aggs.append(bar)
```

---

### 5. Last Trade — `GET /v2/last/trade/{ticker}`

Returns the single most recent trade for a ticker. Lower latency than the snapshot endpoint for a single ticker.

**Example Request**

```python
trade = client.get_last_trade("AAPL")
print(f"Last price: ${trade.results.p:.2f}")
print(f"Size: {trade.results.s} shares")
print(f"Timestamp (ns): {trade.results.t}")
```

**Key response fields**

| Field | Description |
|-------|-------------|
| `p` | Price (dollars) |
| `s` | Size (shares) |
| `t` | SIP timestamp (Unix nanoseconds) |
| `x` | Exchange ID |

---

## Python Client: Installation and Setup

```bash
pip install massive
# or with uv:
uv add massive
```

**Basic usage**

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_KEY")

# Disable auto-pagination for one-shot requests
client_no_page = RESTClient(api_key="YOUR_KEY", pagination=False)

# Enable debug tracing
client_debug = RESTClient(api_key="YOUR_KEY", trace=True, verbose=True)
```

**The RESTClient is synchronous.** In an async context (FastAPI, asyncio), run it in a thread:

```python
import asyncio
from massive import RESTClient

client = RESTClient(api_key="YOUR_KEY")

async def fetch_prices(tickers: list[str]):
    snapshots = await asyncio.to_thread(
        client.get_snapshot_all,
        market_type="stocks",
        tickers=tickers,
    )
    return snapshots
```

---

## Plan Tiers Summary

| Feature | Free/Basic | Starter/Developer | Advanced/Business |
|---------|------------|-------------------|-------------------|
| Rate limit | 5 req/min | Unlimited | Unlimited |
| Data freshness | End-of-day | 15-min delayed | Real-time |
| Snapshot endpoint | Not available | Available | Available |
| Historical data | 2 years | 2–5 years | Full history |
| Recommended poll interval | N/A | 5–15 seconds | 2–5 seconds |

> **Note:** The snapshot endpoint (`/v2/snapshot/...`) is not available on the free Basic plan. A Starter plan or higher is required. For development without a paid key, use the built-in GBM simulator.

---

## Error Handling

| HTTP Status | Meaning | Action |
|-------------|---------|--------|
| `200 OK` | Success | Parse response |
| `401 Unauthorized` | Bad API key | Check `MASSIVE_API_KEY` env var |
| `403 Forbidden` | Feature not in plan | Upgrade plan or use simulator |
| `404 Not Found` | Unknown ticker | Log and skip |
| `429 Too Many Requests` | Rate limit exceeded | Back off, retry after reset |
| `5xx` | Server error | Log, retry on next interval |

**Recommended error handling pattern**

```python
try:
    snapshots = client.get_snapshot_all(
        market_type=SnapshotMarketType.STOCKS,
        tickers=tickers,
    )
except Exception as e:
    logger.error("Massive API poll failed: %s", e)
    # Don't crash — retry on next scheduled interval
    return
```

---

## Relevant Links

- [Massive REST API Docs](https://massive.com/docs)
- [Stocks REST Overview](https://massive.com/docs/rest/stocks/overview)
- [Massive Python Client (GitHub)](https://github.com/massive-com/client-python)
- [Pricing / Plans](https://massive.com/pricing)
