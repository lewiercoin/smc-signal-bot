# Workflow: Nowy Tydzień
*Uruchom komendą: /week-start*

## Na początku każdego tygodnia pracy wykonaj:

1. Sprawdź `db/schema.sql` — czy jest aktualny względem kodu w `db/database.py`
2. Uruchom testy: `pytest tests/ -x --tb=short`
3. Sprawdź linting: `ruff check . && mypy .`
4. Pokaż mi listę zadań na ten tydzień z harmonogramu w `01-project-overview.md`
5. Jeśli są otwarte `TODO` lub `FIXME` w kodzie — wylistuj je

## Pytania które zadaj mi na start tygodnia:
- "Co skończyłeś w poprzednim tygodniu? Pokażę co jest kolejne."
- "Czy są jakieś nowe decyzje architektoniczne które powinienem znać?"
