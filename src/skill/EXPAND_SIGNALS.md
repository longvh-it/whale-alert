# EXPAND_SIGNALS.md — Mở rộng nguồn kèo Futu cho Whale Bot

## Mục tiêu

Mở rộng bot hiện tại (đang chỉ theo dõi Hyperliquid) để tổng hợp tín hiệu từ nhiều nguồn:
Binance Futures, Bybit Futures, và Coinglass. Thêm 3 loại detector mới: OI Spike, Funding Rate Extreme, và Confluence Scoring.

Không được phá vỡ logic hiện tại. Mọi nguồn mới đều plug-in vào pipeline sẵn có.

---

## Yêu cầu chung

- Tất cả API client mới phải implement cùng interface với `HyperliquidWS`: method `.on(event, callback)` và `.start()`
- Alert mới phải dùng chung `WhaleAlert` dataclass (mở rộng nếu cần, không tạo class mới)
- Mọi thứ phải async (`asyncio` + `aiohttp`)
- Không dùng thư viện websocket nào khác ngoài `websockets` đang dùng
- Thêm env var mới vào `config/settings.py` và `.env.example`, không hardcode
- Không cần viết test

---

## Task 1 — Binance Futures WebSocket Client

**File:** `src/api/binance_ws.py`

Kết nối WebSocket public của Binance Futures (không cần API key).

**Streams cần subscribe:**
```
wss://fstream.binance.com/stream?streams=
  btcusdt@aggTrade/
  ethusdt@aggTrade/
  solusdt@aggTrade/
  btcusdt@forceOrder/
  ethusdt@forceOrder/
  solusdt@forceOrder/
  btcusdt@openInterest/   ← polling REST vì WS không có
```

**Symbols cần theo dõi** (lấy từ config, default): `BTC, ETH, SOL, BNB, DOGE, AVAX`

**Events cần emit:**
1. `binance_trade` — từ `aggTrade` stream, chỉ emit nếu `quoteQty >= MIN_TRADE_SIZE_USD`
2. `binance_liquidation` — từ `forceOrder` stream, chỉ emit nếu `ap * q >= MIN_LIQUIDATION_SIZE_USD`

**Payload chuẩn hóa** (dùng dict, map sang `WhaleAlert` ở detector):
```python
{
    "source": "binance",
    "symbol": "BTC",        # normalized, không có USDT
    "side": "BUY"|"SELL",
    "size_usd": float,
    "price": float,
    "timestamp": int,       # ms
    "raw": dict             # raw Binance payload
}
```

**Auto-reconnect:** logic giống `HyperliquidWS`, backoff 5s, max 10 lần.

**Config mới cần thêm vào `settings.py`:**
```
BINANCE_ENABLED=true
BINANCE_SYMBOLS=BTC,ETH,SOL,BNB,DOGE,AVAX
```

---

## Task 2 — Bybit Futures WebSocket Client

**File:** `src/api/bybit_ws.py`

Kết nối WebSocket public Bybit Futures V5.

**Endpoint:** `wss://stream.bybit.com/v5/public/linear`

**Topics cần subscribe:**
```json
{
  "op": "subscribe",
  "args": [
    "publicTrade.BTCUSDT",
    "publicTrade.ETHUSDT",
    "publicTrade.SOLUSDT",
    "liquidation.BTCUSDT",
    "liquidation.ETHUSDT",
    "liquidation.SOLUSDT"
  ]
}
```

Bybit yêu cầu gửi ping mỗi 20s để giữ kết nối — implement heartbeat loop riêng.

**Events cần emit:** `bybit_trade`, `bybit_liquidation` — cùng payload format như Binance ở Task 1.

**Config mới:**
```
BYBIT_ENABLED=true
BYBIT_SYMBOLS=BTC,ETH,SOL
```

---

## Task 3 — Coinglass REST Poller

**File:** `src/api/coinglass_rest.py`

Poll Coinglass API mỗi 5 phút để lấy aggregated data.

**Endpoints dùng (free, không cần key):**
```
GET https://open-api.coinglass.com/public/v2/open_interest
    ?symbol=BTC&interval=0
    
GET https://open-api.coinglass.com/public/v2/funding
    ?symbol=BTC

GET https://open-api.coinglass.com/public/v2/liquidation_history
    ?symbol=BTC&interval=1  (1h)
```

**Lưu ý:** Nếu Coinglass yêu cầu API key (free tier), đọc từ env `COINGLASS_API_KEY` (optional). Nếu không có key thì skip gracefully, log warning.

**Data cần lấy:**
- Open Interest tổng hợp toàn thị trường (tất cả sàn) theo symbol
- Funding rate hiện tại của top exchanges
- Liquidation tổng hợp 1h gần nhất

**Không emit event** — class này chỉ cung cấp data cho các detector (pull model, không push).

**Interface:**
```python
class CoinglassRest:
    async def get_open_interest(self, symbol: str) -> dict  # {total_oi_usd, change_pct_1h, change_pct_24h}
    async def get_funding_rates(self, symbol: str) -> dict  # {binance: float, bybit: float, hyperliquid: float, ...}
    async def get_liquidations_1h(self, symbol: str) -> dict  # {long_usd: float, short_usd: float}
```

**Config mới:**
```
COINGLASS_API_KEY=        # optional, để trống nếu không có
COINGLASS_POLL_INTERVAL=300
```

---

## Task 4 — OI Spike Detector

**File:** `src/detector/oi_detector.py`

**Class:** `OISpikeDetector`

Phát hiện Open Interest tăng/giảm đột biến so với baseline.

**Logic:**
1. Mỗi `OI_POLL_INTERVAL` giây (default 300), gọi `CoinglassRest.get_open_interest()` cho từng symbol trong watchlist
2. So sánh với giá trị OI lần poll trước (lưu in-memory dict)
3. Nếu `abs(change_pct) >= OI_SPIKE_THRESHOLD` (default 5%) thì tạo alert

**Alert type mới cần thêm vào `WhaleAlert`:** `OI_SPIKE`

**Message format Telegram:**
```
📊 OI SPIKE — BTC
━━━━━━━━━━━━━━━━
📈 OI tăng +7.3% trong 5 phút
💰 Total OI: $12.4B → $13.3B
🔗 Source: Coinglass (aggregated)
```

**Config mới:**
```
OI_SPIKE_THRESHOLD=5.0     # % thay đổi để trigger
OI_POLL_INTERVAL=300       # giây
```

---

## Task 5 — Funding Rate Extreme Detector

**File:** `src/detector/funding_detector.py`

**Class:** `FundingRateDetector`

Phát hiện funding rate ở mức cực đoan (thị trường quá long hoặc quá short).

**Logic:**
1. Poll funding rates mỗi `FUNDING_POLL_INTERVAL` giây (default 3600 = mỗi giờ)
2. Tính average funding rate across exchanges cho mỗi symbol
3. Nếu `avg_funding >= FUNDING_EXTREME_HIGH` (default 0.10%) hoặc `avg_funding <= FUNDING_EXTREME_LOW` (default -0.05%) thì tạo alert

**Alert type mới:** `FUNDING_EXTREME`

**Message format:**
```
💸 FUNDING EXTREME — ETH
━━━━━━━━━━━━━━━━━━━━━
🔴 Funding rate cực cao: +0.15%
📊 Binance: +0.17% | Bybit: +0.13% | HL: +0.14%
⚠️ Thị trường đang over-leveraged LONG
💡 Lịch sử: thường precede short squeeze
```

**Config mới:**
```
FUNDING_EXTREME_HIGH=0.10  # %
FUNDING_EXTREME_LOW=-0.05  # %
FUNDING_POLL_INTERVAL=3600
```

---

## Task 6 — Multi-source Confluence Scorer

**File:** `src/aggregator/confluence_scorer.py`

**Class:** `ConfluenceScorer`

Gộp signals từ nhiều nguồn trong một time window để tạo high-confidence alert.

**Logic:**
1. Lắng nghe tất cả events từ các detector (dùng in-memory event bus đơn giản, không cần thêm lib)
2. Trong cửa sổ `CONFLUENCE_WINDOW` giây (default 300), group signals theo `(symbol, direction)`
3. Tính score = số nguồn độc lập cùng báo cùng chiều
4. Nếu `score >= CONFLUENCE_MIN_SOURCES` (default 3) thì emit `CONFLUENCE_ALERT`

**Scoring:**
```
+1  Binance large trade (cùng chiều)
+1  Bybit large trade (cùng chiều)
+1  Hyperliquid large trade (cùng chiều)
+1  OI Spike (matching direction)
+1  Funding extreme (counter-signal = fading opportunity)
+1  Liquidation cascade (cùng chiều với dòng tiền đang thắng)
```

**Alert type mới:** `CONFLUENCE`

**Message format:**
```
🎯 CONFLUENCE SIGNAL — BTC LONG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ Score: 4/6 nguồn cùng chiều (5 phút)
✅ Hyperliquid: Whale mua $2.1M
✅ Binance: Large buy $850K
✅ Bybit: Large buy $1.2M  
✅ OI: Tăng +6.1%
❌ Funding: Neutral
❌ Liquidation: Không có
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Không phải lời khuyên đầu tư
```

**Config mới:**
```
CONFLUENCE_WINDOW=300          # giây
CONFLUENCE_MIN_SOURCES=3       # số nguồn tối thiểu để trigger
CONFLUENCE_ENABLED=true
```

---

## Task 7 — Kết nối vào main.py

Cập nhật `main.py` để khởi động các client và detector mới theo đúng thứ tự:

```python
# Sau khi init WhaleDetector và AlertEngine hiện tại:

if settings.BINANCE_ENABLED:
    binance_ws = BinanceWS(settings)
    binance_ws.on("binance_trade", whale_detector.on_binance_trade)
    binance_ws.on("binance_liquidation", whale_detector.on_binance_liquidation)

if settings.BYBIT_ENABLED:
    bybit_ws = BybitWS(settings)
    bybit_ws.on("bybit_trade", whale_detector.on_bybit_trade)
    bybit_ws.on("bybit_liquidation", whale_detector.on_bybit_liquidation)

coinglass = CoinglassRest(settings)
oi_detector = OISpikeDetector(coinglass, alert_engine, settings)
funding_detector = FundingRateDetector(coinglass, alert_engine, settings)
confluence_scorer = ConfluenceScorer(alert_engine, settings)

# Thêm vào asyncio.gather():
tasks = [
    ...,  # tasks hiện tại
    binance_ws.start() if settings.BINANCE_ENABLED else asyncio.sleep(0),
    bybit_ws.start() if settings.BYBIT_ENABLED else asyncio.sleep(0),
    oi_detector.run(),
    funding_detector.run(),
]
```

---

## Task 8 — Cập nhật Telegram commands

Thêm vào `src/bot/handlers.py`:

**Command `/sources`** — hiển thị trạng thái các nguồn đang active:
```
📡 Nguồn dữ liệu đang hoạt động:
✅ Hyperliquid (WS)
✅ Binance Futures (WS)
✅ Bybit Futures (WS)
✅ Coinglass (REST, 5m poll)
━━━━━━━━━━━━━━━━━
📊 OI Detector: enabled (threshold 5%)
💸 Funding Detector: enabled (±0.10%)
🎯 Confluence: enabled (min 3 sources)
```

**Command `/confluence on|off`** — toggle confluence alerts cho user hiện tại (lưu vào `users` table, thêm column `confluence_enabled BOOLEAN DEFAULT 1`)

---

## File cần tạo mới (tóm tắt)

```
src/api/binance_ws.py
src/api/bybit_ws.py
src/api/coinglass_rest.py
src/detector/oi_detector.py
src/detector/funding_detector.py
src/aggregator/__init__.py
src/aggregator/confluence_scorer.py
```

## File cần sửa

```
src/detector/whale_detector.py   — thêm on_binance_trade, on_bybit_trade, on_binance_liquidation, on_bybit_liquidation handlers; thêm OI_SPIKE, FUNDING_EXTREME, CONFLUENCE vào AlertType enum
config/settings.py               — thêm tất cả env vars mới
.env.example                     — thêm tất cả env vars mới với giá trị default
main.py                          — wiring các component mới
src/bot/handlers.py              — thêm /sources và /confluence commands
src/storage/database.py          — thêm column confluence_enabled vào users table (migration nhẹ)
requirements.txt                 — kiểm tra, thêm nếu thiếu: aiohttp (nếu chưa có)
```

---

## Thứ tự làm

Làm lần lượt theo task, commit sau mỗi task. Nếu Coinglass free API không hoạt động hoặc bị block, dùng Binance public REST thay thế cho OI và funding data:

```
GET https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT
GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT  ← funding rate
```

Không cần API key, public endpoint.
