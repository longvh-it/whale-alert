import aiohttp
from loguru import logger
from config.settings import config


class HyperliquidREST:
    def __init__(self):
        self.url = config.hl_api_url

    async def _post(self, payload: dict) -> dict | list | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.warning(f"REST {resp.status}: {await resp.text()}")
        except Exception as e:
            logger.error(f"REST error: {e}")
        return None

    async def get_user_state(self, address: str) -> dict | None:
        """Get all open positions + account value for an address."""
        return await self._post({"type": "clearinghouseState", "user": address})

    async def get_leaderboard(self) -> list | None:
        """Top traders by PnL."""
        return await self._post({"type": "leaderboard"})

    async def get_all_mids(self) -> dict | None:
        """Current mark prices for all assets."""
        return await self._post({"type": "allMids"})

    async def get_open_orders(self, address: str) -> list | None:
        return await self._post({"type": "openOrders", "user": address})

    async def get_user_fills(self, address: str) -> list | None:
        """Recent fill history for an address."""
        return await self._post({"type": "userFills", "user": address})


rest = HyperliquidREST()
