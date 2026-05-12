import asyncio

from loguru import logger

from config.settings import config


class FundingRateDetector:
    def __init__(self, coinglass, alert_engine, cfg=None, confluence=None):
        self.coinglass = coinglass
        self.alert_engine = alert_engine
        self.cfg = cfg or config
        self.confluence = confluence

    async def run(self):
        symbols = self.cfg.binance_symbols
        while True:
            for sym in symbols:
                try:
                    await self._check(sym)
                except Exception as e:
                    logger.error(f"Funding detector [{sym}]: {e}")
            await asyncio.sleep(self.cfg.funding_poll_interval)

    async def _check(self, symbol: str):
        rates = await self.coinglass.get_funding_rates(symbol)
        valid = [v for v in rates.values() if v != 0.0]
        if not valid:
            return
        avg = sum(valid) / len(valid)
        if avg >= self.cfg.funding_extreme_high or avg <= self.cfg.funding_extreme_low:
            await self._emit(symbol, rates, avg)

    async def _emit(self, symbol: str, rates: dict, avg: float):
        from src.detector.whale_detector import WhaleAlert, AlertType
        # Over-leveraged LONG → counter-signal is SHORT (fade). Over-leveraged SHORT → LONG.
        direction = "SHORT" if avg > 0 else "LONG"
        alert = WhaleAlert(
            alert_type=AlertType.FUNDING_EXTREME,
            address=f"funding_{symbol.lower()}",
            coin=symbol,
            size_usd=0.0,
            direction=direction,
            price=0.0,
            extra={
                "avg_funding": avg,
                "rates": rates,
            },
        )
        await self.alert_engine.send_global_alert(alert)
        if self.confluence:
            self.confluence.ingest(symbol, direction, "funding", 0.0)
        logger.info(f"Funding Extreme: {symbol} avg={avg:+.4f}%")
