# Performer -- stdlib only, so there is nothing to install.
PYTHON ?= python3
CLI    := ./collector/bin/performer
RUNS   ?= ./runs

.PHONY: help test check fake inspect clean

help:
	@echo "make test     run the test suite (no dependencies)"
	@echo "make check    byte-compile the collector and run the tests"
	@echo "make fake     write a synthetic bundle into $(RUNS)"
	@echo "make inspect  inspect every bundle in $(RUNS)"
	@echo "make clean    remove $(RUNS) and __pycache__"

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

check:
	$(PYTHON) -m compileall -q collector/oxfscope
	$(PYTHON) -m unittest discover -s tests -t .

fake:
	$(CLI) fake-run --out $(RUNS) --label baseline
	$(CLI) fake-run --out $(RUNS) --label degraded --degraded --bad-frame-pointers

inspect:
	@for b in $(RUNS)/*.tgz; do $(CLI) inspect "$$b"; echo; done

clean:
	rm -rf $(RUNS)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
