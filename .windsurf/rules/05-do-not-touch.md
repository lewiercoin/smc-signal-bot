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
- Nie zmieniaj progów 60 (agenci) i 70 (Telegram) bez wyraźnej decyzji
- Nie zmieniaj wag scoringu bez analizy danych z `ob_quality_log`
- Nie zmieniaj zakresów IPDA (0–40 / 40–60 / 60–100)

### Logika risk engine
- Nie zmieniaj maksymalnego ryzyka per trade (1.25%)
- Nie usuwaj circuit breakerów (dzienna strata, korelacja portfela)
- Nie zmieniaj poziomów TP (1.5R/2.5R/4.0R dla EUR, itd.)

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
Jeśli nowa biblioteka nie jest w pyproject.toml
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
BTC: TP1=1.2R (50%), TP2=2.5R (25%), TP3=4.5R (25%).
