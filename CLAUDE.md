# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Telegram bot that monitors whale trading activity on Hyperliquid DEX and cross-exchange signals (Binance Futures, Bybit Futures, Coinglass). Detects large trades, liquidations, OI spikes, funding extremes, and multi-source confluence. Also runs a signal (kèo) system: auto-creates paper trades when whale activity confirms a technical trend, tracks TP/SL hits in real time, and posts results to a Telegram channel.

## Running the Bot

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill TELEGRAM_BOT_TOKEN and ADMIN_CHAT_ID
python main.py
# or
docker-compose up -d
```

No tests, no lint scripts, no build step.

## Configuration

All config lives in `config/settings.py` as a single `Config` dataclass, loaded from `.env`. The singleton is `config` (imported as `from config.settings import config`).

Key variable groups:
- **Whale thresholds**: `MIN_TRADE_SIZE_USD`, `MIN_POSITION_SIZE_USD`, `MIN_LIQUIDATION_SIZE_USD`
- **Signal (kèo)**: `AUTO_SIGNAL_ENABLED`, `AUTO_SIGNAL_MIN_USD` (BTC/ETH), `AUTO_SIGNAL_MIN_USD_ALT` (altcoins), `AUTO_SIGNAL_MAJOR_COINS`
- **Trend detector**: `TREND_POLL_INTERVAL` (default 900s), `TREND_MIN_SCORE` (default 2/3), `TREND_ATR_SL_MULT`, `TREND_ATR_TP_MULT`
- **Multi-source**: `BINANCE_ENABLED`, `BYBIT_ENABLED`, `COINGLASS_API_KEY`, `OI_SPIKE_THRESHOLD`, `FUNDING_EXTREME_HIGH/LOW`, `CONFLUENCE_ENABLED`
- **Channel**: `SIGNAL_CHANNEL_ID` — Telegram channel where kèo are posted

## Architecture

```
Binance WS ──┐
Bybit WS ────┤→ AlertEngine → Telegram channel/users
Hyperliquid ─┤→ WhaleDetector
             │
Coinglass REST (poll) → OISpikeDetector, FundingRateDetector
                              ↓
                       ConfluenceScorer (in-memory event bus)

Binance REST (4h klines, poll) → TrendDetector → _trend_cache
                                                       ↓
                                              SignalTracker.maybe_create_auto_signal()
                                              (whale trade triggers kèo only if trend confirms)
```

**Startup flow** (`main.py`): logging → DB init → Telegram bot → SignalTracker → WhaleDetector + AlertEngine → register WS event handlers → PositionPoller → `asyncio.gather()` all long-running coroutines.

## Key Modules

**`src/api/`** — Raw data clients. All implement `.on(event, callback)` + `.start()`.
- `hyperliquid_ws.py` — subscribes `trades` + `userEvents`, auto-reconnects with 5s backoff
- `binance_ws.py` — Binance Futures aggTrade + forceOrder streams
- `bybit_ws.py` — Bybit V5 linear public stream, 20s heartbeat
- `coinglass_rest.py` — poll-based, returns dicts (no events); fallback to Binance public REST if no API key

**`src/detector/whale_detector.py`** — Parses raw payloads into `WhaleAlert` dataclass. `AlertType` enum: `BIG_TRADE`, `LARGE_POSITION`, `LIQUIDATION`, `POSITION_FLIP`, `PNL_MILESTONE`, `WATCHLIST_TRADE`, `OI_SPIKE`, `FUNDING_EXTREME`, `CONFLUENCE`. Tracks per-address position history for flip detection. All message formatting lives in `WhaleAlert.format_message()`.

**`src/detector/alert_engine.py`** — Routes `WhaleAlert` objects: global alerts → all active users; watchlist alerts → only subscribed users. Deduplicates via `alert_log` table with `ALERT_COOLDOWN_SECONDS` window. Also exposes `process_binance_trade/liquidation` and `process_bybit_trade/liquidation`.

**`src/detector/trend_detector.py`** — Polls Binance Futures 4h klines every 15 min. Computes EMA20/50, RSI(14), MACD, ATR(14) without external TA libraries. Stores `TrendState` per coin in module-level `_trend_cache`. `get_trend(coin)` is the read interface used by `SignalTracker`.

**`src/detector/oi_detector.py` / `funding_detector.py`** — Periodic pollers that call `CoinglassRest`, emit `OI_SPIKE` / `FUNDING_EXTREME` alerts, and call `confluence.ingest()`.

**`src/aggregator/confluence_scorer.py`** — In-memory buffer, groups signals by `(symbol, direction)` within `CONFLUENCE_WINDOW` seconds. Fires `CONFLUENCE` alert when `≥ CONFLUENCE_MIN_SOURCES` independent sources agree. Sources: `hyperliquid`, `binance`, `bybit`, `oi_spike`, `funding`, `liquidation`.

**`src/signals/signal_tracker.py`** — Manages the kèo lifecycle:
- `maybe_create_auto_signal()` — dedup (1 active kèo per coin), trend gate (whale direction must match `TrendState.direction` with `score ≥ TREND_MIN_SCORE`), ATR-based TP/SL calculation
- `start_price_poll()` — REST polls every 5s, drives `_check_signals()` for TP/SL detection
- `_post_to_channel()` / `_edit_signal_message()` / `_send_hit_notification()` — Telegram messaging
- Kèo status flow: `PENDING` (limit, waiting entry) → `ACTIVE` → `TP1_HIT` → `TP2_HIT` → `TP3_HIT` / `SL_HIT` / `CANCELLED`
- **Dedup rule**: a coin is "blocked" only while status is `PENDING` or `ACTIVE`; once `TP1_HIT` or higher, a new kèo can be created

**`src/bot/handlers.py`** — aiogram 3 router, user-facing commands. Per-user thresholds in `users` table.

**`src/bot/signal_handlers.py`** — Admin-only commands: `/signal` (create kèo manually), `/cancel`, `/signals`, `/signal_stats`, `/signal_report`, `/whales`, `/whale_scores`.

**`src/bot/poller.py`** — `PositionPoller`: polls open whale positions via Hyperliquid REST every N seconds, calculates real-time PnL, fires `PNL_MILESTONE` alerts, and edits the original Telegram message in-place via `auto_watch_msgs`.

**`src/api/hyperliquid_rest.py`** — Thin async REST wrapper around `https://api.hyperliquid.xyz/info`. Used by `PositionPoller` for open positions and by `SignalTracker` for current mark prices.

**`src/storage/database.py`** — Single `Database` class, async SQLite via `aiosqlite`. Singleton `db`. Tables: `users`, `watchlist`, `alert_log`, `known_whales`, `whale_scores`, `auto_watch`, `auto_watch_msgs`, `signals`. Schema migrations run at init via `ALTER TABLE ... ADD COLUMN` wrapped in try/except.

Key `signals` columns: `coin`, `direction` (LONG/SHORT), `entry_price`, `tp1/tp2/tp3`, `sl_price`, `leverage`, `status`, `order_type` (MARKET/LIMIT), `source` (ADMIN/AUTO), `channel_msg_id` (Telegram message to edit on TP/SL hit).

## Signal (Kèo) Auto-Creation Logic

```
whale trade arrives
  → size_usd >= threshold? (major vs altcoin)
  → db.has_active_signal(coin)?  [blocks if PENDING or ACTIVE]
  → get_trend(coin).direction == whale direction?
  → trend.score >= TREND_MIN_SCORE?
  → compute TP/SL from ATR (trend_detector.atr)
  → create_and_post() → DB insert → post to SIGNAL_CHANNEL_ID
```

TP/SL formula: `SL = entry ± ATR × TREND_ATR_SL_MULT`, `TP3 = entry ± ATR × TREND_ATR_TP_MULT`, TP1/TP2 at 1/3 and 2/3 of TP3 distance.

## src/skill/

Contains `EXPAND_SIGNALS.md` — a spec that drove the implementation of Binance WS, Bybit WS, Coinglass REST, OI spike, funding rate, confluence scorer, and related Telegram commands. All tasks in that spec are already implemented; the file is historical reference only.

## Adding a New Alert Type

1. Add variant to `AlertType` enum in `whale_detector.py`
2. Add `format_message()` branch in `WhaleAlert`
3. Emit from a detector and call `engine._route_alert(alert)`
4. Call `confluence.ingest(symbol, direction, source)` if directional
