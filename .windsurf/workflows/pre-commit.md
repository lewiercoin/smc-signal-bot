# Workflow: Przed Commitem
*Uruchom komendą: /pre-commit*

## Obowiązkowe przed każdym git commit:

```bash
ruff check .          # linting — zero błędów
ruff format .         # formatowanie
mypy . --ignore-missing-imports  # type checking
pytest tests/ -x      # testy — stop na pierwszym błędzie
```

Jeśli cokolwiek failuje — napraw zanim commit. Nie commituj broken kodu.

## Wiadomość commitu (format):
```
feat(moduł): krótki opis co dodano
fix(moduł): krótki opis co naprawiono
refactor(moduł): co zrefactorowano
test(moduł): co przetestowano
```

## Czego NIE commituj:
- Pliku `.env` (klucze API)
- Katalogu `.venv/`
- Pliku `signals.db` (dane lokalne)
- Plików `__pycache__/`
- Pliku `*.pyc`
