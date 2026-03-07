"""Tests for Database module.

Uses in-memory SQLite database for isolation.
"""

from __future__ import annotations

import pytest

from db.database import Database


@pytest.fixture
def db() -> Database:
    """Create in-memory database instance for testing."""
    database = Database(":memory:")
    database.initialize()
    yield database
    database.close()


class TestInitialize:
    """Test database initialization."""

    def test_tables_created(self, db: Database) -> None:
        """Test that all required tables are created."""
        cursor = db.conn.cursor()

        # Check signals table
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='signals'"
        )
        assert cursor.fetchone() is not None

        # Check candles table
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='candles'"
        )
        assert cursor.fetchone() is not None

        # Check ob_quality_log table
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ob_quality_log'"
        )
        assert cursor.fetchone() is not None

        # Check optimizer_log table
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='optimizer_log'"
        )
        assert cursor.fetchone() is not None

    def test_indexes_created(self, db: Database) -> None:
        """Test that indexes are created."""
        cursor = db.conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_candles_instrument_time'"
        )
        assert cursor.fetchone() is not None

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_signals_instrument_status'"
        )
        assert cursor.fetchone() is not None


class TestSaveSignal:
    """Test save_signal method."""

    def test_save_signal_minimal(self, db: Database) -> None:
        """Test saving signal with minimal data."""
        signal = {
            "instrument": "EUR_USD",
            "direction": "LONG",
        }

        signal_id = db.save_signal(signal)

        assert signal_id > 0

        # Verify saved
        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
        row = cursor.fetchone()
        assert row is not None
        assert row["instrument"] == "EUR_USD"
        assert row["direction"] == "LONG"
        assert row["status"] == "OPEN"

    def test_save_signal_full(self, db: Database) -> None:
        """Test saving signal with all fields."""
        signal = {
            "instrument": "XAU_USD",
            "direction": "SHORT",
            "entry_price": 2000.50,
            "sl_price": 2010.00,
            "tp1_price": 1990.00,
            "tp2_price": 1980.00,
            "tp3_price": 1970.00,
            "confluence_score": 75,
            "session": "London",
            "status": "PENDING",
        }

        signal_id = db.save_signal(signal)

        assert signal_id > 0

        # Verify
        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
        row = cursor.fetchone()
        assert row["entry_price"] == 2000.50
        assert row["confluence_score"] == 75
        assert row["session"] == "London"

    def test_save_multiple_signals(self, db: Database) -> None:
        """Test saving multiple signals."""
        signals = [
            {"instrument": "EUR_USD", "direction": "LONG"},
            {"instrument": "EUR_USD", "direction": "SHORT"},
            {"instrument": "XAU_USD", "direction": "LONG"},
        ]

        ids = [db.save_signal(s) for s in signals]

        assert len(set(ids)) == 3  # All unique
        assert all(id > 0 for id in ids)


class TestUpdateSignalStatus:
    """Test update_signal_status method."""

    def test_update_to_tp1(self, db: Database) -> None:
        """Test updating signal to TP1."""
        signal_id = db.save_signal({"instrument": "EUR_USD", "direction": "LONG"})

        db.update_signal_status(signal_id, "TP1", 1.1050, 1.5)

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
        row = cursor.fetchone()

        assert row["status"] == "TP1"
        assert row["closed_price"] == 1.1050
        assert row["pnl_r"] == 1.5
        assert row["closed_at"] is not None

    def test_update_to_sl(self, db: Database) -> None:
        """Test updating signal to SL (stop loss)."""
        signal_id = db.save_signal({"instrument": "XAU_USD", "direction": "SHORT"})

        db.update_signal_status(signal_id, "SL", 2010.00, -1.0)

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
        row = cursor.fetchone()

        assert row["status"] == "SL"
        assert row["pnl_r"] == -1.0

    def test_update_nonexistent_signal(self, db: Database) -> None:
        """Test updating non-existent signal doesn't raise error."""
        db.update_signal_status(99999, "TP1", 1.1000, 1.0)

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM signals WHERE id = ?", (99999,))
        assert cursor.fetchone() is None


class TestGetSignals:
    """Test get_signals method."""

    def test_get_all_signals(self, db: Database) -> None:
        """Test retrieving all signals."""
        db.save_signal({"instrument": "EUR_USD", "direction": "LONG"})
        db.save_signal({"instrument": "XAU_USD", "direction": "SHORT"})

        signals = db.get_signals()

        assert len(signals) == 2

    def test_get_by_instrument(self, db: Database) -> None:
        """Test filtering by instrument."""
        db.save_signal({"instrument": "EUR_USD", "direction": "LONG"})
        db.save_signal({"instrument": "EUR_USD", "direction": "SHORT"})
        db.save_signal({"instrument": "XAU_USD", "direction": "LONG"})

        eur_signals = db.get_signals(instrument="EUR_USD")

        assert len(eur_signals) == 2
        assert all(s["instrument"] == "EUR_USD" for s in eur_signals)

    def test_get_signals_limit(self, db: Database) -> None:
        """Test limit parameter."""
        for i in range(5):
            db.save_signal({"instrument": "EUR_USD", "direction": "LONG"})

        signals = db.get_signals(limit=3)

        assert len(signals) == 3

    def test_get_signals_order(self, db: Database) -> None:
        """Test that signals are ordered by created_at DESC."""
        db.save_signal({
            "instrument": "EUR_USD",
            "direction": "LONG",
            "created_at": "2024-01-01 10:00:00",
        })
        db.save_signal({
            "instrument": "XAU_USD",
            "direction": "SHORT",
            "created_at": "2024-01-01 11:00:00",
        })

        signals = db.get_signals()

        # Most recent first
        assert signals[0]["instrument"] == "XAU_USD"
        assert signals[1]["instrument"] == "EUR_USD"


class TestSaveCandles:
    """Test save_candles method."""

    def test_save_single_candle(self, db: Database) -> None:
        """Test saving single candle."""
        candles = [
            {
                "instrument": "EUR_USD",
                "granularity": "H1",
                "time": "2024-01-01 12:00:00",
                "open": 1.1000,
                "high": 1.1050,
                "low": 1.0950,
                "close": 1.1020,
                "volume": 1000,
                "complete": True,
            }
        ]

        db.save_candles(candles)

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM candles WHERE instrument = ?", ("EUR_USD",))
        row = cursor.fetchone()

        assert row is not None
        assert row["open"] == 1.1000
        assert row["volume"] == 1000

    def test_save_multiple_candles(self, db: Database) -> None:
        """Test saving multiple candles."""
        candles = [
            {
                "instrument": "EUR_USD",
                "granularity": "H1",
                "time": "2024-01-01 12:00:00",
                "open": 1.1000,
                "high": 1.1050,
                "low": 1.0950,
                "close": 1.1020,
                "volume": 1000,
            },
            {
                "instrument": "EUR_USD",
                "granularity": "H1",
                "time": "2024-01-01 13:00:00",
                "open": 1.1020,
                "high": 1.1080,
                "low": 1.1000,
                "close": 1.1050,
                "volume": 1500,
            },
        ]

        db.save_candles(candles)

        cursor = db.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM candles")
        count = cursor.fetchone()[0]

        assert count == 2

    def test_upsert_existing_candle(self, db: Database) -> None:
        """Test that INSERT OR REPLACE updates existing candle."""
        candle = {
            "instrument": "EUR_USD",
            "granularity": "H1",
            "time": "2024-01-01 12:00:00",
            "open": 1.1000,
            "high": 1.1050,
            "low": 1.0950,
            "close": 1.1020,
            "volume": 1000,
        }

        db.save_candles([candle])

        # Update same candle
        updated_candle = {
            "instrument": "EUR_USD",
            "granularity": "H1",
            "time": "2024-01-01 12:00:00",
            "open": 1.1000,
            "high": 1.1100,  # Changed
            "low": 1.0950,
            "close": 1.1050,  # Changed
            "volume": 2000,  # Changed
        }

        db.save_candles([updated_candle])

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM candles WHERE instrument = ?", ("EUR_USD",))
        row = cursor.fetchone()

        assert row["high"] == 1.1100
        assert row["close"] == 1.1050
        assert row["volume"] == 2000

    def test_save_empty_list(self, db: Database) -> None:
        """Test saving empty list doesn't error."""
        db.save_candles([])

        cursor = db.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM candles")
        count = cursor.fetchone()[0]

        assert count == 0


class TestSignalLifecycle:
    """Integration test for full signal lifecycle."""

    def test_full_signal_lifecycle(self, db: Database) -> None:
        """Test create -> update -> retrieve signal."""
        # Create signal
        signal_id = db.save_signal({
            "instrument": "EUR_USD",
            "direction": "LONG",
            "entry_price": 1.1000,
            "sl_price": 1.0950,
            "tp1_price": 1.1050,
            "confluence_score": 72,
            "session": "London",
        })

        # Update to TP1
        db.update_signal_status(signal_id, "TP1", 1.1050, 1.0)

        # Retrieve and verify
        signals = db.get_signals(instrument="EUR_USD")

        assert len(signals) == 1
        assert signals[0]["id"] == signal_id
        assert signals[0]["status"] == "TP1"
        assert signals[0]["pnl_r"] == 1.0
        assert signals[0]["confluence_score"] == 72
