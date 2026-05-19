from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from loguru import logger

from src.storage.database import db
from src.api.hyperliquid_ws import ws as hl_ws
from config.settings import config

router = Router()


class AddWatchState(StatesGroup):
    waiting_address = State()
    waiting_label = State()


# Preset tiers: (label, min_trade, min_liq)
FILTER_PRESETS = {
    "small":  ("🐟 Cá nhỏ",   50_000,    100_000),
    "medium": ("🐋 Cá to",    200_000,   500_000),
    "huge":   ("🦈 Cá khủng", 1_000_000, 2_000_000),
}


def _filter_keyboard(active: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    for key, (label, trade, liq) in FILTER_PRESETS.items():
        tick = " ✓" if key == active else ""
        rows.append([InlineKeyboardButton(
            text=f"{label}{tick}",
            callback_data=f"fp:{key}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /start ─────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(msg: Message):
    await db.add_user(msg.chat.id, msg.from_user.username or "")
    await msg.answer(
        "🐋 <b>Hyperliquid Whale Bot</b>\n\n"
        "Theo dõi và cảnh báo khi:\n"
        "• 🐋 Trade lớn vượt ngưỡng\n"
        "• 💥 Liquidation lớn\n"
        "• 👁 Địa chỉ trong watchlist hoạt động\n\n"
        "<b>Lệnh chính:</b>\n"
        "/filter — 🎚 lọc cỡ lệnh (cá nhỏ/to/khủng)\n"
        "/watchlist — xem danh sách theo dõi\n"
        "/add — thêm địa chỉ theo dõi\n"
        "/remove — xóa địa chỉ\n"
        "/top — top PnL cao nhất\n"
        "/settings — xem cài đặt\n"
        "/help — hướng dẫn đầy đủ\n"
        "/export_excel — 📊 xuất thống kê kèo Excel <i>(admin)</i>\n"
        "/sim_vol — 💵 đổi mức vốn giả định P&amp;L <i>(admin)</i>",
        parse_mode="HTML",
    )


# ── /help ──────────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "📖 <b>Hướng dẫn sử dụng</b>\n\n"
        "<b>Theo dõi địa chỉ cụ thể:</b>\n"
        "<code>/add 0xABCDEF... Tên label</code>\n\n"
        "<b>Xóa theo dõi:</b>\n"
        "<code>/remove 0xABCDEF...</code>\n\n"
        "<b>Đặt ngưỡng alert (USD):</b>\n"
        "<code>/threshold trade 50000</code>\n"
        "<code>/threshold liq 100000</code>\n\n"
        "<b>Các loại alert:</b>\n"
        "🐋 Big Trade — lệnh khớp > ngưỡng\n"
        "💥 Liquidation — bị thanh lý lớn\n"
        "📊 Large Position — mở vị thế khủng\n"
        "🔄 Position Flip — đổi chiều Long↔Short\n"
        "💰 PnL Update — cập nhật lãi/lỗ lệnh whale\n"
        "👁 Watchlist — hoạt động của địa chỉ bạn theo dõi\n\n"
        "<b>Lọc theo cỡ lệnh:</b>\n"
        "/filter — chọn preset nhanh\n"
        "🐟 Cá nhỏ &gt;$50K  |  🐋 Cá to &gt;$200K  |  🦈 Cá khủng &gt;$1M\n\n"
        "<b>Xem thống kê:</b>\n"
        "<code>/top</code> — top PnL đang theo dõi (4h gần nhất)\n\n"
        "<b>Admin:</b>\n"
        "<code>/export_excel</code> — xuất thống kê kèo ra file Excel\n"
        "<code>/sim_vol [số]</code> — đổi mức vốn giả định P&amp;L (mặc định $200)",
        parse_mode="HTML",
    )


# ── /watchlist ─────────────────────────────────────────────
@router.message(Command("watchlist"))
async def cmd_watchlist(msg: Message):
    items = await db.get_watchlist(msg.chat.id)
    if not items:
        await msg.answer(
            "📋 Watchlist của bạn đang trống.\n\n"
            "Dùng <code>/add 0xAddress Label</code> để thêm địa chỉ.",
            parse_mode="HTML",
        )
        return

    lines = ["📋 <b>Watchlist của bạn</b>", ""]
    for i, item in enumerate(items, 1):
        addr = item["address"]
        label = item["label"] or "—"
        short = f"{addr[:8]}…{addr[-6:]}"
        lines.append(f"{i}. <code>{short}</code>")
        lines.append(f"    🏷 {label}")
        lines.append("")

    lines.append(f"<i>Tổng: {len(items)} địa chỉ</i>")
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── /add ───────────────────────────────────────────────────
@router.message(Command("add"))
async def cmd_add(msg: Message):
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 2:
        await msg.answer(
            "❌ Cú pháp: <code>/add 0xAddress [Label tùy chọn]</code>",
            parse_mode="HTML",
        )
        return

    address = parts[1].strip()
    label = parts[2].strip() if len(parts) > 2 else ""

    if not address.startswith("0x") or len(address) != 42:
        await msg.answer("❌ Địa chỉ không hợp lệ. Phải bắt đầu bằng 0x và dài 42 ký tự.")
        return

    await db.add_user(msg.chat.id, msg.from_user.username or "")
    await db.add_to_watchlist(msg.chat.id, address, label)
    await hl_ws.add_user_subscription(address)

    await msg.answer(
        f"✅ Đã thêm vào watchlist!\n\n"
        f"📍 <code>{address}</code>\n"
        f"🏷 Label: {label or '(không có)'}",
        parse_mode="HTML",
    )
    logger.info(f"User {msg.chat.id} added {address}")


# ── /remove ────────────────────────────────────────────────
@router.message(Command("remove"))
async def cmd_remove(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer(
            "❌ Cú pháp: <code>/remove 0xAddress</code>",
            parse_mode="HTML",
        )
        return

    address = parts[1].strip()
    await db.remove_from_watchlist(msg.chat.id, address)
    # Only unsubscribe if no other user still watches this address
    remaining = await db.get_all_watched_addresses()
    if address.lower() not in remaining:
        hl_ws.remove_user_subscription(address)
    await msg.answer(f"🗑 Đã xóa <code>{address}</code> khỏi watchlist.", parse_mode="HTML")


# ── /threshold ─────────────────────────────────────────────
@router.message(Command("threshold"))
async def cmd_threshold(msg: Message):
    parts = msg.text.split()
    if len(parts) != 3:
        await msg.answer(
            "❌ Cú pháp:\n"
            "<code>/threshold trade 50000</code>  — ngưỡng trade\n"
            "<code>/threshold liq 100000</code>   — ngưỡng liquidation",
            parse_mode="HTML",
        )
        return

    kind = parts[1].lower()
    try:
        value = float(parts[2])
    except ValueError:
        await msg.answer("❌ Giá trị phải là số.")
        return

    field_map = {"trade": "min_trade", "liq": "min_liq"}
    if kind not in field_map:
        await msg.answer("❌ Loại phải là <code>trade</code> hoặc <code>liq</code>.", parse_mode="HTML")
        return

    await db.update_user_threshold(msg.chat.id, field_map[kind], value)
    await msg.answer(
        f"✅ Ngưỡng <b>{kind}</b> đã cập nhật: <b>${value:,.0f}</b>",
        parse_mode="HTML",
    )


# ── /settings ──────────────────────────────────────────────
@router.message(Command("settings"))
async def cmd_settings(msg: Message):
    s = await db.get_user_settings(msg.chat.id)
    if not s:
        await msg.answer("Bạn chưa đăng ký. Gõ /start để bắt đầu.")
        return

    await msg.answer(
        f"⚙️ <b>Cài đặt của bạn</b>\n\n"
        f"📊 Ngưỡng trade: <b>${s['min_trade']:,.0f}</b>\n"
        f"💥 Ngưỡng liquidation: <b>${s['min_liq']:,.0f}</b>\n"
        f"🔔 Trạng thái: {'✅ Active' if s else '❌ Inactive'}\n\n"
        f"Dùng /threshold để thay đổi ngưỡng.",
        parse_mode="HTML",
    )


# ── /top ───────────────────────────────────────────────────
@router.message(Command("top"))
async def cmd_top(msg: Message):
    rows = await db.get_top_pnl(limit=10)
    if not rows:
        await msg.answer(
            "📊 Chưa có dữ liệu PnL.\n\n"
            "Bot tự động theo dõi PnL sau mỗi BIG_TRADE alert. "
            "Hãy chờ vài phút để có dữ liệu.",
            parse_mode="HTML",
        )
        return

    def fmt_pnl(v: float) -> str:
        sign = "+" if v >= 0 else ""
        if abs(v) >= 1_000_000:
            return f"{sign}${v/1_000_000:.2f}M"
        if abs(v) >= 1_000:
            return f"{sign}${v/1_000:.1f}K"
        return f"{sign}${v:.0f}"

    lines = ["🏆 <b>Top PnL — đang theo dõi</b>", ""]
    for i, r in enumerate(rows, 1):
        addr = f"{r['address'][:6]}…{r['address'][-4:]}"
        pnl = r["last_pnl"]
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        dir_emoji = "🟢" if r["direction"] == "LONG" else "🔴"
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        lines.append(
            f"{medal} <b>{r['coin']}</b>  {dir_emoji}  "
            f"{pnl_emoji} <b>{fmt_pnl(pnl)}</b>\n"
            f"    👤 <code>{addr}</code>"
        )

    lines.append(f"\n<i>Cập nhật mỗi 60s · Dữ liệu tối đa 4 giờ</i>")
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── /filter ────────────────────────────────────────────────
@router.message(Command("filter"))
async def cmd_filter(msg: Message):
    s = await db.get_user_settings(msg.chat.id)
    if not s:
        await db.add_user(msg.chat.id, msg.from_user.username or "")
        s = await db.get_user_settings(msg.chat.id)

    active = _detect_active_preset(s["min_trade"], s["min_liq"])
    await msg.answer(
        f"🎚 <b>Lọc cỡ lệnh (vol)</b>\n\n"
        f"Chọn ngưỡng phù hợp — chỉ nhận alert từ mức đó trở lên:\n\n"
        f"🐟 <b>Cá nhỏ</b>  — Trade &gt;$50K · Liq &gt;$100K\n"
        f"🐋 <b>Cá to</b>   — Trade &gt;$200K · Liq &gt;$500K\n"
        f"🦈 <b>Cá khủng</b> — Trade &gt;$1M · Liq &gt;$2M\n\n"
        f"Hiện tại: Trade &gt;<b>${s['min_trade']:,.0f}</b> · Liq &gt;<b>${s['min_liq']:,.0f}</b>",
        reply_markup=_filter_keyboard(active),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("fp:"))
async def cb_filter_preset(cq: CallbackQuery):
    key = cq.data.split(":")[1]
    if key not in FILTER_PRESETS:
        await cq.answer("Preset không hợp lệ.")
        return

    label, trade, liq = FILTER_PRESETS[key]
    await db.add_user(cq.message.chat.id, "")
    await db.update_user_threshold(cq.message.chat.id, "min_trade", trade)
    await db.update_user_threshold(cq.message.chat.id, "min_liq", liq)

    await cq.message.edit_text(
        f"✅ Đã chọn <b>{label}</b>\n\n"
        f"📊 Trade &gt; <b>${trade:,.0f}</b>\n"
        f"💥 Liquidation &gt; <b>${liq:,.0f}</b>\n\n"
        f"<i>Thay đổi có hiệu lực ngay cho các alert tiếp theo.</i>",
        reply_markup=_filter_keyboard(key),
        parse_mode="HTML",
    )
    await cq.answer(f"{label} đã được áp dụng!")
    logger.info(f"User {cq.message.chat.id} set filter preset={key}")


def _detect_active_preset(min_trade: float, min_liq: float) -> str | None:
    for key, (_, trade, liq) in FILTER_PRESETS.items():
        if min_trade == trade and min_liq == liq:
            return key
    return None


# ── /sources ───────────────────────────────────────────────
@router.message(Command("sources"))
async def cmd_sources(msg: Message):
    def _tick(flag: bool) -> str:
        return "✅" if flag else "❌"

    lines = [
        "📡 <b>Nguồn dữ liệu đang hoạt động</b>",
        "",
        f"✅ Hyperliquid (WebSocket)",
        f"{_tick(config.binance_enabled)} Binance Futures (WebSocket)",
        f"{_tick(config.bybit_enabled)} Bybit Futures (WebSocket)",
        f"✅ Coinglass/Binance (REST, poll mỗi {config.coinglass_poll_interval}s)",
        "",
        "━━━━━━━━━━━━━━━━━",
        f"📊 OI Detector: enabled  (threshold {config.oi_spike_threshold}%)",
        f"💸 Funding Detector: enabled  (high={config.funding_extreme_high:+.2f}% / low={config.funding_extreme_low:+.2f}%)",
        f"🎯 Confluence: {_tick(config.confluence_enabled)}  (min {config.confluence_min_sources} nguồn / {config.confluence_window}s window)",
        "",
        "<i>Dùng /confluence on|off để bật/tắt confluence alerts cho bạn.</i>",
    ]
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── /confluence ─────────────────────────────────────────────
@router.message(Command("confluence"))
async def cmd_confluence(msg: Message):
    parts = msg.text.split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        s = await db.get_user_settings(msg.chat.id)
        if not s:
            await msg.answer("Bạn chưa đăng ký. Gõ /start để bắt đầu.")
            return
        status = "✅ bật" if s.get("confluence_enabled", True) else "❌ tắt"
        await msg.answer(
            f"🎯 <b>Confluence alerts</b>: {status}\n\n"
            f"Dùng <code>/confluence on</code> hoặc <code>/confluence off</code> để thay đổi.",
            parse_mode="HTML",
        )
        return

    enabled = parts[1].lower() == "on"
    await db.add_user(msg.chat.id, msg.from_user.username or "")
    await db.set_confluence_enabled(msg.chat.id, enabled)
    status = "✅ bật" if enabled else "❌ tắt"
    await msg.answer(
        f"🎯 Confluence alerts đã <b>{status}</b>.\n\n"
        f"<i>Bạn {'sẽ' if enabled else 'sẽ không'} nhận alerts khi nhiều nguồn cùng báo một chiều.</i>",
        parse_mode="HTML",
    )


# ── fallback ───────────────────────────────────────────────
@router.message()
async def fallback(msg: Message):
    await msg.answer(
        "Không hiểu lệnh này. Gõ /help để xem hướng dẫn."
    )
