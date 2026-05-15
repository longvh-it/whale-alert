# Hyperliquid Whale Bot

Telegram bot theo dõi hoạt động whale trên Hyperliquid DEX, tổng hợp tín hiệu đa nguồn (Binance, Bybit, Coinglass), và tự động tạo/quản lý kèo (paper trade) theo xu hướng kỹ thuật.

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
| Coinglass REST | Open Interest, Funding Rate |

Khi ≥3 nguồn xác nhận cùng chiều → **Confluence Alert** được kích hoạt.

### Hệ Thống Kèo Tự Động
- Phát hiện whale trade + xác nhận xu hướng đa khung giờ (1h/4h/1d)
- Tính TP/SL tự động từ ATR
- Theo dõi TP1 → TP2 → TP3 và SL real-time, cập nhật message Telegram in-place
- Tự động move SL về entry sau khi chạm TP1
- Tự đóng kèo nếu trend đảo chiều (reversal cut)
- Giới hạn số SL thua/ngày để bảo vệ vốn

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
SIGNAL_MIN_QUALITY_SCORE=5

TREND_MIN_SCORE=2                  # Tối thiểu 2/3 khung giờ xác nhận
TREND_ATR_SL_MULT=1.5
TREND_ATR_TP_MULT=3.0

TP1_REVERSAL_MOVE_SL_ENABLED=true  # Move SL về entry sau TP1
REVERSAL_CUT_ENABLED=true          # Tự đóng khi trend đảo
REVERSAL_GRACE_MINUTES=60
SIGNAL_PENDING_TIMEOUT_HOURS=4
DAILY_SL_LIMIT=3                   # Max SL/ngày
```

### Multi-Source

```env
BINANCE_ENABLED=true
BYBIT_ENABLED=true
COINGLASS_API_KEY=                 # Để trống → fallback Binance REST
OI_SPIKE_THRESHOLD=0.05
FUNDING_EXTREME_HIGH=0.10
FUNDING_EXTREME_LOW=-0.05
CONFLUENCE_ENABLED=true
```

### DOM Analysis

```env
DOM_ENABLED=true
DOM_COINS=BTC,ETH,SOL
DOM_WALL_MIN_USD=1000000
DOM_WALL_DISTANCE_MAX_PCT=0.02
```

---

## Lệnh Bot

### Người Dùng

| Lệnh | Mô tả |
|------|-------|
| `/start` | Đăng ký nhận alert |
| `/watchlist` | Xem địa chỉ đang theo dõi |
| `/add 0xABC... [label]` | Thêm địa chỉ vào watchlist |
| `/remove 0xABC...` | Xóa khỏi watchlist |
| `/threshold <usd>` | Đặt ngưỡng alert riêng |
| `/settings` | Xem cài đặt hiện tại |
| `/help` | Danh sách lệnh |

### Admin

| Lệnh | Mô tả |
|------|-------|
| `/signal` | Tạo kèo thủ công |
| `/cancel [id]` | Hủy kèo |
| `/signals` | Danh sách kèo đang mở |
| `/signal_stats` | Thống kê thắng/thua |
| `/signal_report` | Báo cáo chi tiết |
| `/whales` | Danh sách whale đang track |
| `/whale_scores` | Bảng xếp hạng whale |

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
- [Spec multi-source signals](src/docs/SPEC_MULTI_SOURCE.md)
- [Spec signal lifecycle](src/docs/SPEC_SIGNAL_LIFECYCLE.md)
