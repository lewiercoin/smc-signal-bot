# Workflow: Nowy Moduł
*Uruchom komendą: /new-module*

## Kolejność dla każdego nowego modułu:

1. **Najpierw kontrakt** — zdefiniuj interfejs (dataclass lub TypedDict) w osobnym bloku
2. **Potem test** — napisz `tests/test_NAZWAMODUŁU.py` z co najmniej 3 przypadkami:
   - Poprawne wejście → oczekiwany wynik
   - Przypadek brzegowy (puste dane, zero, None)
   - Błędne wejście → czy funkcja obsługuje gracefully?
3. **Implementacja** — napisz kod który przechodzi testy
4. **Integracja** — sprawdź czy nowy moduł jest zgodny z `04-data-contracts.md`
5. **Logowanie** — dodaj `structlog` do kluczowych punktów modułu

## Checklist przed "gotowe":
- [ ] Wszystkie funkcje publiczne mają type hints
- [ ] Wszystkie funkcje publiczne mają docstring
- [ ] `pytest tests/test_MODUŁ.py` przechodzi
- [ ] `ruff check smc/MODUŁ.py` bez błędów
- [ ] Brak hardkodowanych wartości (wszystko z configu YAML)
