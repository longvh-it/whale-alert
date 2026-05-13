import json
from loguru import logger
from aiogram import Bot

from src.detector.whale_detector import WhaleAlert, WhaleDetector, AlertType
from src.storage.database import db
from config.settings import config


_ALWAYS_PASS = {"oi_spike", "funding_extreme", "confluence"}


def _passes_threshold(alert: "WhaleAlert", min_trade: float, min_liq: float) -> bool:
    from src.detector.whale_detector import AlertType
    if alert.alert_type.value in _ALWAYS_PASS:
        return True
    if alert.alert_type == AlertType.LIQUIDATION:
        return alert.size_usd >= min_liq
    # BIG_TRADE, LARGE_POSITION, POSITION_FLIP, PNL_MILESTONE, WATCHLIST_TRADE
    return alert.size_usd >= min_trade


def _is_valid_address(addr: str) -> bool:
    return isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42


def _fmt_usd_local(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.2f}"


class AlertEngine:
    """
    Sits between WhaleDetector and Telegram.
    - Deduplicates alerts (cooldown window)
    - Routes to correct subscribers
    - Handles global broadcast vs watchlist-only alerts
    - Auto-watches BIG_TRADE addresses for PnL tracking
    """

    def __init__(self, bot: Bot, detector: WhaleDetector):
        self.bot = bot
        self.detector = detector

    async def send_global_alert(self, alert: "WhaleAlert"):
        """Broadcast an alert to all active users (used by OI/Funding/Confluence detectors)."""
        await self._route_alert(alert)

    async def process_binance_trade(self, payload: dict):
        alerts = self.detector.on_binance_trade(payload)
        for alert in alerts:
            await self._route_alert(alert)

    async def process_bybit_trade(self, payload: dict):
        alerts = self.detector.on_bybit_trade(payload)
        for alert in alerts:
            await self._route_alert(alert)

    async def process_binance_liquidation(self, payload: dict):
        alerts = self.detector.on_binance_liquidation(payload)
        for alert in alerts:
            await self._route_alert(alert)

    async def process_bybit_liquidation(self, payload: dict):
        alerts = self.detector.on_bybit_liquidation(payload)
        for alert in alerts:
            await self._route_alert(alert)

    async def process_trade_data(self, data):
        alerts = self.detector.process_trades(data)
        for alert in alerts:
            await self._route_alert(alert)

    async def process_user_event(self, data: dict, watched_map: dict[str, list[int]]):
        """watched_map = {address: [chat_id, ...]}"""
        watched_set = set(watched_map.keys())
        alerts = self.detector.process_user_event(data, watched_set)

        for alert in alerts:
            subscribers = watched_map.get(alert.address.lower(), [])
            await self._route_alert(alert, explicit_subscribers=subscribers)

    async def process_position_snapshot(self, address: str, state: dict, label: str = ""):
        alerts = self.detector.process_position_snapshot(address, state, label)
        watched_map = await db.get_all_watched_addresses()
        subscribers = watched_map.get(address.lower(), [])

        for alert in alerts:
            await self._route_alert(alert, explicit_subscribers=subscribers)

    async def send_pnl_update(self, address: str, coin: str, direction: str,
                               entry_px: float, current_px: float, pnl: float,
                               size_usd: float, elapsed_min: int,
                               liq_px: float | None = None, leverage: int | None = None):
        """Send a PnL update, replying to the original BIG_TRADE alert message."""
        # Only send to users who received the original BIG_TRADE alert
        msg_map = await db.get_auto_watch_msgs(address, coin)
        if not msg_map:
            return

        dir_emoji = "🟢" if direction == "LONG" else "🔴"
        pnl_sign = "+" if pnl >= 0 else ""
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        addr_short = f"{address[:6]}…{address[-4:]}"

        if abs(pnl) >= 1_000_000:
            pnl_fmt = f"${pnl/1_000_000:.2f}M"
        elif abs(pnl) >= 1_000:
            pnl_fmt = f"${pnl/1_000:.1f}K"
        else:
            pnl_fmt = f"${pnl:.0f}"

        pnl_pct = (pnl / size_usd * 100) if size_usd else 0
        lev_str = f"  ⚡<b>{leverage}x</b>" if leverage else ""

        lines = [
            f"💰 <b>PNL UPDATE</b>",
            "",
            f"<b>{coin}</b>  {dir_emoji} <b>{direction}</b>{lev_str}",
            f"💵 <b>{_fmt_usd_local(size_usd)}</b>",
            f"📌 ${entry_px:,.2f} → <b>${current_px:,.2f}</b>",
            f"{pnl_emoji} PnL: <b>{pnl_sign}{pnl_fmt}</b>  ({pnl_sign}{pnl_pct:.1f}%)",
        ]
        if liq_px:
            lines.append(f"⚠️ Liq: ${liq_px:,.2f}")
        lines += [
            f"⏱ {elapsed_min} phút trước",
            "",
            f"👤 <code>{addr_short}</code>",
            "",
            f"🔗 <a href='https://app.hyperliquid.xyz/explorer/{address}'>Xem trên Hyperliquid</a>",
        ]
        text = "\n".join(lines)

        for chat_id, origin_msg_id in msg_map.items():
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_to_message_id=origin_msg_id,
                )
            except Exception as e:
                # Original message may have been deleted — send without reply
                try:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception as e2:
                    logger.error(f"PnL update send error to {chat_id}: {e2}")

        logger.info(f"PnL update sent: {coin} {direction} {pnl_sign}{pnl_fmt} ({elapsed_min}m)")

    async def _route_alert(
        self, alert: WhaleAlert, explicit_subscribers: list[int] | None = None
    ):
        if explicit_subscribers is not None:
            # Watchlist alerts: bypass global threshold filter, use per-user threshold
            users_with_thresholds = await db.get_active_users_with_thresholds()
            threshold_map = {u["chat_id"]: u for u in users_with_thresholds}
            targets_raw = explicit_subscribers
        else:
            users_with_thresholds = await db.get_active_users_with_thresholds()
            threshold_map = {u["chat_id"]: u for u in users_with_thresholds}
            targets_raw = [u["chat_id"] for u in users_with_thresholds]

        if not targets_raw:
            return

        message = alert.format_message()
        sent_any = False

        for chat_id in targets_raw:
            # Per-user threshold filter
            user = threshold_map.get(chat_id)
            if user and not _passes_threshold(alert, user["min_trade"], user["min_liq"]):
                continue
            # Confluence alerts respect user opt-out
            if alert.alert_type.value == "confluence" and user and not user.get("confluence_enabled", True):
                continue

            already = await db.was_alerted_recently(
                chat_id, alert.alert_type, alert.address
            )
            if already:
                continue

            try:
                sent = await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                await db.log_alert(
                    chat_id, alert.alert_type, alert.address, json.dumps({
                        "coin": alert.coin,
                        "size": alert.size_usd,
                        "direction": alert.direction,
                    })
                )
                if alert.alert_type == AlertType.BIG_TRADE:
                    await db.save_auto_watch_msg(alert.address, alert.coin, chat_id, sent.message_id)
                logger.info(f"Alert sent → {chat_id}: {alert.alert_type} {alert.coin} ${alert.size_usd:,.0f}")
                sent_any = True
            except Exception as e:
                logger.error(f"Failed to send alert to {chat_id}: {e}")

        if alert.alert_type == AlertType.BIG_TRADE and sent_any and _is_valid_address(alert.address):
            await db.add_auto_watch(
                alert.address, alert.coin, alert.price, alert.direction, alert.size_usd
            )
            # Auto-signal nếu được bật
            try:
                from src.signals.signal_tracker import get_tracker
                tracker = get_tracker()
                await tracker.maybe_create_auto_signal(
                    coin=alert.coin,
                    direction=alert.direction,
                    entry=alert.price,
                    size_usd=alert.size_usd,
                    leverage=alert.extra.get("leverage"),
                    whale_address=alert.address,
                )
                # Buffer event for reversal detection (Task 1)
                try:
                    tracker.add_whale_event(alert)
                except Exception:
                    pass
            except RuntimeError:
                pass  # SignalTracker chưa khởi tạo (test mode)
