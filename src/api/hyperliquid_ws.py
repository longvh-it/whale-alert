import asyncio
import json
from typing import Callable, Awaitable
import websockets
from loguru import logger
from config.settings import config


_dom_analyzer = None


def _get_dom_analyzer():
    global _dom_analyzer
    if _dom_analyzer is None and config.dom_enabled:
        try:
            from src.detector.dom_analyzer import dom_analyzer
            _dom_analyzer = dom_analyzer
        except Exception:
            pass
    return _dom_analyzer


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
                    # Run all three concurrently so _listen drains server
                    # responses while _subscribe is sending subscriptions.
                    # gather cancels siblings when any one raises.
                    await asyncio.gather(
                        self._subscribe(ws),
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

        # Subscribe L2 order book for DOM analysis
        if config.dom_enabled:
            await asyncio.sleep(0.5)  # let server digest trade subs first
            ok = []
            for coin in config.dom_coins:
                try:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": coin},
                    }))
                    ok.append(coin)
                    await asyncio.sleep(0.4)
                except Exception as e:
                    logger.warning(f"l2Book subscribe failed for {coin}: {e}")
            if ok:
                logger.info(f"Subscribed to L2 order book for {len(ok)} DOM coins: {ok}")

        # Re-subscribe to all watched users (important on reconnect)
        for addr in list(self._watched_users):
            try:
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userEvents", "user": addr}}))
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.warning(f"userEvents subscribe failed for {addr[:10]}: {e}")
                break
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
                elif channel == "l2Book":
                    await self._handle_l2_book(data)
                elif channel == "pong":
                    logger.debug("pong")
                elif channel:
                    logger.debug(f"WS unhandled channel={channel}: {str(data)[:120]}")
            except json.JSONDecodeError:
                pass

    async def _handle_l2_book(self, data: dict):
        dom = _get_dom_analyzer()
        if dom is None:
            return
        try:
            coin = data.get("coin", "")
            levels = data.get("levels", [[], []])
            bids = [[float(l["px"]), float(l["sz"])] for l in levels[0]] if levels[0] else []
            asks = [[float(l["px"]), float(l["sz"])] for l in levels[1]] if levels[1] else []
            mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else 0
            if mid > 0:
                await dom.process_l2_update(coin, bids, asks, mid)
        except Exception as e:
            logger.debug(f"L2 book parse error: {e}")

    def stop(self):
        self._running = False


ws = HyperliquidWS()
