import asyncio
import time
from collections import defaultdict
from typing import Optional

from loguru import logger

from config.settings import config

# All possible signal sources for scoring
ALL_SOURCES = ["hyperliquid", "binance", "bybit", "oi_spike", "funding", "liquidation"]

# Weighted importance per source (total = 10)
SOURCE_WEIGHTS = {
    "hyperliquid":  3,
    "oi_spike":     2,
    "liquidation":  2,
    "binance":      1,
    "bybit":        1,
    "funding":      1,
}
_MAX_WEIGHTED_SCORE = sum(SOURCE_WEIGHTS.values())  # 10


class ConfluenceScorer:
    def __init__(self, alert_engine, cfg=None):
        self.alert_engine = alert_engine
        self.cfg = cfg or config
        self._buffer: list[dict] = []
        # Cooldown per (symbol, direction) to avoid re-firing within same window
        self._last_alert: dict[str, float] = {}

    def get_score(self, symbol: str, direction: str) -> int:
        """Return number of unique sources within the current window for (symbol, direction)."""
        now = time.time()
        window = self.cfg.confluence_window
        sources = {
            s["source"]
            for s in self._buffer
            if s["symbol"] == symbol and s["direction"] == direction and now - s["ts"] <= window
        }
        return len(sources)

    def ingest(self, symbol: str, direction: str, source: str, size_usd: float = 0.0):
        """Record a directional signal from a source. Thread-safe (GIL)."""
        if not self.cfg.confluence_enabled:
            return
        self._buffer.append({
            "symbol": symbol,
            "direction": direction,
            "source": source,
            "size_usd": size_usd,
            "ts": time.time(),
        })

    async def run(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self._check()
                self._prune()
            except Exception as e:
                logger.error(f"Confluence scorer error: {e}")

    async def _check(self):
        now = time.time()
        window = self.cfg.confluence_window
        recent = [s for s in self._buffer if now - s["ts"] <= window]

        # Group by (symbol, direction) → {source: total_size_usd}
        grouped: dict[tuple, dict] = defaultdict(dict)
        for sig in recent:
            key = (sig["symbol"], sig["direction"])
            prev = grouped[key].get(sig["source"], 0.0)
            grouped[key][sig["source"]] = prev + sig["size_usd"]

        for (symbol, direction), sources in grouped.items():
            raw_score = len(sources)
            if raw_score < self.cfg.confluence_min_sources:
                continue

            # Weighted score check
            total_score = sum(SOURCE_WEIGHTS.get(src, 1) for src in sources)
            if total_score < self.cfg.confluence_min_score_weighted:
                continue

            alert_key = f"{symbol}_{direction}"
            last = self._last_alert.get(alert_key, 0.0)
            if now - last < window:
                continue

            self._last_alert[alert_key] = now
            await self._emit(symbol, direction, raw_score, total_score, sources)

    def _prune(self):
        cutoff = time.time() - self.cfg.confluence_window * 2
        self._buffer = [s for s in self._buffer if s["ts"] >= cutoff]

    async def _emit(
        self,
        symbol: str,
        direction: str,
        score: int,
        weighted_score: int,
        sources: dict,
    ):
        from src.detector.whale_detector import WhaleAlert, AlertType
        total_usd = sum(sources.values())
        alert = WhaleAlert(
            alert_type=AlertType.CONFLUENCE,
            address=f"confluence_{symbol.lower()}",
            coin=symbol,
            size_usd=total_usd,
            direction=direction,
            price=0.0,
            extra={
                "score": score,
                "weighted_score": weighted_score,
                "max_score": _MAX_WEIGHTED_SCORE,
                "sources": dict(sources),
            },
        )
        await self.alert_engine.send_global_alert(alert)
        logger.info(
            f"Confluence: {symbol} {direction} sources={score}/{len(ALL_SOURCES)} "
            f"weighted={weighted_score}/{_MAX_WEIGHTED_SCORE}"
        )


# Module-level singleton — set by main.py after instantiation
_instance: Optional["ConfluenceScorer"] = None


def set_confluence_instance(scorer: "ConfluenceScorer") -> None:
    global _instance
    _instance = scorer


def get_confluence_score(symbol: str, direction: str) -> int:
    """Query current source count for (symbol, direction) within the confluence window."""
    if _instance is None:
        return 0
    return _instance.get_score(symbol, direction)
