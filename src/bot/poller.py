import asyncio
import time
from datetime import datetime, timezone
from loguru import logger
from src.api.hyperliquid_rest import rest
from src.storage.database import db
from src.detector.alert_engine import AlertEngine

# PnL notification thresholds (USD) — notify when PnL crosses these from either side
PNL_MILESTONES = [5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000]
# Minimum PnL change to re-notify (avoid spam)
PNL_MIN_CHANGE = 5_000


class PositionPoller:
    """
    Every N seconds:
    1. Fetches open positions for all watchlist addresses → large position / flip alerts
    2. Checks PnL for auto-watched addresses (added when BIG_TRADE fires)
    """

    MIN_WIN_RATE = 60.0   # % tối thiểu để promote
    MIN_TRADES   = 3      # số trade tối thiểu để xét

    def __init__(self, engine: AlertEngine, interval: int = 60):
        self.engine = engine
        self.interval = interval
        self._running = False
        self._last_promote_check: float = 0.0

    async def start(self):
        self._running = True
        logger.info("Position poller started")
        while self._running:
            try:
                await self._poll_all()
                await self._poll_pnl()
                await db.clean_auto_watch()
                await self._promote_whales()
            except Exception as e:
                logger.error(f"Poller error: {e}")
            await asyncio.sleep(self.interval)

    async def _poll_all(self):
        watched = await db.get_all_watched_addresses()
        if not watched:
            return

        logger.debug(f"Polling {len(watched)} watched addresses…")
        for address, chat_ids in watched.items():
            try:
                state = await rest.get_user_state(address)
                if not state:
                    continue

                watchlist = await db.get_watchlist(chat_ids[0])
                label = next(
                    (w["label"] for w in watchlist if w["address"] == address), ""
                )
                await self.engine.process_position_snapshot(address, state, label)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Poll error for {address}: {e}")

    async def _poll_pnl(self):
        entries = await db.get_active_auto_watch()
        if not entries:
            return

        logger.debug(f"Checking PnL for {len(entries)} auto-watched trades…")

        # Group by address to avoid duplicate REST calls
        by_address: dict[str, list[dict]] = {}
        for entry in entries:
            by_address.setdefault(entry["address"], []).append(entry)

        for address, coins in by_address.items():
            try:
                state = await rest.get_user_state(address)
                if not state:
                    continue

                positions = {
                    p["position"]["coin"]: p["position"]
                    for p in state.get("assetPositions", [])
                }

                for entry in coins:
                    coin = entry["coin"]
                    pos = positions.get(coin)

                    if not pos or abs(float(pos.get("szi", 0))) < 0.001:
                        await self._notify_closed(address, entry)
                        await db.remove_auto_watch(address, coin)
                        continue

                    unrealized_pnl = float(pos.get("unrealizedPnl") or 0)
                    szi = float(pos.get("szi", 0))
                    pos_value = float(pos.get("positionValue") or 0)
                    current_px = (pos_value / abs(szi)) if szi and pos_value else float(pos.get("entryPx") or entry["entry_px"])
                    liq_px = float(pos.get("liquidationPx") or 0) or None
                    leverage_info = pos.get("leverage", {})
                    leverage = leverage_info.get("value") if isinstance(leverage_info, dict) else None

                    if self._should_notify_pnl(entry["last_pnl"], unrealized_pnl):
                        elapsed = self._elapsed_minutes(entry["created_at"])
                        await self.engine.send_pnl_update(
                            address=address,
                            coin=coin,
                            direction=entry["direction"],
                            entry_px=entry["entry_px"],
                            current_px=current_px,
                            pnl=unrealized_pnl,
                            size_usd=entry["size_usd"],
                            elapsed_min=elapsed,
                            liq_px=liq_px,
                            leverage=leverage,
                        )
                        await db.update_auto_watch_pnl(address, coin, unrealized_pnl)

                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"PnL poll error for {address}: {e}")

    def _should_notify_pnl(self, last_pnl: float, current_pnl: float) -> bool:
        """Return True if PnL crossed a milestone or changed significantly."""
        if abs(current_pnl - last_pnl) < PNL_MIN_CHANGE:
            return False
        for milestone in PNL_MILESTONES:
            crossed = (last_pnl < milestone <= current_pnl) or (current_pnl <= -milestone < last_pnl)
            if crossed:
                return True
            crossed_neg = (last_pnl > -milestone >= current_pnl) or (current_pnl >= milestone > last_pnl)
            if crossed_neg:
                return True
        return False

    async def _notify_closed(self, address: str, entry: dict):
        msg_map = await db.get_auto_watch_msgs(address, entry["coin"])
        if not msg_map:
            return
        addr_short = f"{address[:6]}…{address[-4:]}"
        elapsed = self._elapsed_minutes(entry["created_at"])
        last_pnl = entry["last_pnl"]
        pnl_sign = "+" if last_pnl >= 0 else ""
        pnl_emoji = "✅" if last_pnl >= 0 else "❌"
        dir_emoji = "🟢" if entry["direction"] == "LONG" else "🔴"

        if abs(last_pnl) >= 1_000_000:
            pnl_fmt = f"${last_pnl/1_000_000:.2f}M"
        elif abs(last_pnl) >= 1_000:
            pnl_fmt = f"${last_pnl/1_000:.1f}K"
        else:
            pnl_fmt = f"${last_pnl:.0f}"

        text = "\n".join([
            f"{pnl_emoji} <b>ĐÓNG LỆNH</b>",
            "",
            f"<b>{entry['coin']}</b>  {dir_emoji} <b>{entry['direction']}</b>",
            f"PnL cuối: <b>{pnl_sign}{pnl_fmt}</b>  ⏱ {elapsed} phút",
            "",
            f"👤 <code>{addr_short}</code>",
            "",
            f"🔗 <a href='https://app.hyperliquid.xyz/explorer/{address}'>Xem trên Hyperliquid</a>",
        ])

        for chat_id, origin_msg_id in msg_map.items():
            try:
                await self.engine.bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode="HTML", disable_web_page_preview=True,
                    reply_to_message_id=origin_msg_id,
                )
            except Exception:
                try:
                    await self.engine.bot.send_message(
                        chat_id=chat_id, text=text,
                        parse_mode="HTML", disable_web_page_preview=True,
                    )
                except Exception as e:
                    logger.error(f"Closed notify error to {chat_id}: {e}")

        await db.delete_auto_watch_msgs(address, entry["coin"])
        logger.info(f"Position closed notify: {entry['coin']} {entry['direction']} {pnl_sign}{pnl_fmt}")

        # Cập nhật whale score
        await db.update_whale_score(address, last_pnl, profitable=(last_pnl > 0))

    def _elapsed_minutes(self, created_at: str) -> int:
        try:
            created = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return int((now - created).total_seconds() / 60)
        except Exception:
            return 0

    async def _promote_whales(self):
        """Mỗi 10 phút: xét promote whale có win rate cao vào known_whales."""
        if time.monotonic() - self._last_promote_check < 600:
            return
        self._last_promote_check = time.monotonic()

        candidates = await db.get_promotable_whales(
            min_trades=self.MIN_TRADES,
            min_win_rate=self.MIN_WIN_RATE,
        )
        if not candidates:
            return

        from src.api.hyperliquid_ws import ws as hl_ws
        for w in candidates:
            await db.promote_to_known_whale(
                address=w["address"],
                win_rate=w["win_rate"],
                total_pnl=w["total_pnl"],
                trade_count=w["total_trades"],
            )
            await hl_ws.add_user_subscription(w["address"])
            logger.info(
                f"Promoted whale {w['address'][:10]}… "
                f"WR={w['win_rate']:.0f}% trades={w['total_trades']} pnl=${w['total_pnl']:,.0f}"
            )

    def stop(self):
        self._running = False
