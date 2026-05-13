"""
DOM Analyzer — Subscribe Hyperliquid L2 order book, phân tích bid/ask ratio,
phát hiện wall và absorption. Kết quả dùng để confirm/reject kèo.
"""
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
from loguru import logger

from config.settings import config


@dataclass
class WallInfo:
    side: str           # "BID" | "ASK"
    price: float
    size_usd: float
    distance_pct: float


@dataclass
class AbsorptionInfo:
    side: str            # "BID" bị sell vào | "ASK" bị buy vào
    price: float
    absorbed_usd: float
    absorbed_pct: float
    window_seconds: int


@dataclass
class DOMSnapshot:
    coin: str
    timestamp: datetime
    mid_price: float
    bid_ask_ratio: float
    total_bid_usd: float
    total_ask_usd: float
    wall: Optional[WallInfo]
    absorption: Optional[AbsorptionInfo]
    signal: str           # "BULLISH" | "BEARISH" | "NEUTRAL"
    signal_strength: int  # 0-3


class DOMAnalyzer:
    def __init__(self):
        self._snapshots: dict[str, DOMSnapshot] = {}
        self._book_history: dict[str, list] = {}
        self._callbacks: list = []

    def on(self, event: str, callback):
        """event: 'dom_signal'"""
        self._callbacks.append((event, callback))

    async def process_l2_update(self, coin: str, bids: list, asks: list, mid_price: float):
        """
        Gọi mỗi khi nhận L2 update từ Hyperliquid WS.
        bids/asks: list of [price, size] đã convert sang float.
        """
        if coin not in config.dom_coins:
            return

        now = datetime.utcnow()
        if coin not in self._book_history:
            self._book_history[coin] = []
        self._book_history[coin].append((now, bids[:], asks[:]))

        # Giữ tối đa 5 phút history
        cutoff = now.timestamp() - 300
        self._book_history[coin] = [
            h for h in self._book_history[coin]
            if h[0].timestamp() > cutoff
        ]

        snapshot = self._analyze(coin, bids, asks, mid_price)
        self._snapshots[coin] = snapshot

        if snapshot.signal != "NEUTRAL":
            for event, cb in self._callbacks:
                if event == "dom_signal":
                    try:
                        await cb(snapshot)
                    except Exception as e:
                        logger.debug(f"DOM callback error: {e}")

    def _analyze(self, coin: str, bids: list, asks: list, mid_price: float) -> DOMSnapshot:
        depth = config.dom_book_depth_levels
        total_bid = sum(p * s for p, s in bids[:depth])
        total_ask = sum(p * s for p, s in asks[:depth])
        ratio = total_bid / total_ask if total_ask > 0 else 1.0

        wall = self._find_wall(bids, asks, mid_price)
        absorption = self._calc_absorption(coin, bids, asks)
        signal, strength = self._calc_signal(ratio, wall, absorption)

        return DOMSnapshot(
            coin=coin,
            timestamp=datetime.utcnow(),
            mid_price=mid_price,
            bid_ask_ratio=ratio,
            total_bid_usd=total_bid,
            total_ask_usd=total_ask,
            wall=wall,
            absorption=absorption,
            signal=signal,
            signal_strength=strength,
        )

    def _find_wall(self, bids: list, asks: list, mid_price: float) -> Optional[WallInfo]:
        candidates = []
        for price, size in bids:
            size_usd = price * size
            dist_pct = abs(mid_price - price) / mid_price * 100
            if size_usd >= config.dom_wall_min_usd and dist_pct <= config.dom_wall_distance_max_pct:
                candidates.append(WallInfo("BID", price, size_usd, dist_pct))
        for price, size in asks:
            size_usd = price * size
            dist_pct = abs(price - mid_price) / mid_price * 100
            if size_usd >= config.dom_wall_min_usd and dist_pct <= config.dom_wall_distance_max_pct:
                candidates.append(WallInfo("ASK", price, size_usd, dist_pct))
        if not candidates:
            return None
        return min(candidates, key=lambda w: w.distance_pct)

    def _calc_absorption(self, coin: str, bids: list, asks: list) -> Optional[AbsorptionInfo]:
        history = self._book_history.get(coin, [])
        if len(history) < 2:
            return None
        old_ts, old_bids, old_asks = history[0]

        # Ask wall bị absorbed (buyer đang eat nguồn cung → bullish)
        for (op, os), (np, ns) in zip(old_asks[:10], asks[:10]):
            if abs(op - np) < op * 0.001:
                old_usd = op * os
                new_usd = np * ns
                if old_usd >= config.dom_wall_min_usd:
                    absorbed = old_usd - new_usd
                    pct = absorbed / old_usd * 100
                    if pct >= config.dom_absorption_pct_threshold:
                        secs = int((datetime.utcnow() - old_ts).seconds)
                        return AbsorptionInfo("ASK", op, absorbed, pct, secs)

        # Bid wall bị absorbed (seller đang eat demand → bearish)
        for (op, os), (np, ns) in zip(old_bids[:10], bids[:10]):
            if abs(op - np) < op * 0.001:
                old_usd = op * os
                new_usd = np * ns
                if old_usd >= config.dom_wall_min_usd:
                    absorbed = old_usd - new_usd
                    pct = absorbed / old_usd * 100
                    if pct >= config.dom_absorption_pct_threshold:
                        secs = int((datetime.utcnow() - old_ts).seconds)
                        return AbsorptionInfo("BID", op, absorbed, pct, secs)

        return None

    def _calc_signal(self, ratio: float, wall: Optional[WallInfo], absorption: Optional[AbsorptionInfo]) -> tuple[str, int]:
        strength = 0
        direction_votes: dict[str, int] = {"BULLISH": 0, "BEARISH": 0}

        if ratio >= config.dom_bid_ask_bullish:
            direction_votes["BULLISH"] += 1
            strength += 1
        elif ratio <= config.dom_bid_ask_bearish:
            direction_votes["BEARISH"] += 1
            strength += 1

        if wall:
            if wall.side == "BID":
                direction_votes["BULLISH"] += 1
                strength += 1
            else:
                direction_votes["BEARISH"] += 1
                strength += 1

        if absorption:
            if absorption.side == "ASK":  # ask bị eat = bullish
                direction_votes["BULLISH"] += 1
                strength += 1
            else:
                direction_votes["BEARISH"] += 1
                strength += 1

        if direction_votes["BULLISH"] > direction_votes["BEARISH"]:
            return "BULLISH", strength
        elif direction_votes["BEARISH"] > direction_votes["BULLISH"]:
            return "BEARISH", strength
        return "NEUTRAL", 0

    def get_snapshot(self, coin: str) -> Optional[DOMSnapshot]:
        return self._snapshots.get(coin)


dom_analyzer = DOMAnalyzer()
