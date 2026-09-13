.PHONY: release-check

PYTHON ?= python3

release-check:
	$(PYTHON) scripts/release_check.py
