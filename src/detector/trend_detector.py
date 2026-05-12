"""
Trend Detector — poll Binance Futures 4h klines, tính EMA/RSI/MACD.
Cung cấp trend direction + ATR cho signal_tracker.
"""
import asyncio
from dataclasses import dataclass
from typing import Optional
import aiohttp
from loguru import logger

from config.settings import config


@dataclass
class TrendState:
    direction: str        # "LONG" | "SHORT" | "NEUTRAL"
    score: int            # 0-3 (số indicator đồng thuận)
    atr: float            # ATR(14) trên 4h, tính bằng USD
    ema20: float
    ema50: float
    rsi: float
    macd_hist: float      # MACD histogram (positive = bullish)


# coin → TrendState
_trend_cache: dict[str, TrendState] = {}


def get_trend(coin: str) -> Optional[TrendState]:
    return _trend_cache.get(coin.upper())


# ── Indicator math (không dùng thư viện ngoài) ─────────────

def _ema(closes: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    result = [closes[0]]
    for p in closes[1:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def _rsi(closes: list[float], period: int = 14) -> float:
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(closes: list[float]) -> tuple[float, float, float]:
    """Returns (macd_line, signal_line, histogram)."""
    if len(closes) < 35:
        return 0.0, 0.0, 0.0
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = [m - n for m, n in zip(ema12, ema26)]
    signal = _ema(macd_line, 9)
    hist = macd_line[-1] - signal[-1]
    return macd_line[-1], signal[-1], hist


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if not trs:
        return 0.0
    return sum(trs[-period:]) / min(period, len(trs))


def _compute_trend(
    closes: list[float], highs: list[float], lows: list[float]
) -> TrendState:
    if len(closes) < 55:
        return TrendState("NEUTRAL", 0, 0.0, closes[-1], closes[-1], 50.0, 0.0)

    ema20_series = _ema(closes, 20)
    ema50_series = _ema(closes, 50)
    ema20 = ema20_series[-1]
    ema50 = ema50_series[-1]
    rsi = _rsi(closes)
    _, _, macd_hist = _macd(closes)
    atr = _atr(highs, lows, closes)

    long_score = 0
    short_score = 0

    # EMA cross
    if ema20 > ema50:
        long_score += 1
    else:
        short_score += 1

    # RSI
    if rsi > 52:
        long_score += 1
    elif rsi < 48:
        short_score += 1

    # MACD histogram
    if macd_hist > 0:
        long_score += 1
    elif macd_hist < 0:
        short_score += 1

    if long_score >= 2:
        direction = "LONG"
        score = long_score
    elif short_score >= 2:
        direction = "SHORT"
        score = short_score
    else:
        direction = "NEUTRAL"
        score = 0

    return TrendState(
        direction=direction,
        score=score,
        atr=atr,
        ema20=ema20,
        ema50=ema50,
        rsi=rsi,
        macd_hist=macd_hist,
    )


# ── Binance Futures klines ──────────────────────────────────

BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"


async def _fetch_klines(session: aiohttp.ClientSession, symbol: str) -> Optional[dict]:
    """Fetch 60 candles 4h từ Binance Futures. Trả về {closes, highs, lows} hoặc None."""
    params = {"symbol": f"{symbol}USDT", "interval": "4h", "limit": 60}
    try:
        async with session.get(BINANCE_FUTURES_KLINES, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                logger.warning(f"TrendDetector: Binance klines {symbol} status {r.status}")
                return None
            data = await r.json()
            # [open_time, open, high, low, close, volume, ...]
            closes = [float(c[4]) for c in data]
            highs  = [float(c[2]) for c in data]
            lows   = [float(c[3]) for c in data]
            return {"closes": closes, "highs": highs, "lows": lows}
    except Exception as e:
        logger.warning(f"TrendDetector: fetch {symbol} error: {e}")
        return None


# ── Poll loop ───────────────────────────────────────────────

async def run_trend_poll(coins: list[str], interval: int = None):
    """Chạy song song với bot — cập nhật _trend_cache mỗi interval giây."""
    if interval is None:
        interval = config.trend_poll_interval

    logger.info(f"TrendDetector started — {len(coins)} coins, interval={interval}s (4h klines)")

    async with aiohttp.ClientSession() as session:
        while True:
            for coin in coins:
                klines = await _fetch_klines(session, coin)
                if klines:
                    state = _compute_trend(**klines)
                    _trend_cache[coin.upper()] = state
                    logger.debug(
                        f"Trend {coin}: {state.direction} {state.score}/3 "
                        f"EMA20={state.ema20:.2f} EMA50={state.ema50:.2f} "
                        f"RSI={state.rsi:.1f} ATR={state.atr:.2f}"
                    )
                await asyncio.sleep(0.5)  # tránh rate limit
            await asyncio.sleep(interval)
