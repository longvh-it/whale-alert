"""
TradingView Webhook Server
Nhận Pine Script alerts qua HTTP POST, tạo kèo tự động với source="TV".

Payload JSON từ TradingView:
{
  "coin": "BTC",
  "direction": "LONG",        // hoặc "SHORT"
  "entry": 50000,             // optional — nếu bỏ qua sẽ dùng giá thị trường
  "tp1": 52000,
  "tp2": 54000,               // optional
  "tp3": 56000,               // optional
  "sl": 48000,
  "leverage": 10,             // optional
  "note": "RSI oversold + support"  // optional
}

Cấu hình trong .env:
TV_WEBHOOK_ENABLED=true
TV_WEBHOOK_PORT=8080
TV_WEBHOOK_SECRET=your_secret_here  // TradingView gửi header X-TV-Secret
"""

import asyncio

from aiohttp import web
from loguru import logger

from config.settings import config
import src.signals.signal_tracker as st_module


async def _handle_webhook(request: web.Request) -> web.Response:
    secret = config.tv_webhook_secret
    if secret:
        token = (
            request.headers.get("X-TV-Secret", "")
            or request.rel_url.query.get("secret", "")
        )
        if token != secret:
            logger.warning(f"TV webhook: invalid secret from {request.remote}")
            return web.Response(status=401, text="Unauthorized")

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    coin = str(data.get("coin", "")).upper().strip()
    direction = str(data.get("direction", "")).upper().strip()

    if not coin:
        return web.Response(status=400, text="Missing coin")
    if direction not in ("LONG", "SHORT"):
        return web.Response(status=400, text="direction must be LONG or SHORT")

    sl_raw = data.get("sl")
    if not sl_raw:
        return web.Response(status=400, text="Missing sl")

    tp1_raw = data.get("tp1")
    tp2_raw = data.get("tp2")
    tp3_raw = data.get("tp3")
    if not any([tp1_raw, tp2_raw, tp3_raw]):
        return web.Response(status=400, text="At least one TP required")

    tracker = st_module.get_tracker()
    if not tracker:
        return web.Response(status=503, text="SignalTracker not ready")

    entry_raw = data.get("entry")
    if entry_raw:
        entry = float(entry_raw)
        order_type = "MARKET"
        current = tracker.get_current_price(coin)
        if current:
            is_limit = (
                (direction == "LONG" and entry < current * 0.9995)
                or (direction == "SHORT" and entry > current * 1.0005)
            )
            order_type = "LIMIT" if is_limit else "MARKET"
    else:
        entry = tracker.get_current_price(coin)
        if not entry:
            return web.Response(status=400, text=f"No price for {coin}, provide entry")
        order_type = "MARKET"

    try:
        sig_id = await tracker.create_and_post(
            coin=coin,
            direction=direction,
            entry=entry,
            tp1=float(tp1_raw) if tp1_raw else None,
            tp2=float(tp2_raw) if tp2_raw else None,
            tp3=float(tp3_raw) if tp3_raw else None,
            sl=float(sl_raw),
            leverage=int(data["leverage"]) if data.get("leverage") else None,
            source="TV",
            note=str(data.get("note", "TradingView alert")),
            order_type=order_type,
        )
        logger.info(f"TV webhook: kèo #{sig_id} {direction} {coin} @ {entry}")
        return web.Response(text=f"OK #{sig_id}")
    except Exception as e:
        logger.error(f"TV webhook create error: {e}")
        return web.Response(status=500, text=str(e))


async def run_webhook_server():
    if not config.tv_webhook_enabled:
        return
    app = web.Application()
    app.router.add_post("/webhook/tv", _handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.tv_webhook_port)
    await site.start()
    logger.success(f"TradingView webhook server on port {config.tv_webhook_port}")
    while True:
        await asyncio.sleep(3600)
