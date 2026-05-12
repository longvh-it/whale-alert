import asyncio
import json
from typing import Callable, Awaitable

import websockets
from loguru import logger

from config.settings import config


class BinanceWS:
    BASE_URL = "wss://fstream.binance.com/stream?streams="

    def __init__(self, cfg=None):
        self.cfg = cfg or config
        self._handlers: dict[str, list] = {}
        self._running = False

    def on(self, event: str, handler: Callable[..., Awaitable]):
        self._handlers.setdefault(event, []).append(handler)

    async def _dispatch(self, event: str, data):
        for h in self._handlers.get(event, []):
            try:
                await h(data)
            except Exception as e:
                logger.error(f"BinanceWS handler error [{event}]: {e}")

    def _build_url(self) -> str:
        parts = []
        for sym in self.cfg.binance_symbols:
            s = sym.lower()
            parts.append(f"{s}usdt@aggTrade")
            parts.append(f"{s}usdt@forceOrder")
        return self.BASE_URL + "/".join(parts)

    async def start(self):
        self._running = True
        retry = 0
        while self._running and retry < 10:
            try:
                url = self._build_url()
                logger.info(f"Connecting to Binance Futures WS ({len(self.cfg.binance_symbols)} symbols)…")
                async with websockets.connect(url, ping_interval=None, close_timeout=5) as ws:
                    logger.success("Connected to Binance Futures WebSocket")
                    retry = 0
                    await self._listen(ws)
            except Exception as e:
                retry += 1
                logger.warning(f"Binance WS disconnected: {e} — retry {retry}/10 in 5s")
                await asyncio.sleep(5)
        logger.error("Binance WS: max retries reached, giving up.")

    async def _listen(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
                data = msg.get("data", msg)
                etype = data.get("e", "")
                if etype == "aggTrade":
                    await self._on_agg_trade(data)
                elif etype == "forceOrder":
                    await self._on_force_order(data)
            except Exception as e:
                logger.debug(f"Binance parse error: {e}")

    async def _on_agg_trade(self, data: dict):
        try:
            sym = data.get("s", "")
            coin = sym.replace("USDT", "")
            price = float(data.get("p", 0))
            qty = float(data.get("q", 0))
            # m=True: buyer is market maker (seller was aggressor) → SELL
            side = "SELL" if data.get("m", False) else "BUY"
            size_usd = price * qty
            if size_usd < self.cfg.min_trade_size:
                return
            await self._dispatch("binance_trade", {
                "source": "binance",
                "symbol": coin,
                "side": side,
                "size_usd": size_usd,
                "price": price,
                "timestamp": data.get("T", 0),
                "raw": data,
            })
        except Exception as e:
            logger.debug(f"Binance aggTrade error: {e}")

    async def _on_force_order(self, data: dict):
        try:
            order = data.get("o", {})
            sym = order.get("s", "")
            coin = sym.replace("USDT", "")
            price = float(order.get("ap", 0) or order.get("p", 0))
            qty = float(order.get("q", 0))
            side = order.get("S", "BUY")
            size_usd = price * qty
            if size_usd < self.cfg.min_liquidation_size:
                return
            await self._dispatch("binance_liquidation", {
                "source": "binance",
                "symbol": coin,
                "side": side,
                "size_usd": size_usd,
                "price": price,
                "timestamp": data.get("E", 0),
                "raw": data,
            })
        except Exception as e:
            logger.debug(f"Binance forceOrder error: {e}")

    def stop(self):
        self._running = False
