.PHONY: install run-bot lint format test build-docker run-docker

DOCKER ?= docker
IMAGE ?= discord-incident-assistant

install:
	poetry install

run-bot:
	poetry run python -m incident_mod_bot.bot

build-docker:
	@mkdir -p data
	sudo $(DOCKER) build -t "$(IMAGE)" "$(CURDIR)"

run-docker: build-docker
	@mkdir -p data
	sudo $(DOCKER) run --rm \
		--env-file "$(CURDIR)/.env" \
		-v "$(CURDIR)/data:/app/data" \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,nodev \
		--cap-drop ALL \
		--security-opt no-new-privileges \
		--pids-limit 256 \
		--memory 512m \
		--cpus 1.0 \
		--user "$$(id -u):$$(id -g)" \
		"$(IMAGE)"

lint:
	poetry run ruff check src

format:
	poetry run ruff format src

test:
	poetry run pytest
