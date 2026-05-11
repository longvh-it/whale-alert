# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Telegram bot that monitors whale trading activity on the Hyperliquid DEX. It listens to WebSocket streams and REST API endpoints, detects large trades/positions/liquidations, and sends alerts to subscribed Telegram users.

## Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your TELEGRAM_BOT_TOKEN and ADMIN_CHAT_ID

# Run locally
python main.py

# Run in Docker
docker-compose up -d
```

There are no tests, no lint scripts, and no build step.

## Configuration

All config is loaded from `.env` via `config/settings.py` as a dataclass. Key variables:

- `TELEGRAM_BOT_TOKEN`, `ADMIN_CHAT_ID` — required
- `MIN_TRADE_SIZE_USD` (default 100000), `MIN_POSITION_SIZE_USD` (default 500000), `MIN_LIQUIDATION_SIZE_USD` (default 200000) — whale detection thresholds
- `ALERT_COOLDOWN_SECONDS` (default 300) — deduplication window
- `DB_PATH` (default `whale_bot.db`) — SQLite database file

## Architecture

```
Hyperliquid WebSocket/REST
        ↓
  src/api/          — Raw API clients (ws + REST)
        ↓
  src/detector/     — Whale detection logic + alert routing
        ↓
  src/bot/          — Telegram command handlers + periodic poller
        ↓
  src/storage/      — SQLite async layer (4 tables)
```

**Startup flow** (`main.py`): logging → DB init → Telegram bot/dispatcher → WhaleDetector + AlertEngine → HyperliquidWS with event handlers → position poller → `asyncio.gather()` all tasks.

## Key Modules

**`src/api/hyperliquid_ws.py`** — Async WebSocket client. Subscribes to `trades` (BTC/ETH/SOL/ARB/DOGE/AVAX/WIF/PEPE) and `userEvents`. Auto-reconnects with 5s backoff. Handlers registered via `.on(event, callback)`.

**`src/api/hyperliquid_rest.py`** — REST client for user state, leaderboard, mark prices, open orders, fills.

**`src/detector/whale_detector.py`** — Parses events into `WhaleAlert` objects. Tracks position history per address for flip detection (Long↔Short). Six alert types: `BIG_TRADE`, `LARGE_POSITION`, `LIQUIDATION`, `POSITION_FLIP`, `WATCHLIST_TRADE`, `PNL_MILESTONE`.

**`src/detector/alert_engine.py`** — Routes alerts: global alerts (big trades, liquidations) go to all active users; watchlist alerts go only to users monitoring that address. Deduplicates via `alert_log` table using the cooldown window. Sends HTML-formatted Telegram messages.

**`src/bot/handlers.py`** — aiogram 3 router with 7 commands: `/start`, `/help`, `/watchlist`, `/add`, `/remove`, `/threshold`, `/settings`. Per-user thresholds are stored in the `users` table.

**`src/bot/poller.py`** — Polls all watched addresses via REST every 60s with 0.5s delay between requests to avoid rate limiting. Feeds results through the alert engine.

**`src/storage/database.py`** — Async SQLite via aiosqlite/SQLAlchemy. Tables: `users`, `watchlist`, `alert_log`, `known_whales`.

## Data Flow

1. WebSocket fires `on_trades` or `on_user_events` → `WhaleDetector` checks thresholds → `AlertEngine` deduplicates and routes → Telegram message sent
2. Poller fires every 60s → REST calls for each watched address → same detector/engine path
3. Telegram commands → handlers update DB → no immediate alert, takes effect on next detection cycle
