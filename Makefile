.PHONY: verify test live lint fmt fmt-check typecheck mdlint

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

# Real Claude Code; run locally after every Claude Code upgrade, never in CI. It costs API
# usage (capped by CSESSION_LIVE_MAX_USD, default 3) and needs credentials: see docs/live-tests.md.
# `pytest -m live` alone only skips: CSESSION_LIVE=1 is the opt-in.
live:
	CSESSION_LIVE=1 uv run pytest -m live tests/live $(PYTEST_ARGS)

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .
