# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

docs: ## Compile the project documentation
	cd docs && hatch run docs:zensical build

docs-serve: ## Locally publish the documentation on the network
	cd docs && hatch run docs:zensical serve

docs-open: ## Open a locally compiled instance of the documentation
	open ./docs/site/index.html