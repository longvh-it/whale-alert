"""
Trend Detector — poll Binance Futures klines (1h/4h/1d), tính EMA/RSI/MACD.
Cung cấp trend direction + ATR cho signal_tracker.
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
import aiohttp
from loguru import logger

from config.settings import config


@dataclass
class TrendState:
    direction: str          # "LONG" | "SHORT" | "NEUTRAL"
    score: int              # 0-3 (số indicator đồng thuận)
    atr: float              # ATR(14), tính bằng USD
    ema20: float
    ema50: float
    rsi: float
    macd_hist: float        # MACD histogram (positive = bullish)
    macd_hist_prev: float = 0.0
    macd_line: float = 0.0
    macd_signal_line: float = 0.0   # tránh collision với module signal
    timeframe: str = "4h"


# coin → {timeframe → TrendState}
_trend_cache: dict[str, dict[str, TrendState]] = {}


def get_trend(coin: str) -> Optional[TrendState]:
    """Backward-compat: trả về TrendState 4h."""
    return _trend_cache.get(coin.upper(), {}).get("4h")


def get_multi_trend(coin: str) -> tuple[str, int]:
    """
    Tổng hợp vote 1h/4h/1d.
    Trả về (direction, votes_count) — LONG/SHORT nếu ≥2 khung cùng chiều, NEUTRAL nếu không.
    """
    timeframes = ["1h", "4h", "1d"]
    votes: dict[str, int] = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}
    for tf in timeframes:
        trend = _trend_cache.get(coin.upper(), {}).get(tf)
        if trend:
            votes[trend.direction] += 1
    if votes["LONG"] >= 2:
        return "LONG", votes["LONG"]
    elif votes["SHORT"] >= 2:
        return "SHORT", votes["SHORT"]
    else:
        return "NEUTRAL", 0


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


def _macd(closes: list[float]) -> tuple[float, float, float, float]:
    """Returns (macd_line, signal_line, histogram, histogram_prev)."""
    if len(closes) < 35:
        return 0.0, 0.0, 0.0, 0.0
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_series = [m - n for m, n in zip(ema12, ema26)]
    sig_series = _ema(macd_series, 9)
    hist = macd_series[-1] - sig_series[-1]
    hist_prev = (
        (macd_series[-2] - sig_series[-2])
        if len(macd_series) >= 2 and len(sig_series) >= 2
        else 0.0
    )
    return macd_series[-1], sig_series[-1], hist, hist_prev


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
    closes: list[float],
    highs: list[float],
    lows: list[float],
    timeframe: str = "4h",
) -> TrendState:
    if len(closes) < 55:
        return TrendState(
            "NEUTRAL", 0, 0.0, closes[-1], closes[-1], 50.0, 0.0, timeframe=timeframe
        )

    ema20_series = _ema(closes, 20)
    ema50_series = _ema(closes, 50)
    ema20 = ema20_series[-1]
    ema50 = ema50_series[-1]
    rsi = _rsi(closes)
    macd_line_val, macd_signal_val, macd_hist, macd_hist_prev = _macd(closes)
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
        macd_hist_prev=macd_hist_prev,
        macd_line=macd_line_val,
        macd_signal_line=macd_signal_val,
        timeframe=timeframe,
    )


# ── Binance Futures klines ──────────────────────────────────

BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"


async def _fetch_klines_raw(
    session: aiohttp.ClientSession,
    symbol: str,
    timeframe: str = "4h",
    limit: int = 60,
) -> Optional[list]:
    """Fetch klines từ Binance Futures. Trả về raw list hoặc None."""
    params = {"symbol": f"{symbol}USDT", "interval": timeframe, "limit": limit}
    try:
        async with session.get(
            BINANCE_FUTURES_KLINES, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                logger.warning(f"TrendDetector: Binance klines {symbol} [{timeframe}] status {r.status}")
                return None
            return await r.json()
    except Exception as e:
        logger.warning(f"TrendDetector: fetch {symbol} [{timeframe}] error: {e}")
        return None


async def _fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    timeframe: str = "4h",
    limit: int = 60,
) -> Optional[dict]:
    """Fetch klines từ Binance Futures. Trả về {closes, highs, lows} hoặc None."""
    data = await _fetch_klines_raw(session, symbol, timeframe, limit)
    if data is None:
        return None
    closes = [float(c[4]) for c in data]
    highs  = [float(c[2]) for c in data]
    lows   = [float(c[3]) for c in data]
    return {"closes": closes, "highs": highs, "lows": lows}


# ── Multi-timeframe poll loop ───────────────────────────────

async def _check_volume_spike(coin: str, klines_raw: list, direction_from_close: bool = True):
    """
    So sánh volume nến hiện tại với MA20 volume.
    Gọi ecosystem_detector.on_volume_spike() nếu đủ ngưỡng.
    """
    if len(klines_raw) < 21:
        return
    try:
        from src.detector.ecosystem_detector import ecosystem_detector
        from config.settings import config as _cfg
        volumes = [float(k[5]) for k in klines_raw]
        current_vol = volumes[-1]
        ma20_vol = sum(volumes[-21:-1]) / 20
        if ma20_vol == 0:
            return
        ratio = current_vol / ma20_vol
        if ratio >= _cfg.ecosystem_volume_spike_min:
            open_price = float(klines_raw[-1][1])
            close_price = float(klines_raw[-1][4])
            direction = "LONG" if close_price >= open_price else "SHORT"
            logger.info(f"Volume spike {coin}: {ratio:.1f}x MA20, direction={direction}")
            await ecosystem_detector.on_volume_spike(coin, ratio, direction)
    except Exception as e:
        logger.debug(f"_check_volume_spike error: {e}")


async def run_trend_poll(coins: list[str], interval: int = None):
    """Polls 1h/4h/1d klines với interval riêng biệt cho mỗi timeframe."""
    tf_config = {
        "1h": {"interval": config.trend_poll_interval_1h, "limit": 100},
        "4h": {"interval": config.trend_poll_interval_4h, "limit": 60},
        "1d": {"interval": config.trend_poll_interval_1d, "limit": 60},
    }
    last_poll: dict[str, float] = {tf: 0.0 for tf in tf_config}

    logger.info(
        f"TrendDetector (multi-TF) started — {len(coins)} coins, "
        f"1h/{config.trend_poll_interval_1h}s  "
        f"4h/{config.trend_poll_interval_4h}s  "
        f"1d/{config.trend_poll_interval_1d}s"
    )

    async with aiohttp.ClientSession() as session:
        while True:
            now = time.time()
            for tf, cfg_tf in tf_config.items():
                if now - last_poll[tf] >= cfg_tf["interval"]:
                    for coin in coins:
                        klines_raw = await _fetch_klines_raw(session, coin, tf, cfg_tf["limit"])
                        if klines_raw:
                            closes = [float(c[4]) for c in klines_raw]
                            highs  = [float(c[2]) for c in klines_raw]
                            lows   = [float(c[3]) for c in klines_raw]
                            state = _compute_trend(closes=closes, highs=highs, lows=lows, timeframe=tf)
                            _trend_cache.setdefault(coin.upper(), {})[tf] = state
                            logger.debug(
                                f"Trend {coin} [{tf}]: {state.direction} {state.score}/3 "
                                f"RSI={state.rsi:.1f} ATR={state.atr:.2f}"
                            )
                            # Check volume spike on 4h candle for ecosystem detection
                            if tf == "4h":
                                await _check_volume_spike(coin.upper(), klines_raw)
                        await asyncio.sleep(0.3)
                    last_poll[tf] = time.time()
            await asyncio.sleep(60)
