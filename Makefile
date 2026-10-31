.PHONY: build test test-all audit smoke demo verify-results

build:
	python3 scripts/build.py

test:
	./scripts/test.sh

audit:
	./scripts/audit-public.sh

smoke:
	PYTHONDONTWRITEBYTECODE=1 python3 src/agent/run_kernadapt_online.py --help

test-all:
	./tests/run.sh

demo:
	./demo/run.sh

verify-results:
	python3 tests/verify_results.py
