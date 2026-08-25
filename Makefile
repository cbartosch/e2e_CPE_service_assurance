# Every target here is a thin wrapper over the commands its recipe shows -- most over a single one,
# `lint`, `fmt` and `setup` over a short sequence. If `make` is unavailable (a plain Windows shell,
# for instance) run those commands directly. This file is their only owner, so README.md points
# here rather than repeating them.

PY ?= python
VENV := .venv
ifeq ($(OS),Windows_NT)
  VPY := $(VENV)/Scripts/python.exe
else
  VPY := $(VENV)/bin/python
endif

.DEFAULT_GOAL := help
.PHONY: help setup test test-fast lint fmt typecheck demo serve cov check audit clean

help:  ## Show this help
	@$(PY) -c "import re,sys; [print(f'  {m[1]:<12} {m[2]}') for m in (re.match(r'^([a-z-]+):.*?## (.*)$$', l) for l in open('Makefile')) if m]"

setup:  ## Create .venv and install the package with dev extras
	$(PY) -m venv $(VENV)
	$(VPY) -m pip install --upgrade pip
	$(VPY) -m pip install -e ".[dev,optimizer]"
	@echo "postgres extra is optional: $(VPY) -m pip install -e \".[postgres]\""

test:  ## Full suite with coverage gate
	$(VPY) -m pytest --cov --cov-report=term-missing --cov-fail-under=85

test-fast:  ## Suite without coverage instrumentation
	$(VPY) -m pytest

cov:  ## HTML coverage report into htmlcov/
	$(VPY) -m pytest --cov --cov-report=html

lint:  ## Ruff check
	$(VPY) -m ruff check src tests
	$(VPY) -m ruff format --check src tests

fmt:  ## Ruff autofix and format
	$(VPY) -m ruff check --fix src tests
	$(VPY) -m ruff format src tests

typecheck:  ## Mypy strict over src
	$(VPY) -m mypy

# The two targets below name a gap and stop. Each previously invoked something that has never been
# written -- `lpr_cpe.cli demo` and `lpr_cpe.api.app` -- so the failure a reader met was an argparse
# error or a ModuleNotFoundError, which reads as a broken install rather than as unbuilt work. The
# targets are kept rather than deleted because IMPLEMENTATION_PLAN.md carries both as pending, and a
# target that says why it cannot run is worth more than a name nobody can find.

DEMO_SERVICE ?= SVC-UT-001-B-01

demo:  ## Drive one incident end to end against the simulators (DEMO_SERVICE=... to pick another)
	$(VPY) -m lpr_cpe.cli run $(DEMO_SERVICE)

# This target used to name a gap and exit 1, because the scenarios it would run were unwritten and a
# command that printed nothing and exited zero would be indistinguishable from a demonstration that
# had run. `lpr-cpe run` is not those scenarios -- the seventeen named ones are still unwritten and
# IMPLEMENTATION_PLAN.md still carries them as pending -- but it is one incident driven from event to
# closure, which is more than the gap message could say. `DEMO_SERVICE` is a variable because 38 of
# the 41 fixtures escalate rather than close, and watching one of those is the more instructive run.

SERVE_HOST ?= 127.0.0.1
SERVE_PORT ?= 8000

serve:  ## Run the HTTP surface (SERVE_HOST / SERVE_PORT to change where)
	$(VPY) -m uvicorn lpr_cpe.api.app:create_app --factory --host $(SERVE_HOST) --port $(SERVE_PORT)

check: lint typecheck test  ## lint, typecheck, then the suite behind the coverage gate

audit:  ## Run every gate and write the evidence bundle into audit/
	$(VPY) -m lpr_cpe.audit

clean:  ## Remove caches and build artefacts
	$(PY) -c "import shutil,pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['.pytest_cache','.mypy_cache','.ruff_cache','htmlcov','build','dist']]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
