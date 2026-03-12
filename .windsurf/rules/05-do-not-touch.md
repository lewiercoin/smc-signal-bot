# Do Not Touch — Guardrails dla Cascade
*Windsurf Rule: Always On | NAJWYŻSZY PRIORYTET*

## Zanim cokolwiek zmienisz — zapytaj mnie

Jeśli którakolwiek z poniższych zmian jest częścią Twojego planu,
**ZATRZYMAJ SIĘ i zapytaj użytkownika o potwierdzenie** zanim napiszesz kod.

---

## 🔴 KRYTYCZNE — nigdy bez wyraźnej zgody

### Schemat bazy danych
- Nie usuwaj kolumn z tabeli `signals`
- Nie zmieniaj nazw istniejących kolumn
- Nie dodawaj NOT NULL bez DEFAULT do istniejących tabel
- Migracje tylko przez osobny plik `db/migrations/YYYYMMDD_opis.sql`

### Progi confluence (źródło prawdy: `02-smc-conventions.md`)
- Próg to **65** (jeden próg, nie dwa). NIE zmieniaj na 60 ani 70.
- Nie zmieniaj wag scoringu bez analizy danych z `ob_quality_log`
- Nie zmieniaj zakresów IPDA (0–40 / 40–60 / 60–100)

### Logika risk engine
- Nie zmieniaj maksymalnego ryzyka per trade (**2%**, nie 1.25%)
- Nie usuwaj circuit breakerów (dzienna strata, korelacja portfela)
- Nie zmieniaj poziomów TP: **EUR/XAU: 1.5R/2.5R/3.5R, BTC: 1.5R/2.5R/5.5R**
- Spread porównuj zawsze w **pip units** (konwertuj price units przez `/ pip_size` w `_check_spread()`)

### Signal.id — UUID, nie integer
- `Signal.id` to UUID string (`str(uuid.uuid4())`)
- `signals.id` w DB to INTEGER PRIMARY KEY (autoincrement) — to inny klucz
- Do aktualizacji statusu zawsze używaj `update_signal_status_by_uuid()`, nigdy `update_signal_status(signal_id=signal.id)`

### Klucze API i zmienne środowiskowe
- Nigdy nie wpisuj kluczy API bezpośrednio w kodzie
- Zawsze czytaj z `os.environ["NAZWA"]`
- Plik `.env` MUSI być w `.gitignore`

---

## 🟡 WYMAGAJĄ ROZMOWY — zapytaj przed implementacją

### Zmiana silnika SMC
Jeśli chcesz zmienić algorytm detekcji OB, FVG, lub BoS/CHoCH
→ najpierw omów impakt na istniejące sygnały

### Dodanie nowej zależności
Jeśli nowa biblioteka nie jest w requirements.txt
→ zapytaj czy jest potrzebna i czy nie duplikuje istniejącej funkcji

### Zmiana struktury katalogów
Jeśli chcesz przenieść pliki między katalogami
→ to wymaga aktualizacji importów w WIELU miejscach

### Zmiana brokera lub API
Zmiana z OANDA na MT5, Interactive Brokers lub inny broker
→ wymagałoby przepisania ~30% kodu

### Dodanie zewnętrznego frameworka agentów
CrewAI, LangGraph, AutoGen — odrzucone świadomie
→ nie implementuj bez nowej decyzji architektonicznej

---

## 🟢 Możesz robić autonomicznie (nie pytaj)

- Naprawianie błędów mypy i ruff
- Pisanie testów do istniejącego kodu
- Dodawanie logowania (structlog) tam gdzie go brakuje
- Refactoring wewnątrz pojedynczego modułu (bez zmiany interfejsu)
- Uzupełnianie docstringów
- Optymalizacja wydajności bez zmiany zachowania

---

## Zamrożone decyzje techniczne (nie podważaj)

Poniższe decyzje zostały podjęte świadomie po analizie 5 modeli LLM (DeepSeek ×2, ChatGPT, Gemini, Grok).
Nie proponuj alternatyw — zostały odrzucone z konkretnych powodów.

| Decyzja | Dlaczego nie dyskutujemy |
|---------|--------------------------|
| SQLite zamiast PostgreSQL (na start) | <500 sygnałów/mc = SQLite wystarczy |
| Brak XGBoost ML Gate | <200 sygnałów = overfitting, wdrożenie po 6 mies. |
| Brak CrewAI/LangGraph | Zbędny overhead dla 4–5 wywołań LLM |
| OANDA zamiast MT5 | MT5 API nie jest potrzebne do bota sygnałowego |
| Claude Haiku zamiast GPT | Koszt: ~$3–8/mc vs. ~$15–25/mc przy tym samym jakości |
| Webhook zamiast polling (produkcja) | Polling tylko na dev — webhook na VPS |
| Hetzner CX22 (~$4/mc) | Wystarczający do bota sygnałowego |

---

## Historia krytycznych bugów (nie powtarzaj)

### JBlanked API — ODRZUCONY
JBlanked ma limit 1 request/dzień (od 2026). Używamy Finnhub `/calendar/economic`.
NIE implementuj integracji z JBlanked.

### Finnhub Sentiment — NIE ISTNIEJE dla FX
Endpoint `finnhub.io/api/v1/news-sentiment` zwraca dane tylko dla spółek giełdowych USA.
Dla Forex i Crypto: Agent 2 klasyfikuje nagłówki BEZPOŚREDNIO przez LLM.
NIE używaj finnhub sentiment API dla walut ani metali.

### TP3 = 8R dla BTC — ODRZUCONY
8R na timeframe 4H jest nierealistyczne — wicki BTC invalidują BE zanim cena dojdzie do celu.
BTC: TP1=1.5R, TP2=2.5R, TP3=5.5R (zaimplementowane w `REWARD_RATIOS` w `risk_engine.py`).

### Absorption body_ratio — NIE odwracaj logiki
Absorption = **duże** ciało świecy: `body_ratio > 0.70`.
NIE zmieniaj na `body_ratio ≤ 0.30` (stara błędna specyfikacja). Mała świeca to doji/pin bar, nie absorption.

### Spread gate — price units vs pip units
`OandaClient.get_current_spread()` zwraca **price units** (ask - bid, np. 0.00012 dla EUR/USD).
`MAX_SPREADS` w `risk_engine.py` są w **pip units** (np. 2.0 pips).
`_check_spread()` musi konwertować: `spread_in_pips = current_spread / pip_size`.
NIE porównuj raw price units z pip limits — to bug który powoduje że spread gate nigdy nie blokuje.

### Signal.id UUID — nie używaj do zapytań INTEGER PK
`send_signal()` w `TelegramBot` wywołuje `update_signal_status_by_uuid(signal_uuid=signal.id)`.
NIGDY nie zmieniaj tego na `update_signal_status(signal_id=signal.id)` — UUID string nie trafi w INTEGER PK.

### 3-tier fallback — Ollama odrzucony
Fallback chain: **Cache → Claude Haiku → Deterministic** (template).
Ollama llama3:8b został odrzucony z powodu overhead i złożoności deploymentu.
NIE dodawaj Ollama jako pośredniego tier.
