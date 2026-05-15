import asyncio
from datetime import datetime
from typing import Optional
import aiosqlite
from loguru import logger
from config.settings import config

# Full column list for signals SELECT queries (includes all migrated columns)
_SIGNAL_COLS = [
    "id", "coin", "direction", "entry_price", "tp1", "tp2", "tp3",
    "sl_price", "leverage", "status", "order_type", "source", "whale_address",
    "channel_msg_id", "note", "created_at",
    "sl_moved_to_entry", "sl_move_reason", "close_reason", "close_price",
    "reversal_score", "quality_score", "source_detail",
]

_SIGNAL_SELECT = (
    "SELECT id, coin, direction, entry_price, tp1, tp2, tp3, sl_price, "
    "leverage, status, order_type, source, whale_address, channel_msg_id, note, created_at, "
    "sl_moved_to_entry, sl_move_reason, close_reason, close_price, reversal_score, quality_score, "
    "source_detail "
    "FROM signals"
)


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id            INTEGER PRIMARY KEY,
    username           TEXT,
    joined_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active          INTEGER DEFAULT 1,
    min_trade          REAL DEFAULT 100000,
    min_liq            REAL DEFAULT 200000,
    confluence_enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER,
    address     TEXT,
    label       TEXT,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, address),
    FOREIGN KEY(chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS alert_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER,
    alert_type  TEXT,
    address     TEXT,
    payload     TEXT,
    sent_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS known_whales (
    address     TEXT PRIMARY KEY,
    label       TEXT,
    win_rate    REAL DEFAULT 0,
    total_pnl   REAL DEFAULT 0,
    trade_count INTEGER DEFAULT 0,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_global   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS whale_scores (
    address             TEXT PRIMARY KEY,
    total_trades        INTEGER DEFAULT 0,
    profitable_trades   INTEGER DEFAULT 0,
    total_pnl           REAL DEFAULT 0,
    best_trade_pnl      REAL DEFAULT 0,
    last_pnl            REAL DEFAULT 0,
    promoted            INTEGER DEFAULT 0,
    last_seen           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auto_watch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    address     TEXT,
    coin        TEXT,
    entry_px    REAL,
    direction   TEXT,
    size_usd    REAL,
    last_pnl    REAL DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(address, coin)
);

CREATE TABLE IF NOT EXISTS auto_watch_msgs (
    address     TEXT,
    coin        TEXT,
    chat_id     INTEGER,
    message_id  INTEGER,
    PRIMARY KEY (address, coin, chat_id)
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    coin            TEXT NOT NULL,
    direction       TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    tp1             REAL,
    tp2             REAL,
    tp3             REAL,
    sl_price        REAL NOT NULL,
    leverage        INTEGER,
    status          TEXT DEFAULT 'ACTIVE',
    order_type      TEXT DEFAULT 'MARKET',
    source          TEXT DEFAULT 'ADMIN',
    whale_address   TEXT,
    channel_msg_id  INTEGER,
    note            TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at       TIMESTAMP
);
"""


class Database:
    def __init__(self, db_path: str = config.db_path):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(DB_SCHEMA)
            # Migrate known_whales nếu chưa có các cột mới
            for col, typedef in [
                ("win_rate",    "REAL DEFAULT 0"),
                ("total_pnl",   "REAL DEFAULT 0"),
                ("trade_count", "INTEGER DEFAULT 0"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE known_whales ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            try:
                await db.execute("ALTER TABLE signals ADD COLUMN order_type TEXT DEFAULT 'MARKET'")
            except Exception:
                pass
            try:
                await db.execute("ALTER TABLE users ADD COLUMN confluence_enabled INTEGER DEFAULT 1")
            except Exception:
                pass
            # New columns for signal improvements
            for col, typedef in [
                ("sl_moved_to_entry", "INTEGER DEFAULT 0"),
                ("sl_move_reason",    "TEXT"),
                ("close_reason",      "TEXT"),
                ("close_price",       "REAL"),
                ("reversal_score",    "INTEGER"),
                ("quality_score",     "INTEGER DEFAULT 0"),
                ("source_detail",     "TEXT"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE signals ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            await db.commit()
        logger.info(f"Database initialized: {self.db_path}")

    # ── Users ──────────────────────────────────────────────
    async def add_user(self, chat_id: int, username: str = ""):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (chat_id, username) VALUES (?, ?)",
                (chat_id, username),
            )
            await db.commit()

    async def get_all_active_users(self) -> list[int]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT chat_id FROM users WHERE is_active = 1"
            )
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def get_active_users_with_thresholds(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT chat_id, min_trade, min_liq, confluence_enabled FROM users WHERE is_active = 1"
            )
            rows = await cursor.fetchall()
            return [
                {"chat_id": r[0], "min_trade": r[1], "min_liq": r[2], "confluence_enabled": bool(r[3])}
                for r in rows
            ]

    async def set_confluence_enabled(self, chat_id: int, enabled: bool):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET confluence_enabled = ? WHERE chat_id = ?",
                (int(enabled), chat_id),
            )
            await db.commit()

    async def get_user_settings(self, chat_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT chat_id, username, min_trade, min_liq, confluence_enabled FROM users WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            if row:
                return {
                    "chat_id": row[0], "username": row[1],
                    "min_trade": row[2], "min_liq": row[3],
                    "confluence_enabled": bool(row[4]) if row[4] is not None else True,
                }
            return None

    async def update_user_threshold(self, chat_id: int, field: str, value: float):
        if field not in ("min_trade", "min_liq"):
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE users SET {field} = ? WHERE chat_id = ?", (value, chat_id)
            )
            await db.commit()

    # ── Watchlist ──────────────────────────────────────────
    async def add_to_watchlist(self, chat_id: int, address: str, label: str = ""):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO watchlist (chat_id, address, label) VALUES (?, ?, ?)",
                (chat_id, address.lower(), label),
            )
            await db.commit()

    async def remove_from_watchlist(self, chat_id: int, address: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM watchlist WHERE chat_id = ? AND address = ?",
                (chat_id, address.lower()),
            )
            await db.commit()

    async def get_watchlist(self, chat_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT address, label, added_at FROM watchlist WHERE chat_id = ?",
                (chat_id,),
            )
            rows = await cursor.fetchall()
            return [{"address": r[0], "label": r[1], "added_at": r[2]} for r in rows]

    async def get_all_watched_addresses(self) -> dict[str, list[int]]:
        """Returns {address: [chat_id, ...]}"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT w.address, w.chat_id FROM watchlist w "
                "JOIN users u ON w.chat_id = u.chat_id WHERE u.is_active = 1"
            )
            rows = await cursor.fetchall()
            result: dict[str, list[int]] = {}
            for addr, cid in rows:
                result.setdefault(addr, []).append(cid)
            return result

    # ── Alert dedup ────────────────────────────────────────
    async def was_alerted_recently(
        self, chat_id: int, alert_type: str, address: str, seconds: int = config.alert_cooldown
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id FROM alert_log "
                "WHERE chat_id=? AND alert_type=? AND address=? "
                "AND (strftime('%s','now') - strftime('%s', sent_at)) < ?",
                (chat_id, alert_type, address, seconds),
            )
            return await cursor.fetchone() is not None

    async def log_alert(self, chat_id: int, alert_type: str, address: str, payload: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO alert_log (chat_id, alert_type, address, payload) VALUES (?,?,?,?)",
                (chat_id, alert_type, address, payload),
            )
            await db.commit()

    # ── Auto-watch (PnL tracking) ──────────────────────────
    async def add_auto_watch(self, address: str, coin: str, entry_px: float, direction: str, size_usd: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO auto_watch (address, coin, entry_px, direction, size_usd, last_pnl, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)",
                (address.lower(), coin, entry_px, direction, size_usd),
            )
            await db.commit()

    async def get_active_auto_watch(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT address, coin, entry_px, direction, size_usd, last_pnl, created_at FROM auto_watch "
                "WHERE (strftime('%s','now') - strftime('%s', created_at)) < 14400"
            )
            rows = await cursor.fetchall()
            return [
                {"address": r[0], "coin": r[1], "entry_px": r[2],
                 "direction": r[3], "size_usd": r[4], "last_pnl": r[5], "created_at": r[6]}
                for r in rows
            ]

    async def update_auto_watch_pnl(self, address: str, coin: str, pnl: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE auto_watch SET last_pnl = ? WHERE address = ? AND coin = ?",
                (pnl, address.lower(), coin),
            )
            await db.commit()

    async def remove_auto_watch(self, address: str, coin: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM auto_watch WHERE address = ? AND coin = ?",
                (address.lower(), coin),
            )
            await db.commit()

    async def save_auto_watch_msg(self, address: str, coin: str, chat_id: int, message_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO auto_watch_msgs (address, coin, chat_id, message_id) VALUES (?,?,?,?)",
                (address.lower(), coin, chat_id, message_id),
            )
            await db.commit()

    async def get_auto_watch_msgs(self, address: str, coin: str) -> dict[int, int]:
        """Returns {chat_id: message_id} for the original alert of this position."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT chat_id, message_id FROM auto_watch_msgs WHERE address = ? AND coin = ?",
                (address.lower(), coin),
            )
            rows = await cursor.fetchall()
            return {r[0]: r[1] for r in rows}

    async def delete_auto_watch_msgs(self, address: str, coin: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM auto_watch_msgs WHERE address = ? AND coin = ?",
                (address.lower(), coin),
            )
            await db.commit()

    async def clean_auto_watch(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM auto_watch WHERE (strftime('%s','now') - strftime('%s', created_at)) >= 14400"
            )
            await db.commit()

    async def get_top_pnl(self, limit: int = 10) -> list[dict]:
        """Top entries in auto_watch by absolute PnL, most profitable first."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT address, coin, direction, entry_px, size_usd, last_pnl, created_at "
                "FROM auto_watch "
                "WHERE last_pnl != 0 "
                "ORDER BY last_pnl DESC "
                "LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [
                {"address": r[0], "coin": r[1], "direction": r[2],
                 "entry_px": r[3], "size_usd": r[4], "last_pnl": r[5], "created_at": r[6]}
                for r in rows
            ]


    # ── Whale scoring ──────────────────────────────────────
    async def update_whale_score(self, address: str, pnl: float, profitable: bool):
        """Ghi kết quả một trade vào whale_scores."""
        addr = address.lower()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO whale_scores (address, total_trades, profitable_trades, total_pnl, best_trade_pnl, last_pnl, last_seen)
                VALUES (?, 1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(address) DO UPDATE SET
                    total_trades      = total_trades + 1,
                    profitable_trades = profitable_trades + ?,
                    total_pnl         = total_pnl + ?,
                    best_trade_pnl    = MAX(best_trade_pnl, ?),
                    last_pnl          = ?,
                    last_seen         = CURRENT_TIMESTAMP
                """,
                (addr, int(profitable), pnl, pnl, pnl,
                 int(profitable), pnl, pnl, pnl),
            )
            await db.commit()

    async def get_promotable_whales(
        self, min_trades: int = 3, min_win_rate: float = 60.0
    ) -> list[dict]:
        """Địa chỉ đủ điều kiện promote (chưa promoted)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT address, total_trades, profitable_trades, total_pnl, best_trade_pnl
                FROM whale_scores
                WHERE total_trades >= ?
                  AND (profitable_trades * 100.0 / total_trades) >= ?
                  AND promoted = 0
                ORDER BY (profitable_trades * 100.0 / total_trades) DESC, total_pnl DESC
                """,
                (min_trades, min_win_rate),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "address": r[0],
                    "total_trades": r[1],
                    "profitable_trades": r[2],
                    "total_pnl": r[3],
                    "best_trade_pnl": r[4],
                    "win_rate": r[2] / r[1] * 100 if r[1] else 0,
                }
                for r in rows
            ]

    async def promote_to_known_whale(
        self, address: str, win_rate: float, total_pnl: float, trade_count: int
    ):
        addr = address.lower()
        label = f"WR:{win_rate:.0f}% PnL:${total_pnl:,.0f} ({trade_count}T)"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO known_whales (address, label, win_rate, total_pnl, trade_count, is_global)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(address) DO UPDATE SET
                    label       = excluded.label,
                    win_rate    = excluded.win_rate,
                    total_pnl   = excluded.total_pnl,
                    trade_count = excluded.trade_count
                """,
                (addr, label, win_rate, total_pnl, trade_count),
            )
            await db.execute(
                "UPDATE whale_scores SET promoted = 1 WHERE address = ?", (addr,)
            )
            await db.commit()

    async def get_known_whales(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT address, label, win_rate, total_pnl, trade_count, added_at "
                "FROM known_whales ORDER BY win_rate DESC, total_pnl DESC"
            )
            rows = await cursor.fetchall()
            return [
                {"address": r[0], "label": r[1], "win_rate": r[2],
                 "total_pnl": r[3], "trade_count": r[4], "added_at": r[5]}
                for r in rows
            ]

    async def get_whale_score_summary(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*), COUNT(CASE WHEN promoted=1 THEN 1 END), "
                "AVG(profitable_trades*100.0/total_trades), SUM(total_pnl) "
                "FROM whale_scores WHERE total_trades > 0"
            )
            r = await cursor.fetchone()
            return {
                "total_tracked": r[0] or 0,
                "promoted": r[1] or 0,
                "avg_win_rate": r[2] or 0,
                "total_observed_pnl": r[3] or 0,
            }

    async def get_top_whale_scores(self, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT address, total_trades, profitable_trades, total_pnl, best_trade_pnl, promoted
                FROM whale_scores
                WHERE total_trades >= 1
                ORDER BY (profitable_trades * 100.0 / total_trades) DESC, total_pnl DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "address": r[0],
                    "total_trades": r[1],
                    "profitable_trades": r[2],
                    "total_pnl": r[3],
                    "best_trade_pnl": r[4],
                    "promoted": bool(r[5]),
                    "win_rate": r[2] / r[1] * 100 if r[1] else 0,
                }
                for r in rows
            ]

    # ── Signals ────────────────────────────────────────────
    async def create_signal(
        self,
        coin: str,
        direction: str,
        entry_price: float,
        tp1: Optional[float],
        tp2: Optional[float],
        tp3: Optional[float],
        sl_price: float,
        leverage: Optional[int] = None,
        source: str = "ADMIN",
        whale_address: Optional[str] = None,
        note: Optional[str] = None,
        order_type: str = "MARKET",
        quality_score: int = 0,
        source_detail: Optional[str] = None,
    ) -> int:
        initial_status = "PENDING" if order_type == "LIMIT" else "ACTIVE"
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO signals (coin, direction, entry_price, tp1, tp2, tp3, sl_price, "
                "leverage, status, order_type, source, whale_address, note, quality_score, source_detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (coin, direction, entry_price, tp1, tp2, tp3, sl_price,
                 leverage, initial_status, order_type, source, whale_address, note,
                 quality_score, source_detail),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_signal(self, sig_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                _SIGNAL_SELECT + " WHERE id = ?",
                (sig_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return dict(zip(_SIGNAL_COLS, row))

    async def get_active_signals(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                _SIGNAL_SELECT + " WHERE status IN ('PENDING','ACTIVE','TP1_HIT','TP2_HIT') AND closed_at IS NULL"
            )
            rows = await cursor.fetchall()
            return [dict(zip(_SIGNAL_COLS, r)) for r in rows]

    async def list_signals(self, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                _SIGNAL_SELECT + " ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(zip(_SIGNAL_COLS, r)) for r in rows]

    async def get_signals_by_status(self, statuses: list[str]) -> list[dict]:
        """Trả về signals theo danh sách status, chưa đóng (closed_at IS NULL)."""
        placeholders = ",".join("?" for _ in statuses)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                _SIGNAL_SELECT + f" WHERE status IN ({placeholders}) AND closed_at IS NULL",
                statuses,
            )
            rows = await cursor.fetchall()
            return [dict(zip(_SIGNAL_COLS, r)) for r in rows]

    async def close_signal_early(
        self,
        signal_id: int,
        close_price: Optional[float],
        reason: str,
        reversal_score: int,
    ):
        """Đóng kèo sớm với lý do cụ thể (timeout / trend_reversal)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE signals SET status='CANCELLED', closed_at=CURRENT_TIMESTAMP, "
                "close_reason=?, close_price=?, reversal_score=? WHERE id=?",
                (reason, close_price, reversal_score, signal_id),
            )
            await db.commit()

    async def update_signal_sl(
        self,
        signal_id: int,
        new_sl: float,
        moved_to_entry: bool = False,
        reason: str = None,
    ):
        """Cập nhật SL price, tuỳ chọn đánh dấu đã dời về entry."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE signals SET sl_price=?, sl_moved_to_entry=?, sl_move_reason=? WHERE id=?",
                (new_sl, int(moved_to_entry), reason, signal_id),
            )
            await db.commit()

    async def count_sl_hits_since(self, since: datetime) -> int:
        """Đếm số kèo SL_HIT kể từ mốc thời gian."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM signals WHERE status='SL_HIT' AND closed_at >= ?",
                (since.isoformat(),),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def update_signal_status(self, sig_id: int, status: str, close: bool = False):
        async with aiosqlite.connect(self.db_path) as db:
            if close:
                _reason_map = {
                    "SL_HIT":    "sl_hit",
                    "TP3_HIT":   "tp3_hit",
                    "CANCELLED": "manual",
                }
                close_reason = _reason_map.get(status, status.lower())
                await db.execute(
                    "UPDATE signals SET status=?, closed_at=CURRENT_TIMESTAMP, close_reason=? WHERE id=?",
                    (status, close_reason, sig_id),
                )
            else:
                await db.execute(
                    "UPDATE signals SET status=? WHERE id=?",
                    (status, sig_id),
                )
            await db.commit()

    async def save_signal_msg(self, sig_id: int, msg_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE signals SET channel_msg_id=? WHERE id=?",
                (msg_id, sig_id),
            )
            await db.commit()

    async def has_active_signal(self, coin: str) -> bool:
        """Trả về True nếu đã có kèo đang mở cho coin này (bất kể chiều)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM signals "
                "WHERE coin=? "
                "AND status IN ('PENDING','ACTIVE') "
                "AND closed_at IS NULL "
                "LIMIT 1",
                (coin,),
            )
            return await cursor.fetchone() is not None

    async def get_signal_stats(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM signals GROUP BY status"
            )
            rows = await cursor.fetchall()
            counts = {r[0]: r[1] for r in rows}
            total = sum(counts.values())
            wins = counts.get("TP1_HIT", 0) + counts.get("TP2_HIT", 0) + counts.get("TP3_HIT", 0)
            losses = counts.get("SL_HIT", 0)
            active = counts.get("ACTIVE", 0)
            return {
                "total": total,
                "wins": wins,
                "losses": losses,
                "active": active,
                "tp1": counts.get("TP1_HIT", 0),
                "tp2": counts.get("TP2_HIT", 0),
                "tp3": counts.get("TP3_HIT", 0),
                "cancelled": counts.get("CANCELLED", 0),
            }

    async def get_signal_stats_by_source(self) -> list[dict]:
        """Win/loss breakdown theo từng nguồn kèo (WHALE, SCAN, ECOSYSTEM, TV, ADMIN, OKX…)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT
                    source,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('TP1_HIT','TP2_HIT','TP3_HIT') THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN status = 'SL_HIT' THEN 1 ELSE 0 END) AS losses
                FROM signals
                WHERE status IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT')
                GROUP BY source
                ORDER BY total DESC
                """
            )
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                source, total, wins, losses = r[0], r[1], r[2], r[3]
                closed = wins + losses
                win_rate = wins / closed * 100 if closed > 0 else 0.0
                result.append({
                    "source": source or "UNKNOWN",
                    "total": total,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": win_rate,
                })
            return result

    async def get_closed_signals(self, limit: int = 20) -> list[dict]:
        """Signals đã đóng (SL_HIT hoặc TPx_HIT), sắp xếp mới nhất trước."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id, coin, direction, entry_price, tp1, tp2, tp3, sl_price, "
                "status, source, created_at, closed_at "
                "FROM signals "
                "WHERE status IN ('TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT') "
                "AND closed_at IS NOT NULL "
                "ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            cols = ["id", "coin", "direction", "entry_price", "tp1", "tp2", "tp3",
                    "sl_price", "status", "source", "created_at", "closed_at"]
            return [dict(zip(cols, r)) for r in rows]


db = Database()
