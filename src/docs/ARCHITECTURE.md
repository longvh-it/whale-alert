# Whale Bot — Tài Liệu Dự Án

> Bot Telegram theo dõi hoạt động giao dịch whale trên Hyperliquid DEX, tích hợp tín hiệu đa nguồn và hệ thống quản lý kèo tự động.

---

## Mục Lục

1. [Tổng Quan](#1-tổng-quan)
2. [Kiến Trúc Hệ Thống](#2-kiến-trúc-hệ-thống)
3. [Cấu Trúc Thư Mục](#3-cấu-trúc-thư-mục)
4. [Modules Chi Tiết](#4-modules-chi-tiết)
5. [Luồng Dữ Liệu](#5-luồng-dữ-liệu)
6. [Hệ Thống Kèo (Signal Lifecycle)](#6-hệ-thống-kèo-signal-lifecycle)
7. [Cấu Hình](#7-cấu-hình)
8. [Cài Đặt & Chạy](#8-cài-đặt--chạy)
9. [Lệnh Bot Telegram](#9-lệnh-bot-telegram)
10. [Database Schema](#10-database-schema)

---

## 1. Tổng Quan

Whale Bot là hệ thống giám sát giao dịch real-time cho Hyperliquid DEX, với ba chức năng chính:

| Chức năng | Mô tả |
|-----------|-------|
| **Whale Alert** | Phát hiện giao dịch lớn (>$100K), mở vị thế (>$500K), thanh lý (>$200K) |
| **Signal Confluence** | Tổng hợp tín hiệu từ nhiều nguồn (Hyperliquid, Binance, Bybit, OKX, OI, Funding, Liquidation) |
| **Auto Kèo** | Tự tạo paper trade từ 4 nguồn: whale, volume scan, TradingView webhook, admin thủ công |

### Công Nghệ

- **Runtime**: Python 3.11+, asyncio
- **Bot**: aiogram 3.x (Telegram)
- **WebSocket**: websockets, aiohttp
- **Database**: SQLite (aiosqlite)
- **Deployment**: Docker / Koyeb

---

## 2. Kiến Trúc Hệ Thống

```
┌─────────────────────────────────────────────────────────────────────┐
│  NGUỒN DỮ LIỆU (Async Data Sources)                                │
│                                                                     │
│  Hyperliquid WS ──── trades + userEvents + L2 book                 │
│  Binance WS ───────── aggTrade + forceOrder                        │
│  Bybit WS ─────────── linear public stream (20s heartbeat)         │
│  OKX WS ───────────── SWAP public stream (tùy chọn)               │
│  Binance REST ─────── klines 1h/4h/1d (poll định kỳ)              │
│  Coinglass REST ───── OI + Funding (poll mỗi 5 phút)              │
│  TradingView HTTP ─── POST /webhook/tv (Pine Script alerts)        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────┐
│  DETECTORS (Phân Tích & Phát Hiện)                                  │
│                                                                     │
│  WhaleDetector ──── phân tích giao dịch → WhaleAlert dataclass     │
│  TrendDetector ──── EMA20/50 + RSI(14) + MACD, multi-timeframe     │
│  DOMAnalyzer ────── depth order book → BULLISH/BEARISH/NEUTRAL     │
│  OISpikeDetector ── OI thay đổi > ngưỡng                           │
│  FundingDetector ── funding cực đoan (cao/thấp)                    │
│  EcosystemDetector  coin tương quan cùng đợt                       │
│  TrendScanner ───── volume spike 4h → kèo không cần whale          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────┐
│  AGGREGATION (Tổng Hợp Tín Hiệu)                                    │
│                                                                     │
│  ConfluenceScorer                                                   │
│    • Buffer in-memory theo (symbol, direction) trong 300s           │
│    • Nguồn có trọng số: HL=3, OI=2, Liq=2, Binance/Bybit/OKX/Fund=1│
│    • Fire khi ≥3 nguồn + weighted score ≥5                          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────┐
│  SIGNAL TRACKER (Quản Lý Kèo)                                       │
│                                                                     │
│  Nguồn tạo kèo:                                                     │
│    maybe_create_auto_signal()      ← whale trade                   │
│    maybe_create_ecosystem_signal() ← ecosystem spike               │
│    maybe_create_scan_signal()      ← TrendScanner volume spike     │
│    create_and_post(source="TV")    ← TradingView webhook           │
│    create_and_post(source="ADMIN") ← /signal command               │
│                                                                     │
│  start_price_poll() — REST poll 5s                                  │
│    PENDING → ACTIVE → TP1_HIT → TP2_HIT → TP3_HIT                 │
│                              └→ SL_HIT / CANCELLED                 │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────┐
│  OUTPUT (Telegram)                                                  │
│                                                                     │
│  AlertEngine ───── route alert → users DM + channel               │
│  SignalTracker ──── post kèo → SIGNAL_CHANNEL_ID, edit on update   │
│  PositionPoller──── cập nhật PnL vị thế whale real-time           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Cấu Trúc Thư Mục

```
whale-bot/
├── config/
│   └── settings.py              # Config dataclass duy nhất, load từ .env
├── src/
│   ├── api/                     # Clients kết nối nguồn dữ liệu ngoài
│   │   ├── hyperliquid_ws.py    # WS: trades + userEvents + L2 book
│   │   ├── hyperliquid_rest.py  # REST: mark price, open positions
│   │   ├── binance_ws.py        # WS: aggTrade + forceOrder streams
│   │   ├── bybit_ws.py          # WS: Bybit V5 linear public
│   │   ├── okx_ws.py            # WS: OKX SWAP public (tùy chọn)
│   │   └── coinglass_rest.py    # REST poll: OI, Funding; fallback Binance
│   │
│   ├── detector/                # Business logic phát hiện tín hiệu
│   │   ├── whale_detector.py    # Parse raw payload → WhaleAlert
│   │   ├── alert_engine.py      # Route alert, dedup cooldown
│   │   ├── trend_detector.py    # Multi-TF EMA/RSI/MACD → TrendState
│   │   ├── dom_analyzer.py      # L2 book → DOMSnapshot
│   │   ├── oi_detector.py       # OI spike polling
│   │   ├── funding_detector.py  # Funding extreme polling
│   │   ├── ecosystem_detector.py# Coin correlation signals
│   │   └── trend_scanner.py     # Volume spike callback → scan kèo
│   │
│   ├── aggregator/
│   │   └── confluence_scorer.py # Event bus: tổng hợp tín hiệu đa nguồn
│   │
│   ├── signals/
│   │   └── signal_tracker.py    # Vòng đời kèo, TP/SL, auto-cut
│   │
│   ├── bot/
│   │   ├── handlers.py          # User commands: /start /watchlist ...
│   │   ├── signal_handlers.py   # Admin commands: /signal /cancel /signals
│   │   ├── tv_webhook.py        # aiohttp server: POST /webhook/tv
│   │   └── poller.py            # PositionPoller: PnL tracking
│   │
│   ├── storage/
│   │   └── database.py          # Async SQLite (aiosqlite), schema migrations
│   │
│   └── docs/
│       └── ARCHITECTURE.md      # File này
│
├── data/                        # whale_bot.db (SQLite, mount volume)
├── logs/                        # Log xoay vòng (10MB, 7 ngày)
├── main.py                      # Entry point: orchestrate tất cả tasks
├── requirements.txt
├── docker-compose.yml
└── .env.example                 # Template biến môi trường
```

---

## 4. Modules Chi Tiết

### 4.1 `src/api/` — Clients Dữ Liệu

#### `hyperliquid_ws.py`
- Subscribe `trades` stream: phát hiện giao dịch tức thì
- Subscribe `userEvents`: theo dõi thay đổi vị thế whale cụ thể
- Subscribe L2 book: feed cho `DOMAnalyzer`
- Auto-reconnect với 5s exponential backoff
- `_watched_users`: set địa chỉ được pre-load từ DB (watchlist + known whales) để tối ưu subscription `userEvents`

#### `binance_ws.py`
- `aggTrade` stream: giá và khối lượng giao dịch tổng hợp
- `forceOrder` stream: thanh lý bắt buộc Binance Futures
- Feed vào `AlertEngine.process_binance_trade/liquidation()`

#### `bybit_ws.py`
- Bybit V5 linear public stream
- 20s heartbeat tự động để giữ kết nối
- Feed vào `AlertEngine.process_bybit_trade/liquidation()`

#### `okx_ws.py`
- OKX Futures SWAP public stream (bật bằng `OKX_ENABLED=true`)
- Fetch contract values (`ctVal`) từ REST API khi khởi động để quy đổi contract count → USD
- Feed vào `AlertEngine.process_okx_trade()`

#### `coinglass_rest.py`
- Poll định kỳ: Open Interest, Funding Rate
- Fallback sang Binance public REST nếu không có API key

#### `hyperliquid_rest.py`
- `GET /info` → mark prices (dùng bởi `SignalTracker.start_price_poll()`)
- `GET /info` → open positions (dùng bởi `PositionPoller`)

---

### 4.2 `src/detector/` — Business Logic

#### `whale_detector.py`

**WhaleAlert Dataclass:**
```python
@dataclass
class WhaleAlert:
    alert_type: AlertType
    symbol: str
    size_usd: float
    direction: str        # LONG / SHORT
    price: float
    trader_address: str
    ...
```

**AlertType Enum:**
| Type | Trigger |
|------|---------|
| `BIG_TRADE` | Giao dịch đơn > `MIN_TRADE_SIZE_USD` ($100K) |
| `LARGE_POSITION` | Mở vị thế > `MIN_POSITION_SIZE_USD` ($500K) |
| `LIQUIDATION` | Thanh lý > `MIN_LIQUIDATION_SIZE_USD` ($200K) |
| `POSITION_FLIP` | Đảo chiều vị thế (LONG → SHORT hoặc ngược lại) |
| `PNL_MILESTONE` | PnL đạt mốc > `MIN_PNL_ALERT_USD` ($50K) |
| `WATCHLIST_TRADE` | Giao dịch từ địa chỉ trong watchlist |
| `OI_SPIKE` | OI thay đổi > `OI_SPIKE_THRESHOLD` (%) |
| `FUNDING_EXTREME` | Funding > `FUNDING_EXTREME_HIGH` hoặc < `FUNDING_EXTREME_LOW` |
| `CONFLUENCE` | ≥3 nguồn xác nhận cùng chiều |

**Per-address tracking:** Lưu lịch sử vị thế để phát hiện `POSITION_FLIP`. Tất cả format message nằm trong `WhaleAlert.format_message()`.

#### `alert_engine.py`

- **Route global alerts**: gửi đến tất cả user đang active
- **Route watchlist alerts**: chỉ gửi user đã subscribe địa chỉ đó
- **Dedup**: bảng `alert_log` + `ALERT_COOLDOWN_SECONDS` — tránh spam cùng alert
- Expose `process_binance/bybit/okx_trade/liquidation()`

#### `trend_detector.py`

**TrendState Dataclass:**
```python
@dataclass
class TrendState:
    direction: str        # LONG | SHORT | NEUTRAL
    score: int            # 0-3 (số chỉ báo đồng thuận)
    atr: float            # Average True Range (USD)
    ema20: float
    ema50: float
    rsi: float            # RSI(14)
    macd_hist: float
    timeframe: str        # 1h | 4h | 1d
```

**Chỉ Báo Kỹ Thuật** (tính thủ công, không dùng thư viện ngoài):
- EMA20 / EMA50: so sánh giá với EMA để xác định xu hướng
- RSI(14): >55 = bullish bias, <45 = bearish bias
- MACD histogram: dương = bullish momentum, âm = bearish

**Multi-Timeframe Voting:**
```
1h vote + 4h vote + 1d vote
→ ≥2 phiếu LONG → direction = LONG
→ ≥2 phiếu SHORT → direction = SHORT
→ còn lại → NEUTRAL
```

**API:**
- `get_trend(coin)` → TrendState của khung 4h
- `get_multi_trend(coin)` → `(direction, confirmed_count)` dùng để gate kèo

Sau mỗi poll 4h candle, nếu phát hiện volume spike → gọi `trend_scanner.on_volume_spike()`.

#### `dom_analyzer.py`

Phân tích độ sâu sổ lệnh (L2 order book) mỗi khi có cập nhật:

| Phân tích | Logic |
|-----------|-------|
| **Bid/Ask Ratio** | Tổng khối lượng bid / ask. > `DOM_BID_ASK_BULLISH` (1.5) → bull signal |
| **Wall Detection** | Lệnh đơn > `DOM_WALL_MIN_USD` trong khoảng `DOM_WALL_DISTANCE_MAX_PCT` (1.5%) từ mid |
| **Wall Absorption** | Kích thước wall giảm ≥ `DOM_ABSORPTION_PCT_THRESHOLD` (25%) → tường đang bị hấp thụ |

**DOMSnapshot Output:**
```python
@dataclass
class DOMSnapshot:
    signal: str           # BULLISH | BEARISH | NEUTRAL
    signal_strength: int  # 0-3
    bid_ask_ratio: float
    wall_side: str        # BID | ASK | NONE
    wall_price: float
    absorption_detected: bool
```

Module singleton: `dom_analyzer`. Chỉ xử lý coins trong `DOM_COINS`.

#### `ecosystem_detector.py`

Khi một coin có volume spike (`on_volume_spike(coin, ratio, direction)`):
1. Tra `ECOSYSTEM_MAP` để tìm các coin tương quan
2. Với mỗi coin liên quan: kiểm tra `get_multi_trend`, whale events gần đây, DOM snapshot
3. Escalate trạng thái: `WATCH` → `ALERT` → `SIGNAL`
4. Khi đạt `SIGNAL` → gọi `maybe_create_ecosystem_signal()`

Module singleton: `ecosystem_detector`. Được wire trong `main.py` qua setters.

#### `trend_scanner.py`

`TrendScanner` class (module singleton `trend_scanner`). Được kích hoạt bởi `trend_detector` sau mỗi poll 4h candle khi phát hiện volume spike — **không có polling loop riêng**.

Điều kiện tạo kèo:
- Volume nến hiện tại / MA20 >= `SCAN_VOLUME_MIN` (2×)
- `get_multi_trend()` cùng chiều volume spike, confirmed >= `SCAN_MIN_TREND_SCORE`
- Không có kèo PENDING/ACTIVE cho coin đó
- Chưa vượt `SCAN_DAILY_MAX` kèo/ngày (reset 00:00 UTC)
- Coin chưa có kèo trong `SCAN_COIN_COOLDOWN_HOURS` giờ
- DOM không đối nghịch mạnh (signal_strength >= 2 theo hướng ngược)

Khi đủ điều kiện → gọi `signal_tracker.maybe_create_scan_signal()` với `source="TREND_SCAN"`.

#### `confluence_scorer.py`

**Trọng Số Nguồn:**
```
Hyperliquid whale trade  → 3 điểm  (đáng tin nhất)
OI Spike                 → 2 điểm
Liquidation              → 2 điểm
Binance / Bybit / OKX    → 1 điểm mỗi nguồn
Funding extreme          → 1 điểm
```

**Logic Fire:**
1. `ingest(symbol, direction, source)` → buffer in-memory
2. Mỗi 30s: group theo `(symbol, direction)` trong cửa sổ 300s
3. Nếu `sources ≥ CONFLUENCE_MIN_SOURCES` VÀ `weighted_score ≥ CONFLUENCE_MIN_SCORE_WEIGHTED`
4. → emit `CONFLUENCE` alert, cooldown để tránh lặp

---

### 4.3 `src/signals/signal_tracker.py` — Quản Lý Kèo

**Điều Kiện Tạo Kèo Tự Động (Auto từ whale):**
```
whale trade đến
  ├─ size_usd >= threshold? (major coin vs altcoin)
  ├─ daily_sl_limit chưa đạt?
  ├─ db.has_active_signal(coin)?  [PENDING/ACTIVE thì block]
  ├─ get_multi_trend(coin) → cùng chiều whale?
  ├─ confirmed_timeframes >= TREND_MIN_SCORE?
  ├─ quality_score >= SIGNAL_MIN_QUALITY_SCORE? (mặc định 50)
  └─ → tính TP/SL từ ATR → tạo kèo → post channel
```

**Quality Score (điểm chất lượng, 0–100):**

| Thành phần | Điểm |
|-----------|------|
| Trend score (0-3 timeframes) | +1 mỗi TF |
| DOM confirm cùng chiều | +1 |
| Confluence event | +1 |
| Ecosystem detection | -`ECOSYSTEM_SIGNAL_QUALITY_PENALTY` (mặc định -15) |

**ATR-based TP/SL:**
```
SL = entry ± ATR × TREND_ATR_SL_MULT  (mặc định ×1.5)
TP3 = entry ± ATR × TREND_ATR_TP_MULT (mặc định ×3.0)
TP1 = entry ± (TP3 - entry) × 1/3
TP2 = entry ± (TP3 - entry) × 2/3
```

**Auto-Management Rules:**

| Rule | Điều kiện | Hành động |
|------|-----------|-----------|
| **TP1 Continue** | TP1_HIT | Milestone, tiếp tục tracking TP2/TP3 với SL giữ nguyên |
| **TP1 Reversal Cut** | TP1_HIT + reversal score ≥ 4 | Đóng tại entry (hoà vốn) → CANCELLED |
| **Reversal Cut** | ACTIVE/TP2_HIT + opposite trend ≥ `REVERSAL_MIN_SCORE` trong `REVERSAL_GRACE_MINUTES` | Auto close → CANCELLED |
| **Stale Cancel** | PENDING > `SIGNAL_PENDING_TIMEOUT_HOURS` giờ | Auto → CANCELLED |
| **Daily SL Limit** | Số SL_HIT hôm nay ≥ `DAILY_SL_LIMIT` | Block tạo kèo mới |

**Status Flow:**
```
PENDING ──(giá chạm entry)──► ACTIVE
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
                 TP1_HIT      SL_HIT      CANCELLED
                    │                   (reversal/stale)
              ┌─────┤
              ▼     ▼
           TP2_HIT  SL_HIT
              │
              ▼
           TP3_HIT
```

**Dedup rule:** Coin bị block khi status là `PENDING` hoặc `ACTIVE`. Sau `TP1_HIT` hoặc cao hơn → có thể tạo kèo mới.

---

### 4.4 `src/bot/` — Telegram Interface

#### `handlers.py` — User Commands

| Lệnh | Chức năng |
|------|-----------|
| `/start` | Đăng ký user, hiển thị menu |
| `/filter` | Lọc cỡ lệnh: cá nhỏ / cá to / cá khủng |
| `/watchlist` | Xem danh sách địa chỉ đang theo dõi |
| `/add <address>` | Thêm địa chỉ vào watchlist |
| `/remove <address>` | Xóa địa chỉ khỏi watchlist |
| `/top` | Top PnL cao nhất đang theo dõi |
| `/threshold <usd>` | Đặt ngưỡng alert riêng |
| `/sources` | Xem nguồn dữ liệu đang hoạt động |
| `/confluence` | Bật/tắt confluence alerts |
| `/settings` | Xem/chỉnh cài đặt cá nhân |
| `/help` | Danh sách lệnh |

#### `signal_handlers.py` — Admin Commands

| Lệnh | Chức năng |
|------|-----------|
| `/signal` | Tạo kèo thủ công |
| `/cancel [id]` | Hủy kèo |
| `/signals` | Danh sách kèo gần nhất |
| `/signal_stats` | Thống kê win rate tổng hợp |
| `/source_stats` | Win rate theo từng nguồn kèo (AUTO/TV/TREND_SCAN/ADMIN) |
| `/signal_report` | Báo cáo chi tiết thắng/thua |
| `/whales` | Danh sách known whales |
| `/whale_scores` | Điểm xếp hạng whale |

**Cú pháp `/signal`:**
```
/signal BTC LONG
Entry: 65000
TP1: 67000
TP2: 69000
TP3: 72000
SL: 63000
```

#### `tv_webhook.py` — TradingView Webhook

aiohttp server lắng nghe `POST /webhook/tv`. Khi nhận alert từ Pine Script:
- Xác thực `X-TV-Secret` header hoặc `?secret=` query param
- Parse payload `{coin, direction, entry?, tp1, tp2?, tp3?, sl, leverage?, note?}`
- Gọi `tracker.create_and_post(source="TV")` — bỏ qua trend gate, TP/SL đến từ Pine Script
- Endpoint: `http://server:TV_WEBHOOK_PORT/webhook/tv`

#### `poller.py` — PositionPoller

- Poll open positions whale via Hyperliquid REST mỗi 60 giây
- Tính real-time PnL, emit `PNL_MILESTONE` khi đạt mốc
- Chỉnh sửa message Telegram gốc in-place qua `auto_watch_msgs`

---

### 4.5 `src/storage/database.py` — Database

**Tables:**

| Table | Mô tả |
|-------|-------|
| `users` | User đã đăng ký, threshold riêng, trạng thái |
| `watchlist` | `(user_id, address)` — địa chỉ whale theo dõi |
| `alert_log` | Dedup log với timestamp, tránh spam alert |
| `known_whales` | Địa chỉ whale đã biết + metadata |
| `whale_scores` | Điểm xếp hạng theo lịch sử giao dịch |
| `auto_watch` | Vị thế đang tự động theo dõi PnL |
| `auto_watch_msgs` | Message ID để edit in-place |
| `signals` | Kèo: entry/TP/SL/status/channel_msg_id |

**Key `signals` columns:**
```sql
coin          TEXT    -- BTC, ETH, ...
direction     TEXT    -- LONG | SHORT
entry_price   REAL
tp1, tp2, tp3 REAL
sl_price      REAL
leverage      INTEGER
status        TEXT    -- PENDING|ACTIVE|TP1_HIT|TP2_HIT|TP3_HIT|SL_HIT|CANCELLED
order_type    TEXT    -- MARKET | LIMIT
source        TEXT    -- ADMIN | AUTO | TV | TREND_SCAN
quality_score REAL
channel_msg_id INTEGER -- Telegram message ID để edit khi TP/SL hit
created_at    TEXT
updated_at    TEXT
```

**Schema Migration:** `ALTER TABLE ... ADD COLUMN` trong try/except tại init — tương thích ngược với DB cũ.

---

## 5. Luồng Dữ Liệu

### Flow 1: Whale Alert

```
Hyperliquid WS
  → raw trade payload
  → WhaleDetector.process_trades()
  → WhaleAlert(type=BIG_TRADE, symbol, size_usd, ...)
  → AlertEngine._route_alert()
      ├─ check dedup (alert_log)
      ├─ global alert → tất cả user đang bật
      └─ watchlist alert → chỉ user subscribe address đó
```

### Flow 2: Auto Kèo (từ whale)

```
WhaleDetector phát hiện trade > threshold
  → SignalTracker.maybe_create_auto_signal(alert)
      ├─ check daily SL limit
      ├─ check has_active_signal(coin) → PENDING/ACTIVE → skip
      ├─ get_multi_trend(coin) → direction + votes
      ├─ direction == whale direction? votes >= TREND_MIN_SCORE?
      ├─ tính quality_score (trend + DOM + confluence)
      ├─ quality_score >= SIGNAL_MIN_QUALITY_SCORE? (50)
      ├─ lấy ATR từ trend_cache → tính entry/SL/TP
      ├─ db.insert signal (status=PENDING, source=AUTO)
      └─ _post_to_channel() → SIGNAL_CHANNEL_ID
```

### Flow 3: Kèo từ Volume Spike (TrendScanner)

```
TrendDetector: poll 4h candle
  → phát hiện volume/MA20 >= SCAN_VOLUME_MIN
  → TrendScanner.on_volume_spike(coin, ratio, direction)
      ├─ check scan_enabled, daily_count, cooldown
      ├─ get_multi_trend(coin) >= scan_min_trend_score?
      ├─ DOM không đối nghịch mạnh?
      └─ signal_tracker.maybe_create_scan_signal()
           → db.insert signal (source=TREND_SCAN)
           → _post_to_channel()
```

### Flow 4: Kèo từ TradingView

```
Pine Script alert → POST /webhook/tv
  → tv_webhook._handle_webhook()
      ├─ xác thực X-TV-Secret
      ├─ parse {coin, direction, entry, tp1/2/3, sl, leverage}
      └─ tracker.create_and_post(source="TV")
           → db.insert signal
           → _post_to_channel()
```

### Flow 5: TP/SL Tracking

```
start_price_poll() — REST poll 5s
  → lấy mark price từ Hyperliquid
  → _check_signals(current_prices)
      ├─ PENDING: giá chạm entry → ACTIVE → edit message
      ├─ ACTIVE: giá chạm TP1 → TP1_HIT → hit notification
      ├─ TP1_HIT: giá chạm TP2 → TP2_HIT
      ├─ TP2_HIT: giá chạm TP3 → TP3_HIT → đóng kèo
      ├─ bất kỳ: giá chạm SL → SL_HIT → đóng kèo
      ├─ ACTIVE/TP1_HIT: reversal check → CANCELLED nếu đủ điều kiện
      └─ PENDING: timeout → CANCELLED
```

### Flow 6: Confluence Alert

```
6+ detectors emit signals
  → confluence_scorer.ingest(symbol, direction, source, size_usd)
  → buffer in-memory

Mỗi 30s:
  → group by (symbol, direction) trong 300s window
  → count sources + weighted score
  → nếu sources >= 3 AND weighted >= 5
  → AlertEngine.route CONFLUENCE alert → users
```

---

## 6. Hệ Thống Kèo (Signal Lifecycle)

### Vòng Đời Đầy Đủ

```
┌─────────────────────────────────────────────────────────┐
│                        PENDING                          │
│  Kèo vừa tạo, chờ giá chạm vào entry zone             │
│  Timeout: SIGNAL_PENDING_TIMEOUT_HOURS (mặc định 4h)   │
└────────────────────────┬────────────────────────────────┘
                         │ giá chạm entry
                         ▼
┌─────────────────────────────────────────────────────────┐
│                        ACTIVE                           │
│  Đang theo dõi TP/SL                                   │
│  Reversal check mỗi poll cycle                         │
└──────┬─────────────────┬──────────────────┬────────────┘
       │ chạm TP1        │ chạm SL          │ reversal
       ▼                 ▼                  ▼
  TP1_HIT            SL_HIT          CANCELLED
  (tiếp tục          (đóng,          (đóng,
   track)             tính thua)      không tính W/L)
       │
       │ chạm TP2
       ▼
  TP2_HIT
       │
       │ chạm TP3
       ▼
  TP3_HIT (thắng toàn bộ)
```

### Telegram Message Format

Mỗi kèo được post lên `SIGNAL_CHANNEL_ID` với format:
```
🐋 AUTO SIGNAL — BTC LONG

📍 Entry: $65,000
🎯 TP1: $67,000 (+3.1%)
🎯 TP2: $69,000 (+6.2%)
🎯 TP3: $72,000 (+10.8%)
🛡️ SL: $63,000 (-3.1%)

📊 Trend: 3/3 TF xác nhận
💪 Quality: 65/100
⚡ Nguồn: Whale $2.5M LONG
```

Message được **edit in-place** khi TP/SL hit, không tạo message mới.

---

## 7. Cấu Hình

Tất cả config nằm trong `config/settings.py` dưới dạng `Config` dataclass, load từ `.env`.

### Nhóm Whale

```env
MIN_TRADE_SIZE_USD=100000
MIN_POSITION_SIZE_USD=500000
MIN_LIQUIDATION_SIZE_USD=200000
MIN_PNL_ALERT_USD=50000
ALERT_COOLDOWN_SECONDS=300
```

### Nhóm Kèo (Signal)

```env
AUTO_SIGNAL_ENABLED=true
AUTO_SIGNAL_MIN_USD=500000
AUTO_SIGNAL_MIN_USD_ALT=200000
AUTO_SIGNAL_MAJOR_COINS=BTC,ETH
SIGNAL_MIN_QUALITY_SCORE=50       # Điểm chất lượng tối thiểu (0-100)
SIGNAL_CHANNEL_ID=-1001234567890
```

### Nhóm Trend

```env
TREND_POLL_INTERVAL_1H=300         # Poll klines 1h mỗi 5 phút
TREND_POLL_INTERVAL_4H=900         # Poll klines 4h mỗi 15 phút
TREND_POLL_INTERVAL_1D=3600        # Poll klines 1d mỗi 1 giờ
TREND_MIN_SCORE=2                  # Tối thiểu 2/3 TF xác nhận
TREND_ATR_SL_MULT=1.5
TREND_ATR_TP_MULT=3.0
```

### Nhóm Multi-Source

```env
BINANCE_ENABLED=true
BINANCE_SYMBOLS=BTC,ETH,SOL,BNB,DOGE,AVAX

BYBIT_ENABLED=true
BYBIT_SYMBOLS=BTC,ETH,SOL

OKX_ENABLED=false
OKX_SYMBOLS=BTC,ETH,SOL,BNB,DOGE,XRP,AVAX

COINGLASS_API_KEY=
OI_SPIKE_THRESHOLD=5.0             # % thay đổi OI để alert
FUNDING_EXTREME_HIGH=0.10
FUNDING_EXTREME_LOW=-0.05

CONFLUENCE_ENABLED=true
CONFLUENCE_MIN_SOURCES=3
CONFLUENCE_MIN_SCORE_WEIGHTED=5
CONFLUENCE_WINDOW=300              # Cửa sổ tổng hợp 300s
```

### Nhóm Signal Lifecycle

```env
REVERSAL_CUT_ENABLED=true
REVERSAL_MIN_SCORE=4               # Điểm trend ngược để cut ACTIVE/TP2
REVERSAL_GRACE_MINUTES=60
REVERSAL_ALT_MIN_SCORE=5           # Ngưỡng cao hơn cho altcoin
SIGNAL_PENDING_TIMEOUT_HOURS=4
DAILY_SL_LIMIT=3
DAILY_LOSS_LIMIT_ENABLED=true

# TP1: move SL to entry
TP1_REVERSAL_MOVE_SL_ENABLED=true
TP1_REVERSAL_MIN_SCORE=2
```

### Nhóm Trend Scanner

```env
SCAN_ENABLED=false                 # Volume spike → kèo (cần trend_detector trigger)
SCAN_VOLUME_MIN=2.0
SCAN_MIN_TREND_SCORE=2
SCAN_DAILY_MAX=2
SCAN_COIN_COOLDOWN_HOURS=24
```

### Nhóm TradingView Webhook

```env
TV_WEBHOOK_ENABLED=false
TV_WEBHOOK_PORT=8080
TV_WEBHOOK_SECRET=                 # Header X-TV-Secret hoặc ?secret=
```

### Nhóm DOM

```env
DOM_ENABLED=true
DOM_COINS=BTC,ETH,SOL,ARB,DOGE,AVAX
DOM_WALL_MIN_USD=1000000
DOM_WALL_DISTANCE_MAX_PCT=1.5      # 1.5% từ mid price
DOM_BID_ASK_BULLISH=1.5
DOM_BID_ASK_BEARISH=0.67
DOM_ABSORPTION_PCT_THRESHOLD=25    # Wall giảm 25%
DOM_BOOK_DEPTH_LEVELS=20
```

### Nhóm Ecosystem

```env
ECOSYSTEM_ENABLED=true
ECOSYSTEM_VOLUME_SPIKE_MIN=2.5
ECOSYSTEM_CALL_MIN_TREND_SCORE=2
ECOSYSTEM_SIGNAL_QUALITY_PENALTY=15
```

---

## 8. Cài Đặt & Chạy

### Chạy Local

```bash
pip install -r requirements.txt
cp .env.example .env
# Điền: TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, SIGNAL_CHANNEL_ID
python main.py
```

### Chạy Docker

```bash
docker-compose up -d
docker-compose logs -f
docker-compose restart
```

### Biến Bắt Buộc

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
ADMIN_CHAT_ID=123456789
```

---

## 9. Lệnh Bot Telegram

### Lệnh Người Dùng

| Lệnh | Mô tả |
|------|-------|
| `/start` | Bắt đầu sử dụng bot |
| `/help` | Xem danh sách lệnh |
| `/filter` | Lọc cỡ lệnh: cá nhỏ / cá to / cá khủng |
| `/watchlist` | Xem địa chỉ đang theo dõi |
| `/add <address>` | Thêm địa chỉ whale vào watchlist |
| `/remove <address>` | Xóa khỏi watchlist |
| `/top` | Top PnL cao nhất đang theo dõi |
| `/threshold <usd>` | Đặt ngưỡng alert riêng |
| `/sources` | Xem nguồn dữ liệu đang hoạt động |
| `/confluence` | Bật/tắt confluence alerts |
| `/settings` | Xem/chỉnh cài đặt |
| `/signals` | Danh sách kèo gần nhất |

### Lệnh Admin

| Lệnh | Mô tả |
|------|-------|
| `/signal` | Tạo kèo thủ công |
| `/cancel [id]` | Hủy kèo đang active |
| `/signal_stats` | Thống kê W/L tổng hợp |
| `/source_stats` | Win rate theo nguồn kèo (AUTO/TV/TREND_SCAN/ADMIN) |
| `/signal_report` | Báo cáo chi tiết theo ngày/tuần |
| `/whales` | Danh sách whale đang track |
| `/whale_scores` | Bảng xếp hạng whale theo điểm |

---

## 10. Database Schema

```sql
CREATE TABLE users (
    chat_id       INTEGER PRIMARY KEY,
    username      TEXT,
    threshold_usd REAL DEFAULT 100000,
    is_active     BOOLEAN DEFAULT 1,
    created_at    TEXT
);

CREATE TABLE watchlist (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER,
    address    TEXT,
    label      TEXT,
    created_at TEXT
);

CREATE TABLE alert_log (
    id         INTEGER PRIMARY KEY,
    alert_key  TEXT UNIQUE,
    created_at TEXT
);

CREATE TABLE signals (
    id             INTEGER PRIMARY KEY,
    coin           TEXT,
    direction      TEXT,      -- LONG | SHORT
    entry_price    REAL,
    tp1            REAL,
    tp2            REAL,
    tp3            REAL,
    sl_price       REAL,
    leverage       INTEGER,
    status         TEXT,      -- PENDING|ACTIVE|TP1_HIT|TP2_HIT|TP3_HIT|SL_HIT|CANCELLED
    order_type     TEXT,      -- MARKET | LIMIT
    source         TEXT,      -- ADMIN | AUTO | TV | TREND_SCAN
    channel_msg_id INTEGER,
    quality_score  REAL,
    created_at     TEXT,
    updated_at     TEXT
);

CREATE TABLE auto_watch (
    id             INTEGER PRIMARY KEY,
    address        TEXT,
    coin           TEXT,
    direction      TEXT,
    size_usd       REAL,
    entry_price    REAL,
    created_at     TEXT
);

CREATE TABLE auto_watch_msgs (
    id         INTEGER PRIMARY KEY,
    watch_id   INTEGER,
    chat_id    INTEGER,
    msg_id     INTEGER,
    created_at TEXT
);
```

---

## Phụ Lục: Startup Flow

```python
# main.py — thứ tự khởi động
async def main():
    setup_logging()
    await db.init()                          # 1. DB + schema migration

    bot = Bot(config.bot_token)              # 2. Telegram bot
    dp = Dispatcher()
    dp.include_router(signal_router)
    dp.include_router(router)

    signal_tracker = SignalTracker(bot)      # 3. Signal tracker (singleton)
    _st_module.set_tracker(signal_tracker)

    # 4. Wire dependencies
    ecosystem_detector.set_signal_tracker(signal_tracker)
    ecosystem_detector.set_dom_analyzer(dom_analyzer)
    trend_scanner.set_signal_tracker(signal_tracker)
    trend_scanner.set_dom_analyzer(dom_analyzer)

    detector = WhaleDetector()               # 5. Whale detection
    engine = AlertEngine(bot, detector)

    # 6. Pre-load watchlist + known whales vào HL WS subscription
    all_watched = await db.get_all_watched_addresses()
    known_whales = await db.get_known_whales()
    for addr in [*all_watched, *[w["address"] for w in known_whales]]:
        hl_ws._watched_users.add(addr)

    # 7. Multi-source components
    coinglass  = CoinglassRest(config)
    confluence = ConfluenceScorer(engine, config)
    oi_detector   = OISpikeDetector(coinglass, engine, config, confluence=confluence)
    funding_det   = FundingRateDetector(coinglass, engine, config, confluence=confluence)

    # 8. Build task list (WS + pollers)
    tasks = [
        dp.start_polling(bot),
        hl_ws.connect(),
        PositionPoller(engine, interval=60).start(),
        signal_tracker.start_price_poll(interval=5),
        oi_detector.run(),
        funding_det.run(),
        confluence.run(),
        run_trend_poll(trend_coins),
    ]
    if config.binance_enabled: tasks.append(BinanceWS(config).start())
    if config.bybit_enabled:   tasks.append(BybitWS(config).start())
    if config.okx_enabled:     tasks.append(OkxWS(config).start())
    if config.tv_webhook_enabled: tasks.append(run_webhook_server())

    await asyncio.gather(*tasks)
```
