.PHONY: install run-bot lint format test build-docker stop-docker run-docker docker ensure-data ensure-env

DOCKER_BIN ?= docker
DOCKER ?= $(shell \
	if command -v "$(DOCKER_BIN)" >/dev/null 2>&1; then \
		if "$(DOCKER_BIN)" ps >/dev/null 2>&1; then \
			printf '%s' "$(DOCKER_BIN)"; \
		elif command -v sudo >/dev/null 2>&1 && sudo -n "$(DOCKER_BIN)" ps >/dev/null 2>&1; then \
			printf '%s' "sudo -n $(DOCKER_BIN)"; \
		else \
			printf '%s' "$(DOCKER_BIN)"; \
		fi; \
	else \
		printf '%s' "$(DOCKER_BIN)"; \
	fi)
IMAGE ?= discord-incident-assistant
CONTAINER ?= discord-incident-assistant

ensure-data:
	@mkdir -p data

ensure-env:
	@test -f "$(CURDIR)/.env" || (cp "$(CURDIR)/.env.example" "$(CURDIR)/.env" && \
		printf '%s\n' "Created .env from .env.example. Edit .env (DISCORD_TOKEN, OPENAI_API_KEY), then re-run." && \
		exit 1)

install:
	poetry install

run-bot: ensure-env
	poetry run python -m incident_mod_bot.bot

build-docker: ensure-data
	$(DOCKER) build -t "$(IMAGE)" "$(CURDIR)"

stop-docker:
	@if $(DOCKER) container inspect "$(CONTAINER)" >/dev/null 2>&1; then \
		if [ "$$($(DOCKER) inspect -f '{{.State.Running}}' "$(CONTAINER)" 2>/dev/null)" = "true" ]; then \
			echo "Stopping container $(CONTAINER)"; \
			$(DOCKER) stop "$(CONTAINER)" >/dev/null; \
		fi; \
		echo "Removing container $(CONTAINER)"; \
		$(DOCKER) rm "$(CONTAINER)" >/dev/null; \
	else \
		echo "Container $(CONTAINER) not found; nothing to stop."; \
	fi

run-docker: build-docker ensure-env ensure-data stop-docker
	$(DOCKER) run --rm \
		--name "$(CONTAINER)" \
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

docker: build-docker run-docker

lint:
	poetry run ruff check src

format:
	poetry run ruff format src

test:
	poetry run pytest
