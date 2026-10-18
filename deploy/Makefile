# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

# Run OrbitalWindow from this folder. A server needs only a copy of deploy/ (and Docker and make).
#
#   make init              create .env from .env.example (then edit it)
#   make start             start the stack (published images, OW_VERSION from .env)
#   make update            pull newer images for OW_VERSION and recreate what changed
#   make stop / restart    stop and remove the stack / stop and start (data under OW_ROOT is kept)
#   make logs / status     follow the service log / show the containers
#   make install-conda     install the shared Python science environment (python-science add-on)
#   make install-matlab    install the shared MATLAB (matlab add-on)
#   make backup DEST=…     back up homes, group folders, state and Samba passwords
#   make export-cert       save the self-signed certificate as orbitalwindow.crt for users' PCs
#
# The compose files follow .env: compose.nvidia.yaml when OW_GPU_VENDOR=nvidia, compose.intel.yaml
# when it is intel, compose.tls.yaml when OW_ACME_DNS_PROVIDER is set. DEV=1 (used by the
# repository's Makefile) adds compose.dev.yaml, which builds the images from the checkout.

.DEFAULT_GOAL := help
ENV_VALUE = $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null | tail -1)
GPU_VENDOR := $(call ENV_VALUE,OW_GPU_VENDOR)
ACME_PROVIDER := $(call ENV_VALUE,OW_ACME_DNS_PROVIDER)
OW_ROOT := $(or $(call ENV_VALUE,OW_ROOT),/srv/orbitalwindow)
COMPOSE_FILES := -f compose.yaml $(if $(DEV),-f compose.dev.yaml) \
  $(if $(filter nvidia,$(GPU_VENDOR)),-f compose.nvidia.yaml) \
  $(if $(filter intel,$(GPU_VENDOR)),-f compose.intel.yaml) \
  $(if $(ACME_PROVIDER),-f compose.tls.yaml)
COMPOSE ?= docker compose $(COMPOSE_FILES) --env-file .env
SERVICE ?= orbitalwindow
KEEP ?= 14
.PHONY: help init start update stop restart logs status install-conda install-matlab backup \
  export-cert

help: ## List the targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; \
	  {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

init: ## Create .env from .env.example with a generated first admin password
	@if [ -e .env ]; then echo ".env already exists; edit it instead." >&2; exit 1; fi
	@umask 077 && password=$$(head -c 18 /dev/urandom | base64 | tr '+/' '-_') && \
	  sed "s/^CONTROL_ADMIN_PASSWORD=.*/CONTROL_ADMIN_PASSWORD=$$password/" .env.example > .env && \
	  echo "Created .env (mode 600). First admin: user admin, password $$password" && \
	  echo "Now set OW_HOSTNAME, OW_ROOT and OW_GPU_VENDOR in .env, then run: make start"

start: .env ## Start the stack
	$(COMPOSE) up -d $(if $(DEV),--build)
	@echo "OrbitalWindow: https://$(call ENV_VALUE,OW_HOSTNAME)/"

update: .env ## Pull newer images for OW_VERSION and recreate what changed
	$(if $(DEV),,$(COMPOSE) pull)
	$(COMPOSE) up -d $(if $(DEV),--build)

stop: .env ## Stop and remove the stack (data under OW_ROOT is kept)
	$(COMPOSE) down

restart: stop start ## Stop and start the whole stack (interrupts every service)

logs: .env ## Follow the OrbitalWindow service log
	$(COMPOSE) logs -f $(SERVICE)

status: .env ## Show the stack's containers
	$(COMPOSE) ps

install-conda: .env ## Install the shared conda (SCIENCE_PACKAGES=…, FORCE=1 to reinstall)
	OW_ROOT=$(OW_ROOT) sh scripts/install-conda.sh

install-matlab: .env ## Install the shared MATLAB (RELEASE=R2026b, PRODUCTS="MATLAB …")
	OW_ROOT=$(OW_ROOT) sh scripts/install-matlab.sh $(PRODUCTS)

backup: .env ## Back up to DEST (required), keeping KEEP days (default 14)
	@[ -n "$(DEST)" ] || { echo "Usage: make backup DEST=/backup/orbitalwindow [KEEP=14]" >&2; exit 1; }
	OW_ROOT=$(OW_ROOT) sh scripts/backup.sh "$(DEST)" $(KEEP)

export-cert: ## Save the self-signed certificate as orbitalwindow.crt (install it on users' PCs)
	docker run --rm -v ow-traefik-certs:/c:ro alpine:3.22 cat /c/default.crt > orbitalwindow.crt
	@echo "Saved orbitalwindow.crt"

.env:
	@echo "Missing .env in $(CURDIR). Create it with 'make init' (or copy .env.example), then edit it." >&2
	@exit 1
