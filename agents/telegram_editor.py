"""Telegram Editor Agent — Agent 4 w pipeline (LLM, dziedziczy BaseAgent).

Formatuje sygnał tradingowy na wiadomość Telegram w stylu makuchaku/ICT.
3-tier fallback: Cache → LLM (Claude Haiku, temp=0.3) → Deterministic template.

PIP_VALUES: importowane z engine/risk_engine.PIP_VALUES (istnieje w tym pliku).
Sprawdzono: engine/risk_engine.py zawiera PIP_VALUES dict z pip_size i pip_value_per_lot.
Używamy pip_size do obliczeń pipów w _deterministic_fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agents.base_agent import AgentConfig, AgentResult, AgentTier, BaseAgent, MarketBias
from engine.risk_engine import PIP_VALUES as _ENGINE_PIP_VALUES

# Precyzja formatowania cen per instrument
_PRICE_FORMATS: dict[str, str] = {
    "EUR_USD": ".5f",
    "XAU_USD": ".2f",
    "BTC_USD": ".1f",
}

# pip_size per instrument (wyciągnięte z PIP_VALUES dla czytelności)
_PIP_SIZE: dict[str, float] = {
    instr: cfg["pip_size"] for instr, cfg in _ENGINE_PIP_VALUES.items()
}

_DISCLAIMER = "⚠️ Not financial advice. Trading involves risk."
_MAX_MESSAGE_LENGTH = 500


class TelegramEditor(BaseAgent):
    """Formatuje sygnał tradingowy na wiadomość Telegram — Agent 4."""

    def __init__(self, api_client: object = None) -> None:
        config = AgentConfig(
            temperature=0.3,
            max_tokens=512,
        )
        super().__init__(config=config, api_client=api_client)

    @property
    def agent_name(self) -> str:
        return "telegram_editor"

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _build_prompt(self, input_data: dict) -> tuple[str, str]:
        """Buduje system prompt i user prompt. Zwraca (system, user)."""
        system_prompt = (
            "You are a Telegram signal editor for an ICT/SMC trading channel.\n"
            "\n"
            "Format the given trade signal into a clean Telegram message using this EXACT structure:\n"
            "\n"
            "1. First line: emoji + direction + instrument (e.g., \"🟢 LONG EUR/USD\")\n"
            "2. Entry, SL, TP lines with precise prices\n"
            "3. \"Confluences:\" section listing the setup factors\n"
            "4. Score line showing confluence score\n"
            "5. Disclaimer footer: \"⚠️ Not financial advice. Trading involves risk.\"\n"
            "\n"
            "RULES:\n"
            "- Use 🟢 for LONG, 🔴 for SHORT\n"
            "- Show pips distance for SL and each TP\n"
            "- Show R:R ratio for each TP (e.g., \"1.5R\")\n"
            "- List confluences as bullet points (▸ OB, ▸ FVG, ▸ BOS, ▸ Session, etc.)\n"
            "- Keep message under 500 characters total\n"
            "- Write in English\n"
            "- Do NOT include ATR values, lot sizes, or account details — too technical for channel\n"
            "- Do NOT add personal commentary, predictions, or emojis beyond 🟢/🔴/⚠️\n"
            "- Do NOT mention AI, bot, algorithm, or automated system\n"
            "\n"
            "Respond ONLY with the formatted Telegram message. No markdown code blocks, no explanation."
        )

        instrument: str = input_data.get("instrument", "")
        direction: str = input_data.get("direction", "")
        entry: float = float(input_data.get("entry", 0.0))
        stop_loss: float = float(input_data.get("stop_loss", 0.0))
        tps: dict = input_data.get("take_profits", {})
        tp1: float = float(tps.get("tp1", 0.0))
        tp2: float = float(tps.get("tp2", 0.0))
        tp3: float = float(tps.get("tp3", 0.0))
        setup_type: str = input_data.get("setup_type", "")
        structure_bias: str = input_data.get("structure_bias", "")
        fundamental_bias: str = input_data.get("fundamental_bias", "")
        confluence_score: int = int(input_data.get("confluence_score", 0))
        session: str = input_data.get("session", "")
        risk_notes: list = input_data.get("risk_notes", [])

        risk_notes_formatted = ", ".join(risk_notes) if risk_notes else "None"

        user_prompt = (
            f"Format this signal:\n"
            f"Instrument: {instrument}\n"
            f"Direction: {direction}\n"
            f"Entry: {entry}\n"
            f"Stop Loss: {stop_loss}\n"
            f"TP1: {tp1} | TP2: {tp2} | TP3: {tp3}\n"
            f"Setup: {setup_type}\n"
            f"Confluences: Structure={structure_bias}, Fundamental={fundamental_bias}\n"
            f"Score: {confluence_score}/110\n"
            f"Session: {session}\n"
            f"Risk notes: {risk_notes_formatted}"
        )

        return system_prompt, user_prompt

    # ── Parse LLM response ────────────────────────────────────────────────────

    def _parse_llm_response(self, response_text: str, input_data: dict) -> AgentResult:
        """Parsuje odpowiedź LLM (plain string) na AgentResult."""
        if not response_text or not response_text.strip():
            raise ValueError("Empty LLM response for telegram_editor")

        message = response_text.strip()

        # Truncate jeśli za długa
        if len(message) > _MAX_MESSAGE_LENGTH:
            message = message[: _MAX_MESSAGE_LENGTH - 3] + "..."

        # Dopisz disclaimer jeśli brak
        if "⚠️" not in message and "Not financial advice" not in message:
            if len(message) + len(_DISCLAIMER) + 1 <= _MAX_MESSAGE_LENGTH:
                message = message + "\n" + _DISCLAIMER
            else:
                available = _MAX_MESSAGE_LENGTH - len(_DISCLAIMER) - 1
                message = message[:available] + "\n" + _DISCLAIMER

        direction: str = input_data.get("direction", "")
        bias = self._direction_to_bias(direction)

        return AgentResult(
            agent_name=self.agent_name,
            tier_used=AgentTier.LLM,
            bias=bias,
            confidence=0.8,
            reasoning="LLM-formatted Telegram message",
            timestamp=datetime.now(timezone.utc),
            raw_data={"telegram_message": message},
        )

    # ── Deterministic fallback ────────────────────────────────────────────────

    def _deterministic_fallback(self, input_data: dict) -> AgentResult:
        """Tier 3: generuje template bez LLM. Nigdy nie rzuca wyjątku."""
        direction: str = input_data.get("direction", "bullish")
        bias = self._direction_to_bias(direction)

        try:
            message = self._build_template(input_data)
        except Exception:
            instrument: str = input_data.get("instrument", "")
            entry = input_data.get("entry", "")
            stop_loss = input_data.get("stop_loss", "")
            emoji = "🟢" if direction == "bullish" else "🔴"
            dir_str = "LONG" if direction == "bullish" else "SHORT"
            message = (
                f"{emoji} {dir_str} {instrument}\n"
                f"Entry: {entry} | SL: {stop_loss}\n"
                f"{_DISCLAIMER}"
            )

        return AgentResult(
            agent_name=self.agent_name,
            tier_used=AgentTier.DETERMINISTIC,
            bias=bias,
            confidence=0.5,
            reasoning="Deterministic fallback template",
            timestamp=datetime.now(timezone.utc),
            raw_data={"telegram_message": message},
        )

    # ── Template builder ──────────────────────────────────────────────────────

    def _build_template(self, input_data: dict) -> str:
        """Buduje template Telegram message programatycznie."""
        instrument: str = input_data.get("instrument", "")
        direction: str = input_data.get("direction", "bullish")
        entry: float = float(input_data.get("entry", 0.0))
        stop_loss: float = float(input_data.get("stop_loss", 0.0))
        tps: dict = input_data.get("take_profits", {})
        tp1: float = float(tps.get("tp1", 0.0))
        tp2: float = float(tps.get("tp2", 0.0))
        tp3: float = float(tps.get("tp3", 0.0))
        confluence_score: int = int(input_data.get("confluence_score", 0))
        session: str = input_data.get("session", "")

        emoji = "🟢" if direction == "bullish" else "🔴"
        dir_str = "LONG" if direction == "bullish" else "SHORT"
        instrument_display = instrument.replace("_", "/")

        price_fmt = _PRICE_FORMATS.get(instrument, ".5f")
        pip_size = _PIP_SIZE.get(instrument, 0.0001)

        def fmt(price: float) -> str:
            return format(price, price_fmt)

        def pips(a: float, b: float) -> float:
            if pip_size == 0.0:
                return 0.0
            return round(abs(a - b) / pip_size, 1)

        sl_pips = pips(entry, stop_loss)
        if sl_pips == 0.0:
            sl_pips = 1.0  # ochrona przed division by zero

        tp1_pips = pips(tp1, entry)
        tp2_pips = pips(tp2, entry)
        tp3_pips = pips(tp3, entry)

        tp1_r = round(tp1_pips / sl_pips, 1)
        tp2_r = round(tp2_pips / sl_pips, 1)
        tp3_r = round(tp3_pips / sl_pips, 1)

        sl_sign = "-" if direction == "bullish" else "+"
        tp_sign = "+" if direction == "bullish" else "-"

        lines = [
            f"{emoji} {dir_str} {instrument_display}",
            "",
            f"▸ Entry: {fmt(entry)}",
            f"▸ SL:    {fmt(stop_loss)}  ({sl_sign}{sl_pips} pips)",
            f"▸ TP1:   {fmt(tp1)}  ({tp_sign}{tp1_pips} pips | {tp1_r}R)",
            f"▸ TP2:   {fmt(tp2)}  ({tp_sign}{tp2_pips} pips | {tp2_r}R)",
            f"▸ TP3:   {fmt(tp3)}  ({tp_sign}{tp3_pips} pips | {tp3_r}R)",
            "",
            f"Score: {confluence_score}/110 | {session}",
            _DISCLAIMER,
        ]

        message = "\n".join(lines)

        if len(message) > _MAX_MESSAGE_LENGTH:
            message = message[: _MAX_MESSAGE_LENGTH - 3] + "..."

        return message

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _direction_to_bias(direction: str) -> MarketBias:
        """Mapuje direction string na MarketBias."""
        if direction == "bullish":
            return MarketBias.BULLISH
        if direction == "bearish":
            return MarketBias.BEARISH
        return MarketBias.NEUTRAL
