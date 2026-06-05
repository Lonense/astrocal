.PHONY: default lint format

default: lint

ifeq ($(OS),Windows_NT)
PYTHON?=py -3.8
else
PYTHON?=python3
endif

lint:
	$(PYTHON) -m black -t py38 --check --diff update.py

format:
	$(PYTHON) -m black -t py38 update.py
