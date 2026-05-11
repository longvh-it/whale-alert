import re
from datetime import datetime, timezone
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from src.storage.database import db
from src.signals import signal_tracker as st_module
from src.signals.signal_tracker import STATUS_LABEL
from config.settings import config

router = Router()


def _is_admin(chat_id: int) -> bool:
    return str(chat_id) == str(config.admin_chat_id)


def _fmt(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:,.2f}"


def _dur(minutes: int) -> str:
    if minutes <= 0:
        return "—"
    if minutes < 60:
        return f"{minutes}p"
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}p"


def _format_closed_row(s: dict) -> str:
    from datetime import datetime
    entry = s["entry_price"]
    d = s["direction"]
    status = s["status"]
    dir_e = "🟢" if d == "LONG" else "🔴"

    if status == "SL_HIT":
        exit_px = s["sl_price"]
        result_e = "❌"
    elif status == "TP1_HIT":
        exit_px = s["tp1"] or entry
        result_e = "✅"
    elif status == "TP2_HIT":
        exit_px = s["tp2"] or s["tp1"] or entry
        result_e = "✅"
    else:
        exit_px = s["tp3"] or s["tp2"] or s["tp1"] or entry
        result_e = "✅"

    pct = (exit_px - entry) / entry * 100 if d == "LONG" else (entry - exit_px) / entry * 100
    sign = "+" if pct >= 0 else ""

    dur_str = "—"
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        t0 = datetime.strptime(s["created_at"][:19], fmt)
        t1 = datetime.strptime(s["closed_at"][:19], fmt)
        dur_str = _dur(int((t1 - t0).total_seconds() / 60))
    except Exception:
        pass

    tp_label = status.replace("_HIT", "") if status != "SL_HIT" else "SL"
    return (
        f"{result_e} <code>#{s['id']:04d}</code> {dir_e} {d} <b>{s['coin']}</b> "
        f"— {tp_label} <b>{sign}{pct:.1f}%</b>  ⏱{dur_str}"
    )


# ── /signal ────────────────────────────────────────────────
@router.message(Command("signal"))
async def cmd_signal(msg: Message):
    if not _is_admin(msg.chat.id):
        await msg.answer("❌ Chỉ admin mới có thể tạo kèo.")
        return

    text = msg.text or ""
    parts = text.split(maxsplit=4)

    usage = (
        "❌ Cú pháp:\n"
        "<code>/signal BTC LONG 95000 TP1:97000 TP2:99000 SL:93500</code>\n\n"
        "Tham số:\n"
        "• <b>TP1, TP2, TP3</b> — mức chốt lời (ít nhất 1)\n"
        "• <b>SL</b> — cắt lỗ (bắt buộc)\n"
        "• <b>LEV</b> — đòn bẩy, vd: <code>LEV:20</code>\n"
        "• <b>NOTE</b> — ghi chú, vd: <code>NOTE:Breakout key level</code>"
    )

    if len(parts) < 5:
        await msg.answer(usage, parse_mode="HTML")
        return

    _, coin, direction, entry_str, kv_str = parts
    coin = coin.upper()
    direction = direction.upper()

    if direction not in ("LONG", "SHORT"):
        await msg.answer("❌ Direction phải là <code>LONG</code> hoặc <code>SHORT</code>.", parse_mode="HTML")
        return

    try:
        entry = float(entry_str)
    except ValueError:
        await msg.answer("❌ Entry price không hợp lệ.")
        return

    # Parse NOTE before numeric kv (it can contain spaces)
    note = None
    note_m = re.search(r'\bNOTE:(.+?)(?:\s+[A-Z]+:\S|$)', kv_str, re.IGNORECASE)
    if note_m:
        note = note_m.group(1).strip()

    # Parse numeric key:value pairs
    kv: dict[str, float] = {}
    for m in re.finditer(r'\b(TP1|TP2|TP3|SL|LEV):([\d.]+)', kv_str, re.IGNORECASE):
        kv[m.group(1).upper()] = float(m.group(2))

    tp1 = kv.get("TP1")
    tp2 = kv.get("TP2")
    tp3 = kv.get("TP3")
    sl = kv.get("SL")
    lev = int(kv["LEV"]) if "LEV" in kv else None

    if sl is None:
        await msg.answer("❌ <b>SL</b> (stop loss) là bắt buộc.", parse_mode="HTML")
        return
    if not any([tp1, tp2, tp3]):
        await msg.answer("❌ Phải có ít nhất một mức <b>TP</b>.", parse_mode="HTML")
        return

    # Validate direction logic
    err = None
    if direction == "LONG":
        if sl >= entry:
            err = "SL phải nhỏ hơn entry với LONG."
        elif any(t is not None and t <= entry for t in [tp1, tp2, tp3]):
            err = "TP phải lớn hơn entry với LONG."
    else:
        if sl <= entry:
            err = "SL phải lớn hơn entry với SHORT."
        elif any(t is not None and t >= entry for t in [tp1, tp2, tp3]):
            err = "TP phải nhỏ hơn entry với SHORT."
    if err:
        await msg.answer(f"❌ {err}")
        return

    tracker = st_module.get_tracker()
    sig_id = await tracker.create_and_post(
        coin=coin, direction=direction, entry=entry,
        tp1=tp1, tp2=tp2, tp3=tp3, sl=sl,
        leverage=lev, source="ADMIN", note=note,
    )

    current = tracker.get_current_price(coin)
    price_str = f"${current:,.2f}" if current else "chưa có"

    await msg.answer(
        f"✅ <b>Kèo #{sig_id:04d} đã tạo!</b>\n\n"
        f"{('🟢' if direction=='LONG' else '🔴')} <b>{direction} {coin}</b>\n"
        f"📍 Entry: <b>${entry:,.2f}</b>\n"
        f"💰 Giá hiện tại: <b>{price_str}</b>\n\n"
        f"{'📢 Đã post lên channel.' if tracker._channel_id else '⚠️ Chưa cấu hình SIGNAL_CHANNEL_ID.'}",
        parse_mode="HTML",
    )
    logger.info(f"Admin {msg.chat.id} tạo kèo #{sig_id}: {direction} {coin} @ {entry}")


# ── /cancel ────────────────────────────────────────────────
@router.message(Command("cancel"))
async def cmd_cancel(msg: Message):
    if not _is_admin(msg.chat.id):
        await msg.answer("❌ Chỉ admin mới có thể hủy kèo.")
        return

    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("❌ Cú pháp: <code>/cancel &lt;ID kèo&gt;</code>", parse_mode="HTML")
        return

    sig_id = int(parts[1])
    tracker = st_module.get_tracker()
    ok = await tracker.cancel_signal(sig_id)

    if ok:
        await msg.answer(f"✅ Kèo <code>#{sig_id:04d}</code> đã hủy.", parse_mode="HTML")
        logger.info(f"Admin {msg.chat.id} hủy kèo #{sig_id}")
    else:
        await msg.answer(
            f"❌ Không thể hủy kèo <code>#{sig_id:04d}</code>.\n"
            f"Kèo không tồn tại hoặc đã đóng.",
            parse_mode="HTML",
        )


# ── /signals ───────────────────────────────────────────────
@router.message(Command("signals"))
async def cmd_signals(msg: Message):
    rows = await db.list_signals(limit=15)
    if not rows:
        await msg.answer("📋 Chưa có kèo nào.")
        return

    lines = ["📋 <b>Danh sách kèo (15 gần nhất)</b>", ""]
    for s in rows:
        d = s["direction"]
        dir_e = "🟢" if d == "LONG" else "🔴"
        status_label = STATUS_LABEL.get(s["status"], s["status"])
        tp_last = s.get("tp3") or s.get("tp2") or s.get("tp1")
        tp_str = f"→ {_fmt(tp_last)}" if tp_last else ""
        sl_str = _fmt(s["sl_price"])
        lines.append(
            f"<code>#{s['id']:04d}</code> {dir_e} <b>{d} {s['coin']}</b>\n"
            f"    📍 {_fmt(s['entry_price'])} {tp_str} / SL {sl_str}\n"
            f"    {status_label}"
        )

    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── /signal_stats ──────────────────────────────────────────
@router.message(Command("signal_stats"))
async def cmd_signal_stats(msg: Message):
    if not _is_admin(msg.chat.id):
        await msg.answer("❌ Chỉ admin mới xem được thống kê kèo.")
        return

    stats = await db.get_signal_stats()
    closed_signals = await db.get_closed_signals(limit=50)

    total = stats["total"]
    if total == 0:
        await msg.answer("📊 Chưa có dữ liệu kèo.")
        return

    closed = stats["wins"] + stats["losses"]
    win_rate = stats["wins"] / closed * 100 if closed > 0 else 0

    # Tính avg % thắng/thua từ các kèo đã đóng
    win_pcts, loss_pcts, durations_win, durations_loss = [], [], [], []
    for s in closed_signals:
        entry = s["entry_price"]
        d = s["direction"]
        status = s["status"]

        if status == "SL_HIT":
            exit_px = s["sl_price"]
        elif status == "TP1_HIT":
            exit_px = s["tp1"] or s["sl_price"]
        elif status == "TP2_HIT":
            exit_px = s["tp2"] or s["tp1"] or s["sl_price"]
        else:  # TP3_HIT
            exit_px = s["tp3"] or s["tp2"] or s["tp1"] or s["sl_price"]

        pct = (exit_px - entry) / entry * 100 if d == "LONG" else (entry - exit_px) / entry * 100

        # Duration in minutes
        dur = None
        try:
            from datetime import datetime
            fmt = "%Y-%m-%d %H:%M:%S"
            t0 = datetime.strptime(s["created_at"][:19], fmt)
            t1 = datetime.strptime(s["closed_at"][:19], fmt)
            dur = int((t1 - t0).total_seconds() / 60)
        except Exception:
            pass

        if status == "SL_HIT":
            loss_pcts.append(pct)
            if dur is not None:
                durations_loss.append(dur)
        else:
            win_pcts.append(pct)
            if dur is not None:
                durations_win.append(dur)

    avg_win = sum(win_pcts) / len(win_pcts) if win_pcts else 0
    avg_loss = sum(loss_pcts) / len(loss_pcts) if loss_pcts else 0
    expectancy = (win_rate / 100) * avg_win + (1 - win_rate / 100) * avg_loss
    avg_dur_win = sum(durations_win) // len(durations_win) if durations_win else 0
    avg_dur_loss = sum(durations_loss) // len(durations_loss) if durations_loss else 0

    lines = [
        "📊 <b>Tổng hợp kèo</b>  <i>(Admin)</i>", "",
        f"📌 Tổng: <b>{total}</b>  |  🟡 Active: <b>{stats['active']}</b>  |  ⚪ Đã hủy: {stats['cancelled']}",
        "",
        f"✅ Thắng: <b>{stats['wins']}</b>  |  ❌ Thua: <b>{stats['losses']}</b>  |  Tỉ lệ: <b>{win_rate:.1f}%</b>",
        f"    TP1: {stats['tp1']}  •  TP2: {stats['tp2']}  •  TP3: {stats['tp3']}",
        "",
        f"📈 Avg thắng: <b>+{avg_win:.2f}%</b>  ({_dur(avg_dur_win)})",
        f"📉 Avg thua:  <b>{avg_loss:.2f}%</b>  ({_dur(avg_dur_loss)})",
        f"💡 Expectancy: <b>{'+' if expectancy >= 0 else ''}{expectancy:.2f}%</b>",
    ]
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── /whales ────────────────────────────────────────────────
@router.message(Command("whales"))
async def cmd_whales(msg: Message):
    if not _is_admin(msg.chat.id):
        await msg.answer("❌ Chỉ admin mới xem được danh sách whale.")
        return

    known = await db.get_known_whales()
    summary = await db.get_whale_score_summary()

    lines = [
        "🐋 <b>Known Whales</b>  <i>(Admin)</i>",
        f"<i>Đang theo dõi: {summary['total_tracked']} địa chỉ | Promoted: {summary['promoted']}</i>",
        "",
    ]

    if not known:
        lines.append("Chưa có whale nào được promote.")
        lines.append("Bot tự động promote sau khi quan sát đủ trades.")
    else:
        for i, w in enumerate(known, 1):
            addr = f"{w['address'][:8]}…{w['address'][-6:]}"
            wr = w["win_rate"] or 0
            pnl = w["total_pnl"] or 0
            tc = w["trade_count"] or 0
            wr_emoji = "🏆" if wr >= 80 else ("✅" if wr >= 60 else "⚠️")
            lines.append(
                f"{i}. <code>{addr}</code>\n"
                f"    {wr_emoji} WR: <b>{wr:.0f}%</b>  |  "
                f"Trades: {tc}  |  PnL: <b>${pnl:,.0f}</b>"
            )

    lines += [
        "",
        f"<i>Avg win rate observed: {summary['avg_win_rate']:.1f}%</i>",
    ]
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── /whale_scores ───────────────────────────────────────────
@router.message(Command("whale_scores"))
async def cmd_whale_scores(msg: Message):
    if not _is_admin(msg.chat.id):
        await msg.answer("❌ Chỉ admin mới xem được.")
        return

    rows = await db.get_top_whale_scores(limit=15)
    if not rows:
        await msg.answer(
            "📊 Chưa có dữ liệu.\n\n"
            "Bot sẽ tự động thu thập sau khi các whale đóng position."
        )
        return

    lines = ["📊 <b>Whale Scores</b>  <i>(top 15 theo win rate)</i>", ""]
    for i, w in enumerate(rows, 1):
        addr = f"{w['address'][:8]}…{w['address'][-6:]}"
        star = "⭐" if w["promoted"] else "  "
        wr = w["win_rate"]
        bar = "█" * int(wr / 10) + "░" * (10 - int(wr / 10))
        lines.append(
            f"{star}<code>{addr}</code>  "
            f"<b>{wr:.0f}%</b> [{bar}]\n"
            f"    {w['profitable_trades']}/{w['total_trades']} trades  •  "
            f"PnL ${w['total_pnl']:,.0f}  •  Best ${w['best_trade_pnl']:,.0f}"
        )

    lines.append("\n<i>⭐ = đã promote lên Known Whales</i>")
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── /signal_report ─────────────────────────────────────────
@router.message(Command("signal_report"))
async def cmd_signal_report(msg: Message):
    if not _is_admin(msg.chat.id):
        await msg.answer("❌ Chỉ admin mới xem được báo cáo kèo.")
        return

    closed = await db.get_closed_signals(limit=20)
    if not closed:
        await msg.answer("📋 Chưa có kèo nào đã đóng.")
        return

    wins = [s for s in closed if s["status"] != "SL_HIT"]
    losses = [s for s in closed if s["status"] == "SL_HIT"]

    parts = []

    if wins:
        parts.append("✅ <b>Kèo thắng</b>")
        for s in wins[:10]:
            parts.append(_format_closed_row(s))

    if losses:
        parts.append("")
        parts.append("❌ <b>Kèo thua</b>")
        for s in losses[:10]:
            parts.append(_format_closed_row(s))

    await msg.answer("\n".join(parts), parse_mode="HTML")
