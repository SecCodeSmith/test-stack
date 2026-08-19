# Convenience targets. On Windows without `make`, run the docker/pytest commands directly.

.PHONY: help up down destroy logs mock-logs ps-logs build test venv

help:
	@echo "up          - build & start the full stack (PrestaShop + DB + mock)"
	@echo "down        - stop the stack (keep volumes)"
	@echo "destroy     - stop the stack and delete volumes (fresh PrestaShop next time)"
	@echo "logs        - follow all logs"
	@echo "mock-logs   - follow the FastAPI mock logs"
	@echo "ps-logs     - follow the PrestaShop logs"
	@echo "test        - run the mock's pytest suite locally"

up:
	docker compose up -d --build

down:
	docker compose down

destroy:
	docker compose down -v

logs:
	docker compose logs -f

mock-logs:
	docker compose logs -f ifirma-mock

ps-logs:
	docker compose logs -f prestashop

build:
	docker compose build

venv:
	python -m venv .venv && .venv/bin/pip install -r ifirma-mock/requirements-dev.txt

test:
	cd ifirma-mock && python -m pytest
