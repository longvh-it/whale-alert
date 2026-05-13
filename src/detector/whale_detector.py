from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from loguru import logger
from config.settings import config


class AlertType(str, Enum):
    BIG_TRADE = "big_trade"
    LARGE_POSITION = "large_position"
    LIQUIDATION = "liquidation"
    POSITION_FLIP = "position_flip"
    PNL_MILESTONE = "pnl_milestone"
    WATCHLIST_TRADE = "watchlist_trade"
    OI_SPIKE = "oi_spike"
    FUNDING_EXTREME = "funding_extreme"
    CONFLUENCE = "confluence"


@dataclass
class WhaleAlert:
    alert_type: AlertType
    address: str
    coin: str
    size_usd: float
    direction: str          # LONG / SHORT / LIQUIDATED
    price: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    extra: dict = field(default_factory=dict)

    def format_message(self) -> str:
        # ── OI Spike ───────────────────────────────────────
        if self.alert_type == AlertType.OI_SPIKE:
            change = self.extra.get("change_pct", 0.0)
            prev   = self.extra.get("prev_oi", 0.0)
            curr   = self.extra.get("current_oi", 0.0)
            trend  = "📈" if change > 0 else "📉"
            word   = "tăng" if change > 0 else "giảm"
            return "\n".join([
                f"📊 <b>OI SPIKE — {self.coin}</b>",
                "━━━━━━━━━━━━━━━━",
                f"{trend} OI {word} <b>{change:+.1f}%</b>",
                f"💰 ${prev/1e9:.2f}B → <b>${curr/1e9:.2f}B</b>",
                f"🔗 Source: Binance/Coinglass (aggregated)",
                f"🕐 {(self.timestamp + timedelta(hours=7)).strftime('%H:%M:%S')} ICT",
            ])

        # ── Funding Extreme ────────────────────────────────
        if self.alert_type == AlertType.FUNDING_EXTREME:
            avg   = self.extra.get("avg_funding", 0.0)
            rates = self.extra.get("rates", {})
            is_high = avg > 0
            emoji   = "🔴" if is_high else "🟢"
            label   = "cực cao" if is_high else "cực thấp"
            bias    = "over-leveraged LONG" if is_high else "over-leveraged SHORT"
            squeeze = "short squeeze" if is_high else "long squeeze"
            rate_parts = [f"{k.capitalize()}: {v:+.4f}%" for k, v in rates.items() if v != 0.0]
            lines = [
                f"💸 <b>FUNDING EXTREME — {self.coin}</b>",
                "━━━━━━━━━━━━━━━━━━━━━",
                f"{emoji} Funding rate <b>{label}: {avg:+.4f}%</b>",
            ]
            if rate_parts:
                lines.append(f"📊 {'  |  '.join(rate_parts)}")
            lines += [
                f"⚠️ Thị trường đang {bias}",
                f"💡 Lịch sử: thường precede {squeeze}",
                f"🕐 {(self.timestamp + timedelta(hours=7)).strftime('%H:%M:%S')} ICT",
            ]
            return "\n".join(lines)

        # ── Confluence ─────────────────────────────────────
        if self.alert_type == AlertType.CONFLUENCE:
            score     = self.extra.get("score", 0)
            max_score = self.extra.get("max_score", 6)
            sources   = self.extra.get("sources", {})
            dir_emoji = "🟢" if self.direction == "LONG" else "🔴"
            source_labels = [
                ("hyperliquid", "Hyperliquid"),
                ("binance",     "Binance"),
                ("bybit",       "Bybit"),
                ("oi_spike",    "OI Spike"),
                ("funding",     "Funding"),
                ("liquidation", "Liquidation"),
            ]
            lines = [
                f"🎯 <b>CONFLUENCE SIGNAL — {self.coin} {dir_emoji} {self.direction}</b>",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"⚡ Score: <b>{score}/{max_score}</b> nguồn cùng chiều",
            ]
            for key, lbl in source_labels:
                if key in sources:
                    usd = sources[key]
                    suffix = f": <b>{_fmt_usd(usd)}</b>" if usd > 0 else ""
                    lines.append(f"✅ {lbl}{suffix}")
                else:
                    lines.append(f"❌ {lbl}: Không có")
            lines += [
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "⚠️ <i>Không phải lời khuyên đầu tư</i>",
                f"🕐 {(self.timestamp + timedelta(hours=7)).strftime('%H:%M:%S')} ICT",
            ]
            return "\n".join(lines)

        # ── Standard types ─────────────────────────────────
        header_emoji = {
            AlertType.BIG_TRADE:      "🐋",
            AlertType.LARGE_POSITION: "📊",
            AlertType.LIQUIDATION:    "💥",
            AlertType.POSITION_FLIP:  "🔄",
            AlertType.PNL_MILESTONE:  "💰",
            AlertType.WATCHLIST_TRADE:"👁",
        }.get(self.alert_type, "⚡")

        label     = self.extra.get("label", "")
        source    = self.extra.get("source", "hyperliquid")
        dir_emoji = "🟢" if self.direction == "LONG" else ("🔴" if self.direction == "SHORT" else "💀")
        lev       = self.extra.get("leverage")
        lev_str   = f"  ⚡<b>{lev}x</b>" if lev else ""
        type_name = self.alert_type.value.upper().replace("_", " ")
        is_wallet = self.address.startswith("0x") and len(self.address) == 42

        lines = [f"{header_emoji} <b>{type_name}</b>", ""]

        if self.alert_type == AlertType.POSITION_FLIP:
            prev       = self.extra.get("prev_side", "?")
            prev_emoji = "🟢" if prev == "LONG" else "🔴"
            lines.append(f"<b>{self.coin}</b>  {prev_emoji} {prev} → {dir_emoji} <b>{self.direction}</b>{lev_str}")
        else:
            lines.append(f"<b>{self.coin}</b>  {dir_emoji} <b>{self.direction}</b>{lev_str}")

        if self.price > 0:
            lines.append(f"💵 <b>{_fmt_usd(self.size_usd)}</b>  @  ${self.price:,.2f}")
        else:
            lines.append(f"💵 <b>{_fmt_usd(self.size_usd)}</b>")

        if self.extra.get("pnl"):
            pnl       = self.extra["pnl"]
            sign      = "+" if pnl > 0 else ""
            pnl_emoji = "📈" if pnl > 0 else "📉"
            lines.append(f"{pnl_emoji} PnL: <b>{sign}{_fmt_usd(pnl)}</b>")

        if self.extra.get("liq_px") and self.direction != "LIQUIDATED":
            lines.append(f"⚠️ Liq: ${self.extra['liq_px']:,.2f}")

        lines.append("")
        if is_wallet:
            addr_short = f"{self.address[:6]}…{self.address[-4:]}"
            addr_line  = f"👤 <code>{addr_short}</code>"
            if label:
                addr_line += f"  🏷 {label}"
            lines.append(addr_line)
            lines.append(f"🕐 {self.timestamp.strftime('%H:%M:%S')} UTC")
            lines.append("")
            lines.append(f"🔗 <a href='https://app.hyperliquid.xyz/explorer/{self.address}'>Xem trên Hyperliquid</a>")
        else:
            src_display = source.capitalize() if source else "External"
            lines.append(f"🔗 Source: <b>{src_display}</b>")
            lines.append(f"🕐 {self.timestamp.strftime('%H:%M:%S')} UTC")

        return "\n".join(lines)


def _fmt_usd(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.2f}"


class WhaleDetector:
    """
    Processes raw Hyperliquid trade data and emits WhaleAlert objects.
    """

    def __init__(
        self,
        min_trade_usd: float = config.min_trade_size,
        min_position_usd: float = config.min_position_size,
        min_liq_usd: float = config.min_liquidation_size,
    ):
        self.min_trade_usd = min_trade_usd
        self.min_position_usd = min_position_usd
        self.min_liq_usd = min_liq_usd

        # Track last known side per address per coin to detect flips
        self._last_side: dict[str, dict[str, str]] = {}

    # ── Public entry points ────────────────────────────────

    def process_trades(self, data: dict | list) -> list[WhaleAlert]:
        """
        Handle 'trades' channel payload.
        data is typically a list of trade objects.
        """
        alerts: list[WhaleAlert] = []
        trades = data if isinstance(data, list) else [data]

        for t in trades:
            alert = self._check_trade(t)
            if alert:
                alerts.append(alert)
        return alerts

    def process_user_event(self, data: dict, watched_addresses: set[str]) -> list[WhaleAlert]:
        """
        Handle 'userEvents' channel payload for a specific user.
        Detects: liquidations, big fills for watched addresses.
        """
        alerts: list[WhaleAlert] = []

        fills = data.get("fills", [])
        liquidations = data.get("liquidations", [])

        for fill in fills:
            alert = self._check_fill(fill, watched_addresses)
            if alert:
                alerts.append(alert)

        for liq in liquidations:
            alert = self._check_liquidation(liq)
            if alert:
                alerts.append(alert)

        return alerts

    def process_position_snapshot(
        self, address: str, state: dict, label: str = ""
    ) -> list[WhaleAlert]:
        """
        Scan open positions from REST snapshot.
        Detects: large position, position flip.
        """
        alerts: list[WhaleAlert] = []
        positions = state.get("assetPositions", [])

        for pos_wrap in positions:
            pos = pos_wrap.get("position", {})
            coin = pos.get("coin", "?")
            szi = float(pos.get("szi", 0))
            entry_px = float(pos.get("entryPx") or 0)
            unrealized_pnl = float(pos.get("unrealizedPnl") or 0)
            liq_px = float(pos.get("liquidationPx") or 0) or None
            leverage_info = pos.get("leverage", {})
            leverage = leverage_info.get("value") if isinstance(leverage_info, dict) else None

            size_usd = abs(szi) * entry_px
            direction = "LONG" if szi > 0 else "SHORT"

            # Large position alert
            if size_usd >= self.min_position_usd:
                extra = {"pnl": unrealized_pnl}
                if liq_px:
                    extra["liq_px"] = liq_px
                if leverage:
                    extra["leverage"] = leverage
                if label:
                    extra["label"] = label
                alerts.append(WhaleAlert(
                    alert_type=AlertType.LARGE_POSITION,
                    address=address,
                    coin=coin,
                    size_usd=size_usd,
                    direction=direction,
                    price=entry_px,
                    extra=extra,
                ))

            # Position flip detection
            prev = self._last_side.get(address, {}).get(coin)
            if prev and prev != direction:
                extra_flip = {"prev_side": prev}
                if liq_px:
                    extra_flip["liq_px"] = liq_px
                if leverage:
                    extra_flip["leverage"] = leverage
                if label:
                    extra_flip["label"] = label
                alerts.append(WhaleAlert(
                    alert_type=AlertType.POSITION_FLIP,
                    address=address,
                    coin=coin,
                    size_usd=size_usd,
                    direction=direction,
                    price=entry_px,
                    extra=extra_flip,
                ))

            self._last_side.setdefault(address, {})[coin] = direction

        return alerts

    # ── Internal helpers ───────────────────────────────────

    def _check_trade(self, t: dict) -> WhaleAlert | None:
        try:
            coin = t.get("coin", "?")
            px = float(t.get("px", 0))
            sz = float(t.get("sz", 0))
            side = t.get("side", "B")          # B = buy / A = ask(sell)
            tid = t.get("tid", "")
            user = t.get("users", [None])[0] or tid

            size_usd = px * sz
            if size_usd < self.min_trade_usd:
                return None

            direction = "LONG" if side == "B" else "SHORT"
            return WhaleAlert(
                alert_type=AlertType.BIG_TRADE,
                address=str(user),
                coin=coin,
                size_usd=size_usd,
                direction=direction,
                price=px,
            )
        except Exception as e:
            logger.debug(f"Trade parse error: {e}")
            return None

    def _check_fill(self, fill: dict, watched: set[str]) -> WhaleAlert | None:
        try:
            user = fill.get("user", "")
            if user.lower() not in watched:
                return None
            coin = fill.get("coin", "?")
            px = float(fill.get("px", 0))
            sz = float(fill.get("sz", 0))
            side = fill.get("side", "B")
            size_usd = px * sz

            direction = "LONG" if side == "B" else "SHORT"
            return WhaleAlert(
                alert_type=AlertType.WATCHLIST_TRADE,
                address=user,
                coin=coin,
                size_usd=size_usd,
                direction=direction,
                price=px,
            )
        except Exception as e:
            logger.debug(f"Fill parse error: {e}")
            return None

    def _check_liquidation(self, liq: dict) -> WhaleAlert | None:
        try:
            coin = liq.get("coin", "?")
            px = float(liq.get("px", 0))
            sz = float(liq.get("sz", 0))
            user = liq.get("user", "unknown")
            size_usd = px * sz

            if size_usd < self.min_liq_usd:
                return None

            return WhaleAlert(
                alert_type=AlertType.LIQUIDATION,
                address=user,
                coin=coin,
                size_usd=size_usd,
                direction="LIQUIDATED",
                price=px,
            )
        except Exception as e:
            logger.debug(f"Liquidation parse error: {e}")
            return None

    # ── External exchange handlers ─────────────────────────

    def on_binance_trade(self, payload: dict) -> list["WhaleAlert"]:
        try:
            symbol   = payload.get("symbol", "?")
            side     = payload.get("side", "BUY")
            size_usd = float(payload.get("size_usd", 0.0))
            price    = float(payload.get("price", 0.0))
            if size_usd < self.min_trade_usd:
                return []
            return [WhaleAlert(
                alert_type=AlertType.BIG_TRADE,
                address=f"binance_{symbol.lower()}",
                coin=symbol,
                size_usd=size_usd,
                direction="LONG" if side == "BUY" else "SHORT",
                price=price,
                extra={"source": "Binance"},
            )]
        except Exception as e:
            logger.debug(f"on_binance_trade error: {e}")
            return []

    def on_bybit_trade(self, payload: dict) -> list["WhaleAlert"]:
        try:
            symbol   = payload.get("symbol", "?")
            side     = payload.get("side", "BUY")
            size_usd = float(payload.get("size_usd", 0.0))
            price    = float(payload.get("price", 0.0))
            if size_usd < self.min_trade_usd:
                return []
            return [WhaleAlert(
                alert_type=AlertType.BIG_TRADE,
                address=f"bybit_{symbol.lower()}",
                coin=symbol,
                size_usd=size_usd,
                direction="LONG" if side == "BUY" else "SHORT",
                price=price,
                extra={"source": "Bybit"},
            )]
        except Exception as e:
            logger.debug(f"on_bybit_trade error: {e}")
            return []

    def on_binance_liquidation(self, payload: dict) -> list["WhaleAlert"]:
        try:
            symbol   = payload.get("symbol", "?")
            side     = payload.get("side", "BUY")
            size_usd = float(payload.get("size_usd", 0.0))
            price    = float(payload.get("price", 0.0))
            if size_usd < self.min_liq_usd:
                return []
            return [WhaleAlert(
                alert_type=AlertType.LIQUIDATION,
                address=f"binance_{symbol.lower()}",
                coin=symbol,
                size_usd=size_usd,
                direction="LIQUIDATED",
                price=price,
                extra={"source": "Binance", "original_side": side},
            )]
        except Exception as e:
            logger.debug(f"on_binance_liquidation error: {e}")
            return []

    def on_bybit_liquidation(self, payload: dict) -> list["WhaleAlert"]:
        try:
            symbol   = payload.get("symbol", "?")
            side     = payload.get("side", "BUY")
            size_usd = float(payload.get("size_usd", 0.0))
            price    = float(payload.get("price", 0.0))
            if size_usd < self.min_liq_usd:
                return []
            return [WhaleAlert(
                alert_type=AlertType.LIQUIDATION,
                address=f"bybit_{symbol.lower()}",
                coin=symbol,
                size_usd=size_usd,
                direction="LIQUIDATED",
                price=price,
                extra={"source": "Bybit", "original_side": side},
            )]
        except Exception as e:
            logger.debug(f"on_bybit_liquidation error: {e}")
            return []
