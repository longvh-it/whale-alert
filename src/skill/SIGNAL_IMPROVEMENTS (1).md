# Signal Optimization Tasks

Spec này mô tả các tính năng cần implement để tối ưu hệ thống kèo (signal) của bot.
Đọc `CLAUDE.md` trước để nắm kiến trúc hiện tại.

---

## TASK 1 — TP1 Hit: Move SL về Entry khi có dấu hiệu đảo chiều

**File chính:** `src/signals/signal_tracker.py`

### Mô tả
Khi kèo hit TP1, kiểm tra dấu hiệu đảo chiều. Nếu đủ điều kiện thì dời SL về entry (breakeven) thay vì giữ SL gốc. Không move SL vô điều kiện.

### Config mới — thêm vào `config/settings.py`
```python
TP1_REVERSAL_MOVE_SL_ENABLED: bool = True
TP1_REVERSAL_MIN_SCORE: int = 2   # số dấu hiệu tối thiểu để move SL
TP1_REVERSAL_WARN_SCORE: int = 1  # chỉ post warning, chưa move
```

### Schema DB — thêm vào `src/storage/database.py`
```sql
ALTER TABLE signals ADD COLUMN sl_moved_to_entry INTEGER DEFAULT 0;
ALTER TABLE signals ADD COLUMN sl_move_reason TEXT;
-- sl_move_reason: comma-separated, e.g. "rsi_weak,whale_opposite"
```
Wrap trong `try/except` như các migration hiện tại.

Thêm method vào `Database`:
```python
async def update_signal_sl(self, signal_id: int, new_sl: float, moved_to_entry: bool = False, reason: str = None)
```

### Hàm tính reversal score — thêm vào `signal_tracker.py`
```python
def _count_reversal_signals_soft(
    self,
    signal: Signal,
    trend: TrendState,
    recent_whale_events: list  # list WhaleAlert trong 30 phút gần nhất
) -> tuple[int, list[str]]:
    """
    Trả về (score, reasons[]).
    Dùng threshold nhẹ hơn _count_reversal_signals() vì chỉ để move SL, không cắt kèo.
    """
    score = 0
    reasons = []

    # 1. EMA momentum yếu: EMA20 gần hoặc dưới EMA50
    if signal.direction == "LONG" and trend.ema20 < trend.ema50 * 1.002:
        score += 1
        reasons.append("ema_weak")
    elif signal.direction == "SHORT" and trend.ema20 > trend.ema50 * 0.998:
        score += 1
        reasons.append("ema_weak")

    # 2. RSI quay đầu
    if signal.direction == "LONG" and trend.rsi < 52:
        score += 1
        reasons.append("rsi_weak")
    elif signal.direction == "SHORT" and trend.rsi > 48:
        score += 1
        reasons.append("rsi_weak")

    # 3. MACD histogram thu hẹp >50% so với nến trước
    if hasattr(trend, 'macd_hist') and hasattr(trend, 'macd_hist_prev'):
        if trend.macd_hist_prev != 0:
            shrink = trend.macd_hist / trend.macd_hist_prev
            if signal.direction == "LONG" and shrink < 0.5:
                score += 1
                reasons.append("macd_shrink")
            elif signal.direction == "SHORT" and shrink < 0.5:
                score += 1
                reasons.append("macd_shrink")

    # 4. Xuất hiện whale ngược chiều trong 30 phút gần nhất
    opposite_side = "SELL" if signal.direction == "LONG" else "BUY"
    has_opposite_whale = any(
        e.coin == signal.coin and e.side == opposite_side
        for e in recent_whale_events
    )
    if has_opposite_whale:
        score += 1
        reasons.append("whale_opposite")

    return score, reasons
```

**Lưu ý:** `TrendState` hiện tại trong `trend_detector.py` có thể chưa có `macd_hist_prev`. Nếu chưa có thì thêm field này khi tính MACD — lưu lại histogram của nến trước đó.

### Logic trong `_on_tp1_hit` (tạo mới hoặc tích hợp vào flow hiện tại)
```python
async def _on_tp1_hit(self, signal, current_price):
    # --- phần hiện tại: update status, gửi notification ---
    await db.update_signal_status(signal.id, "TP1_HIT")
    await self._send_hit_notification(signal, "TP1", current_price)

    # --- phần mới ---
    if not config.TP1_REVERSAL_MOVE_SL_ENABLED:
        return

    trend = get_trend(signal.coin)
    if trend is None:
        return

    score, reasons = self._count_reversal_signals_soft(
        signal, trend, self._recent_whale_events
    )

    if score >= config.TP1_REVERSAL_MIN_SCORE:
        await db.update_signal_sl(
            signal.id,
            signal.entry_price,
            moved_to_entry=True,
            reason=",".join(reasons)
        )
        await self._edit_signal_message(signal)
        await self._notify_sl_moved(signal, score, reasons, current_price)

    elif score >= config.TP1_REVERSAL_WARN_SCORE:
        await self._notify_sl_move_warning(signal, score, reasons)
```

### Buffer whale events
Thêm `self._recent_whale_events: list = []` vào `__init__` của `SignalTracker`.
Mỗi khi `AlertEngine` route một `WhaleAlert` kiểu `BIG_TRADE`, append vào list này.
Giữ tối đa 30 phút: mỗi event lưu thêm `timestamp`, dọn dẹp trong `_check_signals()`.

### Messages Telegram

**Score = 0 — không thay đổi:**
```
✅ TP1 HIT — {coin} {direction}

📍 Entry: {entry}
🎯 TP1: {tp1} (+{pct}%) ✅
🎯 TP2: {tp2}
🎯 TP3: {tp3}
🛑 SL: {sl} (giữ nguyên)

📊 Trend vẫn mạnh, tiếp tục hold TP2/TP3
```

**Score = 1 — warning:**
```
✅ TP1 HIT — {coin} {direction}

📍 Entry: {entry}
🎯 TP1: {tp1} (+{pct}%) ✅
🛑 SL: {sl} (chưa thay đổi)

⚠️ Có 1 dấu hiệu yếu ({reason})
→ Đang theo dõi, chưa cần hành động
```

**Score >= 2 — move SL:**
```
✅ TP1 HIT — {coin} {direction}

📍 Entry: {entry}
🎯 TP1: {tp1} (+{pct}%) ✅
🎯 TP2: {tp2}
🛑 SL: {entry} ← dời về entry (breakeven)

🛡 Lý do bảo vệ vốn:
  {reason_1} ✗
  {reason_2} ✗
→ Worst case: hoà vốn. Vẫn target TP2/TP3 nếu thị trường hồi
```

Reason display mapping:
- `ema_weak` → "EMA20 yếu so với EMA50"
- `rsi_weak` → "RSI quay đầu ({rsi_value})"
- `macd_shrink` → "MACD histogram thu hẹp"
- `whale_opposite` → "Whale {opposite_side} xuất hiện"

---

## TASK 2 — Tự động cắt kèo khi xu hướng đảo chiều

**File chính:** `src/signals/signal_tracker.py`
**File phụ:** `src/detector/trend_detector.py`

### Mô tả
Mỗi chu kỳ poll giá (5s), kiểm tra thêm dấu hiệu đảo chiều cho các kèo đang `ACTIVE` hoặc `TP1_HIT`. Nếu đủ điều kiện thì đóng kèo sớm và thông báo.

### Config mới — thêm vào `config/settings.py`
```python
REVERSAL_CUT_ENABLED: bool = True
REVERSAL_MIN_SCORE: int = 2        # cắt kèo khi đạt score này
REVERSAL_GRACE_MINUTES: int = 60   # không cắt trong N phút đầu nếu score == 2
REVERSAL_ALT_MIN_SCORE: int = 3    # altcoin cần score cao hơn major
REVERSAL_CHECK_INTERVAL: int = 15  # check mỗi N * price_poll_cycles
                                    # (tránh check mỗi 5s cho nặng)
```

### Schema DB — thêm vào `src/storage/database.py`
```sql
ALTER TABLE signals ADD COLUMN close_reason TEXT;
-- values: 'sl_hit' | 'tp3_hit' | 'trend_reversal' | 'manual' | 'timeout'

ALTER TABLE signals ADD COLUMN close_price REAL;
ALTER TABLE signals ADD COLUMN reversal_score INTEGER;
```

Thêm method:
```python
async def close_signal_early(self, signal_id: int, close_price: float, reason: str, reversal_score: int)
```

### Hàm tính reversal score (hard version — dùng cho cắt kèo)
```python
def _count_reversal_signals(
    self,
    signal: Signal,
    trend: TrendState,
    recent_whale_events: list
) -> tuple[int, list[str]]:
    """
    Threshold mạnh hơn _count_reversal_signals_soft().
    Cần ít nhất các điều kiện rõ ràng hơn để tránh cắt nhầm.
    """
    score = 0
    reasons = []

    # 1. EMA cross hoàn toàn (không chỉ gần nhau)
    if signal.direction == "LONG" and trend.ema20 < trend.ema50:
        score += 1
        reasons.append("ema_cross_bearish")
    elif signal.direction == "SHORT" and trend.ema20 > trend.ema50:
        score += 1
        reasons.append("ema_cross_bullish")

    # 2. RSI breakdown rõ ràng
    if signal.direction == "LONG" and trend.rsi < 45:
        score += 1
        reasons.append(f"rsi_breakdown({trend.rsi:.0f})")
    elif signal.direction == "SHORT" and trend.rsi > 55:
        score += 1
        reasons.append(f"rsi_breakout({trend.rsi:.0f})")

    # 3. MACD cross ngược chiều
    if hasattr(trend, 'macd_line') and hasattr(trend, 'macd_signal'):
        if signal.direction == "LONG" and trend.macd_line < trend.macd_signal:
            score += 1
            reasons.append("macd_cross_bearish")
        elif signal.direction == "SHORT" and trend.macd_line > trend.macd_signal:
            score += 1
            reasons.append("macd_cross_bullish")

    # 4. Whale ngược chiều (lớn hơn threshold thông thường)
    opposite_side = "SELL" if signal.direction == "LONG" else "BUY"
    opposite_whales = [
        e for e in recent_whale_events
        if e.coin == signal.coin and e.side == opposite_side
    ]
    if opposite_whales:
        score += 1
        reasons.append(f"whale_opposite(${sum(e.size_usd for e in opposite_whales)/1e6:.1f}M)")

    # 5. Trend direction đảo hoàn toàn
    if trend.direction != signal.direction:
        score += 1
        reasons.append("trend_flipped")

    return score, reasons
```

### Logic check trong price poll loop
```python
async def _check_signals(self):
    # ... phần check TP/SL hiện tại giữ nguyên ...

    # Thêm: check reversal định kỳ
    self._reversal_check_counter = getattr(self, '_reversal_check_counter', 0) + 1
    if self._reversal_check_counter >= config.REVERSAL_CHECK_INTERVAL:
        self._reversal_check_counter = 0
        await self._check_trend_reversals()

async def _check_trend_reversals(self):
    if not config.REVERSAL_CUT_ENABLED:
        return

    active_signals = await db.get_signals_by_status(["ACTIVE", "TP1_HIT"])

    for signal in active_signals:
        trend = get_trend(signal.coin)
        if trend is None:
            continue

        is_major = signal.coin in config.AUTO_SIGNAL_MAJOR_COINS
        min_score = config.REVERSAL_MIN_SCORE if is_major else config.REVERSAL_ALT_MIN_SCORE

        score, reasons = self._count_reversal_signals(
            signal, trend, self._recent_whale_events
        )

        if score < min_score:
            continue

        # Grace period: kèo non trẻ + score vừa đủ → bỏ qua
        age_minutes = (datetime.utcnow() - signal.created_at).seconds / 60
        if score == min_score and age_minutes < config.REVERSAL_GRACE_MINUTES:
            continue

        current_price = await self._get_current_price(signal.coin)
        await self._close_signal_early(signal, current_price, score, reasons)

async def _close_signal_early(self, signal, current_price, score, reasons):
    await db.close_signal_early(
        signal.id,
        close_price=current_price,
        reason="trend_reversal",
        reversal_score=score
    )
    await self._send_reversal_cut_notification(signal, current_price, score, reasons)
    await self._edit_signal_message(signal, status="CANCELLED", note="⚠️ Cắt sớm - trend đảo")
```

### Message Telegram khi cắt kèo
```
⚠️ KÈO CẮT SỚM — {coin} {direction}

📍 Entry: {entry}
📉 Giá lúc cắt: {current_price} ({pnl_pct:+.1f}%)
⏱ Thời gian giữ: {duration}

🔄 Dấu hiệu đảo chiều ({score} điểm):
  {reason_1}
  {reason_2}
  ...

📊 Kết quả: {cắt lỗ X% / hoà vốn / giữ lãi TP1}
→ SL gốc tại {sl_price} ({sl_pct:+.1f}%) — tránh được {saved_pct:.1f}%
```

Dòng "tránh được X%" tính: `abs(current_price - sl_price) / entry_price * 100`

### Trường hợp đặc biệt cho kèo `TP1_HIT`
Nếu SL đã được move về entry (từ Task 1), kèo `TP1_HIT` sẽ tự nhiên bị stop out ở breakeven khi giá về entry. Tuy nhiên nếu giá chưa về entry mà trend đảo mạnh (score >= min_score + 1), vẫn cắt ngay:

```python
if signal.status == "TP1_HIT" and signal.sl_moved_to_entry:
    # Chỉ cắt chủ động nếu score rất cao
    if score < min_score + 1:
        continue  # để SL tự xử lý
```

---

## TASK 3 — Auto-cancel kèo PENDING bị stale

**File chính:** `src/signals/signal_tracker.py`

### Mô tả
Kèo LIMIT không được fill sau N giờ → tự động cancel và thông báo channel.

### Config mới
```python
SIGNAL_PENDING_TIMEOUT_HOURS: float = 4.0
```

### Logic — thêm vào `_check_signals()`
```python
async def _cancel_stale_pending_signals(self):
    pending_signals = await db.get_signals_by_status(["PENDING"])
    now = datetime.utcnow()

    for signal in pending_signals:
        age_hours = (now - signal.created_at).seconds / 3600
        if age_hours >= config.SIGNAL_PENDING_TIMEOUT_HOURS:
            await db.close_signal_early(signal.id, close_price=None, reason="timeout", reversal_score=0)
            await self._send_timeout_notification(signal, age_hours)
            await self._edit_signal_message(signal, status="CANCELLED", note="⏰ Hết thời gian chờ")
```

Gọi `_cancel_stale_pending_signals()` trong `_check_signals()` mỗi 60 cycles (~5 phút).

### Message Telegram
```
⏰ KÈO HẾT HẠN — {coin} {direction}

Kèo LIMIT chờ {age_hours:.1f}h chưa được fill.
📍 Entry limit: {entry}
💀 Tự động huỷ.

→ Giá hiện tại: {current_price} (cách entry {diff_pct:+.1f}%)
```

---

## TASK 4 — Weighted Confluence Scoring

**File chính:** `src/aggregator/confluence_scorer.py`

### Mô tả
Thay vì đếm số source bằng nhau, mỗi source có trọng số khác nhau.

### Thay đổi trong `confluence_scorer.py`
```python
SOURCE_WEIGHTS = {
    "hyperliquid":  3,   # smart money on-chain
    "oi_spike":     2,
    "liquidation":  2,
    "binance":      1,
    "bybit":        1,
    "funding":      1,
}

# Thay vì đếm len(sources) >= CONFLUENCE_MIN_SOURCES
# Tính tổng điểm
total_score = sum(SOURCE_WEIGHTS.get(src, 1) for src in sources)
if total_score >= config.CONFLUENCE_MIN_SCORE_WEIGHTED:  # config mới, default 5
    # fire confluence alert
```

Config mới:
```python
CONFLUENCE_MIN_SCORE_WEIGHTED: int = 5
```

Thêm `weighted_score` vào `WhaleAlert` format message để hiển thị điểm.

---

## TASK 5 — Multi-timeframe Trend Confirmation

**File chính:** `src/detector/trend_detector.py`

### Mô tả
Hiện chỉ dùng 4h. Thêm 1h và 1d. Kèo chỉ được tạo khi ≥ 2/3 timeframe đồng thuận.

### Thay đổi `TrendState`
```python
@dataclass
class TrendState:
    direction: str          # "LONG" | "SHORT" | "NEUTRAL"
    score: float            # existing
    ema20: float
    ema50: float
    rsi: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    macd_hist_prev: float   # THÊM MỚI — nến trước
    atr: float
    timeframe: str          # THÊM MỚI — "1h" | "4h" | "1d"
```

### Thay đổi `_trend_cache`
```python
# Hiện tại: _trend_cache[coin] = TrendState
# Đổi thành:
_trend_cache: dict[str, dict[str, TrendState]] = {}
# _trend_cache[coin][timeframe] = TrendState
```

### Thêm hàm `get_multi_trend(coin)`
```python
def get_multi_trend(coin: str) -> tuple[str, int]:
    """
    Trả về (direction, confirmed_count).
    direction: "LONG" | "SHORT" | "NEUTRAL"
    confirmed_count: số timeframe đồng thuận (0-3)
    """
    timeframes = ["1h", "4h", "1d"]
    votes = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}

    for tf in timeframes:
        trend = _trend_cache.get(coin, {}).get(tf)
        if trend:
            votes[trend.direction] += 1

    if votes["LONG"] >= 2:
        return "LONG", votes["LONG"]
    elif votes["SHORT"] >= 2:
        return "SHORT", votes["SHORT"]
    else:
        return "NEUTRAL", 0
```

### Thay đổi trong `signal_tracker.py` — `maybe_create_auto_signal()`
```python
# Hiện tại
trend = get_trend(coin)
if trend.direction != whale_direction: return
if trend.score < config.TREND_MIN_SCORE: return

# Đổi thành
direction, confirmed = get_multi_trend(coin)
if direction != whale_direction: return
if confirmed < 2: return  # yêu cầu ít nhất 2/3 timeframe
```

### Poll intervals
Thêm config:
```python
TREND_POLL_INTERVAL_1H: int = 300   # 5 phút
TREND_POLL_INTERVAL_4H: int = 900   # 15 phút (hiện tại)
TREND_POLL_INTERVAL_1D: int = 3600  # 1 giờ
```

---

## TASK 6 — Signal Quality Score (0-100)

**File chính:** `src/signals/signal_tracker.py`

### Mô tả
Tính điểm chất lượng kèo ngay lúc tạo, lưu DB, hiển thị trên Telegram. Chỉ post kèo khi score đủ ngưỡng.

### Schema DB
```sql
ALTER TABLE signals ADD COLUMN quality_score INTEGER DEFAULT 0;
```

### Hàm tính score
```python
def _calc_signal_quality(
    self,
    whale_size_usd: float,
    trend_confirmed: int,       # số timeframe đồng thuận (0-3)
    confluence_score: int,      # weighted score từ Task 4
    volume_ratio: float,        # volume hiện tại / MA20 volume (nếu có)
) -> int:
    score = 0

    # Trend confirmation: max 30 điểm
    score += trend_confirmed * 10  # 0 / 10 / 20 / 30

    # Confluence: max 40 điểm
    score += min(confluence_score * 5, 40)

    # Volume spike: max 20 điểm
    if volume_ratio >= 2.0:
        score += 20
    elif volume_ratio >= 1.5:
        score += 10
    elif volume_ratio >= 1.0:
        score += 5

    # Whale size tier: max 10 điểm
    if whale_size_usd >= 5_000_000:
        score += 10    # XL
    elif whale_size_usd >= 2_000_000:
        score += 7     # L
    elif whale_size_usd >= 1_000_000:
        score += 4     # M
    else:
        score += 1     # S

    return min(score, 100)
```

Config:
```python
SIGNAL_MIN_QUALITY_SCORE: int = 50   # bỏ qua kèo dưới ngưỡng này
```

### Hiển thị trong message kèo
```
🎯 KÈO {direction} — {coin}  [⭐ {score}/100]

📍 Entry: ...
```

---

## TASK 7 — Daily Loss Limit

**File chính:** `src/signals/signal_tracker.py`

### Mô tả
Nếu trong ngày có quá nhiều kèo SL, tắt auto-signal đến hết ngày UTC.

### Config mới
```python
DAILY_SL_LIMIT: int = 3
DAILY_LOSS_LIMIT_ENABLED: bool = True
```

### Logic
```python
async def _check_daily_loss_limit(self) -> bool:
    """Trả về True nếu đã đạt giới hạn thua trong ngày."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0)
    sl_count = await db.count_sl_hits_since(today_start)

    if sl_count >= config.DAILY_SL_LIMIT:
        return True
    return False
```

Trong `maybe_create_auto_signal()`:
```python
if config.DAILY_LOSS_LIMIT_ENABLED and await self._check_daily_loss_limit():
    logger.info(f"Daily SL limit reached, skipping auto signal for {coin}")
    return
```

Thêm DB method:
```python
async def count_sl_hits_since(self, since: datetime) -> int:
    # SELECT COUNT(*) FROM signals WHERE close_reason='sl_hit' AND updated_at >= since
```

Thêm notification admin khi hit limit (1 lần duy nhất per ngày):
```
🚫 AUTO-SIGNAL TẠM DỪNG

Đã có {DAILY_SL_LIMIT} kèo SL hôm nay.
Auto-signal tắt đến 00:00 UTC.
Dùng /signal để tạo kèo thủ công nếu cần.
```

---

## TASK 8 — DOM (Order Book) Analysis

**File mới:** `src/detector/dom_analyzer.py`
**File phụ:** `src/api/hyperliquid_ws.py`, `src/signals/signal_tracker.py`, `src/aggregator/confluence_scorer.py`

### Mô tả
Subscribe Hyperliquid L2 order book feed, phân tích bid/ask ratio, phát hiện wall và absorption. Kết quả dùng để confirm hoặc reject kèo trước khi tạo, và đóng góp vào quality score.

**Chỉ áp dụng cho coin có thanh khoản đủ sâu** — altcoin nhỏ DOM dễ bị manipulate.

### Config mới — thêm vào `config/settings.py`
```python
DOM_ENABLED: bool = True
DOM_COINS: list = ["BTC", "ETH", "SOL", "BNB", "ARB", "OP"]  # chỉ major + mid-cap liquid
DOM_WALL_MIN_USD: float = 1_000_000      # wall phải >= $1M mới tính
DOM_WALL_DISTANCE_MAX_PCT: float = 1.5   # wall trong 1.5% từ giá hiện tại
DOM_BID_ASK_BULLISH: float = 1.5         # ratio > 1.5 = bullish pressure
DOM_BID_ASK_BEARISH: float = 0.67        # ratio < 0.67 = bearish pressure
DOM_ABSORPTION_PCT_THRESHOLD: float = 25 # wall bị eat >= 25% trong 5 phút = absorption
DOM_BOOK_DEPTH_LEVELS: int = 20          # số level lấy từ L2
```

### Dataclass
```python
# src/detector/dom_analyzer.py

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

@dataclass
class WallInfo:
    side: str           # "BID" | "ASK"
    price: float
    size_usd: float
    distance_pct: float  # % cách giá hiện tại

@dataclass
class AbsorptionInfo:
    side: str            # "BID" bị sell vào | "ASK" bị buy vào
    price: float
    absorbed_usd: float
    absorbed_pct: float  # % wall đã bị eat
    window_seconds: int  # trong bao nhiêu giây

@dataclass
class DOMSnapshot:
    coin: str
    timestamp: datetime
    mid_price: float
    bid_ask_ratio: float          # total_bid_usd / total_ask_usd trong DOM_BOOK_DEPTH_LEVELS
    total_bid_usd: float
    total_ask_usd: float
    wall: Optional[WallInfo]      # wall đáng chú ý nhất (nếu có)
    absorption: Optional[AbsorptionInfo]  # absorption đang xảy ra (nếu có)
    signal: str                   # "BULLISH" | "BEARISH" | "NEUTRAL"
    signal_strength: int          # 0-3
```

### Class DOMAnalyzer
```python
class DOMAnalyzer:
    def __init__(self):
        self._snapshots: dict[str, DOMSnapshot] = {}       # coin → latest snapshot
        self._book_history: dict[str, list] = {}           # coin → list of (timestamp, bids, asks)
        self._callbacks: list = []

    def on(self, event: str, callback):
        """event: 'dom_signal'"""
        self._callbacks.append((event, callback))

    async def process_l2_update(self, coin: str, bids: list, asks: list, mid_price: float):
        """
        Gọi mỗi khi nhận L2 update từ Hyperliquid WS.
        bids/asks: list of [price, size] đã convert sang USD.
        """
        if coin not in config.DOM_COINS:
            return

        # Lưu history để tính absorption
        now = datetime.utcnow()
        if coin not in self._book_history:
            self._book_history[coin] = []
        self._book_history[coin].append((now, bids, asks))
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
                    await cb(snapshot)

    def _analyze(self, coin, bids, asks, mid_price) -> DOMSnapshot:
        # 1. Tính bid/ask ratio
        total_bid = sum(p * s for p, s in bids[:config.DOM_BOOK_DEPTH_LEVELS])
        total_ask = sum(p * s for p, s in asks[:config.DOM_BOOK_DEPTH_LEVELS])
        ratio = total_bid / total_ask if total_ask > 0 else 1.0

        # 2. Tìm wall
        wall = self._find_wall(bids, asks, mid_price)

        # 3. Tính absorption
        absorption = self._calc_absorption(coin, bids, asks, mid_price)

        # 4. Tổng hợp signal
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

    def _find_wall(self, bids, asks, mid_price) -> Optional[WallInfo]:
        """
        Tìm level có size_usd >= DOM_WALL_MIN_USD và cách giá <= DOM_WALL_DISTANCE_MAX_PCT.
        Ưu tiên wall gần giá nhất.
        """
        candidates = []

        for price, size in bids:
            size_usd = price * size
            dist_pct = abs(mid_price - price) / mid_price * 100
            if size_usd >= config.DOM_WALL_MIN_USD and dist_pct <= config.DOM_WALL_DISTANCE_MAX_PCT:
                candidates.append(WallInfo("BID", price, size_usd, dist_pct))

        for price, size in asks:
            size_usd = price * size
            dist_pct = abs(price - mid_price) / mid_price * 100
            if size_usd >= config.DOM_WALL_MIN_USD and dist_pct <= config.DOM_WALL_DISTANCE_MAX_PCT:
                candidates.append(WallInfo("ASK", price, size_usd, dist_pct))

        if not candidates:
            return None
        # Trả về wall gần giá nhất
        return min(candidates, key=lambda w: w.distance_pct)

    def _calc_absorption(self, coin, bids, asks, mid_price) -> Optional[AbsorptionInfo]:
        """
        So sánh book hiện tại với book 5 phút trước.
        Nếu một wall đã bị eat >= DOM_ABSORPTION_PCT_THRESHOLD → absorption.
        """
        history = self._book_history.get(coin, [])
        if len(history) < 2:
            return None

        old_ts, old_bids, old_asks = history[0]  # snapshot cũ nhất trong 5 phút

        # Kiểm tra ask wall bị absorbed (buyer đang eat nguồn cung)
        for (op, os), (np, ns) in zip(old_asks[:10], asks[:10]):
            if abs(op - np) < op * 0.001:  # cùng price level
                old_usd = op * os
                new_usd = np * ns
                if old_usd >= config.DOM_WALL_MIN_USD:
                    absorbed = old_usd - new_usd
                    pct = absorbed / old_usd * 100
                    if pct >= config.DOM_ABSORPTION_PCT_THRESHOLD:
                        return AbsorptionInfo("ASK", op, absorbed, pct, int((datetime.utcnow() - old_ts).seconds))

        # Kiểm tra bid wall bị absorbed (seller đang eat demand)
        for (op, os), (np, ns) in zip(old_bids[:10], bids[:10]):
            if abs(op - np) < op * 0.001:
                old_usd = op * os
                new_usd = np * ns
                if old_usd >= config.DOM_WALL_MIN_USD:
                    absorbed = old_usd - new_usd
                    pct = absorbed / old_usd * 100
                    if pct >= config.DOM_ABSORPTION_PCT_THRESHOLD:
                        return AbsorptionInfo("BID", op, absorbed, pct, int((datetime.utcnow() - old_ts).seconds))

        return None

    def _calc_signal(self, ratio, wall, absorption) -> tuple[str, int]:
        strength = 0
        direction_votes = {"BULLISH": 0, "BEARISH": 0}

        # Bid/ask ratio
        if ratio >= config.DOM_BID_ASK_BULLISH:
            direction_votes["BULLISH"] += 1
            strength += 1
        elif ratio <= config.DOM_BID_ASK_BEARISH:
            direction_votes["BEARISH"] += 1
            strength += 1

        # Wall
        if wall:
            if wall.side == "BID":
                direction_votes["BULLISH"] += 1
                strength += 1
            else:
                direction_votes["BEARISH"] += 1
                strength += 1

        # Absorption
        if absorption:
            if absorption.side == "ASK":  # ask đang bị eat = bullish
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
```

### Subscribe L2 trong `hyperliquid_ws.py`
```python
# Trong _subscribe() thêm:
for coin in config.DOM_COINS:
    await self._ws.send(json.dumps({
        "method": "subscribe",
        "subscription": {"type": "l2Book", "coin": coin}
    }))

# Trong _handle_message() thêm:
if msg_type == "l2Book":
    coin = data["coin"]
    bids = [[float(l["px"]), float(l["sz"])] for l in data["levels"][0]]
    asks = [[float(l["px"]), float(l["sz"])] for l in data["levels"][1]]
    mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else 0
    await self._dom_analyzer.process_l2_update(coin, bids, asks, mid)
```

### Tích hợp vào `signal_tracker.py`

Thêm `dom_analyzer` singleton, inject vào `SignalTracker.__init__`.

Trong `maybe_create_auto_signal()`:
```python
# Sau khi pass trend check, trước khi create signal:
if config.DOM_ENABLED and coin in config.DOM_COINS:
    dom = dom_analyzer.get_snapshot(coin)
    if dom:
        # DOM confirm ngược chiều → skip
        if signal_direction == "LONG" and dom.signal == "BEARISH" and dom.signal_strength >= 2:
            logger.info(f"DOM bearish (strength={dom.signal_strength}), skip LONG {coin}")
            return
        if signal_direction == "SHORT" and dom.signal == "BULLISH" and dom.signal_strength >= 2:
            logger.info(f"DOM bullish (strength={dom.signal_strength}), skip SHORT {coin}")
            return
```

### Tích hợp vào quality score (`_calc_signal_quality`)
```python
# Thêm tham số dom: Optional[DOMSnapshot]
# DOM confirmation: max 15 điểm
if dom and dom.coin in config.DOM_COINS:
    if dom.signal == signal_direction_as_dom and dom.signal_strength >= 2:
        score += 15
    elif dom.signal == signal_direction_as_dom and dom.signal_strength == 1:
        score += 7
    elif dom.signal != "NEUTRAL" and dom.signal != signal_direction_as_dom:
        score -= 10  # DOM phản bác → trừ điểm
```

### Hiển thị trong message kèo
Thêm block DOM vào message nếu có signal:
```
📖 Order Book: Bullish (strength 2/3)
   • Bid/Ask ratio: 1.72 📈
   • Bid wall $2.1M tại 66,850 (0.5% dưới giá)
   • Ask wall đang bị eat: 31% trong 4 phút
```

---

## TASK 9 — Ecosystem Call (Volume Spike → Gọi Coin Cùng Hệ)

**File mới:** `src/detector/ecosystem_detector.py`
**File phụ:** `src/signals/signal_tracker.py`, `src/detector/whale_detector.py`

### Mô tả
Khi một coin có volume spike bất thường, tự động scan các coin cùng ecosystem. Nếu coin phụ đang có trend + điều kiện bổ sung → tạo alert và có thể tạo kèo.

### Ecosystem Map — thêm vào `config/settings.py`
```python
ECOSYSTEM_ENABLED: bool = True
ECOSYSTEM_VOLUME_SPIKE_MIN: float = 2.5   # volume >= 2.5x MA20 mới trigger
ECOSYSTEM_CALL_MIN_TREND_SCORE: int = 2   # coin phụ phải có trend score >= này
ECOSYSTEM_SIGNAL_QUALITY_PENALTY: int = 15  # trừ vào quality score vì là secondary signal

ECOSYSTEM_MAP: dict = {
    # Solana ecosystem
    "SOL":    ["JUP", "RAY", "BONK", "JTO", "PYTH", "WIF", "DRIFT", "JITO"],

    # ETH & L2
    "ETH":    ["ARB", "OP", "MATIC", "LDO", "RPL", "SSV"],
    "ARB":    ["GMX", "PENDLE", "RDNT", "GRAIL"],
    "OP":     ["VELO", "SNX", "PERP"],

    # AI / DePIN
    "FET":    ["AGIX", "TAO", "RENDER", "OCEAN", "NMR"],
    "TAO":    ["FET", "AGIX", "RENDER"],
    "RENDER": ["FET", "AGIX", "TAO"],

    # BNB ecosystem
    "BNB":    ["CAKE", "TWT", "BAKE", "XVS", "ALPACA"],

    # DeFi blue chips
    "AAVE":   ["UNI", "CRV", "MKR", "COMP", "BAL"],
    "UNI":    ["AAVE", "CRV", "SUSHI"],

    # Gaming / Metaverse
    "AXS":    ["SAND", "MANA", "GALA", "IMX", "MAGIC"],
    "IMX":    ["AXS", "GODS", "GUILD"],

    # Cosmos ecosystem
    "ATOM":   ["OSMO", "INJ", "TIA", "DYDX", "EVMOS"],
    "INJ":    ["ATOM", "OSMO", "TIA"],

    # RWA / Stablecoin infra
    "MKR":    ["AAVE", "COMP", "FRAX"],
}
```

### Dataclass
```python
# src/detector/ecosystem_detector.py

@dataclass
class EcosystemSignal:
    trigger_coin: str        # coin gây ra spike
    trigger_volume_ratio: float
    target_coin: str         # coin phụ được scan
    target_trend: str        # "LONG" | "SHORT" | "NEUTRAL"
    target_trend_score: int
    has_whale_confirm: bool  # có whale buy/sell target_coin không
    has_dom_confirm: bool    # DOM confirm (nếu enabled)
    strength: str            # "WATCH" | "ALERT" | "SIGNAL"
    reason: list[str]
```

### Class EcosystemDetector
```python
class EcosystemDetector:
    def __init__(self, signal_tracker, dom_analyzer=None):
        self._signal_tracker = signal_tracker
        self._dom_analyzer = dom_analyzer
        self._recent_volume_spikes: dict[str, tuple[float, datetime]] = {}
        # coin → (volume_ratio, timestamp)

    async def on_volume_spike(self, coin: str, volume_ratio: float, direction: str):
        """
        Gọi từ TrendDetector khi phát hiện volume spike trong klines poll.
        direction: hướng của nến spike ("LONG" nếu nến xanh, "SHORT" nếu nến đỏ)
        """
        if not config.ECOSYSTEM_ENABLED:
            return
        if volume_ratio < config.ECOSYSTEM_VOLUME_SPIKE_MIN:
            return
        if coin not in config.ECOSYSTEM_MAP:
            return

        self._recent_volume_spikes[coin] = (volume_ratio, datetime.utcnow())
        related_coins = config.ECOSYSTEM_MAP[coin]

        for target in related_coins:
            await self._evaluate_target(coin, volume_ratio, direction, target)

    async def _evaluate_target(self, trigger: str, vol_ratio: float, direction: str, target: str):
        trend = get_multi_trend(target)

        # Trend phải cùng chiều với trigger coin
        if trend[0] != direction:
            return
        if trend[1] < config.ECOSYSTEM_CALL_MIN_TREND_SCORE:
            return

        reasons = [f"{trigger} volume spike {vol_ratio:.1f}x"]

        # Check whale confirm trên target coin (trong 30 phút gần nhất)
        has_whale = self._check_recent_whale(target, direction)
        if has_whale:
            reasons.append(f"whale {direction.lower()} confirmed")

        # Check DOM
        has_dom = False
        if self._dom_analyzer and target in config.DOM_COINS:
            dom = self._dom_analyzer.get_snapshot(target)
            if dom and dom.signal == ("BULLISH" if direction == "LONG" else "BEARISH"):
                has_dom = True
                reasons.append(f"DOM {dom.signal.lower()} (strength {dom.signal_strength})")

        # Phân loại strength
        if has_whale and has_dom:
            strength = "SIGNAL"   # đủ điều kiện tạo kèo
        elif has_whale or has_dom:
            strength = "ALERT"    # alert nhưng chưa tạo kèo
        else:
            strength = "WATCH"    # chỉ notify đơn giản

        eco_signal = EcosystemSignal(
            trigger_coin=trigger,
            trigger_volume_ratio=vol_ratio,
            target_coin=target,
            target_trend=direction,
            target_trend_score=trend[1],
            has_whale_confirm=has_whale,
            has_dom_confirm=has_dom,
            strength=strength,
            reason=reasons,
        )

        await self._dispatch(eco_signal)

    async def _dispatch(self, eco: EcosystemSignal):
        if eco.strength == "WATCH":
            await self._send_watch_alert(eco)

        elif eco.strength == "ALERT":
            await self._send_alert(eco)

        elif eco.strength == "SIGNAL":
            # Tạo kèo với quality penalty
            await self._send_alert(eco)
            await self._signal_tracker.maybe_create_ecosystem_signal(eco)

    def _check_recent_whale(self, coin: str, direction: str) -> bool:
        """Check _recent_whale_events từ signal_tracker — inject hoặc dùng shared buffer."""
        side = "BUY" if direction == "LONG" else "SELL"
        events = getattr(self._signal_tracker, "_recent_whale_events", [])
        return any(e.coin == coin and e.side == side for e in events)
```

### Method mới trong `signal_tracker.py`
```python
async def maybe_create_ecosystem_signal(self, eco: EcosystemSignal):
    """
    Tương tự maybe_create_auto_signal() nhưng:
    - source = "ECOSYSTEM"
    - quality score bị trừ ECOSYSTEM_SIGNAL_QUALITY_PENALTY
    - Vẫn cần pass dedup check và trend check
    """
    coin = eco.target_coin
    direction = eco.target_trend

    # Dedup check
    if await db.has_active_signal(coin):
        return

    trend = get_trend(coin)  # lấy 4h trend để tính ATR
    if trend is None:
        return

    # Tính TP/SL từ ATR như bình thường
    entry, tp1, tp2, tp3, sl = self._calc_tp_sl(trend, direction)

    # Quality score với penalty
    quality = self._calc_signal_quality(...) - config.ECOSYSTEM_SIGNAL_QUALITY_PENALTY
    if quality < config.SIGNAL_MIN_QUALITY_SCORE:
        logger.info(f"Ecosystem signal {coin} quality too low ({quality}), skip")
        return

    await self._create_and_post(
        coin=coin,
        direction=direction,
        entry=entry, tp1=tp1, tp2=tp2, tp3=tp3, sl=sl,
        source="ECOSYSTEM",
        source_detail=f"trigger:{eco.trigger_coin}",
        quality_score=quality,
    )
```

### Messages Telegram

**WATCH — chỉ thông báo đơn giản:**
```
👀 ECOSYSTEM WATCH

SOL volume spike 3.2x
→ Các coin cần theo dõi: JUP, RAY, BONK
   Trend đang: LONG (score 2/3)
```

**ALERT — có thêm điều kiện:**
```
🔔 ECOSYSTEM ALERT — JUP LONG

📊 Trigger: SOL volume spike 3.2x
✅ JUP trend LONG (4h score 3/3)
✅ DOM bullish (strength 2/3)
⚠️ Chưa có whale confirm trực tiếp

→ Đang theo dõi, chưa tạo kèo tự động
```

**SIGNAL — tạo kèo:**
```
🎯 ECOSYSTEM SIGNAL — JUP LONG  [⭐ 52/100]

📊 Trigger: SOL volume spike 3.2x
✅ JUP trend LONG (4h score 3/3)
✅ Whale buy $450K trên JUP
✅ DOM bullish (bid/ask 1.6)

⚡ Secondary signal — quality score đã điều chỉnh

📍 Entry: $0.842
🎯 TP1: $0.871 | TP2: $0.901 | TP3: $0.930
🛑 SL: $0.813
```

### Trigger volume spike trong `trend_detector.py`

Thêm detection khi poll klines:
```python
async def _check_volume_spike(self, coin: str, klines: list):
    """
    So sánh volume nến hiện tại với MA20 volume.
    Gọi ecosystem_detector.on_volume_spike() nếu đủ ngưỡng.
    """
    if len(klines) < 21:
        return

    volumes = [float(k[5]) for k in klines]  # index 5 = volume
    current_vol = volumes[-1]
    ma20_vol = sum(volumes[-21:-1]) / 20

    if ma20_vol == 0:
        return

    ratio = current_vol / ma20_vol

    if ratio >= config.ECOSYSTEM_VOLUME_SPIKE_MIN:
        # Xác định direction từ nến hiện tại
        open_price = float(klines[-1][1])
        close_price = float(klines[-1][4])
        direction = "LONG" if close_price > open_price else "SHORT"

        logger.info(f"Volume spike {coin}: {ratio:.1f}x MA20, direction={direction}")
        await ecosystem_detector.on_volume_spike(coin, ratio, direction)
```

### Schema DB — thêm cột source_detail
```sql
ALTER TABLE signals ADD COLUMN source_detail TEXT;
-- e.g. "trigger:SOL" cho ecosystem signal
```

---

## Thứ tự implement (khuyến nghị)

| # | Task | Lý do ưu tiên |
|---|------|---------------|
| 1 | **Task 3** — Auto-cancel PENDING | Đơn giản nhất, fix vấn đề thực tế ngay |
| 2 | **Task 1** — TP1 move SL | Logic rõ ràng, impact cao |
| 3 | **Task 2** — Cắt kèo khi đảo chiều | Phụ thuộc Task 1 hoàn chỉnh |
| 4 | **Task 4** — Weighted confluence | Thay đổi nhỏ, hiệu quả cao |
| 5 | **Task 7** — Daily loss limit | Risk management, code ít |
| 6 | **Task 5** — Multi-timeframe | Thay đổi lớn, làm sau khi ổn định |
| 7 | **Task 6** — Quality score | Cần multi-timeframe trước |
| 8 | **Task 9** — Ecosystem call | Cần volume spike detection từ Task 5 |
| 9 | **Task 8** — DOM analysis | Subscribe thêm WS feed, làm cuối cùng |

---

## Notes cho Claude Code

- Tất cả DB migration dùng `try/except` wrap `ALTER TABLE` như pattern hiện tại trong `database.py`
- Giữ nguyên `get_trend(coin)` cho backward compat, thêm `get_multi_trend(coin)` mới
- `_recent_whale_events` cần cleanup định kỳ — xoá events cũ hơn 30 phút trong `_check_signals()`
- `EcosystemDetector` và `DOMAnalyzer` là singleton, khởi tạo trong `main.py` cùng với các component khác
- DOM L2 feed sẽ tăng lượng message WS đáng kể — log ở level DEBUG, không INFO
- Ecosystem map có thể cần cập nhật theo thị trường — đặt trong config để dễ chỉnh
- Test thủ công: dùng `/signal` tạo kèo rồi quan sát log; trigger ecosystem bằng cách mock volume spike
- Không có test framework — dùng `logger.debug` nhiều để trace
