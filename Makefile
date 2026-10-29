# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# Development: tests, lint, and the stack built from this checkout.
#
#   make test       run pytest
#   make format     fix lint issues and format the code (ruff)
#   make lint       check lint and formatting without changing files
#   make start      build and start the whole stack from this checkout (needs deploy/.env)
#   make start PROD=1  start it from the published images instead
#   make update     rebuild (or with PROD=1 pull) and recreate what changed
#   make stop       stop and remove the stack (data under $OW_ROOT is kept)
#   make restart    stop, rebuild and start
#   make logs       follow the OrbitalWindow service log
#   make status     show the stack's containers
#
# The stack targets run deploy/Makefile, the one servers use, which picks the compose files from
# deploy/.env. Its other targets (install-conda, install-matlab, backup, …) run there:
# `make -C deploy help`.

# PROD=1 uses the published images; otherwise compose.dev.yaml builds them from this checkout.
DEPLOY := $(MAKE) -C deploy $(if $(PROD),,DEV=1)
.PHONY: test format lint start update stop restart logs status docs

-include .tidy/*.mk


test: ## Run the test suite (pytest)
	hatch test

format: ## Fix lint issues and format the code (ruff)
	hatch run lint:fmt

lint: ## Check lint and formatting without changing files (ruff)
	hatch run lint:check

start: ## Build and start the stack
	$(DEPLOY) start

update: ## Rebuild (PROD=1: pull) and recreate what changed
	$(DEPLOY) update

stop: ## Stop and remove the stack (data under OW_ROOT is kept)
	$(DEPLOY) stop

restart: ## Stop, rebuild and start
	$(DEPLOY) restart

logs: ## Follow the OrbitalWindow service log
	$(DEPLOY) logs

status: ## Show the stack's containers
	$(DEPLOY) status
