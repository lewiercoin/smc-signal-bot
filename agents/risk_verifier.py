"""Risk Verifier Agent — Agent 3 w pipeline (deterministyczny, BEZ LLM).

Sizing decision: Scenariusz A.
RiskEngine (engine/risk_engine.py) oblicza lots z uwzględnieniem 2% risk rule
(risk_amount = balance * 0.02, lots = risk_amount / (sl_pips * pip_value_per_lot)).
RiskVerifier waliduje zgodność: lots * pip_value_per_lot * sl_pips / account_balance ≤ 0.02.
Jeśli niezgodne → skaluje w dół do 2% risk i loguje ostrzeżenie.

PIP_VALUES: zdefiniowane lokalnie — risk_verifier nie importuje z engine/risk_engine
aby uniknąć circular dependency (engine może w przyszłości importować agents).
Wartości identyczne jak PIP_VALUES w engine/risk_engine.py.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# PIP_VALUES: lokalnie zdefiniowane (identyczne jak engine/risk_engine.PIP_VALUES)
# Powód: unikamy circular import (agents ← engine ← agents w przyszłości)
_PIP_VALUES: dict[str, dict[str, float]] = {
    "EUR_USD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "XAU_USD": {"pip_size": 0.01, "pip_value_per_lot": 1.0},
    "BTC_USD": {"pip_size": 1.0, "pip_value_per_lot": 1.0},
}

# Macierz korelacji — statyczna Faza 1 (3 instrumenty)
# Próg: 0.60 (nie 0.75) — przy 0.75 żadna obecna para nie byłaby blokowana (martwy kod).
# 0.60 łapie przyszłe pary (GBP_USD↔EUR_USD ~0.85) i duplikaty (ten sam instrument = 1.0).
_CORRELATION_MATRIX: dict[tuple[str, str], float] = {
    ("EUR_USD", "XAU_USD"): 0.40,
    ("EUR_USD", "BTC_USD"): 0.20,
    ("XAU_USD", "BTC_USD"): 0.30,
}

_CORRELATION_THRESHOLD = 0.60
_DAILY_LOSS_HARD = 0.05   # 5% → block
_DAILY_LOSS_WARN = 0.03   # 3% → warning only
_MAX_POSITIONS = 3
_MAX_RISK_PCT = 0.02      # 2% risk rule (Scenariusz A)
_MIN_LOT_SIZE = 0.01
_MAX_LOT_SIZE = 10.0


@dataclass(frozen=True)
class RiskVerifierResult:
    """Wynik weryfikacji ryzyka — czysto deterministyczny."""

    risk_approved: bool
    position_size: float              # finalna liczba lotów (może być skalowana w dół)
    risk_notes: list[str] = field(default_factory=list)
    rejection_reason: str = ""
    spread_z_score: float | None = None
    portfolio_corr_blocked: bool = False
    circuit_breaker_hit: str = ""     # "" | "daily_loss" | "max_positions"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class RiskVerifier:
    """Weryfikuje ryzyko sygnału — deterministyczny Agent 3.

    NIE dziedziczy z BaseAgent. Brak LLM, brak cache, brak fallback.
    Publiczna metoda: verify(input_data: dict) -> RiskVerifierResult.
    Nigdy nie rzuca wyjątku na zewnątrz.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger().bind(agent="risk_verifier")

    # ── Public interface ──────────────────────────────────────────────────────

    def verify(self, input_data: dict[str, Any]) -> RiskVerifierResult:
        """Weryfikuje ryzyko sygnału. Nigdy nie rzuca wyjątku.

        Args:
            input_data: słownik z parametrami sygnału.

        Returns:
            RiskVerifierResult z risk_approved, position_size i risk_notes.
        """
        try:
            return self._verify_internal(input_data)
        except Exception as exc:
            self._log.error("risk_verifier_internal_error", error=str(exc))
            return RiskVerifierResult(
                risk_approved=False,
                position_size=0.0,
                risk_notes=[f"Internal error: {exc}"],
                rejection_reason=f"Internal error: {exc}",
                spread_z_score=None,
                portfolio_corr_blocked=False,
                circuit_breaker_hit="",
                timestamp=datetime.now(timezone.utc),
            )

    # ── Internal logic ────────────────────────────────────────────────────────

    def _verify_internal(self, input_data: dict[str, Any]) -> RiskVerifierResult:
        """Wewnętrzna logika weryfikacji (może rzucać wyjątki — opakowane przez verify())."""
        instrument: str = input_data.get("instrument", "")
        direction: str = input_data.get("direction", "")
        entry: float = float(input_data.get("entry", 0.0))
        stop_loss: float = float(input_data.get("stop_loss", 0.0))
        position_size_lots: float = float(input_data.get("position_size_lots", 0.01))
        account_balance: float = float(input_data.get("account_balance", 10000.0))
        current_spread: float | None = input_data.get("current_spread")
        spread_history: list[float] | None = input_data.get("spread_history")
        open_positions: list[dict] = input_data.get("open_positions", [])
        daily_loss_pct: float = float(input_data.get("daily_loss_pct", 0.0))
        confluence_score: int = int(input_data.get("confluence_score", 0))

        risk_notes: list[str] = []
        circuit_breaker_hit = ""
        portfolio_corr_blocked = False
        risk_approved = True
        rejection_reason = ""

        # ── Sprawdzenie 1: Circuit Breaker — dzienna strata ──────────────────
        if daily_loss_pct >= _DAILY_LOSS_HARD:
            circuit_breaker_hit = "daily_loss"
            risk_approved = False
            rejection_reason = "Daily loss limit reached (≥5%)"
            self._log.warning(
                "circuit_breaker_daily_loss_hard",
                daily_loss_pct=daily_loss_pct,
                instrument=instrument,
            )
        elif daily_loss_pct >= _DAILY_LOSS_WARN:
            pct_str = f"{daily_loss_pct * 100:.1f}%"
            risk_notes.append(
                f"WARNING: Daily loss at {pct_str}, approaching 5% limit"
            )
            self._log.info(
                "daily_loss_soft_warning",
                daily_loss_pct=daily_loss_pct,
                instrument=instrument,
            )

        # ── Sprawdzenie 2: Circuit Breaker — max równoległe pozycje ─────────
        if risk_approved and len(open_positions) >= _MAX_POSITIONS:
            circuit_breaker_hit = "max_positions"
            risk_approved = False
            rejection_reason = "Maximum 3 concurrent positions reached"
            self._log.warning(
                "circuit_breaker_max_positions",
                open_count=len(open_positions),
                instrument=instrument,
            )

        # ── Sprawdzenie 3: Korelacja portfela ────────────────────────────────
        if risk_approved:
            corr_rejection = self._check_portfolio_correlation(
                instrument, direction, open_positions
            )
            if corr_rejection is not None:
                portfolio_corr_blocked = True
                risk_approved = False
                rejection_reason = corr_rejection
                self._log.warning(
                    "portfolio_correlation_blocked",
                    instrument=instrument,
                    direction=direction,
                    reason=corr_rejection,
                )

        # ── Sprawdzenie 4: Spread z-score (informacyjny, NIE blokuje) ────────
        spread_z_score = self._calculate_spread_zscore(
            current_spread, spread_history, risk_notes
        )

        # ── Sprawdzenie 5: Sizing validation (Scenariusz A) ──────────────────
        position_size_lots = self._validate_sizing(
            position_size_lots, entry, stop_loss, account_balance,
            instrument, risk_notes
        )

        self._log.info(
            "risk_verification_complete",
            instrument=instrument,
            direction=direction,
            risk_approved=risk_approved,
            circuit_breaker_hit=circuit_breaker_hit or None,
            portfolio_corr_blocked=portfolio_corr_blocked,
            position_size=position_size_lots,
            confluence_score=confluence_score,
        )

        return RiskVerifierResult(
            risk_approved=risk_approved,
            position_size=position_size_lots,
            risk_notes=risk_notes,
            rejection_reason=rejection_reason,
            spread_z_score=spread_z_score,
            portfolio_corr_blocked=portfolio_corr_blocked,
            circuit_breaker_hit=circuit_breaker_hit,
            timestamp=datetime.now(timezone.utc),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_correlation(self, instrument_a: str, instrument_b: str) -> float:
        """Zwraca korelację między dwoma instrumentami (symetryczna macierz)."""
        if instrument_a == instrument_b:
            return 1.0
        key = (instrument_a, instrument_b)
        if key in _CORRELATION_MATRIX:
            return _CORRELATION_MATRIX[key]
        key_rev = (instrument_b, instrument_a)
        if key_rev in _CORRELATION_MATRIX:
            return _CORRELATION_MATRIX[key_rev]
        return 0.0

    def _check_portfolio_correlation(
        self,
        new_instrument: str,
        new_direction: str,
        open_positions: list[dict],
    ) -> str | None:
        """Sprawdza korelację portfela. Zwraca rejection_reason lub None jeśli OK."""
        for pos in open_positions:
            existing_instrument = str(pos.get("instrument", ""))
            existing_direction = str(pos.get("direction", ""))

            if new_instrument == existing_instrument:
                if new_direction == existing_direction:
                    return (
                        f"Duplicate signal: {new_instrument} {new_direction} "
                        f"already in portfolio (corr=1.0)"
                    )
                # Hedging (przeciwny kierunek) — OK
                continue

            corr = self._get_correlation(new_instrument, existing_instrument)
            if corr > _CORRELATION_THRESHOLD and new_direction == existing_direction:
                return (
                    f"High correlation {corr:.2f} between {new_instrument} and "
                    f"{existing_instrument} with same direction ({new_direction})"
                )

        return None

    def _calculate_spread_zscore(
        self,
        current_spread: float | None,
        spread_history: list[float] | None,
        risk_notes: list[str],
    ) -> float | None:
        """Oblicza z-score spreadu. Dodaje ostrzeżenie do risk_notes jeśli > 2.0."""
        if (
            spread_history is None
            or len(spread_history) < 5
            or current_spread is None
        ):
            return None

        mean = statistics.mean(spread_history)
        try:
            std = statistics.stdev(spread_history)
        except statistics.StatisticsError:
            return None

        if std == 0.0:
            z_score = 0.0
        else:
            z_score = (current_spread - mean) / std

        if z_score > 2.0:
            risk_notes.append(
                f"WARNING: Spread z-score {z_score:.2f} — elevated spread"
            )
            self._log.info(
                "spread_zscore_elevated",
                z_score=z_score,
                current_spread=current_spread,
                spread_mean=mean,
            )

        return z_score

    def _validate_sizing(
        self,
        lots: float,
        entry: float,
        stop_loss: float,
        account_balance: float,
        instrument: str,
        risk_notes: list[str],
    ) -> float:
        """Scenariusz A: waliduje zgodność z 2% risk rule. Skaluje w dół jeśli niezgodne."""
        pip_config = _PIP_VALUES.get(instrument, {"pip_size": 0.0001, "pip_value_per_lot": 10.0})
        pip_size = pip_config["pip_size"]
        pip_value_per_lot = pip_config["pip_value_per_lot"]

        sl_distance = abs(entry - stop_loss)
        if pip_size == 0.0:
            return lots

        sl_pips = sl_distance / pip_size
        if sl_pips == 0.0:
            return lots

        risk_dollar = lots * pip_value_per_lot * sl_pips
        if account_balance <= 0.0:
            return lots

        risk_pct = risk_dollar / account_balance

        if risk_pct > _MAX_RISK_PCT:
            max_risk_dollar = account_balance * _MAX_RISK_PCT
            new_lots = max_risk_dollar / (pip_value_per_lot * sl_pips)
            new_lots = max(_MIN_LOT_SIZE, min(_MAX_LOT_SIZE, round(new_lots, 2)))
            risk_notes.append(
                f"ALERT: Position size exceeds 2% risk rule — recalculated "
                f"from {lots:.2f} to {new_lots:.2f} lots"
            )
            self._log.info(
                "sizing_scaled_down",
                original_lots=lots,
                new_lots=new_lots,
                risk_pct=risk_pct,
                instrument=instrument,
            )
            return new_lots

        return lots
