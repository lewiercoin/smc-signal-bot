"""Tests for agents/telegram_editor.py — 14 tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.base_agent import AgentTier, MarketBias
from agents.telegram_editor import TelegramEditor, _MAX_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_input(**overrides) -> dict:
    """Base valid input_data for TelegramEditor."""
    data = {
        "instrument": "EUR_USD",
        "direction": "bullish",
        "entry": 1.0850,
        "stop_loss": 1.0800,
        "take_profits": {"tp1": 1.09250, "tp2": 1.09750, "tp3": 1.10500},
        "position_size_lots": 0.05,
        "confluence_score": 72,
        "setup_type": "OB_FVG",
        "structure_bias": "bullish",
        "fundamental_bias": "neutral",
        "risk_notes": [],
        "session": "London",
        "atr": 0.0045,
    }
    data.update(overrides)
    return data


def _make_editor(api_response: str | None = None) -> TelegramEditor:
    """Returns TelegramEditor with mocked api_client."""
    mock_client = MagicMock()
    if api_response is not None:
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=api_response)]
        mock_client.messages.create.return_value = mock_response
    else:
        mock_client.messages.create.side_effect = RuntimeError("API unavailable")
    return TelegramEditor(api_client=mock_client)


# ---------------------------------------------------------------------------
# Tests: agent_name and config
# ---------------------------------------------------------------------------

class TestTelegramEditorMeta:
    def test_agent_name(self):
        editor = TelegramEditor()
        assert editor.agent_name == "telegram_editor"

    def test_temperature_is_03(self):
        editor = TelegramEditor()
        assert editor.config.temperature == 0.3

    def test_max_tokens_is_512(self):
        editor = TelegramEditor()
        assert editor.config.max_tokens == 512


# ---------------------------------------------------------------------------
# Tests: _build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_build_prompt_do_not_atр_clause(self):
        editor = TelegramEditor()
        system_prompt, _ = editor._build_prompt(_base_input())
        assert "Do NOT include ATR" in system_prompt

    def test_build_prompt_do_not_mention_ai(self):
        editor = TelegramEditor()
        system_prompt, _ = editor._build_prompt(_base_input())
        assert "Do NOT mention AI, bot, algorithm" in system_prompt

    def test_build_prompt_contains_signal_data(self):
        editor = TelegramEditor()
        _, user_prompt = editor._build_prompt(_base_input())
        assert "EUR_USD" in user_prompt
        assert "1.085" in user_prompt   # Python float repr drops trailing zero
        assert "1.08" in user_prompt    # stop_loss 1.0800 → "1.08"
        assert "72/110" in user_prompt


# ---------------------------------------------------------------------------
# Tests: _parse_llm_response
# ---------------------------------------------------------------------------

class TestParseLLMResponse:
    def test_parse_valid_response(self):
        editor = TelegramEditor()
        msg = "🟢 LONG EUR/USD\n▸ Entry: 1.08500\n▸ SL: 1.08000\n⚠️ Not financial advice."
        result = editor._parse_llm_response(msg, _base_input())
        assert result.raw_data["telegram_message"] == msg
        assert result.tier_used == AgentTier.LLM

    def test_parse_empty_response_raises(self):
        editor = TelegramEditor()
        try:
            editor._parse_llm_response("", _base_input())
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_parse_too_long_response_truncated(self):
        editor = TelegramEditor()
        long_msg = "A" * 600 + "\n⚠️ Not financial advice."
        result = editor._parse_llm_response(long_msg, _base_input())
        assert len(result.raw_data["telegram_message"]) <= _MAX_MESSAGE_LENGTH

    def test_parse_missing_disclaimer_appended(self):
        editor = TelegramEditor()
        msg_no_disclaimer = "🟢 LONG EUR/USD\n▸ Entry: 1.08500\n▸ SL: 1.08000"
        result = editor._parse_llm_response(msg_no_disclaimer, _base_input())
        assert "⚠️" in result.raw_data["telegram_message"] or \
               "Not financial advice" in result.raw_data["telegram_message"]


# ---------------------------------------------------------------------------
# Tests: _deterministic_fallback
# ---------------------------------------------------------------------------

class TestDeterministicFallback:
    def test_deterministic_long_eur_usd(self):
        editor = TelegramEditor()
        result = editor._deterministic_fallback(_base_input())
        msg = result.raw_data["telegram_message"]
        assert "🟢" in msg
        assert "LONG" in msg
        assert "EUR/USD" in msg
        assert "1.08500" in msg
        assert "1.08000" in msg

    def test_deterministic_short_btc_usd(self):
        editor = TelegramEditor()
        data = _base_input(
            instrument="BTC_USD",
            direction="bearish",
            entry=87000.0,
            stop_loss=88000.0,
            take_profits={"tp1": 86000.0, "tp2": 84500.0, "tp3": 82000.0},
        )
        result = editor._deterministic_fallback(data)
        msg = result.raw_data["telegram_message"]
        assert "🔴" in msg
        assert "SHORT" in msg
        assert "BTC/USD" in msg

    def test_deterministic_compliance_footer(self):
        editor = TelegramEditor()
        result = editor._deterministic_fallback(_base_input())
        msg = result.raw_data["telegram_message"]
        assert "⚠️" in msg
        assert "Not financial advice" in msg

    def test_deterministic_pips_calculation_eur(self):
        # EUR_USD: abs(1.0850 - 1.0800) / 0.0001 = 50.0 pips
        editor = TelegramEditor()
        result = editor._deterministic_fallback(_base_input())
        msg = result.raw_data["telegram_message"]
        assert "50.0" in msg

    def test_deterministic_r_multiples(self):
        # sl_pips = 50.0, tp1_pips = abs(1.0925 - 1.0850) / 0.0001 = 75.0 → 75/50 = 1.5R
        editor = TelegramEditor()
        result = editor._deterministic_fallback(_base_input())
        msg = result.raw_data["telegram_message"]
        assert "1.5R" in msg

    def test_deterministic_xau_price_format(self):
        # XAU_USD prices should be in .2f format
        editor = TelegramEditor()
        data = _base_input(
            instrument="XAU_USD",
            entry=2650.50,
            stop_loss=2620.00,
            take_profits={"tp1": 2704.50, "tp2": 2755.50, "tp3": 2831.00},
        )
        result = editor._deterministic_fallback(data)
        msg = result.raw_data["telegram_message"]
        assert "2650.50" in msg

    def test_deterministic_never_raises_on_bad_input(self):
        editor = TelegramEditor()
        result = editor._deterministic_fallback({})
        assert isinstance(result.raw_data.get("telegram_message"), str)
        assert len(result.raw_data["telegram_message"]) > 0


# ---------------------------------------------------------------------------
# Tests: bias mapping
# ---------------------------------------------------------------------------

class TestBiasMapping:
    def test_bias_matches_direction_long(self):
        editor = _make_editor(api_response=None)
        result = editor.analyze(_base_input(direction="bullish"))
        assert result.bias == MarketBias.BULLISH

    def test_bias_matches_direction_short(self):
        editor = _make_editor(api_response=None)
        result = editor.analyze(_base_input(direction="bearish"))
        assert result.bias == MarketBias.BEARISH


# ---------------------------------------------------------------------------
# Tests: 3-tier fallback behaviour
# ---------------------------------------------------------------------------

class TestThreeTierFallback:
    def test_llm_failure_uses_deterministic(self):
        editor = _make_editor(api_response=None)  # API raises RuntimeError
        result = editor.analyze(_base_input())
        assert result.tier_used == AgentTier.DETERMINISTIC
        assert "telegram_message" in result.raw_data

    def test_cache_hit_reuses_result(self):
        valid_msg = "🟢 LONG EUR/USD\n▸ Entry: 1.08500\n⚠️ Not financial advice."
        editor = _make_editor(api_response=valid_msg)
        data = _base_input()
        editor.analyze(data)
        result2 = editor.analyze(data)
        assert result2.tier_used == AgentTier.CACHE

    def test_message_length_under_limit(self):
        editor = _make_editor(api_response=None)  # deterministic fallback
        result = editor.analyze(_base_input())
        msg = result.raw_data.get("telegram_message", "")
        assert len(msg) <= _MAX_MESSAGE_LENGTH
