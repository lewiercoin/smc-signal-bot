# SMC Signal Bot — Project Overview
*Windsurf Rule: Always On*

## Co budujemy
Bot sygnałowy Telegram oparty o strategię Smart Money Concepts (makuchaku/ICT).
Publikuje sygnały tradingowe (EUR/USD, XAU/USD, BTC/USD) na kanale Telegram.
Specyfikacja: SMC Signal Bot Master Spec v2.2 (+ ITS v1.0 engineering layer).

## Stack (nie negocjowalny)
- **Język**: Python 3.11 (nie 3.12, nie 3.10)
- **Baza danych**: SQLite na start → PostgreSQL po osiągnięciu >500 sygnałów/miesiąc
- **Broker API**: OANDA REST API v20 (oandapyV20)
- **Crypto data**: CCXT → Binance (BTC wolumen)
- **AI agenci**: Claude Haiku (anthropic SDK) + fallback Ollama llama3:8b + template deterministyczny
- **Telegram**: python-telegram-bot (webhook mode na VPS, polling na dev)
- **Scheduler**: APScheduler
- **Linting**: ruff
- **Testy**: pytest + pytest-asyncio
- **Typy**: mypy (type hints na wszystkich sygnaturach funkcji)

## Struktura repo (nie zmieniaj bez pytania)
```
smc_signal_bot/
├── config/
│   ├── default.yaml
│   └── profiles/
│       ├── eurusd.yaml
│       ├── xauusd.yaml
│       └── btcusd.yaml
├── connectors/
│   ├── oanda.py
│   ├── ccxt_crypto.py
│   └── news.py
├── dq/
│   ├── validators.py
│   └── resample.py
├── smc/
│   ├── detector.py
│   ├── ipda_range.py
│   ├── ppdd_ob.py
│   ├── ob_quality.py
│   ├── fvg_bias.py
│   ├── absorption.py       ← [GROK-1]
│   └── confluence_scorer.py
├── utils/
│   ├── dynamic_swing.py    ← [GROK-2]
│   ├── scheduler.py
│   └── logger.py
├── agents/
│   ├── structure_analyst.py
│   ├── fundamental_analyst.py
│   ├── risk_verifier.py
│   ├── telegram_editor.py
│   └── optimizer_agent.py  ← [GROK-3]
├── bot/
│   ├── telegram_bot.py
│   └── formatter.py
├── db/
│   ├── database.py
│   └── schema.sql
├── monitor/
│   └── h1_monitor.py
├── tests/
├── main.py
└── pyproject.toml
```

## Harmonogram (9 tygodni, zaczynamy od zera)
- Tydz 1–2: Fundament (OANDA, DQ, SQLite, config YAML)
- Tydz 3: SMC Engine (OB, FVG, BoS/CHoCH, PPDD, BAR, RVR, absorption, dynamic swing)
- Tydz 4: IPDA + Confluence Scorer (110 pkt max)
- Tydz 5: H1Monitor + Filtry (news, sesja, spread)
- Tydz 6: AI Agenci 1–4 + fallback
- Tydz 7: Telegram Bot (webhook) + VPS setup
- Tydz 8: Paper Trading (min. 20 sygnałów)
- Tydz 9: Optimizer Agent + Soft Launch

## Budżet
< $500 łącznie. VPS: Hetzner CX22 (~$4/mc). API AI: ~$3–8/mc.
