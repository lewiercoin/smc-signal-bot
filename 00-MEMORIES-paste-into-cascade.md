# Windsurf Memories — wklej do panelu Memories w Cascade
# Każdy blok to osobna Memory (ikona Customizations → Memories → Add)

---

## Memory 1 — Project Identity
```
SMC Signal Bot: bot sygnałowy Telegram dla EUR/USD, XAU/USD, BTC/USD.
Strategia: Smart Money Concepts (makuchaku/ICT). Stack: Python 3.11, SQLite,
OANDA REST API, Claude Haiku, Telegram webhook. VPS: Hetzner CX22.
Specyfikacja: Master Spec v2.2 + ITS v1.0 (engineering layer).
Budżet: <$500. Zaczynam od zera — zero kodu na start.
```

---

## Memory 2 — Progi i Scoring
```
Confluence scoring: maks 110 pkt (po absorption). Progi decyzyjne:
<60 = odrzuć, ≥60 = wywołaj AI agentów, ≥70 = publikuj na Telegram.
IPDA: LONG tylko 0-40%, SHORT tylko 60-100%, SKIP 40-60% (hard reject).
Sesja azjatycka (00-07 UTC) = prawie zawsze blok (próg 88).
```

---

## Memory 3 — Krytyczne Bugi (nie powtarzaj)
```
JBlanked API: 1 req/dzień od 2026 — ZASTĄPIONY Finnhub /calendar/economic.
Finnhub Sentiment: NIE ISTNIEJE dla FX — Agent 2 klasyfikuje nagłówki przez LLM.
TP3 BTC = 8R: ODRZUCONE — nierealistyczne na 4H. BTC: TP3 = 4.5R.
Nie używaj CrewAI/LangGraph — zbędny overhead. Brak XGBoost przed 200+ sygnałami.
```

---

## Memory 4 — Nowe Mechanizmy v2.2 (Grok)
```
[GROK-1] Absorption Detection: body_ratio ≤ 0.30 AND vol_ratio ≥ 1.5 (Forex) /
2.0 (BTC). Zgodna = +3-5pkt, sprzeczna = -8pkt. Forex: tick volume proxy.
[GROK-2] Dynamic swing_length: ATR-adaptive 16-40, 5 reżimów (EXTREME/HIGH/NORMAL/LOW/FLAT).
[GROK-3] Optimizer Agent: co niedzielę analizuje journal, PROPONUJE zmiany, NIE wdraża
automatycznie — operator musi zaakceptować. Min. 10 zamkniętych sygnałów do analizy.
```

---

## Memory 5 — Harmonogram Tygodniowy
```
Tydzień 1-2: Fundament (OANDA API, DQ validators, SQLite schema v2.2, config YAML)
Tydzień 3: SMC Engine (OB+FVG+BoS+PPDD, BAR, RVR, absorption, dynamic swing)
Tydzień 4: IPDA + Confluence Scorer (110 pkt, dual threshold 60/70)
Tydzień 5: H1Monitor + Filtry (news blackout, sesje, spread z-score)
Tydzień 6: AI Agenci 1-4 + 3-tier fallback (Haiku → Ollama → template)
Tydzień 7: Telegram Bot (webhook, EU VPS, formaty sygnałów)
Tydzień 8: Paper Trading (min. 20 zamkniętych sygnałów)
Tydzień 9: Optimizer Agent + Soft Launch publiczny
```
