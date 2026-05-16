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
from src.api.binance_ws import BinanceWS
from src.api.bybit_ws import BybitWS
from src.api.okx_ws import OkxWS
from src.bot.tv_webhook import run_webhook_server
from src.api.coinglass_rest import CoinglassRest
from src.detector.whale_detector import WhaleDetector
from src.detector.trend_detector import run_trend_poll
from src.detector.alert_engine import AlertEngine
from src.detector.oi_detector import OISpikeDetector
from src.detector.funding_detector import FundingRateDetector
from src.detector.dom_analyzer import dom_analyzer
from src.detector.ecosystem_detector import ecosystem_detector
from src.detector.trend_scanner import trend_scanner
from src.aggregator.confluence_scorer import ConfluenceScorer, set_confluence_instance
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

    # Wire up DOM analyzer and ecosystem detector
    ecosystem_detector.set_signal_tracker(signal_tracker)
    ecosystem_detector.set_dom_analyzer(dom_analyzer)
    ecosystem_detector.set_bot(bot)
    trend_scanner.set_signal_tracker(signal_tracker)
    trend_scanner.set_dom_analyzer(dom_analyzer)
    trend_scanner.set_bot(bot)
    logger.info("DOM Analyzer, Ecosystem Detector, and Trend Scanner initialized")

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
        BotCommand(command="sources",       description="Xem nguồn dữ liệu đang hoạt động"),
        BotCommand(command="confluence",    description="Bật/tắt confluence alerts"),
        BotCommand(command="help",          description="Hướng dẫn sử dụng"),
        BotCommand(command="signals",       description="Danh sách kèo gần nhất"),
        BotCommand(command="signal_stats",  description="[Admin] Thống kê win rate kèo"),
        BotCommand(command="source_stats",  description="[Admin] Win rate theo từng nguồn kèo"),
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

    # Init multi-source components
    coinglass  = CoinglassRest(config)
    confluence = ConfluenceScorer(engine, config)
    set_confluence_instance(confluence)
    oi_detector  = OISpikeDetector(coinglass, engine, config, confluence=confluence)
    funding_det  = FundingRateDetector(coinglass, engine, config, confluence=confluence)

    # Register Hyperliquid handlers
    async def on_trades(data):
        alerts = detector.process_trades(data)
        for a in alerts:
            await engine._route_alert(a)
            if a.direction in ("LONG", "SHORT"):
                confluence.ingest(a.coin, a.direction, "hyperliquid", a.size_usd)

    async def on_user_events(data):
        watched_map = await db.get_all_watched_addresses()
        await engine.process_user_event(data, watched_map)

    hl_ws.on("trades", on_trades)
    hl_ws.on("userEvents", on_user_events)

    # Init Binance WS
    binance_ws = None
    if config.binance_enabled:
        binance_ws = BinanceWS(config)

        async def on_binance_trade(payload):
            await engine.process_binance_trade(payload)
            direction = "LONG" if payload.get("side") == "BUY" else "SHORT"
            confluence.ingest(payload.get("symbol", "?"), direction, "binance", payload.get("size_usd", 0))

        async def on_binance_liq(payload):
            await engine.process_binance_liquidation(payload)
            confluence.ingest(payload.get("symbol", "?"), "SHORT", "liquidation", payload.get("size_usd", 0))

        binance_ws.on("binance_trade", on_binance_trade)
        binance_ws.on("binance_liquidation", on_binance_liq)
        logger.info("Binance Futures WS: enabled")

    # Init OKX WS
    okx_ws = None
    if config.okx_enabled:
        okx_ws = OkxWS(config)

        async def on_okx_trade(payload):
            await engine.process_okx_trade(payload)
            direction = "LONG" if payload.get("side") == "BUY" else "SHORT"
            confluence.ingest(payload.get("symbol", "?"), direction, "okx", payload.get("size_usd", 0))

        okx_ws.on("okx_trade", on_okx_trade)
        logger.info("OKX Futures WS: enabled")

    # Init Bybit WS
    bybit_ws = None
    if config.bybit_enabled:
        bybit_ws = BybitWS(config)

        async def on_bybit_trade(payload):
            await engine.process_bybit_trade(payload)
            direction = "LONG" if payload.get("side") == "BUY" else "SHORT"
            confluence.ingest(payload.get("symbol", "?"), direction, "bybit", payload.get("size_usd", 0))

        async def on_bybit_liq(payload):
            await engine.process_bybit_liquidation(payload)
            confluence.ingest(payload.get("symbol", "?"), "SHORT", "liquidation", payload.get("size_usd", 0))

        bybit_ws.on("bybit_trade", on_bybit_trade)
        bybit_ws.on("bybit_liquidation", on_bybit_liq)
        logger.info("Bybit Futures WS: enabled")

    # Init position poller
    poller = PositionPoller(engine, interval=60)

    # Trend detector — gộp tất cả coin cần theo dõi
    trend_coins = list({
        *config.auto_signal_major_coins,
        *config.binance_symbols,
        *config.bybit_symbols,
    })

    # Build task list
    tasks = [
        dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
        hl_ws.connect(),
        poller.start(),
        signal_tracker.start_price_poll(interval=5),
        oi_detector.run(),
        funding_det.run(),
        confluence.run(),
        run_trend_poll(trend_coins),
    ]
    if binance_ws:
        tasks.append(binance_ws.start())
    if bybit_ws:
        tasks.append(bybit_ws.start())
    if okx_ws:
        tasks.append(okx_ws.start())
    if config.tv_webhook_enabled:
        tasks.append(run_webhook_server())

    # Run everything concurrently
    logger.success("All systems initialized. Starting tasks…")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
