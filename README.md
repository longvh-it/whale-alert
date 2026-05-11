# 🐋 Hyperliquid Whale Bot

Telegram bot theo dõi whale trades, liquidations và positions trên Hyperliquid DEX.

---

## Tính năng

| Alert | Mô tả | Ngưỡng mặc định |
|---|---|---|
| 🐋 Big Trade | Lệnh khớp lớn | > $100K |
| 💥 Liquidation | Bị thanh lý | > $200K |
| 📊 Large Position | Vị thế mở lớn | > $500K |
| 🔄 Position Flip | Đổi chiều Long↔Short | — |
| 💰 PnL Update | Cập nhật lãi/lỗ tự động | mốc $5K, $10K, $25K… |
| 👁 Watchlist | Hoạt động địa chỉ bạn theo dõi | — |

---

## Cài đặt nhanh

**1. Tạo bot Telegram**
```
1. Nhắn @BotFather → /newbot
2. Lưu token nhận được
3. Nhắn @userinfobot để lấy Chat ID
```

**2. Cấu hình**
```bash
cp .env.example .env
# Điền TELEGRAM_BOT_TOKEN và ADMIN_CHAT_ID vào .env
```

**3. Chạy**
```bash
# Docker (khuyến nghị)
docker-compose up -d
docker-compose logs -f

# Thủ công
pip install -r requirements.txt
python main.py
```

---

## Lệnh bot

| Lệnh | Mô tả |
|---|---|
| `/start` | Đăng ký nhận alert |
| `/watchlist` | Xem danh sách địa chỉ đang theo dõi |
| `/add 0xABC... Label` | Thêm địa chỉ vào watchlist |
| `/remove 0xABC...` | Xóa địa chỉ khỏi watchlist |
| `/top` | Top PnL cao nhất đang theo dõi |
| `/threshold trade 50000` | Đặt ngưỡng trade alert |
| `/threshold liq 100000` | Đặt ngưỡng liquidation alert |
| `/settings` | Xem cài đặt hiện tại |
| `/help` | Hướng dẫn |

---

## Cấu hình `.env`

```env
# Bắt buộc
TELEGRAM_BOT_TOKEN=your_token
ADMIN_CHAT_ID=your_chat_id

# Ngưỡng phát hiện (USD)
MIN_TRADE_SIZE_USD=100000        # $100K
MIN_POSITION_SIZE_USD=500000     # $500K
MIN_LIQUIDATION_SIZE_USD=200000  # $200K
MIN_PNL_ALERT_USD=50000          # $50K

# Hệ thống
DB_PATH=whale_bot.db
LOG_LEVEL=INFO
ALERT_COOLDOWN_SECONDS=300       # 5 phút giữa các alert giống nhau
```

---

## Deploy VPS

```bash
ssh user@your-vps-ip
git clone <your-repo> whale-bot && cd whale-bot
cp .env.example .env && nano .env
docker-compose up -d
```
