# SMC Signal Bot — Project Overview
*Windsurf Rule: Always On*

## Co budujemy
Bot sygnałowy Telegram oparty o strategię Smart Money Concepts (makuchaku/ICT).
Publikuje sygnały tradingowe (EUR/USD, XAU/USD, BTC/USD) na kanale Telegram.
Specyfikacja: SMC Signal Bot Master Spec v2.2 (+ ITS v1.0 engineering layer).

## Stack (nie negocjowalny)
- **Język**: Python 3.11+ (projekt działa na 3.13.1 — nie zmieniaj na <3.11)
- **Baza danych**: SQLite na start → PostgreSQL po osiągnięciu >500 sygnałów/miesiąc
- **Broker API**: OANDA REST API v20 (oandapyV20)
- **Crypto data**: CCXT → Binance (BTC wolumen)
- **AI agenci**: Claude Haiku (anthropic SDK) + fallback deterministyczny (3-tier: Cache → Haiku → Deterministic). Ollama odrzucony — zbędny overhead.
- **Telegram**: python-telegram-bot (webhook mode na VPS, polling na dev)
- **Scheduler**: APScheduler
- **Linting**: ruff
- **Testy**: pytest + pytest-asyncio
- **Typy**: mypy (type hints na wszystkich sygnaturach funkcji)

## Struktura repo (nie zmieniaj bez pytania)
```
smc-signal-bot/
├── connectors/
│   ├── oanda_client.py        ← OandaClient, Candle dataclass
│   └── news_client.py         ← NewsClient
├── dq/
│   └── data_quality.py        ← DataQualityChecker (spread, news, candle count)
├── smc/
│   ├── swing_detector.py      ← SwingDetector, SwingResult
│   ├── ob_detector.py         ← OrderBlockDetector, OrderBlock
│   ├── fvg_detector.py        ← FairValueGapDetector, FVG
│   ├── liquidity_detector.py  ← LiquidityDetector, LiquiditySweep
│   └── utils.py               ← calculate_atr_series
├── engine/
│   ├── confluence_scorer.py   ← ConfluenceScorer, ConfluenceResult (absorption tu!)
│   ├── risk_engine.py         ← RiskEngine, TradeParameters, SpreadCheck
│   └── signal_generator.py   ← SignalGenerator, Signal (id=UUID)
├── agents/
│   ├── base_agent.py          ← BaseAgent, AgentResult, AgentTier enum
│   ├── structure_agent.py     ← StructureAgent
│   ├── fundamental_agent.py   ← FundamentalAgent
│   ├── risk_verifier.py       ← RiskVerifierAgent
│   └── telegram_editor.py     ← TelegramEditor
├── bot/
│   ├── telegram_bot.py        ← TelegramBot (webhook+polling)
│   ├── scheduler.py           ← SignalScheduler (APScheduler)
│   └── monitoring.py          ← BotMonitor
├── db/
│   └── database.py            ← Database (SQLite, signals+candles+ob_quality+optimizer)
├── paper_trading/
│   └── runner.py              ← PaperTradingRunner (Tydzień 8)
├── tests/
│   ├── integration/           ← conftest.py + test_pipeline.py + test_signal_flow.py
│   └── test_*.py              ← unit testy (381 łącznie)
├── main.py                    ← entry: `python main.py` lub `python main.py paper`
└── requirements.txt
```
**UWAGA**: Brak pliku `config/` — parametry są hardcoded w modułach (np. `MAX_SPREADS`, `PIP_VALUES`, `REWARD_RATIOS` w `risk_engine.py`). Nie twórz YAML config bez decyzji.

## Harmonogram — STAN na Tydzień 7 (UKOŃCZONE)
- ✅ Tydz 1–2: Fundament (OANDA, DQ, SQLite v2.2)
- ✅ Tydz 3: SMC Engine (OB, FVG, BoS, liquidity sweep, absorption w confluence_scorer)
- ✅ Tydz 4: IPDA + Confluence Scorer (110 pkt max, próg 65)
- ✅ Tydz 5: H1Monitor + Filtry (news blackout, sesje, spread)
- ✅ Tydz 6: AI Agenci 1–4 + 3-tier fallback (Cache→Haiku→Deterministic)
- ✅ Tydz 7: Telegram Bot (webhook+polling) + integration tests
- ✅ Tydz 8: Deploy infra + PaperTradingRunner + PaperAnalyzer + rules sync (381 testów)
- ⬜ Tydz 9: Optimizer Agent + Soft Launch publiczny

## Budżet
< $500 łącznie. VPS: Hetzner CX22 (~$4/mc). API AI: ~$3–8/mc.
