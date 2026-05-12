import asyncio

from loguru import logger

from config.settings import config


class OISpikeDetector:
    def __init__(self, coinglass, alert_engine, cfg=None, confluence=None):
        self.coinglass = coinglass
        self.alert_engine = alert_engine
        self.cfg = cfg or config
        self.confluence = confluence
        self._last_oi: dict[str, float] = {}

    async def run(self):
        symbols = self.cfg.binance_symbols
        while True:
            for sym in symbols:
                try:
                    await self._check(sym)
                except Exception as e:
                    logger.error(f"OI detector [{sym}]: {e}")
            await asyncio.sleep(self.cfg.oi_poll_interval)

    async def _check(self, symbol: str):
        data = await self.coinglass.get_open_interest(symbol)
        oi = data.get("total_oi_usd", 0.0)
        if oi <= 0:
            return

        prev = self._last_oi.get(symbol)
        self._last_oi[symbol] = oi

        if prev is None or prev <= 0:
            return

        change_pct = (oi - prev) / prev * 100
        if abs(change_pct) >= self.cfg.oi_spike_threshold:
            await self._emit(symbol, oi, prev, change_pct)

    async def _emit(self, symbol: str, oi: float, prev_oi: float, change_pct: float):
        from src.detector.whale_detector import WhaleAlert, AlertType
        direction = "LONG" if change_pct > 0 else "SHORT"
        alert = WhaleAlert(
            alert_type=AlertType.OI_SPIKE,
            address=f"oi_{symbol.lower()}",
            coin=symbol,
            size_usd=oi,
            direction=direction,
            price=0.0,
            extra={
                "change_pct": change_pct,
                "prev_oi": prev_oi,
                "current_oi": oi,
            },
        )
        await self.alert_engine.send_global_alert(alert)
        if self.confluence:
            self.confluence.ingest(symbol, direction, "oi_spike", oi)
        logger.info(f"OI Spike: {symbol} {change_pct:+.2f}%  total=${oi/1e9:.2f}B")
