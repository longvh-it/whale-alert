import aiohttp
from loguru import logger

from config.settings import config


class CoinglassRest:
    BINANCE_BASE = "https://fapi.binance.com/fapi/v1"
    COINGLASS_BASE = "https://open-api.coinglass.com/public/v2"

    def __init__(self, cfg=None):
        self.cfg = cfg or config
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_open_interest(self, symbol: str) -> dict:
        """Returns {total_oi_usd, change_pct_1h, change_pct_24h}"""
        if self.cfg.coinglass_api_key:
            try:
                result = await self._cg_open_interest(symbol)
                if result["total_oi_usd"] > 0:
                    return result
            except Exception as e:
                logger.debug(f"Coinglass OI error [{symbol}]: {e}")
        return await self._binance_open_interest(symbol)

    async def get_funding_rates(self, symbol: str) -> dict:
        """Returns {binance, bybit, hyperliquid} funding rates in %"""
        if self.cfg.coinglass_api_key:
            try:
                result = await self._cg_funding(symbol)
                if any(v != 0.0 for v in result.values()):
                    return result
            except Exception as e:
                logger.debug(f"Coinglass funding error [{symbol}]: {e}")
        return await self._binance_funding(symbol)

    async def get_liquidations_1h(self, symbol: str) -> dict:
        """Returns {long_usd, short_usd}"""
        return {"long_usd": 0.0, "short_usd": 0.0}

    # ── Binance fallback ───────────────────────────────────

    async def _binance_open_interest(self, symbol: str) -> dict:
        empty = {"total_oi_usd": 0.0, "change_pct_1h": 0.0, "change_pct_24h": 0.0}
        try:
            sess = await self._sess()
            timeout = aiohttp.ClientTimeout(total=10)
            async with sess.get(
                f"{self.BINANCE_BASE}/openInterest",
                params={"symbol": f"{symbol}USDT"},
                timeout=timeout,
            ) as r1:
                if r1.status != 200:
                    return empty
                d1 = await r1.json()
                oi_base = float(d1.get("openInterest", 0))

            async with sess.get(
                f"{self.BINANCE_BASE}/premiumIndex",
                params={"symbol": f"{symbol}USDT"},
                timeout=timeout,
            ) as r2:
                if r2.status != 200:
                    return empty
                d2 = await r2.json()
                mark_price = float(d2.get("markPrice", 0))

            return {
                "total_oi_usd": oi_base * mark_price,
                "change_pct_1h": 0.0,
                "change_pct_24h": 0.0,
            }
        except Exception as e:
            logger.debug(f"Binance OI error [{symbol}]: {e}")
            return empty

    async def _binance_funding(self, symbol: str) -> dict:
        empty = {"binance": 0.0, "bybit": 0.0, "hyperliquid": 0.0}
        try:
            sess = await self._sess()
            async with sess.get(
                f"{self.BINANCE_BASE}/premiumIndex",
                params={"symbol": f"{symbol}USDT"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    return empty
                data = await r.json()
                rate = float(data.get("lastFundingRate", 0)) * 100  # → %
                return {"binance": rate, "bybit": 0.0, "hyperliquid": 0.0}
        except Exception as e:
            logger.debug(f"Binance funding error [{symbol}]: {e}")
            return empty

    # ── Coinglass (optional, requires API key) ─────────────

    async def _cg_open_interest(self, symbol: str) -> dict:
        sess = await self._sess()
        headers = {"coinglassSecret": self.cfg.coinglass_api_key}
        async with sess.get(
            f"{self.COINGLASS_BASE}/open_interest",
            params={"symbol": symbol, "interval": "0"},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = await r.json()
            info = data.get("data", {})
            return {
                "total_oi_usd": float(info.get("oiUsd", 0)),
                "change_pct_1h": float(info.get("change1h", 0)),
                "change_pct_24h": float(info.get("change24h", 0)),
            }

    async def _cg_funding(self, symbol: str) -> dict:
        sess = await self._sess()
        headers = {"coinglassSecret": self.cfg.coinglass_api_key}
        async with sess.get(
            f"{self.COINGLASS_BASE}/funding",
            params={"symbol": symbol},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = await r.json()
            data_map = data.get("data", {}).get("dataMap", {})
            rates: dict[str, float] = {"binance": 0.0, "bybit": 0.0, "hyperliquid": 0.0}
            for item in data_map.get("Binance", [])[:1]:
                rates["binance"] = float(item.get("fundingRate", 0)) * 100
            for item in data_map.get("Bybit", [])[:1]:
                rates["bybit"] = float(item.get("fundingRate", 0)) * 100
            return rates

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
