import asyncio
import json
from typing import Callable, Awaitable
import websockets
from loguru import logger
from config.settings import config


class HyperliquidWS:
    def __init__(self):
        self.url = config.hl_ws_url
        self._handlers: dict[str, list[Callable]] = {}
        self._ws = None
        self._running = False
        self._watched_users: set[str] = set()   # persists across reconnects

    def on(self, event: str, handler: Callable[..., Awaitable]):
        self._handlers.setdefault(event, []).append(handler)

    async def _dispatch(self, event: str, data):
        for h in self._handlers.get(event, []):
            try:
                await h(data)
            except Exception as e:
                logger.error(f"Handler error [{event}]: {e}")

    async def connect(self):
        self._running = True
        while self._running:
            try:
                logger.info("Connecting to Hyperliquid WS…")
                async with websockets.connect(
                    self.url,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    logger.success("Connected to Hyperliquid WebSocket")
                    await self._subscribe(ws)
                    await asyncio.gather(
                        self._listen(ws),
                        self._heartbeat(ws),
                    )
            except Exception as e:
                logger.warning(f"WS disconnected: {e} — reconnecting in 5s")
                self._ws = None
                await asyncio.sleep(5)

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send(json.dumps({"method": "ping"}))
            except Exception:
                break

    async def _subscribe(self, ws):
        coins = ["BTC", "ETH", "SOL", "ARB", "DOGE", "AVAX", "WIF", "PEPE"]
        for coin in coins:
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))
            await asyncio.sleep(0.3)
        logger.info(f"Subscribed to {len(coins)} trade channels")

        # Re-subscribe to all watched users (important on reconnect)
        for addr in list(self._watched_users):
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userEvents", "user": addr}}))
            await asyncio.sleep(0.2)
        if self._watched_users:
            logger.info(f"Re-subscribed userEvents for {len(self._watched_users)} addresses")

    async def add_user_subscription(self, address: str):
        """Add address to userEvents tracking (survives reconnects)."""
        addr = address.lower()
        if addr in self._watched_users:
            return
        self._watched_users.add(addr)
        if self._ws:
            try:
                await self._ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "userEvents", "user": addr},
                }))
                logger.debug(f"Subscribed userEvents: {addr[:10]}…")
            except Exception as e:
                logger.error(f"subscribe_user error: {e}")

    def remove_user_subscription(self, address: str):
        self._watched_users.discard(address.lower())

    async def _listen(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
                channel = msg.get("channel", "")
                data = msg.get("data", {})
                if channel == "trades":
                    await self._dispatch("trades", data)
                elif channel == "userEvents":
                    await self._dispatch("userEvents", data)
                elif channel == "pong":
                    logger.debug("pong")
            except json.JSONDecodeError:
                pass

    def stop(self):
        self._running = False


ws = HyperliquidWS()
