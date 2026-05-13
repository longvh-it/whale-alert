"""
Ecosystem Detector — khi một coin có volume spike, scan các coin cùng ecosystem.
Nếu coin phụ có trend + điều kiện bổ sung → tạo alert hoặc kèo.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from loguru import logger

from config.settings import config

if TYPE_CHECKING:
    from src.signals.signal_tracker import SignalTracker
    from src.detector.dom_analyzer import DOMAnalyzer


def _normalize_channel_id(raw: str) -> str:
    if not raw:
        return raw
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        n = int(raw)
        if n > 0:
            return f"-100{n}"
    return raw


@dataclass
class EcosystemSignal:
    trigger_coin: str
    trigger_volume_ratio: float
    target_coin: str
    target_trend: str        # "LONG" | "SHORT"
    target_trend_score: int
    has_whale_confirm: bool
    has_dom_confirm: bool
    strength: str            # "WATCH" | "ALERT" | "SIGNAL"
    reason: list = field(default_factory=list)


class EcosystemDetector:
    def __init__(self, signal_tracker=None, dom_analyzer=None, bot=None):
        self._signal_tracker = signal_tracker
        self._dom_analyzer = dom_analyzer
        self._bot = bot
        self._recent_volume_spikes: dict[str, tuple[float, datetime]] = {}

    def set_signal_tracker(self, tracker):
        self._signal_tracker = tracker

    def set_dom_analyzer(self, dom):
        self._dom_analyzer = dom

    def set_bot(self, bot):
        self._bot = bot

    async def on_volume_spike(self, coin: str, volume_ratio: float, direction: str):
        if not config.ecosystem_enabled:
            return
        if volume_ratio < config.ecosystem_volume_spike_min:
            return
        ecosystem_map = config.ecosystem_map
        if coin not in ecosystem_map:
            return

        self._recent_volume_spikes[coin] = (volume_ratio, datetime.utcnow())
        related_coins = ecosystem_map[coin]
        logger.info(f"Ecosystem: {coin} volume spike {volume_ratio:.1f}x — scanning {len(related_coins)} related coins")

        for target in related_coins:
            await self._evaluate_target(coin, volume_ratio, direction, target)

    async def _evaluate_target(self, trigger: str, vol_ratio: float, direction: str, target: str):
        from src.detector.trend_detector import get_multi_trend
        multi_direction, confirmed = get_multi_trend(target)

        if multi_direction != direction:
            return
        if confirmed < config.ecosystem_call_min_trend_score:
            return

        reasons = [f"{trigger} volume spike {vol_ratio:.1f}x"]

        has_whale = self._check_recent_whale(target, direction)
        if has_whale:
            reasons.append(f"whale {direction.lower()} confirmed")

        has_dom = False
        if self._dom_analyzer and target in config.dom_coins:
            dom = self._dom_analyzer.get_snapshot(target)
            expected_dom = "BULLISH" if direction == "LONG" else "BEARISH"
            if dom and dom.signal == expected_dom:
                has_dom = True
                reasons.append(f"DOM {dom.signal.lower()} (strength {dom.signal_strength})")

        if has_whale and has_dom:
            strength = "SIGNAL"
        elif has_whale or has_dom:
            strength = "ALERT"
        else:
            strength = "WATCH"

        eco = EcosystemSignal(
            trigger_coin=trigger,
            trigger_volume_ratio=vol_ratio,
            target_coin=target,
            target_trend=direction,
            target_trend_score=confirmed,
            has_whale_confirm=has_whale,
            has_dom_confirm=has_dom,
            strength=strength,
            reason=reasons,
        )
        await self._dispatch(eco)

    async def _dispatch(self, eco: EcosystemSignal):
        if eco.strength == "WATCH":
            await self._send_watch_alert(eco)
        elif eco.strength == "ALERT":
            await self._send_alert(eco)
        elif eco.strength == "SIGNAL":
            await self._send_alert(eco)
            if self._signal_tracker:
                await self._signal_tracker.maybe_create_ecosystem_signal(eco)

    def _check_recent_whale(self, coin: str, direction: str) -> bool:
        if not self._signal_tracker:
            return False
        side = "BUY" if direction == "LONG" else "SELL"
        events = getattr(self._signal_tracker, "_recent_whale_events", [])
        return any(e["coin"] == coin and e["side"] == side for e in events)

    async def _send_watch_alert(self, eco: EcosystemSignal):
        if not self._bot or not config.signal_channel_id:
            return
        channel_id = _normalize_channel_id(config.signal_channel_id)
        dir_emoji = "📈" if eco.target_trend == "LONG" else "📉"
        text = (
            f"👀 <b>ECOSYSTEM WATCH</b>\n\n"
            f"{dir_emoji} <b>{eco.trigger_coin}</b> volume spike <b>{eco.trigger_volume_ratio:.1f}x</b>\n"
            f"→ Coin liên quan cần theo dõi: <b>{eco.target_coin}</b>\n"
            f"   Trend: {eco.target_trend} (score {eco.target_trend_score}/3)"
        )
        try:
            await self._bot.send_message(
                chat_id=channel_id, text=text,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as e:
            logger.debug(f"Ecosystem WATCH send error: {e}")

    async def _send_alert(self, eco: EcosystemSignal):
        if not self._bot or not config.signal_channel_id:
            return
        channel_id = _normalize_channel_id(config.signal_channel_id)
        dir_emoji = "🟢" if eco.target_trend == "LONG" else "🔴"
        whale_line = "✅ Whale confirm trực tiếp" if eco.has_whale_confirm else "⚠️ Chưa có whale confirm"
        dom_line = "✅ DOM confirm" if eco.has_dom_confirm else ""

        lines = [
            f"🔔 <b>ECOSYSTEM {'SIGNAL' if eco.strength == 'SIGNAL' else 'ALERT'}</b> — "
            f"{eco.target_coin} {dir_emoji} {eco.target_trend}",
            "",
            f"📊 Trigger: <b>{eco.trigger_coin}</b> volume spike <b>{eco.trigger_volume_ratio:.1f}x</b>",
            f"✅ {eco.target_coin} trend {eco.target_trend} (score {eco.target_trend_score}/3)",
            whale_line,
        ]
        if dom_line:
            lines.append(dom_line)
        if eco.strength == "SIGNAL":
            lines.append("\n⚡ <i>Đang tạo kèo tự động...</i>")
        else:
            lines.append("\n→ Đang theo dõi, chưa tạo kèo tự động")

        try:
            await self._bot.send_message(
                chat_id=channel_id, text="\n".join(lines),
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as e:
            logger.debug(f"Ecosystem ALERT send error: {e}")


ecosystem_detector = EcosystemDetector()
