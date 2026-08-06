# Performer -- stdlib only, so there is nothing to install.
PYTHON ?= python3
CLI    := ./collector/bin/performer
RUNS   ?= ./runs

.PHONY: help test check target fake profiles inspect clean

help:
	@echo "make test     run the test suite (no dependencies)"
	@echo "make check    build the target, byte-compile, run the tests"
	@echo "make target   build tests/target/contention (needs a C++ compiler)"
	@echo "make fake     write a synthetic bundle into $(RUNS)"
	@echo "make inspect  inspect every bundle in $(RUNS)"
	@echo "make clean    remove $(RUNS) and __pycache__"

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

check: target
	$(PYTHON) -m compileall -q collector/performer
	$(PYTHON) -m unittest discover -s tests -t .

# The process supervision and /proc tests need a real multi threaded process;
# without it they skip rather than fail.
target:
	$(MAKE) -C tests/target

fake:
	$(CLI) fake-run --out $(RUNS) --label baseline
	$(CLI) fake-run --out $(RUNS) --label degraded --degraded --bad-frame-pointers

profiles:
	@$(CLI) schema >/dev/null && ls collector/profiles/*.yaml

inspect:
	@for b in $(RUNS)/*.tgz; do $(CLI) inspect "$$b"; echo; done

clean:
	rm -rf $(RUNS)
	$(MAKE) -C tests/target clean
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
