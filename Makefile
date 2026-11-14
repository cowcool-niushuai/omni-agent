.PHONY: install dev test lint fmt typecheck check frontend docker-build docker-up docker-down

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

frontend:
	npx --yes tailwindcss@3.4.17 -i static/input.css -o static/app.css --content "static/index.html" --minify

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down
