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
| **Signal Confluence** | Tổng hợp tín hiệu từ 6 nguồn (Hyperliquid, Binance, Bybit, OI, Funding, Liquidation) |
| **Auto Kèo** | Tự tạo paper trade khi whale + xu hướng kỹ thuật xác nhận |

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
│  Binance REST ─────── klines 1h/4h/1d (poll mỗi 15 phút)          │
│  Coinglass REST ───── OI + Funding (poll mỗi 5 phút)              │
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
│  TrendScanner ───── quét định kỳ, tạo kèo không cần whale          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────┐
│  AGGREGATION (Tổng Hợp Tín Hiệu)                                    │
│                                                                     │
│  ConfluenceScorer                                                   │
│    • Buffer in-memory theo (symbol, direction) trong 300s           │
│    • Nguồn có trọng số: HL=3, OI=2, Liq=2, Binance/Bybit/Fund=1   │
│    • Fire khi ≥3 nguồn + weighted score ≥5                          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────┐
│  SIGNAL TRACKER (Quản Lý Kèo)                                       │
│                                                                     │
│  maybe_create_auto_signal()                                         │
│    whale trade → trend gate → quality score → ATR TP/SL → post     │
│                                                                     │
│  start_price_poll() — REST poll 5s                                  │
│    PENDING → ACTIVE → TP1_HIT → TP2_HIT → TP3_HIT                 │
│                              └→ SL_HIT                             │
│                                                                     │
│  Tự động: TP1 move SL to entry, reversal cut, stale cancel         │
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
│   │   └── trend_scanner.py     # Periodic scanner (kèo không cần whale)
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
│   │   └── poller.py            # PositionPoller: PnL tracking
│   │
│   ├── storage/
│   │   └── database.py          # Async SQLite (aiosqlite), schema migrations
│   │
│   └── doc/                     # Tài liệu thiết kế
│       └── PROJECT_DOC.md       # File này
│
├── data/                        # whale_bot.db (SQLite, mount volume)
├── logs/                        # Log xoay vòng theo ngày
├── main.py                      # Entry point: orchestrate tất cả tasks
├── requirements.txt
├── docker-compose.yml
├── koyeb.yaml                   # Koyeb deployment config
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

#### `binance_ws.py`
- `aggTrade` stream: giá và khối lượng giao dịch tổng hợp
- `forceOrder` stream: thanh lý bắt buộc Binance Futures
- Dùng cho `AlertEngine.process_binance_trade/liquidation()`

#### `bybit_ws.py`
- Bybit V5 linear public stream
- 20s heartbeat tự động để giữ kết nối
- Feed vào `AlertEngine.process_bybit_trade/liquidation()`

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
| `OI_SPIKE` | OI thay đổi > `OI_SPIKE_THRESHOLD` |
| `FUNDING_EXTREME` | Funding > `FUNDING_EXTREME_HIGH` hoặc < `FUNDING_EXTREME_LOW` |
| `CONFLUENCE` | ≥3 nguồn xác nhận cùng chiều |

**Per-address tracking:** Lưu lịch sử vị thế để phát hiện `POSITION_FLIP`. Tất cả format message nằm trong `WhaleAlert.format_message()`.

#### `alert_engine.py`

- **Route global alerts**: gửi đến tất cả user đang active
- **Route watchlist alerts**: chỉ gửi user đã subscribe địa chỉ đó
- **Dedup**: bảng `alert_log` + `ALERT_COOLDOWN_SECONDS` — tránh spam cùng alert
- Expose `process_binance_trade/liquidation()` và `process_bybit_trade/liquidation()`

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

#### `dom_analyzer.py`

Phân tích độ sâu sổ lệnh (L2 order book) mỗi khi có cập nhật:

| Phân tích | Logic |
|-----------|-------|
| **Bid/Ask Ratio** | Tổng khối lượng bid / ask. > `DOM_BID_ASK_BULLISH` → bull signal |
| **Wall Detection** | Lệnh đơn > `DOM_WALL_MIN_USD` trong khoảng `DOM_WALL_DISTANCE_MAX_PCT` từ mid |
| **Wall Absorption** | Kích thước wall giảm ≥ `DOM_ABSORPTION_PCT_THRESHOLD` → tường đang bị hấp thụ |

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

Scanner định kỳ — tạo kèo dựa trên xu hướng kỹ thuật thuần túy, **không cần whale**:
- Chạy theo `TREND_POLL_INTERVAL_1H/4H/1D`
- Multi-timeframe scan
- Khi trend đủ mạnh → gọi `maybe_create_auto_signal()` trực tiếp

#### `confluence_scorer.py`

**Trọng Số Nguồn:**
```
Hyperliquid whale trade  → 3 điểm  (đáng tin nhất)
OI Spike                 → 2 điểm
Liquidation              → 2 điểm
Binance / Bybit trade    → 1 điểm mỗi nguồn
Funding extreme          → 1 điểm
```

**Logic Fire:**
1. `ingest(symbol, direction, source)` → buffer in-memory
2. Mỗi 30s: group theo `(symbol, direction)` trong cửa sổ 300s
3. Nếu `sources ≥ CONFLUENCE_MIN_SOURCES` VÀ `weighted_score ≥ CONFLUENCE_MIN_SCORE_WEIGHTED`
4. → emit `CONFLUENCE` alert, cooldown để tránh lặp

---

### 4.3 `src/signals/signal_tracker.py` — Quản Lý Kèo

**Điều Kiện Tạo Kèo Tự Động:**
```
whale trade đến
  ├─ size_usd >= threshold? (major coin vs altcoin)
  ├─ daily_sl_limit chưa đạt?
  ├─ db.has_active_signal(coin)?  [PENDING/ACTIVE thì block]
  ├─ get_multi_trend(coin) → cùng chiều whale?
  ├─ confirmed_timeframes >= TREND_MIN_SCORE?
  ├─ quality_score >= SIGNAL_MIN_QUALITY_SCORE?
  └─ → tính TP/SL từ ATR → tạo kèo → post channel
```

**Quality Score (điểm chất lượng):**

| Thành phần | Điểm |
|-----------|------|
| Trend score (0-3 timeframes) | +1 mỗi TF |
| DOM confirm cùng chiều | +1 |
| Confluence event | +1 |
| Ecosystem detection | -`ECOSYSTEM_SIGNAL_QUALITY_PENALTY` |

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
| **TP1 Continue** | TP1_HIT | Tính milestone, tiếp tục tracking TP2/TP3 với SL giữ nguyên |
| **TP1 Reversal Cut** | TP1_HIT + reversal score ≥ 4 | Đóng tại entry (hoà vốn) → CANCELLED |
| **Reversal Cut** | ACTIVE/TP2_HIT + opposite trend score ≥ `REVERSAL_MIN_SCORE` trong `REVERSAL_GRACE_MINUTES` | Auto close → CANCELLED |
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
           TP2_HIT  SL_HIT (sau khi SL move)
              │
              ▼
           TP3_HIT
```

---

### 4.4 `src/bot/` — Telegram Interface

#### `handlers.py` — User Commands

| Lệnh | Chức năng |
|------|-----------|
| `/start` | Đăng ký user, hiển thị menu |
| `/watchlist` | Xem danh sách địa chỉ đang theo dõi |
| `/add <address>` | Thêm địa chỉ vào watchlist |
| `/remove <address>` | Xóa địa chỉ khỏi watchlist |
| `/threshold <usd>` | Đặt ngưỡng alert riêng |
| `/settings` | Xem/chỉnh cài đặt cá nhân |
| `/help` | Danh sách lệnh |

#### `signal_handlers.py` — Admin Commands

| Lệnh | Chức năng |
|------|-----------|
| `/signal` | Tạo kèo thủ công |
| `/cancel [id]` | Hủy kèo |
| `/signals` | Danh sách kèo đang active |
| `/signal_stats` | Thống kê thắng/thua |
| `/signal_report` | Báo cáo chi tiết |
| `/whales` | Danh sách whale đang được track |
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

#### `poller.py` — PositionPoller

- Poll open positions whale via Hyperliquid REST mỗi N giây
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
status        TEXT    -- PENDING|ACTIVE|TP1_HIT|...|SL_HIT|CANCELLED
order_type    TEXT    -- MARKET | LIMIT
source        TEXT    -- ADMIN | AUTO
channel_msg_id INTEGER -- Telegram message ID để edit khi TP/SL hit
created_at    TEXT
```

**Schema Migration:** `ALTER TABLE ... ADD COLUMN` trong try/except tại init — tương thích ngược với DB cũ.

---

## 5. Luồng Dữ Liệu

### Flow 1: Whale Alert

```
Hyperliquid WS
  → raw trade payload
  → WhaleDetector.parse_trade()
  → WhaleAlert(type=BIG_TRADE, symbol, size_usd, ...)
  → AlertEngine._route_alert()
      ├─ check dedup (alert_log)
      ├─ global alert → tất cả user đang bật
      └─ watchlist alert → chỉ user subscribe address đó
```

### Flow 2: Auto Kèo

```
WhaleDetector phát hiện trade > threshold
  → SignalTracker.maybe_create_auto_signal(alert)
      ├─ check daily SL limit
      ├─ check has_active_signal(coin) → PENDING/ACTIVE → skip
      ├─ get_multi_trend(coin) → direction + votes
      ├─ direction == whale direction? votes >= TREND_MIN_SCORE?
      ├─ tính quality_score (trend + DOM + confluence)
      ├─ quality_score >= SIGNAL_MIN_QUALITY_SCORE?
      ├─ lấy ATR từ trend_cache → tính entry/SL/TP
      ├─ db.insert signal (status=PENDING)
      └─ _post_to_channel() → SIGNAL_CHANNEL_ID
```

### Flow 3: TP/SL Tracking

```
start_price_poll() — REST poll 5s
  → lấy mark price từ Hyperliquid
  → _check_signals(current_prices)
      ├─ PENDING: giá chạm entry → ACTIVE → edit message
      ├─ ACTIVE: giá chạm TP1 → TP1_HIT → move SL nếu bật
      ├─ TP1_HIT: giá chạm TP2 → TP2_HIT
      ├─ TP2_HIT: giá chạm TP3 → TP3_HIT → đóng kèo
      ├─ bất kỳ: giá chạm SL → SL_HIT → đóng kèo
      ├─ ACTIVE: reversal check → CANCELLED nếu đủ điều kiện
      └─ PENDING: timeout → CANCELLED
```

### Flow 4: Confluence Alert

```
6 detectors emit signals
  → confluence_scorer.ingest(symbol, direction, source)
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
   track)             tính thua)      không tính)
       │
       │ chạm TP2
       ▼
  TP2_HIT
       │
       │ chạm TP3
       ▼
  TP3_HIT (thắng toàn bộ)
```

### Dedup Rule

- Coin bị **block** khi status là `PENDING` hoặc `ACTIVE`
- Sau khi `TP1_HIT` hoặc cao hơn → có thể tạo kèo mới cho coin đó

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
💪 Quality: 8/10
⚡ Nguồn: Whale $2.5M LONG
```

Message được **edit in-place** khi TP/SL hit, không tạo message mới.

---

## 7. Cấu Hình

Tất cả config nằm trong `config/settings.py` dưới dạng `Config` dataclass, load từ `.env`.

### Nhóm Whale

```env
MIN_TRADE_SIZE_USD=100000          # Giao dịch tối thiểu để alert
MIN_POSITION_SIZE_USD=500000       # Vị thế tối thiểu để alert
MIN_LIQUIDATION_SIZE_USD=200000    # Thanh lý tối thiểu để alert
MIN_PNL_ALERT_USD=50000            # PnL mốc để alert
```

### Nhóm Kèo (Signal)

```env
AUTO_SIGNAL_ENABLED=true
AUTO_SIGNAL_MIN_USD=500000         # Ngưỡng tạo kèo BTC/ETH
AUTO_SIGNAL_MIN_USD_ALT=200000     # Ngưỡng tạo kèo altcoin
AUTO_SIGNAL_MAJOR_COINS=BTC,ETH   # Danh sách major coin
SIGNAL_MIN_QUALITY_SCORE=5        # Điểm chất lượng tối thiểu
SIGNAL_CHANNEL_ID=-1001234567890  # Channel đăng kèo
```

### Nhóm Trend

```env
TREND_POLL_INTERVAL_1H=3600        # Poll klines 1h mỗi 1 giờ
TREND_POLL_INTERVAL_4H=3600        # Poll klines 4h mỗi 1 giờ
TREND_POLL_INTERVAL_1D=21600       # Poll klines 1d mỗi 6 giờ
TREND_MIN_SCORE=2                  # Tối thiểu 2/3 TF xác nhận
TREND_ATR_SL_MULT=1.5              # SL = entry ± ATR × 1.5
TREND_ATR_TP_MULT=3.0              # TP3 = entry ± ATR × 3.0
```

### Nhóm Multi-Source

```env
BINANCE_ENABLED=true
BYBIT_ENABLED=true
COINGLASS_API_KEY=                 # Để trống → fallback Binance REST
OI_SPIKE_THRESHOLD=0.05            # 5% thay đổi OI
FUNDING_EXTREME_HIGH=0.10          # Funding > 10% = long quá đà
FUNDING_EXTREME_LOW=-0.05          # Funding < -5% = short quá đà
CONFLUENCE_ENABLED=true
CONFLUENCE_MIN_SCORE_WEIGHTED=5
```

### Nhóm Signal Lifecycle

```env
REVERSAL_CUT_ENABLED=true         # Tự đóng khi trend đảo
REVERSAL_MIN_SCORE=4              # Điểm trend ngược tối thiểu để cut
REVERSAL_GRACE_MINUTES=60         # Thời gian grace trước khi cut
SIGNAL_PENDING_TIMEOUT_HOURS=4    # Timeout PENDING
DAILY_SL_LIMIT=3                  # Max SL/ngày trước khi dừng auto
DAILY_LOSS_LIMIT_ENABLED=true
```

### Nhóm DOM

```env
DOM_ENABLED=true
DOM_COINS=BTC,ETH,SOL             # Coins phân tích depth
DOM_WALL_MIN_USD=1000000          # $1M trở lên mới coi là wall
DOM_WALL_DISTANCE_MAX_PCT=0.02    # Wall trong vòng 2% từ mid
DOM_BID_ASK_BULLISH=1.3           # Bid/Ask > 1.3 → bullish
DOM_BID_ASK_BEARISH=0.7           # Bid/Ask < 0.7 → bearish
DOM_ABSORPTION_PCT_THRESHOLD=0.3  # Wall giảm 30% → absorption
DOM_BOOK_DEPTH_LEVELS=20          # Số level depth theo dõi
```

### Nhóm Ecosystem

```env
ECOSYSTEM_ENABLED=true
ECOSYSTEM_VOLUME_SPIKE_MIN=2.0    # Volume > 2x bình thường
ECOSYSTEM_CALL_MIN_TREND_SCORE=2  # Trend tối thiểu để scan
ECOSYSTEM_SIGNAL_QUALITY_PENALTY=1 # Trừ 1 điểm quality
```

---

## 8. Cài Đặt & Chạy

### Chạy Local

```bash
# 1. Clone và cài dependencies
pip install -r requirements.txt

# 2. Tạo file config
cp .env.example .env
# Điền: TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, SIGNAL_CHANNEL_ID

# 3. Chạy
python main.py
```

### Chạy Docker

```bash
docker-compose up -d

# Xem log
docker-compose logs -f

# Restart
docker-compose restart
```

### Biến Bắt Buộc

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...  # Từ @BotFather
ADMIN_CHAT_ID=123456789               # Chat ID admin
```

### Biến Tùy Chọn

Xem `.env.example` để biết đầy đủ danh sách với giá trị mặc định.

---

## 9. Lệnh Bot Telegram

### Lệnh Người Dùng

| Lệnh | Mô tả |
|------|-------|
| `/start` | Bắt đầu sử dụng bot |
| `/help` | Xem danh sách lệnh |
| `/watchlist` | Xem địa chỉ đang theo dõi |
| `/add <address>` | Thêm địa chỉ whale vào watchlist |
| `/remove <address>` | Xóa khỏi watchlist |
| `/threshold <usd>` | Đặt ngưỡng alert riêng (VD: `/threshold 500000`) |
| `/settings` | Xem/chỉnh cài đặt |

### Lệnh Admin

| Lệnh | Mô tả |
|------|-------|
| `/signal` | Tạo kèo thủ công |
| `/cancel [id]` | Hủy kèo đang active |
| `/signals` | Danh sách kèo đang mở |
| `/signal_stats` | Thống kê W/L tổng hợp |
| `/signal_report` | Báo cáo chi tiết theo ngày/tuần |
| `/whales` | Danh sách whale đang track |
| `/whale_scores` | Bảng xếp hạng whale theo điểm |

---

## 10. Database Schema

```sql
-- User đã đăng ký
CREATE TABLE users (
    chat_id       INTEGER PRIMARY KEY,
    username      TEXT,
    threshold_usd REAL DEFAULT 100000,
    is_active     BOOLEAN DEFAULT 1,
    created_at    TEXT
);

-- Địa chỉ whale theo dõi
CREATE TABLE watchlist (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER,
    address    TEXT,
    label      TEXT,
    created_at TEXT
);

-- Log để dedup alert
CREATE TABLE alert_log (
    id         INTEGER PRIMARY KEY,
    alert_key  TEXT UNIQUE,  -- hash(type+symbol+address)
    created_at TEXT
);

-- Kèo (paper trades)
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
    source         TEXT,      -- ADMIN | AUTO
    channel_msg_id INTEGER,   -- Telegram msg ID để edit
    quality_score  REAL,
    created_at     TEXT,
    updated_at     TEXT
);

-- Vị thế whale đang auto-watch
CREATE TABLE auto_watch (
    id             INTEGER PRIMARY KEY,
    address        TEXT,
    coin           TEXT,
    direction      TEXT,
    size_usd       REAL,
    entry_price    REAL,
    created_at     TEXT
);

-- Message ID để edit PnL in-place
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
    await db.init_db()                    # 1. DB + schema migration

    bot = Bot(config.bot_token)           # 2. Telegram bot
    dp = Dispatcher()
    dp.include_router(handlers.router)
    dp.include_router(signal_handlers.router)

    signal_tracker = SignalTracker(bot)   # 3. Kèo tracker

    dom_analyzer.set_signal_tracker(signal_tracker)       # 4. Wire dependencies
    ecosystem_detector.set_signal_tracker(signal_tracker)
    ecosystem_detector.set_dom_analyzer(dom_analyzer)

    whale_detector = WhaleDetector(alert_engine)          # 5. Whale detection
    alert_engine.set_signal_tracker(signal_tracker)

    # 6. Start data sources
    hl_ws = HyperliquidWS()
    hl_ws.on("trade", whale_detector.process_trade)
    hl_ws.on("l2book", dom_analyzer.on_l2_update)

    binance_ws = BinanceWS()
    bybit_ws = BybitWS()

    position_poller = PositionPoller(bot, db)

    # 7. Run tất cả concurrently
    await asyncio.gather(
        hl_ws.start(),
        binance_ws.start(),
        bybit_ws.start(),
        trend_detector.run(),
        oi_detector.run(),
        funding_detector.run(),
        confluence_scorer.run(),
        signal_tracker.start_price_poll(),
        trend_scanner.run(),
        position_poller.start(),
        dp.start_polling(bot),
    )
```
