"""
Trend Scanner — phát hiện coin có volume spike + trend mạnh, tự động tạo kèo 1-2 lần/ngày.
Được gọi từ trend_detector._check_volume_spike() sau mỗi lần poll 4h candle.

Điều kiện call kèo:
  - Volume nến hiện tại / MA20 volume >= SCAN_VOLUME_MIN (mặc định 2x)
  - Multi-TF trend >= SCAN_MIN_TREND_SCORE/3 cùng chiều volume spike (mặc định 2/3)
  - Không có kèo PENDING/ACTIVE cho coin đó
  - Chưa vượt giới hạn SCAN_DAILY_MAX kèo/ngày (mặc định 2)
  - Cooldown SCAN_COIN_COOLDOWN_HOURS giờ/coin (mặc định 24h)
  - DOM không đối nghịch mạnh (nếu có dữ liệu)
"""
from datetime import datetime, date
from typing import TYPE_CHECKING, Optional
from loguru import logger

from config.settings import config

if TYPE_CHECKING:
    from src.signals.signal_tracker import SignalTracker
    from src.detector.dom_analyzer import DOMAnalyzer


class TrendScanner:
    def __init__(self):
        self._signal_tracker: Optional["SignalTracker"] = None
        self._dom_analyzer: Optional["DOMAnalyzer"] = None
        self._bot = None
        self._daily_count: int = 0
        self._daily_date: str = ""
        self._coin_last_signal: dict[str, datetime] = {}

    def set_signal_tracker(self, tracker):
        self._signal_tracker = tracker

    def set_dom_analyzer(self, dom):
        self._dom_analyzer = dom

    def set_bot(self, bot):
        self._bot = bot

    def _reset_daily_if_needed(self):
        today = str(date.today())
        if self._daily_date != today:
            self._daily_date = today
            self._daily_count = 0

    def _coin_cooled_down(self, coin: str) -> bool:
        last = self._coin_last_signal.get(coin)
        if last is None:
            return True
        elapsed_hours = (datetime.utcnow() - last).total_seconds() / 3600
        return elapsed_hours >= config.scan_coin_cooldown_hours

    async def on_volume_spike(self, coin: str, volume_ratio: float, direction: str):
        if not config.scan_enabled:
            return

        self._reset_daily_if_needed()

        if self._daily_count >= config.scan_daily_max:
            logger.debug(f"TrendScanner: daily max {config.scan_daily_max} reached, skip {coin}")
            return

        if volume_ratio < config.scan_volume_min:
            return

        if not self._coin_cooled_down(coin):
            logger.debug(f"TrendScanner: {coin} còn trong cooldown {config.scan_coin_cooldown_hours}h, skip")
            return

        from src.detector.trend_detector import get_multi_trend, get_trend
        multi_direction, confirmed = get_multi_trend(coin)

        if multi_direction != direction:
            logger.debug(
                f"TrendScanner: {coin} vol={direction} nhưng trend={multi_direction} ({confirmed}/3), skip"
            )
            return

        if confirmed < config.scan_min_trend_score:
            logger.debug(
                f"TrendScanner: {coin} trend {confirmed}/3 < {config.scan_min_trend_score}, skip"
            )
            return

        trend = get_trend(coin)
        if trend is None:
            logger.debug(f"TrendScanner: chưa có trend data 4h cho {coin}, skip")
            return

        # DOM: chặn nếu đối nghịch mạnh
        dom_snapshot = None
        if self._dom_analyzer and coin in config.dom_coins:
            dom_snapshot = self._dom_analyzer.get_snapshot(coin)
            if dom_snapshot:
                opposite = "BEARISH" if direction == "LONG" else "BULLISH"
                if dom_snapshot.signal == opposite and dom_snapshot.signal_strength >= 2:
                    logger.info(
                        f"TrendScanner: DOM {dom_snapshot.signal} (strength={dom_snapshot.signal_strength}) "
                        f"đối nghịch {direction} {coin}, skip"
                    )
                    return

        logger.info(
            f"TrendScanner: {coin} {direction} — vol={volume_ratio:.1f}x MA20, "
            f"trend={confirmed}/3, RSI={trend.rsi:.0f}"
        )

        if self._signal_tracker:
            created = await self._signal_tracker.maybe_create_scan_signal(
                coin=coin,
                direction=direction,
                volume_ratio=volume_ratio,
                trend_confirmed=confirmed,
                trend=trend,
                dom_snapshot=dom_snapshot,
            )
            if created:
                self._daily_count += 1
                self._coin_last_signal[coin] = datetime.utcnow()
                logger.info(
                    f"TrendScanner: đã tạo kèo #{self._daily_count}/{config.scan_daily_max} "
                    f"hôm nay cho {coin}"
                )


trend_scanner = TrendScanner()
