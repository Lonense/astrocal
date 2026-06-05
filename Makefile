.PHONY: default format

default: format

ifeq ($(OS),Windows_NT)
PYTHON?=py -3.8
else
PYTHON?=python3
endif

format:
	$(PYTHON) -m black -t py38 update.py
