.PHONY: venv install dev test lint fmt seed eval-redteam eval-summ loadtest up down

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
HOST ?= http://localhost:8000
PROVIDER ?= mock

venv:
	$(PY) -m venv $(VENV)

install: venv
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e ".[dev]"

dev:
	$(BIN)/uvicorn app.main:app --reload --port 8000

test:
	$(BIN)/pytest -q

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

seed:
	$(BIN)/python -m scripts.seed

eval-redteam:
	$(BIN)/python -m evals.redteam.run --gate

eval-summ:
	$(BIN)/python -m evals.summarization.run --provider $(PROVIDER) --gate

loadtest:
	$(BIN)/locust -f loadtest/locustfile.py --headless -u 50 -r 50 --run-time 60s --host $(HOST) --csv loadtest/results

up:
	docker compose up --build -d

down:
	docker compose down -v
