# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# Development and test stack around deploy/compose.yaml.
#
#   make test       run pytest
#   make format     fix lint issues and format the code (ruff)
#   make lint       check lint and formatting without changing files
#   make start      build and start the whole stack (needs deploy/.env)
#   make stop       stop and remove the stack (data under $OW_ROOT is kept)
#   make restart    stop, rebuild and start
#   make logs       follow the OrbitalWindow service log
#   make status     show the stack's containers

GPU_VENDOR := $(shell sed -n 's/^OW_GPU_VENDOR=//p' deploy/.env 2>/dev/null)
ACME_PROVIDER := $(shell sed -n 's/^OW_ACME_DNS_PROVIDER=//p' deploy/.env 2>/dev/null)
COMPOSE_FILES := -f deploy/compose.yaml \
  $(if $(filter nvidia,$(GPU_VENDOR)),-f deploy/compose.nvidia.yaml) \
  $(if $(ACME_PROVIDER),-f deploy/compose.tls.yaml)
COMPOSE ?= docker compose $(COMPOSE_FILES) --env-file deploy/.env
SERVICE ?= orbitalwindow
.PHONY: test format lint start stop restart logs status

-include .tidy/*.mk


test: ## Run the test suite (pytest)
	hatch test

format: ## Fix lint issues and format the code (ruff)
	hatch run lint:fmt

lint: ## Check lint and formatting without changing files (ruff)
	hatch run lint:check

start: deploy/.env ## Build and start the stack
	$(COMPOSE) up -d --build
	@echo "OrbitalWindow: https://$$(grep -E '^OW_HOSTNAME=' deploy/.env | cut -d= -f2)/"

stop: ## Stop and remove the stack (data under OW_ROOT is kept)
	$(COMPOSE) down

restart: stop start ## Stop, rebuild and start

logs: ## Follow the OrbitalWindow service log
	$(COMPOSE) logs -f $(SERVICE)

status: ## Show the stack's containers
	$(COMPOSE) ps

deploy/.env:
	@echo "Missing deploy/.env. Copy deploy/.env.example and fill it in:" >&2
	@echo "  cp deploy/.env.example deploy/.env && chmod 600 deploy/.env" >&2
	@exit 1
