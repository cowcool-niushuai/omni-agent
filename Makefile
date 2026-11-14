.PHONY: install dev test lint fmt typecheck check frontend docker-build docker-up docker-down \
	desktop-install desktop desktop-test desktop-smoke desktop-pack desktop-dist

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

desktop-install:
	cd desktop && npm install --no-audit --no-fund

desktop:
	cd desktop && env -u ELECTRON_RUN_AS_NODE npm start

desktop-test:
	cd desktop && npm test

desktop-smoke:
	cd desktop && env -u ELECTRON_RUN_AS_NODE npx electron . --smoke-test

desktop-pack:
	cd desktop && npm run pack

desktop-dist:
	cd desktop && npm run dist

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down
