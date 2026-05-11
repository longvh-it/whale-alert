import asyncio
import sys
from loguru import logger
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config.settings import config
from src.storage.database import db
from src.api.hyperliquid_ws import HyperliquidWS, ws as hl_ws
from src.detector.whale_detector import WhaleDetector
from src.detector.alert_engine import AlertEngine
from src.bot.handlers import router
from src.bot.signal_handlers import router as signal_router
from src.bot.poller import PositionPoller
from src.signals.signal_tracker import SignalTracker
import src.signals.signal_tracker as _st_module


def setup_logging():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logger.remove()
    logger.add(
        sys.stdout,
        level=config.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
    )
    import os
    if os.getenv("LOG_FILE", "true").lower() == "true":
        os.makedirs("logs", exist_ok=True)
        logger.add(
            "logs/whale_bot.log",
            rotation="10 MB",
            retention="7 days",
            level="DEBUG",
            encoding="utf-8",
        )


async def main():
    setup_logging()
    logger.info("🐋 Whale Bot starting…")

    if not config.bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env!")
        return

    # Init DB
    await db.init()

    # Init Telegram bot
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(signal_router)   # signal commands first (more specific)
    dp.include_router(router)

    # Init SignalTracker
    signal_tracker = SignalTracker(bot)
    _st_module.set_tracker(signal_tracker)
    logger.info("SignalTracker initialized")

    # Set command menu (bottom-left "/" button in Telegram)
    await bot.set_my_commands([
        BotCommand(command="start",     description="Bắt đầu & đăng ký nhận alert"),
        BotCommand(command="filter",    description="Lọc cỡ lệnh: cá nhỏ / cá to / cá khủng"),
        BotCommand(command="watchlist", description="Xem danh sách địa chỉ đang theo dõi"),
        BotCommand(command="add",       description="Thêm địa chỉ vào watchlist"),
        BotCommand(command="remove",    description="Xóa địa chỉ khỏi watchlist"),
        BotCommand(command="top",       description="Top PnL cao nhất đang theo dõi"),
        BotCommand(command="settings",  description="Xem cài đặt của bạn"),
        BotCommand(command="threshold", description="Đặt ngưỡng tùy chỉnh (trade/liq)"),
        BotCommand(command="help",        description="Hướng dẫn sử dụng"),
        BotCommand(command="signals",       description="Danh sách kèo gần nhất"),
        BotCommand(command="signal_stats",  description="[Admin] Thống kê win rate kèo"),
        BotCommand(command="signal_report", description="[Admin] Chi tiết kèo thắng/thua"),
        BotCommand(command="whales",        description="[Admin] Danh sách known whales"),
        BotCommand(command="whale_scores",  description="[Admin] Bảng xếp hạng whale theo win rate"),
    ])

    # Init detector & alert engine
    detector = WhaleDetector()
    engine = AlertEngine(bot, detector)

    # Pre-load watchlist + known whales vào WS subscription
    all_watched = await db.get_all_watched_addresses()
    for addr in all_watched:
        hl_ws._watched_users.add(addr)
    known_whales = await db.get_known_whales()
    for w in known_whales:
        hl_ws._watched_users.add(w["address"])
    total_subs = len(hl_ws._watched_users)
    if total_subs:
        logger.info(f"Loaded {len(all_watched)} watchlist + {len(known_whales)} known whales ({total_subs} total) for userEvents")

    # Register handlers
    async def on_trades(data):
        await engine.process_trade_data(data)

    async def on_user_events(data):
        watched_map = await db.get_all_watched_addresses()
        await engine.process_user_event(data, watched_map)

    hl_ws.on("trades", on_trades)
    hl_ws.on("userEvents", on_user_events)

    # Init position poller
    poller = PositionPoller(engine, interval=60)

    # Run everything concurrently
    logger.success("All systems initialized. Starting tasks…")
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
        hl_ws.connect(),
        poller.start(),
        signal_tracker.start_price_poll(interval=5),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
