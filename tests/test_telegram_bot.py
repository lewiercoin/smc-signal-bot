"""Tests for bot/telegram_bot.py — 12 testów."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.telegram_bot import TelegramBot
from engine.signal_generator import Signal


# ── Fixtures ──────────────────────────────────────────────────────────────────


def mock_signal(
    pair: str = "EUR_USD",
    direction: str = "bullish",
    confluence_score: int = 75,
) -> Signal:
    """Tworzy realistyczny Signal dataclass do testów."""
    return Signal(
        id=str(uuid.uuid4()),
        pair=pair,
        timeframe="H1",
        direction=direction,
        timestamp=datetime.now(timezone.utc),
        entry=1.08500,
        stop_loss=1.08200,
        take_profit_1=1.08950,
        take_profit_2=1.09250,
        take_profit_3=1.09700,
        position_size=0.5,
        risk_reward_ratio=1.5,
        confluence_score=confluence_score,
        confluence_components=(),
        risk_amount=50.0,
        risk_pct=0.02,
        sl_distance=0.003,
        atr_at_entry=0.0012,
        status="pending",
        notes="",
    )


def make_bot(
    admin_chat_id: str = "123456",
    channel_id: str = "@test_channel",
) -> tuple[TelegramBot, MagicMock, MagicMock, MagicMock]:
    """Tworzy TelegramBot z zamockowanymi zależnościami."""
    mock_sg = MagicMock()
    mock_editor = MagicMock()
    mock_db = MagicMock()

    bot = TelegramBot(
        signal_generator=mock_sg,
        telegram_editor=mock_editor,
        db=mock_db,
        channel_id=channel_id,
        admin_chat_id=admin_chat_id,
    )
    return bot, mock_sg, mock_editor, mock_db


# ── Signal sending ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_signal_success():
    """send_signal → return True, DB updated to 'sent'."""
    bot, _, mock_editor, mock_db = make_bot()

    editor_result = MagicMock()
    editor_result.raw_data = {"telegram_message": "🟢 LONG EUR/USD\nEntry: 1.08500"}
    editor_result.reasoning = "test"
    mock_editor.analyze.return_value = editor_result

    mock_tg_msg = MagicMock()
    mock_tg_msg.message_id = 42

    mock_app = MagicMock()
    mock_app.bot.send_message = AsyncMock(return_value=mock_tg_msg)
    mock_app.bot.send_message_to_admin = AsyncMock()
    bot._app = mock_app

    bot._notify_admin = AsyncMock()

    signal = mock_signal()
    result = await bot.send_signal(signal)

    assert result is True
    mock_app.bot.send_message.assert_called_once()
    call_kwargs = mock_app.bot.send_message.call_args
    assert call_kwargs.kwargs["chat_id"] == "@test_channel"
    assert call_kwargs.kwargs["parse_mode"] == "HTML"
    mock_db.update_signal_status.assert_called_once()


@pytest.mark.asyncio
async def test_send_signal_failure_notifies_admin():
    """send_message rzuca wyjątek → return False, admin powiadomiony."""
    bot, _, mock_editor, _ = make_bot()

    editor_result = MagicMock()
    editor_result.raw_data = {"telegram_message": "test message"}
    editor_result.reasoning = "test"
    mock_editor.analyze.return_value = editor_result

    mock_app = MagicMock()
    mock_app.bot.send_message = AsyncMock(side_effect=RuntimeError("API down"))
    bot._app = mock_app

    bot._notify_admin = AsyncMock()

    signal = mock_signal()
    result = await bot.send_signal(signal)

    assert result is False
    bot._notify_admin.assert_called_once()
    notify_call = bot._notify_admin.call_args[0][0]
    assert "FAILED" in notify_call
    assert "EUR_USD" in notify_call


@pytest.mark.asyncio
async def test_send_signal_formats_via_editor():
    """TelegramEditor.analyze() wywoływany z poprawnym kontekstem."""
    bot, _, mock_editor, _ = make_bot()

    editor_result = MagicMock()
    editor_result.raw_data = {"telegram_message": "formatted message"}
    editor_result.reasoning = "test"
    mock_editor.analyze.return_value = editor_result

    mock_app = MagicMock()
    mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    bot._app = mock_app
    bot._notify_admin = AsyncMock()

    signal = mock_signal(pair="XAU_USD", direction="bearish", confluence_score=80)
    await bot.send_signal(signal)

    mock_editor.analyze.assert_called_once()
    call_arg = mock_editor.analyze.call_args[0][0]
    assert call_arg["instrument"] == "XAU_USD"
    assert call_arg["direction"] == "bearish"
    assert call_arg["confluence_score"] == 80
    assert "take_profits" in call_arg


# ── Admin commands ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cmd_status_returns_stats():
    """_cmd_status → odpowiedź zawiera licznik sygnałów."""
    bot, _, _, mock_db = make_bot(admin_chat_id="999")

    mock_db.get_signals.return_value = [
        {
            "instrument": "EUR_USD",
            "direction": "bullish",
            "confluence_score": 75,
            "status": "OPEN",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pnl_r": None,
        }
    ]

    update = MagicMock()
    update.effective_chat.id = 999
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await bot._cmd_status(update, context)

    update.message.reply_text.assert_called_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "Status" in reply_text
    assert "Signals today" in reply_text


@pytest.mark.asyncio
async def test_cmd_scan_triggers_scan():
    """_cmd_scan → scan_all_pairs() wywoływane."""
    bot, mock_sg, _, _ = make_bot(admin_chat_id="999")

    mock_sg.scan_all_pairs.return_value = []

    update = MagicMock()
    update.effective_chat.id = 999
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await bot._cmd_scan(update, context)

    mock_sg.scan_all_pairs.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_health_returns_health():
    """_cmd_health → odpowiedź zawiera status komponentów."""
    bot, _, _, mock_db = make_bot(admin_chat_id="999")
    mock_db.get_signals.return_value = []

    update = MagicMock()
    update.effective_chat.id = 999
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await bot._cmd_health(update, context)

    update.message.reply_text.assert_called_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "Health" in reply_text
    assert "Database" in reply_text


@pytest.mark.asyncio
async def test_cmd_rejected_non_admin():
    """Komenda od nie-admina → ignorowana (brak reply)."""
    bot, _, _, _ = make_bot(admin_chat_id="999")

    update = MagicMock()
    update.effective_chat.id = 111  # nie admin
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await bot._cmd_status(update, context)
    await bot._cmd_scan(update, context)
    await bot._cmd_health(update, context)

    update.message.reply_text.assert_not_called()


# ── Setup ──────────────────────────────────────────────────────────────────────


def test_setup_registers_handlers():
    """setup() → Application z poprawnymi handlerami."""
    bot, _, _, _ = make_bot()
    bot.token = "fake:token"

    mock_app = MagicMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    mock_application = MagicMock()
    mock_application.builder.return_value = mock_builder

    mock_command_handler = MagicMock()

    with patch("telegram.ext.Application", mock_application):
        with patch("telegram.ext.CommandHandler", mock_command_handler):
            result = bot.setup()

    assert result is mock_app
    assert mock_app.add_handler.call_count == 6
    mock_app.add_error_handler.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_fallback_to_polling():
    """Brak WEBHOOK_URL → polling mode."""
    bot, _, _, _ = make_bot()
    bot.token = "fake:token"

    mock_app = MagicMock()
    mock_app.run_polling = AsyncMock()
    mock_app.run_webhook = AsyncMock()
    bot._app = mock_app

    with patch.dict("os.environ", {}, clear=True):
        await bot.run_webhook(webhook_url=None)

    mock_app.run_polling.assert_called_once()
    mock_app.run_webhook.assert_not_called()


# ── Edge cases ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_signal_editor_fallback():
    """TelegramEditor tier 3 (deterministic) → wiadomość nadal wysłana."""
    from agents.base_agent import AgentTier, MarketBias  # noqa: PLC0415
    from agents.base_agent import AgentResult  # noqa: PLC0415

    bot, _, mock_editor, _ = make_bot()

    deterministic_result = AgentResult(
        agent_name="telegram_editor",
        tier_used=AgentTier.DETERMINISTIC,
        bias=MarketBias.BULLISH,
        confidence=0.5,
        reasoning="Deterministic fallback template",
        timestamp=datetime.now(timezone.utc),
        raw_data={"telegram_message": "🟢 LONG EUR/USD\n⚠️ Not financial advice."},
    )
    mock_editor.analyze.return_value = deterministic_result

    mock_app = MagicMock()
    mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
    bot._app = mock_app
    bot._notify_admin = AsyncMock()

    signal = mock_signal()
    result = await bot.send_signal(signal)

    assert result is True
    mock_app.bot.send_message.assert_called_once()


def test_build_editor_context_maps_fields():
    """_build_editor_context → poprawne mapowanie pól Signal."""
    bot, _, _, _ = make_bot()
    signal = mock_signal(pair="BTC_USD", direction="bearish", confluence_score=82)

    ctx = bot._build_editor_context(signal)

    assert ctx["instrument"] == "BTC_USD"
    assert ctx["direction"] == "bearish"
    assert ctx["entry"] == signal.entry
    assert ctx["stop_loss"] == signal.stop_loss
    assert ctx["take_profits"]["tp1"] == signal.take_profit_1
    assert ctx["take_profits"]["tp2"] == signal.take_profit_2
    assert ctx["take_profits"]["tp3"] == signal.take_profit_3
    assert ctx["confluence_score"] == 82
    assert ctx["risk_reward"] == signal.risk_reward_ratio
    assert ctx["atr"] == signal.atr_at_entry


@pytest.mark.asyncio
async def test_notify_admin_failure_doesnt_crash():
    """_notify_admin rzuca wyjątek → brak propagacji."""
    bot, _, _, _ = make_bot(admin_chat_id="999")

    mock_app = MagicMock()
    mock_app.bot.send_message = AsyncMock(side_effect=RuntimeError("network error"))
    bot._app = mock_app

    # Nie powinno rzucić wyjątku
    await bot._notify_admin("test notification")
