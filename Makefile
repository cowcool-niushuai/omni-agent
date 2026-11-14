.PHONY: install dev test lint fmt typecheck check

install:
	uv sync --all-groups

dev:
	uv run uvicorn server:app --host $${HOST:-127.0.0.1} --port $${PORT:-8001} --reload

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

typecheck:
	uv run mypy

check: lint typecheck test
