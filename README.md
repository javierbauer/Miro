# Polymarket Predictor

Automated overnight Polymarket prediction tool with a web dashboard.

## Quick Start

```bash
# 1. Copy config
cp .env.example .env

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start dashboard + API
python main.py
# → Open http://localhost:8000
```

## Modes

| Mode | What it does |
|------|-------------|
| **Paper Trading** (default) | Simulates trades against a $1,000 virtual balance. Safe to run immediately — no wallet needed. |
| **Live Trading** | Real trades via Polymarket CLOB API. Requires private key + API credentials in `.env`. |

To enable live trading set `DRY_RUN=false` in `.env` and fill in your credentials.

## Overnight Run

```bash
chmod +x scripts/run_overnight.sh scripts/stop.sh
./scripts/run_overnight.sh      # starts + auto-begins scan loop
./scripts/stop.sh               # stop in the morning
```

## Architecture

```
main.py                 ← CLI entry point
backend/
  config.py             ← Settings from .env
  database.py           ← SQLite via SQLAlchemy async
  polymarket_client.py  ← Gamma + CLOB API (read-only, no auth)
  predictor.py          ← Signal engine (spread, volume, momentum, recency)
  trader.py             ← Paper & live order execution
  scanner.py            ← Ralph Loop: scan → predict → trade cycle
  api.py                ← FastAPI REST + dashboard route
frontend/
  index.html            ← Single-file dashboard (dark theme)
data/
  polymarket.db         ← SQLite database (auto-created)
logs/                   ← Overnight log files
```

## Prediction Strategy (Ralph Loop)

The predictor scores each market on four signals:

1. **Spread signal** — Markets where YES+NO < 1.0 carry an implicit edge
2. **Volume signal** — Low volume relative to liquidity = stale/inefficient price
3. **Recency signal** — Markets closing within 24h near 50/50 tend to break one way
4. **Momentum signal** — 1h price drift (from Gamma API) predicts short-term continuation

A **fractional Kelly criterion** sizes each bet based on the estimated edge.

Only signals with edge ≥ 4% and confidence ≥ 55% trigger a trade.

## Configuration (`.env`)

```
MAX_BET_USDC=10        # Max per trade
MIN_EDGE=0.04          # Minimum edge to trade (4%)
MIN_CONFIDENCE=0.60    # Minimum confidence
MAX_DAILY_SPEND=100    # Budget cap
DRY_RUN=true           # Paper trading (change to false for live)
```

## Dashboard

| Panel | Description |
|-------|-------------|
| Stats bar | Trades, spend, predictions, win rate |
| Controls | Start/stop loop, manual scan |
| Predictions | Latest signals with edge & confidence |
| Trades | Full trade history with status |
| Markets | Live market prices + volume |
| Log | Real-time activity feed |
