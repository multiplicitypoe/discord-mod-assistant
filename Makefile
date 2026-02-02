.PHONY: install run-bot lint format test

install:
	poetry install

run-bot:
	poetry run python -m incident_mod_bot.bot

lint:
	poetry run ruff check src

format:
	poetry run ruff format src

test:
	poetry run pytest
