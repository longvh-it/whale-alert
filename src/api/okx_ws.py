import asyncio
import json
from typing import Callable, Awaitable

import aiohttp
import websockets
from loguru import logger

from config.settings import config


class OkxWS:
    WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
    INST_URL = "https://www.okx.com/api/v5/public/instruments"

    def __init__(self, cfg=None):
        self.cfg = cfg or config
        self._handlers: dict[str, list] = {}
        self._running = False
        self._ws = None
        self._ct_val: dict[str, float] = {}  # coin -> contract value in base units

    def on(self, event: str, handler: Callable[..., Awaitable]):
        self._handlers.setdefault(event, []).append(handler)

    async def _dispatch(self, event: str, data):
        for h in self._handlers.get(event, []):
            try:
                await h(data)
            except Exception as e:
                logger.error(f"OkxWS handler error [{event}]: {e}")

    async def _fetch_ct_vals(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.INST_URL}?instType=SWAP",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    for inst in data.get("data", []):
                        inst_id = inst.get("instId", "")
                        if inst_id.endswith("-USDT-SWAP"):
                            coin = inst_id.replace("-USDT-SWAP", "")
                            try:
                                self._ct_val[coin] = float(inst.get("ctVal", 1))
                            except Exception:
                                pass
            logger.info(f"OKX: loaded {len(self._ct_val)} contract values")
        except Exception as e:
            logger.warning(f"OKX: failed to fetch contract values: {e}")

    def _build_subscribe_args(self) -> list[dict]:
        return [
            {"channel": "trades", "instId": f"{sym}-USDT-SWAP"}
            for sym in self.cfg.okx_symbols
        ]

    async def start(self):
        await self._fetch_ct_vals()
        self._running = True
        retry = 0
        while self._running and retry < 10:
            try:
                logger.info(f"Connecting to OKX Futures WS ({len(self.cfg.okx_symbols)} symbols)…")
                async with websockets.connect(self.WS_URL, ping_interval=None, close_timeout=5) as ws:
                    self._ws = ws
                    logger.success("Connected to OKX Futures WebSocket")
                    retry = 0
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": self._build_subscribe_args(),
                    }))
                    await asyncio.gather(
                        self._listen(ws),
                        self._heartbeat(ws),
                    )
            except Exception as e:
                retry += 1
                logger.warning(f"OKX WS disconnected: {e} — retry {retry}/10 in 5s")
                self._ws = None
                await asyncio.sleep(5)
        logger.error("OKX WS: max retries reached, giving up.")

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(25)
            try:
                await ws.send("ping")
            except Exception:
                break

    async def _listen(self, ws):
        async for raw in ws:
            try:
                if raw == "pong":
                    continue
                msg = json.loads(raw)
                arg = msg.get("arg", {})
                channel = arg.get("channel", "")
                data = msg.get("data", [])
                if channel == "trades":
                    inst_id = arg.get("instId", "")
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        await self._on_trade(item, inst_id)
            except Exception as e:
                logger.debug(f"OKX parse error: {e}")

    async def _on_trade(self, item: dict, inst_id: str):
        try:
            coin = inst_id.replace("-USDT-SWAP", "")
            price = float(item.get("px", 0))
            sz = float(item.get("sz", 0))
            side = "BUY" if item.get("side", "").lower() == "buy" else "SELL"
            ct_val = self._ct_val.get(coin, 1.0)
            size_usd = price * sz * ct_val
            if size_usd < self.cfg.min_trade_size:
                return
            await self._dispatch("okx_trade", {
                "source": "okx",
                "symbol": coin,
                "side": side,
                "size_usd": size_usd,
                "price": price,
                "timestamp": item.get("ts", 0),
                "raw": item,
            })
        except Exception as e:
            logger.debug(f"OKX trade error: {e}")

    def stop(self):
        self._running = False
