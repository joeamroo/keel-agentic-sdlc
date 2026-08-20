VENV := .venv/bin/python

.PHONY: help setup demo demo-live test test-fast scenarios diagram clean

help:
	@echo "setup       create the venv and install"
	@echo "demo        replay the recorded greenfield run, no API key needed"
	@echo "demo-live   run greenfield against the real API, needs ANTHROPIC_API_KEY"
	@echo "scenarios   replay all three scenarios"
	@echo "test        full suite"
	@echo "test-fast   suite without the tests that bind ports"
	@echo "diagram     export docs/architecture.drawio to PDF"

setup:
	uv venv --python 3.13
	uv pip install -e ".[dev]"

demo:
	$(VENV) -m keel.cli run --scenario greenfield --replay-from demo-greenfield

demo-live:
	$(VENV) -m keel.cli run --scenario greenfield --mode live

scenarios:
	$(VENV) -m keel.cli run --scenario greenfield --replay-from demo-greenfield
	$(VENV) -m keel.cli run --scenario ambiguous  --replay-from demo-ambiguous
	$(VENV) -m keel.cli run --scenario ambiguous  --replay-from demo-ambiguous-resumed --answer-ambiguities
	@echo
	@echo "brownfield is recorded only as far as implementation; re-record with --mode live"


test:
	$(VENV) -m pytest -q

test-fast:
	$(VENV) -m pytest -q -m "not http"

diagram:
	@command -v drawio >/dev/null 2>&1 || { echo "drawio CLI not found: brew install --cask drawio"; exit 1; }
	drawio --export --format pdf --output docs/architecture.pdf docs/architecture.drawio

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
