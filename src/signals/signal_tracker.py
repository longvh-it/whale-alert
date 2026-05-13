import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger
from aiogram import Bot

from src.storage.database import db
from src.api.hyperliquid_rest import rest
from config.settings import config


def _normalize_channel_id(raw: str) -> str:
    """Đảm bảo channel ID Telegram có đúng định dạng (chuỗi âm với -100 prefix)."""
    if not raw:
        return raw
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        n = int(raw)
        if n > 0:
            return f"-100{n}"
    return raw


# ── Singleton ──────────────────────────────────────────────
_instance: "SignalTracker | None" = None


def get_tracker() -> "SignalTracker":
    if _instance is None:
        raise RuntimeError("SignalTracker chưa được khởi tạo")
    return _instance


def set_tracker(t: "SignalTracker"):
    global _instance
    _instance = t


# ── Status helpers ─────────────────────────────────────────
_TP_LEVEL = {"TP1_HIT": 1, "TP2_HIT": 2, "TP3_HIT": 3}

STATUS_LABEL = {
    "PENDING":   "⏳ Chờ vào lệnh",
    "ACTIVE":    "🟡 Đang theo dõi",
    "TP1_HIT":   "🟢 TP1 đã chạm ✅",
    "TP2_HIT":   "🟢 TP2 đã chạm ✅✅",
    "TP3_HIT":   "🏆 Tất cả TP đã chạm ✅",
    "SL_HIT":    "🔴 SL bị hit ❌",
    "CANCELLED": "⚪ Đã hủy",
}

SEP = "━" * 18

# Reason code → display text
_REASON_DISPLAY = {
    "ema_weak":       "EMA20 yếu so với EMA50",
    "rsi_weak":       "RSI quay đầu",
    "macd_shrink":    "MACD histogram thu hẹp",
    "whale_opposite": "Whale ngược chiều xuất hiện",
    "ema_cross_bearish": "EMA20 cắt xuống EMA50",
    "ema_cross_bullish": "EMA20 cắt lên EMA50",
    "rsi_breakdown":  "RSI phá xuống 45",
    "rsi_breakout":   "RSI vượt 55",
    "macd_cross_bearish": "MACD line cắt xuống signal",
    "macd_cross_bullish": "MACD line cắt lên signal",
    "trend_flipped":  "Xu hướng đã đảo chiều",
}


class SignalTracker:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._prices: dict[str, float] = {}
        self._cache: list[dict] = []
        self._cache_ts: float = 0.0
        self._channel_id: str = _normalize_channel_id(config.signal_channel_id)

        # Task 2 — reversal cut
        self._reversal_check_counter: int = 0
        # Task 3 — stale PENDING cancel
        self._pending_cancel_counter: int = 0
        # Task 1 — recent whale events buffer for reversal detection
        self._recent_whale_events: list[dict] = []
        # Task 7 — daily loss limit notification tracking
        self._daily_loss_notified_date: str = ""

    # ── REST price poll loop ───────────────────────────────
    async def start_price_poll(self, interval: int = 5):
        """Chạy song song với bot — poll giá qua REST mỗi N giây."""
        logger.info(f"SignalTracker price poll started (every {interval}s)")
        while True:
            try:
                mids = await rest.get_all_mids()
                if isinstance(mids, dict):
                    for coin, px in mids.items():
                        try:
                            self._prices[coin] = float(px)
                        except (ValueError, TypeError):
                            pass
                    if time.monotonic() - self._cache_ts > 30:
                        self._cache = await db.get_active_signals()
                        self._cache_ts = time.monotonic()
                    if self._cache:
                        await self._check_signals()
            except Exception as e:
                logger.warning(f"SignalTracker price poll error: {e}")
            await asyncio.sleep(interval)

    def get_current_price(self, coin: str) -> Optional[float]:
        return self._prices.get(coin)

    # ── Public event buffer (Task 1) ───────────────────────
    def add_whale_event(self, alert):
        """Buffer BIG_TRADE events for reversal detection (called from alert_engine)."""
        from src.detector.whale_detector import AlertType
        if alert.alert_type != AlertType.BIG_TRADE:
            return
        side = "BUY" if alert.direction == "LONG" else "SELL"
        self._recent_whale_events.append({
            "coin": alert.coin,
            "side": side,
            "size_usd": alert.size_usd,
            "timestamp": alert.timestamp,
        })

    # ── Signal lifecycle ───────────────────────────────────
    async def create_and_post(
        self,
        coin: str,
        direction: str,
        entry: float,
        tp1: Optional[float],
        tp2: Optional[float],
        tp3: Optional[float],
        sl: float,
        leverage: Optional[int] = None,
        source: str = "ADMIN",
        whale_address: Optional[str] = None,
        note: Optional[str] = None,
        order_type: str = "MARKET",
        quality_score: int = 0,
        source_detail: Optional[str] = None,
    ) -> int:
        sig_id = await db.create_signal(
            coin, direction, entry, tp1, tp2, tp3, sl,
            leverage, source, whale_address, note, order_type,
            quality_score=quality_score, source_detail=source_detail,
        )
        keo = await db.get_signal(sig_id)
        await self._post_to_channel(keo)
        # Invalidate cache so new signal is picked up immediately
        self._cache_ts = 0.0
        return sig_id

    async def cancel_signal(self, sig_id: int) -> bool:
        keo = await db.get_signal(sig_id)
        if not keo or keo["status"] in ("SL_HIT", "TP3_HIT", "CANCELLED"):
            return False
        await db.update_signal_status(sig_id, "CANCELLED", close=True)
        keo["status"] = "CANCELLED"
        await self._edit_signal_message(keo)
        self._cache_ts = 0.0
        return True

    # ── TP/SL checking ─────────────────────────────────────
    async def _check_signals(self):
        # Task 1 — clean up old whale events (> 30 min)
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        self._recent_whale_events = [
            e for e in self._recent_whale_events
            if e.get("timestamp") and e["timestamp"] > cutoff
        ]

        to_remove = []
        for keo in self._cache:
            price = self._prices.get(keo["coin"])
            if price is None:
                continue
            new_status = self._evaluate(keo, price)
            if new_status:
                terminal = self._is_terminal(keo, new_status)
                await db.update_signal_status(keo["id"], new_status, close=terminal)
                keo["status"] = new_status
                if terminal:
                    to_remove.append(keo)
                await self._edit_signal_message(keo)

                # Task 1 — TP1 special handling
                if new_status == "TP1_HIT":
                    await self._on_tp1_hit_async(keo, price)
                else:
                    await self._send_hit_notification(keo, new_status, price)

                logger.info(
                    f"Signal #{keo['id']} {keo['coin']} → {new_status} @ ${price:,.2f}"
                )

        for s in to_remove:
            if s in self._cache:
                self._cache.remove(s)

        # Refresh cache after status changes so PENDING→ACTIVE continues being tracked
        if any(s["status"] == "ACTIVE" for s in self._cache):
            self._cache_ts = 0.0

        # Task 2 — reversal cut (every REVERSAL_CHECK_INTERVAL cycles)
        self._reversal_check_counter += 1
        if self._reversal_check_counter >= config.reversal_check_interval:
            self._reversal_check_counter = 0
            await self._check_trend_reversals()
            self._cache = [s for s in self._cache if s.get("status") != "CANCELLED"]

        # Task 3 — stale PENDING cancel (every 60 cycles ≈ 5 min at 5s interval)
        self._pending_cancel_counter += 1
        if self._pending_cancel_counter >= 60:
            self._pending_cancel_counter = 0
            await self._cancel_stale_pending_signals()

    @staticmethod
    def _evaluate(keo: dict, price: float) -> Optional[str]:
        d = keo["direction"]
        sl = keo["sl_price"]
        entry = keo["entry_price"]
        tp1 = keo.get("tp1")
        tp2 = keo.get("tp2")
        tp3 = keo.get("tp3")
        status = keo["status"]

        # Limit order waiting for entry
        if status == "PENDING":
            sl_hit = (price <= sl) if d == "LONG" else (price >= sl)
            if sl_hit:
                return "SL_HIT"
            entry_hit = (price <= entry) if d == "LONG" else (price >= entry)
            if entry_hit:
                return "ACTIVE"
            return None

        cur = _TP_LEVEL.get(status, 0)
        sl_hit = (price <= sl) if d == "LONG" else (price >= sl)
        if sl_hit:
            return "SL_HIT"

        def above(tp):
            return tp and ((price >= tp) if d == "LONG" else (price <= tp))

        if above(tp3) and cur < 3:
            return "TP3_HIT"
        if above(tp2) and cur < 2:
            return "TP2_HIT"
        if above(tp1) and cur < 1:
            return "TP1_HIT"
        return None

    @staticmethod
    def _is_terminal(keo: dict, new_status: str) -> bool:
        if new_status == "ACTIVE":
            return False  # Limit order just activated
        if new_status in ("SL_HIT", "TP3_HIT", "CANCELLED"):
            return True
        if new_status == "TP2_HIT" and not keo.get("tp3"):
            return True
        if new_status == "TP1_HIT" and not keo.get("tp2") and not keo.get("tp3"):
            return True
        return False

    # ── Task 1: TP1 hit → reversal check & SL move ─────────
    def _count_reversal_signals_soft(
        self,
        keo: dict,
        trend,
        recent_whale_events: list[dict],
    ) -> tuple[int, list[str]]:
        """Soft threshold (for SL move at TP1), returns (score, reason_codes)."""
        d = keo["direction"]
        coin = keo["coin"]
        reasons = []

        # 1. EMA weak
        ema20 = trend.ema20
        ema50 = trend.ema50
        if d == "LONG" and ema20 < ema50 * 1.002:
            reasons.append("ema_weak")
        elif d == "SHORT" and ema20 > ema50 * 0.998:
            reasons.append("ema_weak")

        # 2. RSI weak
        rsi = trend.rsi
        if d == "LONG" and rsi < 52:
            reasons.append("rsi_weak")
        elif d == "SHORT" and rsi > 48:
            reasons.append("rsi_weak")

        # 3. MACD shrink
        hist = trend.macd_hist
        hist_prev = trend.macd_hist_prev
        if hist_prev != 0 and abs(hist) / abs(hist_prev) < 0.5:
            reasons.append("macd_shrink")

        # 4. Whale opposite
        opposite_side = "SELL" if d == "LONG" else "BUY"
        opposite_whales = [
            e for e in recent_whale_events
            if e["coin"] == coin and e["side"] == opposite_side
        ]
        if opposite_whales:
            reasons.append("whale_opposite")

        return len(reasons), reasons

    async def _on_tp1_hit_async(self, keo: dict, current_price: float):
        """Called when TP1 is hit — decide whether to move SL to entry."""
        from src.detector.trend_detector import get_trend
        entry = keo["entry_price"]
        tp1 = keo.get("tp1", current_price)
        d = keo["direction"]
        coin = keo["coin"]

        if not config.tp1_reversal_move_sl_enabled:
            await self._send_hit_notification(keo, "TP1_HIT", current_price)
            return

        trend = get_trend(coin)
        if trend is None:
            await self._send_hit_notification(keo, "TP1_HIT", current_price)
            return

        score, reasons = self._count_reversal_signals_soft(keo, trend, self._recent_whale_events)

        if score >= config.tp1_reversal_min_score:
            # Move SL to entry (breakeven)
            await db.update_signal_sl(keo["id"], entry, moved_to_entry=True, reason=",".join(reasons))
            keo["sl_price"] = entry
            keo["sl_moved_to_entry"] = 1
            keo["sl_move_reason"] = ",".join(reasons)
            await self._edit_signal_message(keo)
            await self._send_tp1_sl_moved(keo, score, reasons, current_price)
        elif score >= config.tp1_reversal_warn_score:
            await self._send_tp1_warning(keo, score, reasons, current_price)
        else:
            await self._send_tp1_strong(keo, current_price)

    async def _send_tp1_strong(self, keo: dict, current_price: float):
        """TP1 hit, no reversal signs — hold for TP2/TP3."""
        if not self._channel_id:
            return
        d = keo["direction"]
        coin = keo["coin"]
        entry = keo["entry_price"]
        tp1 = keo.get("tp1", current_price)
        tp2 = keo.get("tp2")
        tp3 = keo.get("tp3")
        sl = keo["sl_price"]
        pct = abs(tp1 - entry) / entry * 100
        text_lines = [
            f"✅ <b>TP1 HIT — {coin} {d}</b>",
            "",
            f"📍 Entry: ${entry:,.2f}",
            f"🎯 TP1: ${tp1:,.2f} (+{pct:.1f}%) ✅",
        ]
        if tp2:
            text_lines.append(f"🎯 TP2: ${tp2:,.2f}")
        if tp3:
            text_lines.append(f"🎯 TP3: ${tp3:,.2f}")
        text_lines += [
            f"🛑 SL: ${sl:,.2f} (giữ nguyên)",
            "",
            "📊 Trend vẫn mạnh, tiếp tục hold TP2/TP3",
        ]
        try:
            await self.bot.send_message(
                chat_id=self._channel_id,
                text="\n".join(text_lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"_send_tp1_strong error: {e}")

    async def _send_tp1_warning(self, keo: dict, score: int, reasons: list[str], current_price: float):
        """TP1 hit, 1 weak sign — warn but don't move SL."""
        if not self._channel_id:
            return
        d = keo["direction"]
        coin = keo["coin"]
        entry = keo["entry_price"]
        tp1 = keo.get("tp1", current_price)
        sl = keo["sl_price"]
        pct = abs(tp1 - entry) / entry * 100
        reason_display = ", ".join(_REASON_DISPLAY.get(r, r) for r in reasons)
        text = (
            f"✅ <b>TP1 HIT — {coin} {d}</b>\n\n"
            f"📍 Entry: ${entry:,.2f}\n"
            f"🎯 TP1: ${tp1:,.2f} (+{pct:.1f}%) ✅\n"
            f"🛑 SL: ${sl:,.2f} (chưa thay đổi)\n\n"
            f"⚠️ Có {score} dấu hiệu yếu ({reason_display})\n"
            f"→ Đang theo dõi, chưa cần hành động"
        )
        try:
            await self.bot.send_message(
                chat_id=self._channel_id, text=text,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"_send_tp1_warning error: {e}")

    async def _send_tp1_sl_moved(self, keo: dict, score: int, reasons: list[str], current_price: float):
        """TP1 hit, ≥2 weak signs — SL moved to entry (breakeven)."""
        if not self._channel_id:
            return
        d = keo["direction"]
        coin = keo["coin"]
        entry = keo["entry_price"]
        tp1 = keo.get("tp1", current_price)
        tp2 = keo.get("tp2")
        tp3 = keo.get("tp3")
        pct = abs(tp1 - entry) / entry * 100
        reason_lines = "\n".join(
            f"  {_REASON_DISPLAY.get(r, r)} ✗" for r in reasons
        )
        text_lines = [
            f"✅ <b>TP1 HIT — {coin} {d}</b>",
            "",
            f"📍 Entry: ${entry:,.2f}",
            f"🎯 TP1: ${tp1:,.2f} (+{pct:.1f}%) ✅",
        ]
        if tp2:
            text_lines.append(f"🎯 TP2: ${tp2:,.2f}")
        text_lines += [
            f"🛑 SL: ${entry:,.2f} ← dời về entry (breakeven)",
            "",
            f"🛡 Lý do bảo vệ vốn:\n{reason_lines}",
            "→ Worst case: hoà vốn. Vẫn target TP2/TP3 nếu thị trường hồi",
        ]
        try:
            await self.bot.send_message(
                chat_id=self._channel_id,
                text="\n".join(text_lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"_send_tp1_sl_moved error: {e}")

    # ── Task 2: Trend reversal cut ─────────────────────────
    def _count_reversal_signals(
        self,
        keo: dict,
        trend,
        recent_whale_events: list[dict],
    ) -> tuple[int, list[str]]:
        """Hard threshold for cutting signal. Returns (score, reason_codes)."""
        d = keo["direction"]
        coin = keo["coin"]
        reasons = []

        ema20 = trend.ema20
        ema50 = trend.ema50

        # 1. Full EMA cross
        if d == "LONG" and ema20 < ema50:
            reasons.append("ema_cross_bearish")
        elif d == "SHORT" and ema20 > ema50:
            reasons.append("ema_cross_bullish")

        # 2. RSI breakdown / breakout
        rsi = trend.rsi
        if d == "LONG" and rsi < 45:
            reasons.append(f"rsi_breakdown:{rsi:.0f}")
        elif d == "SHORT" and rsi > 55:
            reasons.append(f"rsi_breakout:{rsi:.0f}")

        # 3. MACD cross
        macd_line = trend.macd_line
        macd_sig = trend.macd_signal_line
        if d == "LONG" and macd_line < macd_sig:
            reasons.append("macd_cross_bearish")
        elif d == "SHORT" and macd_line > macd_sig:
            reasons.append("macd_cross_bullish")

        # 4. Opposite whale trades
        opposite_side = "SELL" if d == "LONG" else "BUY"
        opposite_whales = [
            e for e in recent_whale_events
            if e["coin"] == coin and e["side"] == opposite_side
        ]
        if opposite_whales:
            total_opp = sum(e["size_usd"] for e in opposite_whales)
            reasons.append(f"whale_opposite:{total_opp/1_000_000:.1f}M")

        # 5. Trend fully flipped
        if trend.direction != d and trend.direction != "NEUTRAL":
            reasons.append("trend_flipped")

        return len(reasons), reasons

    async def _check_trend_reversals(self):
        """Check all ACTIVE / TP1_HIT / TP2_HIT signals for trend reversal."""
        if not config.reversal_cut_enabled:
            return
        from src.detector.trend_detector import get_trend

        active_keoes = [
            s for s in self._cache
            if s.get("status") in ("ACTIVE", "TP1_HIT", "TP2_HIT")
        ]
        if not active_keoes:
            return

        for keo in active_keoes:
            coin = keo["coin"]
            trend = get_trend(coin)
            if trend is None:
                continue

            price = self._prices.get(coin)
            if price is None:
                continue

            # Grace period
            try:
                created_at = datetime.fromisoformat(str(keo["created_at"]))
                age_min = (datetime.utcnow() - created_at).total_seconds() / 60
            except Exception:
                age_min = 0

            if age_min < config.reversal_grace_minutes:
                continue

            score, reasons = self._count_reversal_signals(keo, trend, self._recent_whale_events)

            # Determine minimum score: altcoins need higher confidence
            is_major = coin.upper() in config.auto_signal_major_coins
            min_score = config.reversal_min_score if is_major else config.reversal_alt_min_score

            if score >= min_score:
                await self._close_signal_early(keo, price, score, reasons)

    async def _close_signal_early(
        self, keo: dict, current_price: float, score: int, reasons: list[str]
    ):
        """Close a signal early due to trend reversal."""
        await db.close_signal_early(
            keo["id"], close_price=current_price, reason="trend_reversal", reversal_score=score
        )
        keo["status"] = "CANCELLED"
        await self._send_reversal_cut_notification(keo, current_price, score, reasons)
        await self._edit_signal_message(keo)

    async def _send_reversal_cut_notification(
        self, keo: dict, current_price: float, score: int, reasons: list[str]
    ):
        """Send reversal cut notification to channel."""
        if not self._channel_id:
            return

        d = keo["direction"]
        coin = keo["coin"]
        entry = keo["entry_price"]
        orig_sl = keo["sl_price"]

        # pnl_pct: direction-adjusted
        raw_diff = (current_price - entry) / entry * 100
        pnl_pct = raw_diff if d == "LONG" else -raw_diff

        # Duration
        try:
            created_at = datetime.fromisoformat(str(keo["created_at"]))
            elapsed = datetime.utcnow() - created_at
            h = int(elapsed.total_seconds() // 3600)
            m = int((elapsed.total_seconds() % 3600) // 60)
            duration_str = f"{h}h {m}m"
        except Exception:
            duration_str = "N/A"

        if pnl_pct < 0:
            result_str = f"Cắt lỗ {abs(pnl_pct):.1f}%"
        elif pnl_pct == 0:
            result_str = "Hoà vốn"
        else:
            result_str = f"Giữ lãi {pnl_pct:.1f}%"

        saved_pct = abs(current_price - orig_sl) / entry * 100
        sl_pct_raw = (orig_sl - entry) / entry * 100
        sl_pct = sl_pct_raw if d == "LONG" else -sl_pct_raw

        # Format reasons (strip embedded data)
        reason_lines = []
        for r in reasons:
            base = r.split(":")[0]
            suffix = ""
            if ":" in r:
                suffix = f" ({r.split(':',1)[1]})"
            reason_lines.append(f"  • {_REASON_DISPLAY.get(base, base)}{suffix}")

        text = (
            f"⚠️ <b>KÈO CẮT SỚM — {coin} {d}</b>\n\n"
            f"📍 Entry: ${entry:,.2f}\n"
            f"📉 Giá lúc cắt: ${current_price:,.2f} ({pnl_pct:+.1f}%)\n"
            f"⏱ Thời gian giữ: {duration_str}\n\n"
            f"🔄 Dấu hiệu đảo chiều ({score} điểm):\n"
            + "\n".join(reason_lines) + "\n\n"
            f"📊 {result_str}\n"
            f"→ SL gốc tại ${orig_sl:,.2f} ({sl_pct:+.1f}%) — tránh được {saved_pct:.1f}%"
        )
        try:
            await self.bot.send_message(
                chat_id=self._channel_id, text=text,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"_send_reversal_cut_notification error: {e}")

    # ── Task 3: Auto-cancel stale PENDING ─────────────────
    async def _cancel_stale_pending_signals(self):
        """Cancel PENDING signals that have exceeded the timeout."""
        pending_signals = await db.get_signals_by_status(["PENDING"])
        now = datetime.utcnow()
        for keo in pending_signals:
            try:
                created_at = datetime.fromisoformat(str(keo["created_at"]))
            except Exception:
                continue
            age_hours = (now - created_at).total_seconds() / 3600
            if age_hours >= config.signal_pending_timeout_hours:
                await db.close_signal_early(
                    keo["id"], close_price=None, reason="timeout", reversal_score=0
                )
                keo["status"] = "CANCELLED"
                current_price = self._prices.get(keo["coin"])
                await self._send_timeout_notification(keo, age_hours, current_price)
                await self._edit_signal_message(keo)
        self._cache = [s for s in self._cache if s["status"] != "CANCELLED"]
        self._cache_ts = 0.0

    async def _send_timeout_notification(
        self, keo: dict, age_hours: float, current_price: Optional[float]
    ):
        """Notify channel that a PENDING signal was auto-cancelled due to timeout."""
        if not self._channel_id:
            return
        d = keo["direction"]
        coin = keo["coin"]
        entry = keo["entry_price"]

        if current_price is not None:
            raw_diff = (current_price - entry) / entry * 100
            diff_pct = raw_diff if d == "LONG" else -raw_diff
            price_line = f"\n→ Giá hiện tại: ${current_price:,.2f} (cách entry {diff_pct:+.1f}%)"
        else:
            price_line = ""

        text = (
            f"⏰ <b>KÈO HẾT HẠN — {coin} {d}</b>\n\n"
            f"Kèo LIMIT chờ {age_hours:.1f}h chưa được fill.\n"
            f"📍 Entry limit: <b>${entry:,.2f}</b>\n"
            f"💀 Tự động huỷ.{price_line}"
        )
        try:
            await self.bot.send_message(
                chat_id=self._channel_id, text=text,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"_send_timeout_notification error: {e}")

    # ── Task 7: Daily loss limit ───────────────────────────
    async def _check_daily_loss_limit(self) -> bool:
        """Returns True if daily SL limit is reached (auto-signal should be skipped)."""
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        sl_count = await db.count_sl_hits_since(today_start)
        if sl_count >= config.daily_sl_limit:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            if self._daily_loss_notified_date != today:
                self._daily_loss_notified_date = today
                await self._notify_daily_loss_limit(sl_count)
            return True
        return False

    async def _notify_daily_loss_limit(self, sl_count: int):
        if not config.admin_chat_id:
            return
        text = (
            f"🚫 <b>AUTO-SIGNAL TẠM DỪNG</b>\n\n"
            f"Đã có {sl_count} kèo SL hôm nay.\n"
            f"Auto-signal tắt đến 07:00 sáng (00:00 UTC).\n"
            f"Dùng /signal để tạo kèo thủ công nếu cần."
        )
        try:
            await self.bot.send_message(
                chat_id=config.admin_chat_id, text=text, parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Failed to notify admin daily loss limit: {e}")

    # ── Task 6: Signal quality score ──────────────────────
    def _calc_signal_quality(
        self,
        whale_size_usd: float,
        trend_confirmed: int,
        confluence_score: int,
        volume_ratio: float = 1.0,
        dom_snapshot=None,
        signal_direction: str = "",
    ) -> int:
        score = 0
        score += trend_confirmed * 10             # max 30
        score += min(confluence_score * 5, 40)    # max 40
        if volume_ratio >= 2.0:
            score += 20
        elif volume_ratio >= 1.5:
            score += 10
        elif volume_ratio >= 1.0:
            score += 5
        if whale_size_usd >= 5_000_000:
            score += 10
        elif whale_size_usd >= 2_000_000:
            score += 7
        elif whale_size_usd >= 1_000_000:
            score += 4
        else:
            score += 1

        # Task 8 — DOM confirmation bonus/penalty (max 15 / -10)
        if dom_snapshot and signal_direction:
            expected_dom = "BULLISH" if signal_direction == "LONG" else "BEARISH"
            if dom_snapshot.signal == expected_dom and dom_snapshot.signal_strength >= 2:
                score += 15
            elif dom_snapshot.signal == expected_dom and dom_snapshot.signal_strength == 1:
                score += 7
            elif dom_snapshot.signal not in ("NEUTRAL", expected_dom):
                score -= 10

        return min(max(score, 0), 100)

    # ── Telegram messaging ─────────────────────────────────
    async def _post_to_channel(self, keo: dict):
        if not self._channel_id:
            return
        try:
            msg = await self.bot.send_message(
                chat_id=self._channel_id,
                text=self.format_signal(keo),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await db.save_signal_msg(keo["id"], msg.message_id)
            keo["channel_msg_id"] = msg.message_id
            logger.info(f"Signal #{keo['id']} posted to channel (msg {msg.message_id})")
        except Exception as e:
            logger.error(f"Lỗi post signal #{keo['id']} lên channel: {e}")

    async def _edit_signal_message(self, keo: dict):
        if not self._channel_id or not keo.get("channel_msg_id"):
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self._channel_id,
                message_id=keo["channel_msg_id"],
                text=self.format_signal(keo),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"Lỗi edit signal #{keo['id']}: {e}")

    async def _send_hit_notification(self, keo: dict, new_status: str, price: float):
        if not self._channel_id:
            return

        d = keo["direction"]
        coin = keo["coin"]
        sig_id = keo["id"]
        entry = keo["entry_price"]
        dir_emoji = "🟢" if d == "LONG" else "🔴"

        pct = abs(price - entry) / entry * 100
        sign = "+" if (
            (d == "LONG" and price > entry) or (d == "SHORT" and price < entry)
        ) else "-"

        if new_status == "ACTIVE":
            header = "📍 <b>VÀO LỆNH</b>"
            body = (
                f"{dir_emoji} {d} {coin}  <code>#{sig_id:04d}</code>\n"
                f"Limit entry đã chạm: <b>${price:,.2f}</b>\n"
                f"⚡ TP/SL tracking bắt đầu"
            )
        elif new_status == "SL_HIT":
            header = "❌ <b>SL BỊ HIT</b>"
            body = (
                f"{dir_emoji} {d} {coin}  <code>#{sig_id:04d}</code>\n"
                f"📍 ${entry:,.2f} → <b>${price:,.2f}</b>\n"
                f"💸 -{pct:.1f}%"
            )
        elif new_status.startswith("TP"):
            tp_num = new_status[2]
            tp_val = keo.get(f"tp{tp_num}", price)
            header = f"✅ <b>TP{tp_num} ĐÃ CHẠM</b>"
            body = (
                f"{dir_emoji} {d} {coin}  <code>#{sig_id:04d}</code>\n"
                f"📍 ${entry:,.2f} → <b>${tp_val:,.2f}</b>\n"
                f"💰 +{pct:.1f}%"
            )
        else:
            return

        try:
            await self.bot.send_message(
                chat_id=self._channel_id,
                text=f"{header}\n{body}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"Lỗi gửi hit notification signal #{sig_id}: {e}")

    # ── Format ─────────────────────────────────────────────
    @staticmethod
    def format_signal(keo: dict) -> str:
        status = keo["status"]
        d = keo["direction"]
        coin = keo["coin"]
        sig_id = keo["id"]
        entry = keo["entry_price"]
        sl = keo["sl_price"]
        tp1 = keo.get("tp1")
        tp2 = keo.get("tp2")
        tp3 = keo.get("tp3")
        lev = keo.get("leverage")
        source = keo.get("source", "ADMIN")
        whale = keo.get("whale_address") or ""
        note = keo.get("note") or ""
        created = keo.get("created_at", "")
        sl_moved = keo.get("sl_moved_to_entry", 0)
        quality = keo.get("quality_score", 0)

        order_type = keo.get("order_type", "MARKET")
        dir_emoji = "🟢" if d == "LONG" else "🔴"
        lev_str = f"  ⚡<b>{lev}x</b>" if lev else ""
        order_badge = "  <i>[LIMIT]</i>" if order_type == "LIMIT" else ""
        tp_reached = _TP_LEVEL.get(status, 0)
        quality_str = f"  ⭐<b>{quality}/100</b>" if quality > 0 else ""

        def pct(price: float) -> str:
            diff = (price - entry) / entry * 100
            if d == "SHORT":
                diff = -diff
            return f"+{diff:.1f}%" if diff >= 0 else f"{diff:.1f}%"

        def tp_emoji(num: int) -> str:
            return "✅" if tp_reached >= num else "🎯"

        sl_emoji = "❌" if status == "SL_HIT" else "🛑"

        lines = [
            f"{dir_emoji} <b>{d} {coin}</b>{lev_str}  <code>#{sig_id:04d}</code>{quality_str}{order_badge}",
            SEP,
            f"📍 Entry: <b>${entry:,.2f}</b>",
            SEP,
        ]

        for val, num in [(tp1, 1), (tp2, 2), (tp3, 3)]:
            if val is None:
                continue
            lines.append(f"{tp_emoji(num)} TP{num}: <b>${val:,.2f}</b>  <i>({pct(val)})</i>")

        lines.append(SEP)
        sl_label = f"{sl_emoji} SL: <b>${sl:,.2f}</b>  <i>({pct(sl)})</i>"
        if sl_moved:
            sl_label += " <i>← breakeven</i>"
        lines.append(sl_label)
        lines.append(SEP)

        # R:R ratio
        tp_last = tp3 or tp2 or tp1
        if tp_last and abs(sl - entry) > 0:
            rr = abs(tp_last - entry) / abs(sl - entry)
            lines.append(f"⚖️ R:R ≈ 1:{rr:.1f}")

        # Source
        if source == "WHALE" and whale:
            short = f"{whale[:6]}…{whale[-4:]}"
            lines.append(f"🔎 Nguồn: 🐋 Whale <code>{short}</code>")
        elif source == "ECOSYSTEM":
            source_detail = keo.get("source_detail") or ""
            lines.append(f"🔎 Nguồn: 🌐 Ecosystem  <i>{source_detail}</i>")
        else:
            lines.append(f"🔎 Nguồn: 👨‍💼 Admin")

        if note:
            lines.append(f"📝 {note}")

        # Timestamp
        try:
            dt = datetime.fromisoformat(str(created)) + timedelta(hours=7)
            lines.append(f"⏰ {dt.strftime('%d/%m/%Y %H:%M')} ICT")
        except Exception:
            pass

        lines.append("")
        lines.append(f"📊 {STATUS_LABEL.get(status, status)}")

        return "\n".join(lines)

    # ── Auto-signal từ whale ───────────────────────────────
    async def maybe_create_auto_signal(
        self,
        coin: str,
        direction: str,
        entry: float,
        size_usd: float,
        leverage: Optional[int],
        whale_address: str,
    ):
        if not config.auto_signal_enabled:
            return

        # Task 7 — daily loss limit check
        if config.daily_loss_limit_enabled and await self._check_daily_loss_limit():
            logger.info(f"Daily SL limit reached, skipping auto signal for {coin}")
            return

        min_usd = (
            config.auto_signal_min_usd
            if coin.upper() in config.auto_signal_major_coins
            else config.auto_signal_min_usd_alt
        )
        if size_usd < min_usd:
            return
        if await db.has_active_signal(coin):
            logger.debug(f"Auto-signal skipped: đã có kèo {coin} đang active")
            return

        # Task 5 — multi-timeframe trend confirmation
        from src.detector.trend_detector import get_multi_trend, get_trend
        multi_direction, confirmed = get_multi_trend(coin)
        if multi_direction != direction:
            logger.debug(
                f"Auto-signal skipped: {coin} whale={direction} but multi_trend={multi_direction} ({confirmed}/3)"
            )
            return
        if confirmed < 2:
            logger.debug(
                f"Auto-signal skipped: {coin} multi-trend confirmed={confirmed}/3 < 2"
            )
            return

        # Also get the 4h trend for ATR
        trend = get_trend(coin)
        if trend is None:
            logger.debug(f"Auto-signal skipped: no 4h trend data for {coin}")
            return

        # Tính TP/SL: dùng ATR nếu có, fallback về % cố định
        atr = trend.atr if trend.atr > 0 else entry * (config.auto_signal_sl_pct / 100)
        sl_dist = atr * config.trend_atr_sl_mult
        tp_dist = atr * config.trend_atr_tp_mult

        if direction == "LONG":
            sl  = entry - sl_dist
            tp1 = entry + tp_dist / 3
            tp2 = entry + tp_dist * 2 / 3
            tp3 = entry + tp_dist
        else:
            sl  = entry + sl_dist
            tp1 = entry - tp_dist / 3
            tp2 = entry - tp_dist * 2 / 3
            tp3 = entry - tp_dist

        # Task 8 — DOM check
        dom_snapshot = None
        if config.dom_enabled and coin.upper() in config.dom_coins:
            from src.detector.dom_analyzer import dom_analyzer
            dom_snapshot = dom_analyzer.get_snapshot(coin.upper())
            if dom_snapshot:
                opposite_dom = "BEARISH" if direction == "LONG" else "BULLISH"
                if dom_snapshot.signal == opposite_dom and dom_snapshot.signal_strength >= 2:
                    logger.info(
                        f"Auto-signal skipped: DOM {dom_snapshot.signal} "
                        f"(strength={dom_snapshot.signal_strength}) opposes {direction} {coin}"
                    )
                    return

        note = f"Multi-TF {confirmed}/3 | RSI {trend.rsi:.0f} | ATR {atr:.2f}"

        # Task 6 — quality score (with DOM)
        quality = self._calc_signal_quality(
            whale_size_usd=size_usd,
            trend_confirmed=confirmed,
            confluence_score=0,
            dom_snapshot=dom_snapshot,
            signal_direction=direction,
        )
        if quality < config.signal_min_quality_score:
            logger.info(
                f"Auto-signal skipped: {coin} quality={quality} < {config.signal_min_quality_score}"
            )
            return

        sig_id = await self.create_and_post(
            coin=coin,
            direction=direction,
            entry=entry,
            tp1=round(tp1, 4),
            tp2=round(tp2, 4),
            tp3=round(tp3, 4),
            sl=round(sl, 4),
            leverage=leverage,
            source="WHALE",
            whale_address=whale_address,
            note=note,
            quality_score=quality,
        )
        logger.info(
            f"Auto-signal #{sig_id}: {direction} {coin} ${size_usd:,.0f} "
            f"entry={entry} SL={sl:.2f} TP3={tp3:.2f} trend={confirmed}/3 quality={quality}"
        )

    # ── Ecosystem signal ───────────────────────────────────
    async def maybe_create_ecosystem_signal(self, eco):
        """
        Tạo kèo từ ecosystem signal (secondary signal).
        eco: EcosystemSignal dataclass from ecosystem_detector.
        """
        if not config.auto_signal_enabled:
            return

        coin = eco.target_coin.upper()
        direction = eco.target_trend

        if await db.has_active_signal(coin):
            logger.debug(f"Ecosystem signal skipped: đã có kèo {coin} đang active")
            return

        from src.detector.trend_detector import get_trend
        trend = get_trend(coin)
        if trend is None:
            logger.debug(f"Ecosystem signal skipped: no 4h trend for {coin}")
            return

        atr = trend.atr if trend.atr > 0 else 0
        if atr == 0:
            logger.debug(f"Ecosystem signal skipped: ATR=0 for {coin}")
            return

        current_price = self._prices.get(coin)
        if current_price is None:
            logger.debug(f"Ecosystem signal skipped: no price for {coin}")
            return

        sl_dist = atr * config.trend_atr_sl_mult
        tp_dist = atr * config.trend_atr_tp_mult

        if direction == "LONG":
            sl  = current_price - sl_dist
            tp1 = current_price + tp_dist / 3
            tp2 = current_price + tp_dist * 2 / 3
            tp3 = current_price + tp_dist
        else:
            sl  = current_price + sl_dist
            tp1 = current_price - tp_dist / 3
            tp2 = current_price - tp_dist * 2 / 3
            tp3 = current_price - tp_dist

        # Quality score with ecosystem penalty
        dom_snapshot = None
        if config.dom_enabled and coin in config.dom_coins:
            from src.detector.dom_analyzer import dom_analyzer
            dom_snapshot = dom_analyzer.get_snapshot(coin)

        quality = self._calc_signal_quality(
            whale_size_usd=0,
            trend_confirmed=eco.target_trend_score,
            confluence_score=0,
            dom_snapshot=dom_snapshot,
            signal_direction=direction,
        ) - config.ecosystem_signal_quality_penalty

        if quality < config.signal_min_quality_score:
            logger.info(
                f"Ecosystem signal {coin} quality too low ({quality}), skip"
            )
            return

        note = f"Ecosystem: {eco.trigger_coin} spike {eco.trigger_volume_ratio:.1f}x | {' + '.join(eco.reason)}"

        sig_id = await self.create_and_post(
            coin=coin,
            direction=direction,
            entry=round(current_price, 4),
            tp1=round(tp1, 4),
            tp2=round(tp2, 4),
            tp3=round(tp3, 4),
            sl=round(sl, 4),
            source="ECOSYSTEM",
            source_detail=f"trigger:{eco.trigger_coin}",
            note=note,
            quality_score=max(quality, 0),
        )
        logger.info(
            f"Ecosystem signal #{sig_id}: {direction} {coin} "
            f"trigger={eco.trigger_coin} quality={quality}"
        )
