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

## Thứ tự implement (khuyến nghị)

| # | Task | Lý do ưu tiên |
|---|------|---------------|
| 1 | **Task 3** — Auto-cancel PENDING | Đơn giản nhất, fix vấn đề thực tế ngay |
| 2 | **Task 1** — TP1 move SL | Logic rõ ràng, impact cao |
| 3 | **Task 2** — Cắt kèo khi đảo chiều | Phụ thuộc Task 1 hoàn chỉnh |
| 4 | **Task 4** — Weighted confluence | Thay đổi nhỏ, hiệu quả cao |
| 5 | **Task 7** — Daily loss limit | Risk management, code ít |
| 6 | **Task 5** — Multi-timeframe | Thay đổi lớn nhất, làm cuối |
| 7 | **Task 6** — Quality score | Làm sau khi có multi-timeframe |

---

## Notes cho Claude Code

- Tất cả DB migration dùng `try/except` wrap `ALTER TABLE` như pattern hiện tại trong `database.py`
- Giữ nguyên `get_trend(coin)` cho backward compat, thêm `get_multi_trend(coin)` mới
- `_recent_whale_events` cần cleanup định kỳ — xoá events cũ hơn 30 phút trong `_check_signals()`
- Test thủ công bằng cách tạo kèo qua `/signal` rồi quan sát log
- Không có test framework — dùng `logger.debug` nhiều để trace
