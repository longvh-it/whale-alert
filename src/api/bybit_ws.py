import asyncio
import json
from typing import Callable, Awaitable

import websockets
from loguru import logger

from config.settings import config


class BybitWS:
    URL = "wss://stream.bybit.com/v5/public/linear"

    def __init__(self, cfg=None):
        self.cfg = cfg or config
        self._handlers: dict[str, list] = {}
        self._running = False
        self._ws = None

    def on(self, event: str, handler: Callable[..., Awaitable]):
        self._handlers.setdefault(event, []).append(handler)

    async def _dispatch(self, event: str, data):
        for h in self._handlers.get(event, []):
            try:
                await h(data)
            except Exception as e:
                logger.error(f"BybitWS handler error [{event}]: {e}")

    def _build_args(self) -> list[str]:
        args = []
        for sym in self.cfg.bybit_symbols:
            args.append(f"publicTrade.{sym}USDT")
            args.append(f"liquidation.{sym}USDT")
        return args

    async def start(self):
        self._running = True
        retry = 0
        while self._running and retry < 10:
            try:
                logger.info(f"Connecting to Bybit Futures WS ({len(self.cfg.bybit_symbols)} symbols)…")
                async with websockets.connect(self.URL, ping_interval=None, close_timeout=5) as ws:
                    self._ws = ws
                    logger.success("Connected to Bybit Futures WebSocket")
                    retry = 0
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": self._build_args(),
                    }))
                    await asyncio.gather(
                        self._listen(ws),
                        self._heartbeat(ws),
                    )
            except Exception as e:
                retry += 1
                logger.warning(f"Bybit WS disconnected: {e} — retry {retry}/10 in 5s")
                self._ws = None
                await asyncio.sleep(5)
        logger.error("Bybit WS: max retries reached, giving up.")

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send(json.dumps({"op": "ping"}))
            except Exception:
                break

    async def _listen(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
                topic = msg.get("topic", "")
                data = msg.get("data", [])
                if topic.startswith("publicTrade."):
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        await self._on_trade(item, topic)
                elif topic.startswith("liquidation."):
                    await self._on_liquidation(data, topic)
            except Exception as e:
                logger.debug(f"Bybit parse error: {e}")

    async def _on_trade(self, item: dict, topic: str):
        try:
            coin = topic.replace("publicTrade.", "").replace("USDT", "")
            price = float(item.get("p", 0))
            qty = float(item.get("v", 0))
            side = "BUY" if item.get("S", "") == "Buy" else "SELL"
            size_usd = price * qty
            if size_usd < self.cfg.min_trade_size:
                return
            await self._dispatch("bybit_trade", {
                "source": "bybit",
                "symbol": coin,
                "side": side,
                "size_usd": size_usd,
                "price": price,
                "timestamp": item.get("T", 0),
                "raw": item,
            })
        except Exception as e:
            logger.debug(f"Bybit trade error: {e}")

    async def _on_liquidation(self, data: dict, topic: str):
        try:
            coin = topic.replace("liquidation.", "").replace("USDT", "")
            price = float(data.get("price", 0))
            qty = float(data.get("size", 0))
            side = "BUY" if data.get("side", "Buy") == "Buy" else "SELL"
            size_usd = price * qty
            if size_usd < self.cfg.min_liquidation_size:
                return
            await self._dispatch("bybit_liquidation", {
                "source": "bybit",
                "symbol": coin,
                "side": side,
                "size_usd": size_usd,
                "price": price,
                "timestamp": data.get("updatedTime", 0),
                "raw": data,
            })
        except Exception as e:
            logger.debug(f"Bybit liquidation error: {e}")

    def stop(self):
        self._running = False
