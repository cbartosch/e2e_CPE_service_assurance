# Every target here is a thin wrapper over one command. If `make` is unavailable (a plain Windows
# shell, for instance) run the command shown in the recipe directly -- README.md lists them, and
# this file is their only owner, so the README points here rather than repeating them.

PY ?= python
VENV := .venv
ifeq ($(OS),Windows_NT)
  VPY := $(VENV)/Scripts/python.exe
else
  VPY := $(VENV)/bin/python
endif

.DEFAULT_GOAL := help
.PHONY: help setup test test-fast lint fmt typecheck demo cov check clean

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

demo:  ## Run the seven demonstration scenarios end to end, in-memory
	$(VPY) -m lpr_cpe.cli demo

serve:  ## Run the API on :8000 with the in-memory checkpointer
	$(VPY) -m uvicorn lpr_cpe.api.app:app --reload --port 8000

check: lint typecheck test  ## Everything CI runs

clean:
	$(PY) -c "import shutil,pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['.pytest_cache','.mypy_cache','.ruff_cache','htmlcov','build','dist']]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
