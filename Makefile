# discord-mod-assistant: the moderation incident bot.

IMAGE ?= discord-mod-assistant
CONTAINER ?= discord-mod-assistant
DATA_DIR ?= data
ENV_HINT = (DISCORD_TOKEN, OPENAI_API_KEY)

RUN_ARGS = \
	--env-file "$(CURDIR)/.env" \
	-v "$(CURDIR)/data:/app/data"
RUN_ARGS += $(HARDENED_ARGS)

include docker.mk

.PHONY: install run-bot lint format test

install:
	poetry install

run-bot: ensure-env
	poetry run python -m incident_mod_bot.bot

lint:
	poetry run ruff check src

format:
	poetry run ruff format src

test:
	poetry run pytest
