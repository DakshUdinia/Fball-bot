"""SQLite database for match/event/trade tracking."""

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = ""


def get_db_path() -> str:
    global DB_PATH
    if not DB_PATH:
        DB_PATH = str(Path(__file__).parent.parent / "data" / "fball_bot.db")
    return DB_PATH


def get_connection() -> sqlite3.Connection:
    path = get_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database() -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS football_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_id INTEGER UNIQUE NOT NULL,
            league TEXT DEFAULT 'World Cup',
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            match_date TEXT,
            status TEXT DEFAULT 'scheduled',
            home_score INTEGER DEFAULT 0,
            away_score INTEGER DEFAULT 0,
            minute INTEGER DEFAULT 0,
            condition_id TEXT,
            yes_token_id TEXT,
            no_token_id TEXT,
            pre_match_price REAL,
            current_yes_price REAL,
            current_no_price REAL,
            is_tracked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS football_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_id INTEGER NOT NULL,
            trade_type TEXT NOT NULL,
            side TEXT NOT NULL,
            token_id TEXT,
            entry_price REAL NOT NULL,
            exit_price REAL,
            size_usd REAL NOT NULL,
            quantity REAL,
            order_id TEXT,
            pnl REAL,
            status TEXT DEFAULT 'open',
            entry_reason TEXT,
            exit_reason TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    conn.close()
    logger.info("Database at %s", get_db_path())


def upsert_match(m: dict[str, Any]) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO football_matches
            (fixture_id, league, home_team, away_team, match_date, status,
             home_score, away_score, minute, condition_id, yes_token_id, no_token_id,
             pre_match_price, current_yes_price, current_no_price, is_tracked)
        VALUES (:fixture_id, :league, :home_team, :away_team, :match_date, :status,
                :home_score, :away_score, :minute, :condition_id, :yes_token_id, :no_token_id,
                :pre_match_price, :current_yes_price, :current_no_price, :is_tracked)
        ON CONFLICT(fixture_id) DO UPDATE SET
            status=excluded.status, home_score=excluded.home_score,
            away_score=excluded.away_score, minute=excluded.minute,
            current_yes_price=excluded.current_yes_price,
            current_no_price=excluded.current_no_price,
            is_tracked=excluded.is_tracked, updated_at=datetime('now')
    """, m)
    conn.commit()
    conn.close()


def get_tracked_matches(status: str | None = None) -> list[dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor()
    if status:
        cur.execute("SELECT * FROM football_matches WHERE is_tracked = 1 AND status = ? ORDER BY match_date", (status,))
    else:
        cur.execute("SELECT * FROM football_matches WHERE is_tracked = 1 ORDER BY match_date")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_open_trades(fixture_id: int | None = None) -> list[dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor()
    if fixture_id:
        cur.execute("SELECT * FROM football_trades WHERE status = 'open' AND fixture_id = ?", (fixture_id,))
    else:
        cur.execute("SELECT * FROM football_trades WHERE status = 'open'")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def insert_trade(t: dict[str, Any]) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO football_trades
            (fixture_id, trade_type, side, token_id, entry_price, size_usd, quantity, order_id, status, entry_reason)
        VALUES (:fixture_id, :trade_type, :side, :token_id, :entry_price, :size_usd, :quantity, :order_id, :status, :entry_reason)
    """, t)
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def close_trade(trade_id: int, exit_price: float, pnl: float, reason: str) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE football_trades SET exit_price=?, pnl=COALESCE(pnl, 0) + ?, status='closed', exit_reason=?, closed_at=datetime('now')
        WHERE id=?
    """, (exit_price, pnl, reason, trade_id))
    conn.commit()
    conn.close()


def update_trade_size(trade_id: int, new_size: float, added_pnl: float, reason: str) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE football_trades SET size_usd=?, pnl=COALESCE(pnl, 0) + ?, exit_reason=?
        WHERE id=?
    """, (new_size, added_pnl, reason, trade_id))
    conn.commit()
    conn.close()


def set_state(key: str, value: str) -> None:
    conn = get_connection()
    conn.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_state(key: str, default: str = "") -> str:
    conn = get_connection()
    row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def get_daily_analytics() -> dict[str, Any]:
    """Calculate PnL and win rate for today's trades."""
    conn = get_connection()
    cur = conn.cursor()
    # Get all closed trades from the last 24 hours
    cur.execute("""
        SELECT pnl FROM football_trades 
        WHERE status = 'closed' 
        AND closed_at >= datetime('now', '-1 day')
    """)
    rows = cur.fetchall()
    conn.close()
    
    trades = len(rows)
    wins = sum(1 for r in rows if r["pnl"] and r["pnl"] > 0)
    total_pnl = sum(r["pnl"] for r in rows if r["pnl"])
    
    return {
        "trades": trades,
        "wins": wins,
        "win_rate": (wins / trades * 100) if trades > 0 else 0.0,
        "pnl": total_pnl,
    }
