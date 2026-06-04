.PHONY: default lint test format

default: lint test

ifeq ($(OS),Windows_NT)
PYTHON?=py -3.8
else
PYTHON?=python3
endif

lint:
	$(PYTHON) -m black -t py38 --check --diff update.py
	$(PYTHON) -m black -t py38 --check --diff tests/test_update.py

test:
	$(PYTHON) -m pytest

format:
	$(PYTHON) -m black -t py38 update.py
	$(PYTHON) -m black -t py38 tests/test_update.py
