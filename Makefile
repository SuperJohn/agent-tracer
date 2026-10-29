PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: install test run demo record serve dataset

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest -q

run:
	$(PY) -m agenttrace run
	$(PY) -m agenttrace serve

demo record:
	$(PY) -m agenttrace record

serve:
	$(PY) -m agenttrace serve

dataset:
	$(PY) scripts/build_dataset.py
