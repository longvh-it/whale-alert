import asyncio
import time
from datetime import datetime
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


class SignalTracker:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._prices: dict[str, float] = {}
        self._cache: list[dict] = []
        self._cache_ts: float = 0.0
        self._channel_id: str = _normalize_channel_id(config.signal_channel_id)

    # ── REST price poll loop (thay cho allMids WS) ─────────
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
    ) -> int:
        sig_id = await db.create_signal(
            coin, direction, entry, tp1, tp2, tp3, sl,
            leverage, source, whale_address, note, order_type,
        )
        signal = await db.get_signal(sig_id)
        await self._post_to_channel(signal)
        # Invalidate cache so new signal is picked up immediately
        self._cache_ts = 0.0
        return sig_id

    async def cancel_signal(self, sig_id: int) -> bool:
        signal = await db.get_signal(sig_id)
        if not signal or signal["status"] in ("SL_HIT", "TP3_HIT", "CANCELLED"):
            return False
        await db.update_signal_status(sig_id, "CANCELLED", close=True)
        signal["status"] = "CANCELLED"
        await self._edit_signal_message(signal)
        self._cache_ts = 0.0
        return True

    # ── TP/SL checking ─────────────────────────────────────
    async def _check_signals(self):
        to_remove = []
        for signal in self._cache:
            price = self._prices.get(signal["coin"])
            if price is None:
                continue
            new_status = self._evaluate(signal, price)
            if new_status:
                terminal = self._is_terminal(signal, new_status)
                await db.update_signal_status(signal["id"], new_status, close=terminal)
                signal["status"] = new_status
                if terminal:
                    to_remove.append(signal)
                await self._edit_signal_message(signal)
                await self._send_hit_notification(signal, new_status, price)
                logger.info(
                    f"Signal #{signal['id']} {signal['coin']} → {new_status} @ ${price:,.2f}"
                )
        for s in to_remove:
            self._cache.remove(s)
        # Refresh cache after status changes so PENDING→ACTIVE continues being tracked
        if any(s["status"] == "ACTIVE" for s in self._cache):
            self._cache_ts = 0.0

    @staticmethod
    def _evaluate(signal: dict, price: float) -> Optional[str]:
        d = signal["direction"]
        sl = signal["sl_price"]
        entry = signal["entry_price"]
        tp1 = signal.get("tp1")
        tp2 = signal.get("tp2")
        tp3 = signal.get("tp3")
        status = signal["status"]

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
    def _is_terminal(signal: dict, new_status: str) -> bool:
        if new_status == "ACTIVE":
            return False  # Limit order just activated
        if new_status in ("SL_HIT", "TP3_HIT", "CANCELLED"):
            return True
        if new_status == "TP2_HIT" and not signal.get("tp3"):
            return True
        if new_status == "TP1_HIT" and not signal.get("tp2") and not signal.get("tp3"):
            return True
        return False

    # ── Telegram messaging ─────────────────────────────────
    async def _post_to_channel(self, signal: dict):
        if not self._channel_id:
            return
        try:
            msg = await self.bot.send_message(
                chat_id=self._channel_id,
                text=self.format_signal(signal),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await db.save_signal_msg(signal["id"], msg.message_id)
            signal["channel_msg_id"] = msg.message_id
            logger.info(f"Signal #{signal['id']} posted to channel (msg {msg.message_id})")
        except Exception as e:
            logger.error(f"Lỗi post signal #{signal['id']} lên channel: {e}")

    async def _edit_signal_message(self, signal: dict):
        if not self._channel_id or not signal.get("channel_msg_id"):
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self._channel_id,
                message_id=signal["channel_msg_id"],
                text=self.format_signal(signal),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"Lỗi edit signal #{signal['id']}: {e}")

    async def _send_hit_notification(self, signal: dict, new_status: str, price: float):
        if not self._channel_id:
            return

        d = signal["direction"]
        coin = signal["coin"]
        sig_id = signal["id"]
        entry = signal["entry_price"]
        dir_emoji = "🟢" if d == "LONG" else "🔴"

        pct = abs(price - entry) / entry * 100
        sign = "+" if (
            (d == "LONG" and price > entry) or (d == "SHORT" and price < entry)
        ) else "-"

        if new_status == "ACTIVE":
            header = f"📍 <b>VÀO LỆNH</b>"
            body = f"{dir_emoji} {d} {coin}  <code>#{sig_id:04d}</code>\nLimit entry đã chạm: <b>${price:,.2f}</b>\n⚡ TP/SL tracking bắt đầu"
        elif new_status == "SL_HIT":
            header = f"❌ <b>SL BỊ HIT</b>"
            body = f"{dir_emoji} {d} {coin}  <code>#{sig_id:04d}</code>\n📍 ${entry:,.2f} → <b>${price:,.2f}</b>\n💸 -{pct:.1f}%"
        elif new_status.startswith("TP"):
            tp_num = new_status[2]
            tp_val = signal.get(f"tp{tp_num}", price)
            header = f"✅ <b>TP{tp_num} ĐÃ CHẠM</b>"
            body = f"{dir_emoji} {d} {coin}  <code>#{sig_id:04d}</code>\n📍 ${entry:,.2f} → <b>${tp_val:,.2f}</b>\n💰 +{pct:.1f}%"
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
    def format_signal(signal: dict) -> str:
        status = signal["status"]
        d = signal["direction"]
        coin = signal["coin"]
        sig_id = signal["id"]
        entry = signal["entry_price"]
        sl = signal["sl_price"]
        tp1 = signal.get("tp1")
        tp2 = signal.get("tp2")
        tp3 = signal.get("tp3")
        lev = signal.get("leverage")
        source = signal.get("source", "ADMIN")
        whale = signal.get("whale_address") or ""
        note = signal.get("note") or ""
        created = signal.get("created_at", "")

        order_type = signal.get("order_type", "MARKET")
        dir_emoji = "🟢" if d == "LONG" else "🔴"
        lev_str = f"  ⚡<b>{lev}x</b>" if lev else ""
        order_badge = "  <i>[LIMIT]</i>" if order_type == "LIMIT" else ""
        tp_reached = _TP_LEVEL.get(status, 0)

        def pct(price: float) -> str:
            diff = (price - entry) / entry * 100
            if d == "SHORT":
                diff = -diff
            return f"+{diff:.1f}%" if diff >= 0 else f"{diff:.1f}%"

        def tp_emoji(num: int) -> str:
            return "✅" if tp_reached >= num else "🎯"

        sl_emoji = "❌" if status == "SL_HIT" else "🛑"

        lines = [
            f"{dir_emoji} <b>{d} {coin}</b>{lev_str}  <code>#{sig_id:04d}</code>{order_badge}",
            SEP,
            f"📍 Entry: <b>${entry:,.2f}</b>",
            SEP,
        ]

        for val, num in [(tp1, 1), (tp2, 2), (tp3, 3)]:
            if val is None:
                continue
            lines.append(f"{tp_emoji(num)} TP{num}: <b>${val:,.2f}</b>  <i>({pct(val)})</i>")

        lines.append(SEP)
        lines.append(f"{sl_emoji} SL: <b>${sl:,.2f}</b>  <i>({pct(sl)})</i>")
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
        else:
            lines.append(f"🔎 Nguồn: 👨‍💼 Admin")

        if note:
            lines.append(f"📝 {note}")

        # Timestamp
        try:
            dt = datetime.fromisoformat(str(created))
            lines.append(f"⏰ {dt.strftime('%d/%m/%Y %H:%M')} UTC")
        except Exception:
            pass

        lines.append("")
        lines.append(f"📊 {STATUS_LABEL.get(status, status)}")

        return "\n".join(lines)

    # ── Auto-signal từ whale ───────────────────────────────
    async def maybe_create_auto_signal(
        self, coin: str, direction: str, entry: float, size_usd: float,
        leverage: Optional[int], whale_address: str,
    ):
        if not config.auto_signal_enabled:
            return
        min_usd = config.auto_signal_min_usd if coin.upper() in config.auto_signal_major_coins else config.auto_signal_min_usd_alt
        if size_usd < min_usd:
            return
        if await db.has_active_signal(coin):
            logger.debug(f"Auto-signal skipped: đã có kèo {coin} đang active")
            return

        # Kiểm tra trend — whale phải cùng chiều trend mới tạo kèo
        from src.detector.trend_detector import get_trend
        trend = get_trend(coin)
        if trend is None:
            logger.debug(f"Auto-signal skipped: chưa có trend data cho {coin}")
            return
        if trend.direction != direction:
            logger.debug(
                f"Auto-signal skipped: {coin} whale={direction} nhưng trend={trend.direction} "
                f"(score={trend.score}/3)"
            )
            return
        if trend.score < config.trend_min_score:
            logger.debug(
                f"Auto-signal skipped: {coin} trend score {trend.score} < {config.trend_min_score}"
            )
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

        note = f"Trend {trend.score}/3 | RSI {trend.rsi:.0f} | ATR {atr:.2f}"

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
        )
        logger.info(
            f"Auto-signal #{sig_id}: {direction} {coin} ${size_usd:,.0f} "
            f"entry={entry} SL={sl:.2f} TP3={tp3:.2f} trend={trend.score}/3"
        )
