# Performer -- stdlib only, so there is nothing to install.
PYTHON ?= python3
CLI    := ./collector/bin/performer
RUNS   ?= ./runs

.PHONY: help test check target fake pair diff profiles inspect viewer serve clean

help:
	@echo "make test     run the test suite (no dependencies)"
	@echo "make check    build the target, byte-compile, run the tests"
	@echo "make target   build tests/target/contention (needs a C++ compiler)"
	@echo "make fake     write a synthetic bundle into $(RUNS)"
	@echo "make pair     write two comparable synthetic bundles (steady + heavy)"
	@echo "make diff     write the pair and compare them"
	@echo "make inspect  inspect every bundle in $(RUNS)"
	@echo "make viewer   build viewer/dist/index.html (needs npm)"
	@echo "make serve    run the daemon: viewer plus a localhost collect API"
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

# A comparable pair: the same seed, so the same roster and the same code
# paths, at two different loads. What a diff finds is then the load.
pair:
	$(CLI) fake-run --out $(RUNS) --label steady --duration 60 --load steady --seed 4242
	$(CLI) fake-run --out $(RUNS) --label heavy  --duration 90 --load heavy  --seed 4242

diff: pair
	@$(CLI) diff $(RUNS)/*steady.tgz $(RUNS)/*heavy.tgz --kind oncpu

profiles:
	@$(CLI) schema >/dev/null && ls collector/profiles/*.yaml

inspect:
	@for b in $(RUNS)/*.tgz; do $(CLI) inspect "$$b"; echo; done

viewer:
	cd viewer && npm install --no-audit --no-fund && npm run build && npm test

# Needs root to actually collect; without it the API answers can_collect:false
# and the browser says so before anything is disturbed.
serve:
	$(CLI) daemon --out $(RUNS)

clean:
	rm -rf $(RUNS)
	$(MAKE) -C tests/target clean
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
