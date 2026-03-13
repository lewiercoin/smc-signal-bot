"""SQLite database module for SMC Signal Bot.

Handles signals, candles, OB quality log, and optimizer log tables.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class Database:
    """SQLite database handler for SMC Signal Bot.

    Manages signals, candles, OB quality, and optimizer logs.
    Thread-safe via sqlite3 connection per instance.
    """

    def __init__(self, db_path: str = "signals.db") -> None:
        """Initialize database connection.

        Args:
            db_path: Path to SQLite database file. Use ":memory:" for in-memory.
        """
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.logger = logger.bind(module="database", db_path=db_path)
        self.logger.info("database_initialized")

    def initialize(self) -> None:
        """Create tables if they do not exist.

        Tables: signals, candles, ob_quality_log, optimizer_log
        """
        cursor = self.conn.cursor()

        # Signals table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_uuid TEXT,
                instrument TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL,
                sl_price REAL,
                tp1_price REAL,
                tp2_price REAL,
                tp3_price REAL,
                confluence_score INTEGER,
                session TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'OPEN',
                closed_at TIMESTAMP,
                closed_price REAL,
                pnl_r REAL
            )
            """
        )

        # Candles table (for OHLCV storage)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                instrument TEXT NOT NULL,
                granularity TEXT NOT NULL,
                time TIMESTAMP NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER,
                complete BOOLEAN DEFAULT 1,
                PRIMARY KEY (instrument, granularity, time)
            )
            """
        )

        # OB Quality Log table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ob_quality_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                ob_age_bars INTEGER,
                ob_touches INTEGER,
                ob_size_atr REAL,
                passed BOOLEAN,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
            """
        )

        # Optimizer Log table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS optimizer_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start DATE NOT NULL,
                instrument TEXT NOT NULL,
                win_rate REAL,
                profit_factor REAL,
                avg_r REAL,
                signal_count INTEGER,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Index for faster candle queries
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_candles_instrument_time
            ON candles(instrument, granularity, time)
            """
        )

        # Index for signals queries
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_signals_instrument_status
            ON signals(instrument, status)
            """
        )

        # Migration: add signal_uuid column if it doesn't exist (Strategy A)
        try:
            cursor.execute("ALTER TABLE signals ADD COLUMN signal_uuid TEXT")
            self.conn.commit()
            self.logger.info("migrated_signal_uuid_column")
        except sqlite3.OperationalError:
            pass  # Column already exists

        self.conn.commit()
        self.logger.info("database_tables_initialized")

    def save_signal(self, signal: dict[str, Any]) -> int:
        """Insert new signal and return its rowid.

        Args:
            signal: Dictionary with signal data. Required: instrument, direction.

        Returns:
            Row ID of inserted signal
        """
        cursor = self.conn.cursor()

        cursor.execute(
            """
            INSERT INTO signals (
                signal_uuid, instrument, direction, entry_price, sl_price,
                tp1_price, tp2_price, tp3_price, confluence_score, session,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                signal.get("signal_uuid"),
                signal.get("instrument"),
                signal.get("direction"),
                signal.get("entry_price"),
                signal.get("sl_price"),
                signal.get("tp1_price"),
                signal.get("tp2_price"),
                signal.get("tp3_price"),
                signal.get("confluence_score"),
                signal.get("session"),
                signal.get("status", "OPEN"),
                signal.get("created_at"),
            ),
        )

        self.conn.commit()
        signal_id = cursor.lastrowid
        self.logger.info("signal_saved", signal_id=signal_id, instrument=signal.get("instrument"))
        return signal_id

    def update_signal_status(
        self,
        signal_id: int,
        status: str,
        closed_price: float,
        pnl_r: float,
    ) -> None:
        """Update signal status on close.

        Args:
            signal_id: ID of signal to update
            status: New status (TP1, TP2, TP3, SL, BE, EXPIRED)
            closed_price: Price at close
            pnl_r: PnL in R multiples
        """
        cursor = self.conn.cursor()

        cursor.execute(
            """
            UPDATE signals
            SET status = ?, closed_price = ?, pnl_r = ?, closed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, closed_price, pnl_r, signal_id),
        )

        self.conn.commit()
        self.logger.info(
            "signal_status_updated",
            signal_id=signal_id,
            status=status,
            pnl_r=pnl_r,
        )

    def update_signal_status_by_uuid(
        self,
        signal_uuid: str,
        status: str,
        closed_price: float,
        pnl_r: float,
    ) -> None:
        """Update signal status by UUID.

        Args:
            signal_uuid: UUID of signal to update
            status: New status (TP1, TP2, TP3, SL, BE, EXPIRED)
            closed_price: Price at close
            pnl_r: PnL in R multiples
        """
        cursor = self.conn.cursor()

        cursor.execute(
            """
            UPDATE signals
            SET status = ?, closed_price = ?, pnl_r = ?, closed_at = CURRENT_TIMESTAMP
            WHERE signal_uuid = ?
            """,
            (status, closed_price, pnl_r, signal_uuid),
        )

        self.conn.commit()
        self.logger.info(
            "signal_status_updated_by_uuid",
            signal_uuid=signal_uuid,
            status=status,
            pnl_r=pnl_r,
        )

    def get_signal_by_uuid(self, signal_uuid: str) -> dict[str, Any] | None:
        """Retrieve a single signal by its UUID.

        Args:
            signal_uuid: The UUID string from Signal.id

        Returns:
            Signal dict or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM signals WHERE signal_uuid = ?",
            (signal_uuid,),
        )
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def get_signals(
        self,
        instrument: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Retrieve signals with optional filtering.

        Args:
            instrument: Filter by instrument (optional)
            limit: Maximum rows to return

        Returns:
            List of signal dictionaries
        """
        cursor = self.conn.cursor()

        if instrument:
            cursor.execute(
                """
                SELECT * FROM signals
                WHERE instrument = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (instrument, limit),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM signals
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_open_signals(self) -> list[dict[str, Any]]:
        """Retrieve all currently open signals for portfolio correlation checks.

        Returns:
            List of signal dicts with status 'OPEN'.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM signals WHERE status = 'OPEN' ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_closed_signals(
        self,
        days: int = 28,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Retrieve closed signals for Optimizer analysis.

        Args:
            days: Look-back window in days (default 28 = 4 weeks).
            limit: Maximum rows to return.

        Returns:
            List of closed signal dicts ordered oldest-first.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM signals
            WHERE status NOT IN ('OPEN', 'PENDING')
              AND closed_at IS NOT NULL
              AND closed_at >= datetime('now', ? || ' days')
            ORDER BY closed_at ASC
            LIMIT ?
            """,
            (f"-{days}", limit),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def save_candles(self, candles: list[dict[str, Any]]) -> None:
        """Upsert candles (INSERT OR REPLACE).

        Args:
            candles: List of candle dictionaries with keys:
                instrument, granularity, time, open, high, low, close, volume, complete
        """
        if not candles:
            return

        cursor = self.conn.cursor()

        for candle in candles:
            cursor.execute(
                """
                INSERT OR REPLACE INTO candles (
                    instrument, granularity, time, open, high, low, close, volume, complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candle.get("instrument"),
                    candle.get("granularity"),
                    candle.get("time"),
                    candle.get("open"),
                    candle.get("high"),
                    candle.get("low"),
                    candle.get("close"),
                    candle.get("volume"),
                    candle.get("complete", True),
                ),
            )

        self.conn.commit()
        self.logger.info("candles_saved", count=len(candles))

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()
        self.logger.info("database_closed")
