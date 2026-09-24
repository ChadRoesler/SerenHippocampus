# Mirrors the CI matrix legs locally.
# Requires: make (Git for Windows ships it; or: choco install make / scoop install make)
#
# Targets:
#   make test          - base install + dev (.[dev])          - matches CI extras=dev
#   make test-mcp      - with MCP extras   (.[dev,mcp])       - matches CI extras=dev,mcp
#   make test-corp     - with CORP extras  (.[dev,corp])      - matches CI extras=dev,corp
#   make test-corp-mcp - with both         (.[dev,corp,mcp])  - matches CI extras=dev,corp,mcp
#   make test-all      - all four in sequence
#   make clean         - remove any leftover venvs
#
# Each target creates a fresh isolated venv, runs tests, then removes it.
# The [dev] extra pulls seren-memory: the tests run the real Memory app in
# process, so a leg exercises the contract between the two services.

SHELL        := pwsh.exe
.SHELLFLAGS  := -NoProfile -NonInteractive -Command

PKG_DIR       := SerenHippocampus
VENV_BASE     := .venv-base
VENV_MCP      := .venv-mcp
VENV_CORP     := .venv-corp
VENV_CORP_MCP := .venv-corp-mcp

.PHONY: test test-mcp test-corp test-corp-mcp test-all clean

define run-leg
	Remove-Item -Recurse -Force $(1) -ErrorAction SilentlyContinue; \
	python -m venv $(1); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\$(1)\Scripts\pip.exe install -e "$(PKG_DIR)/.[$(2)]"; \
	Set-Location $(PKG_DIR); \
	..\$(1)\Scripts\python.exe -m pytest tests -q; \
	$$code = $$LASTEXITCODE; \
	Set-Location ..; \
	Remove-Item -Recurse -Force $(1) -ErrorAction SilentlyContinue; \
	exit $$code
endef

test:
	$(call run-leg,$(VENV_BASE),dev)

test-mcp:
	$(call run-leg,$(VENV_MCP),dev,mcp)

test-corp:
	$(call run-leg,$(VENV_CORP),dev,corp)

test-corp-mcp:
	$(call run-leg,$(VENV_CORP_MCP),dev,corp,mcp)

test-all: test test-mcp test-corp test-corp-mcp

clean:
	Remove-Item -Recurse -Force $(VENV_BASE), $(VENV_MCP), $(VENV_CORP), $(VENV_CORP_MCP) -ErrorAction SilentlyContinue
