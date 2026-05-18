# Hyperliquid Whale Bot

Telegram bot theo dõi hoạt động whale trên Hyperliquid DEX, tổng hợp tín hiệu đa nguồn (Binance, Bybit, OKX, Coinglass), và tự động tạo/quản lý kèo (paper trade) theo xu hướng kỹ thuật.

---

## Tính Năng

### Whale Alert
| Alert | Mô tả | Ngưỡng mặc định |
|-------|-------|----------------|
| Big Trade | Lệnh khớp lớn | > $100K |
| Liquidation | Thanh lý bắt buộc | > $200K |
| Large Position | Vị thế mở lớn | > $500K |
| Position Flip | Đổi chiều Long ↔ Short | — |
| PnL Update | Cập nhật lãi/lỗ real-time | > $50K |
| Watchlist | Hoạt động địa chỉ đang theo dõi | — |

### Multi-Source Signal
| Nguồn | Dữ liệu |
|-------|---------|
| Hyperliquid WS | Trades, liquidations, L2 order book |
| Binance Futures WS | aggTrade, forced liquidations |
| Bybit Futures WS | Linear public stream |
| OKX Futures WS | SWAP public stream (tùy chọn) |
| Coinglass REST | Open Interest, Funding Rate |

Khi ≥3 nguồn xác nhận cùng chiều → **Confluence Alert** được kích hoạt.

### Hệ Thống Kèo Tự Động

Có 4 nguồn tạo kèo:

| Nguồn | Trigger | source DB |
|-------|---------|-----------|
| **Auto** | Whale trade + trend gate + quality score | `AUTO` |
| **Trend Scan** | Volume spike 4h + multi-TF trend (không cần whale) | `TREND_SCAN` |
| **TradingView** | HTTP POST từ Pine Script alert | `TV` |
| **Admin** | Lệnh `/signal` thủ công | `ADMIN` |

- TP/SL tính tự động từ ATR (hoặc cung cấp tường minh qua TV/Admin)
- Tracking TP1 → TP2 → TP3 và SL real-time, edit message Telegram in-place
- **TP1 milestone**: tiếp tục tracking TP2/TP3, không đóng kèo
- Tự đóng tại entry (hoà vốn) nếu trend đảo mạnh sau TP1
- Reversal auto-cut khi trend đảo đủ điều kiện
- Giới hạn SL thua/ngày (`DAILY_SL_LIMIT`)

### Phân Tích DOM (Order Book Depth)
- Phát hiện tường giá lớn (bid/ask wall)
- Theo dõi hấp thụ tường (wall absorption)
- Tín hiệu BULLISH / BEARISH / NEUTRAL theo độ sâu sổ lệnh

---

## Cài Đặt

### Yêu Cầu
- Python 3.11+ hoặc Docker
- Telegram Bot Token (từ [@BotFather](https://t.me/BotFather))
- Chat ID admin (từ [@userinfobot](https://t.me/userinfobot))

### Chạy Bằng Docker (khuyến nghị)

```bash
git clone <repo-url> whale-bot && cd whale-bot
cp .env.example .env
# Điền TELEGRAM_BOT_TOKEN và ADMIN_CHAT_ID vào .env
docker-compose up -d
docker-compose logs -f
```

### Chạy Thủ Công

```bash
pip install -r requirements.txt
cp .env.example .env
# Điền TELEGRAM_BOT_TOKEN và ADMIN_CHAT_ID vào .env
python main.py
```

---

## Cấu Hình `.env`

### Bắt Buộc

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
ADMIN_CHAT_ID=123456789
```

### Kênh Kèo

```env
SIGNAL_CHANNEL_ID=-1001234567890   # Channel Telegram để đăng kèo tự động
```

### Ngưỡng Whale

```env
MIN_TRADE_SIZE_USD=100000
MIN_POSITION_SIZE_USD=500000
MIN_LIQUIDATION_SIZE_USD=200000
MIN_PNL_ALERT_USD=50000
```

### Kèo Tự Động

```env
AUTO_SIGNAL_ENABLED=true
AUTO_SIGNAL_MIN_USD=500000         # Ngưỡng tạo kèo BTC/ETH
AUTO_SIGNAL_MIN_USD_ALT=200000     # Ngưỡng tạo kèo altcoin
AUTO_SIGNAL_MAJOR_COINS=BTC,ETH
SIGNAL_MIN_QUALITY_SCORE=50        # Điểm chất lượng tối thiểu (0-100)

TREND_MIN_SCORE=2                  # Tối thiểu 2/3 khung giờ xác nhận
TREND_ATR_SL_MULT=1.5
TREND_ATR_TP_MULT=3.0

REVERSAL_CUT_ENABLED=true
REVERSAL_MIN_SCORE=4               # Điểm trend ngược để auto-cut ACTIVE
REVERSAL_GRACE_MINUTES=60
SIGNAL_PENDING_TIMEOUT_HOURS=4
DAILY_SL_LIMIT=3                   # Max SL/ngày trước khi dừng auto
```

### Multi-Source

```env
BINANCE_ENABLED=true
BINANCE_SYMBOLS=BTC,ETH,SOL,BNB,DOGE,AVAX

BYBIT_ENABLED=true
BYBIT_SYMBOLS=BTC,ETH,SOL

OKX_ENABLED=false                  # OKX Futures WS (tùy chọn)
OKX_SYMBOLS=BTC,ETH,SOL,BNB,DOGE,XRP,AVAX

COINGLASS_API_KEY=                 # Để trống → fallback Binance REST
OI_SPIKE_THRESHOLD=5.0             # % thay đổi OI để alert
FUNDING_EXTREME_HIGH=0.10
FUNDING_EXTREME_LOW=-0.05
CONFLUENCE_ENABLED=true
CONFLUENCE_MIN_SCORE_WEIGHTED=5
```

### Trend Scanner (Volume Spike → Kèo, không cần whale)

```env
SCAN_ENABLED=false                 # Bật để auto-kèo từ volume spike 4h
SCAN_VOLUME_MIN=2.0                # Volume / MA20 >= 2x
SCAN_MIN_TREND_SCORE=2             # Tối thiểu 2/3 TF xác nhận
SCAN_DAILY_MAX=2                   # Max kèo/ngày từ scanner
SCAN_COIN_COOLDOWN_HOURS=24        # Cooldown per-coin
```

### TradingView Webhook

```env
TV_WEBHOOK_ENABLED=false           # Bật server nhận Pine Script alerts
TV_WEBHOOK_PORT=8080
TV_WEBHOOK_SECRET=                 # Header X-TV-Secret hoặc ?secret=
```

**Payload JSON từ TradingView:**
```json
{
  "coin": "BTC",
  "direction": "LONG",
  "entry": 65000,
  "tp1": 67000, "tp2": 69000, "tp3": 72000,
  "sl": 63000,
  "leverage": 10,
  "note": "RSI oversold + support"
}
```
`entry`, `tp2`, `tp3`, `leverage`, `note` là tùy chọn.

### DOM Analysis

```env
DOM_ENABLED=true
DOM_COINS=BTC,ETH,SOL,ARB,DOGE,AVAX
DOM_WALL_MIN_USD=1000000           # $1M trở lên mới coi là wall
DOM_WALL_DISTANCE_MAX_PCT=1.5      # Wall trong 1.5% từ mid price
DOM_BID_ASK_BULLISH=1.5            # Bid/Ask ratio > 1.5 → bullish
DOM_BID_ASK_BEARISH=0.67           # Bid/Ask ratio < 0.67 → bearish
DOM_ABSORPTION_PCT_THRESHOLD=25    # Wall giảm 25% → absorption detected
```

---

## Lệnh Bot

### Người Dùng

| Lệnh | Mô tả |
|------|-------|
| `/start` | Đăng ký nhận alert |
| `/filter` | Lọc cỡ lệnh: cá nhỏ / cá to / cá khủng |
| `/watchlist` | Xem địa chỉ đang theo dõi |
| `/add 0xABC... [label]` | Thêm địa chỉ vào watchlist |
| `/remove 0xABC...` | Xóa khỏi watchlist |
| `/top` | Top PnL cao nhất đang theo dõi |
| `/threshold <usd>` | Đặt ngưỡng alert riêng |
| `/sources` | Xem nguồn dữ liệu đang hoạt động |
| `/confluence` | Bật/tắt confluence alerts |
| `/settings` | Xem cài đặt hiện tại |
| `/signals` | Danh sách kèo gần nhất |
| `/help` | Danh sách lệnh |

### Admin

| Lệnh | Mô tả |
|------|-------|
| `/signal` | Tạo kèo thủ công |
| `/cancel [id]` | Hủy kèo |
| `/signal_stats` | Thống kê win rate kèo |
| `/source_stats` | Win rate theo từng nguồn kèo |
| `/signal_report` | Báo cáo chi tiết thắng/thua |
| `/whales` | Danh sách known whales |
| `/whale_scores` | Bảng xếp hạng whale theo win rate |

**Cú pháp tạo kèo thủ công:**
```
/signal BTC LONG
Entry: 65000
TP1: 67000
TP2: 69000
TP3: 72000
SL: 63000
```

---

## Deploy VPS

```bash
ssh user@your-vps
git clone <repo-url> whale-bot && cd whale-bot
cp .env.example .env && nano .env
docker-compose up -d
```

---

## Tài Liệu

- [Kiến trúc hệ thống](src/docs/ARCHITECTURE.md)
