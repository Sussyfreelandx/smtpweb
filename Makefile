# Makefile - Common development and deploy helpers
# Usage:
#   make install
#   make run
#   make worker
#   make migrate
#   make upgrade
#   make release-migrate

PYTHON := python3
PIP := pip
FLASK := flask
MANAGE := $(PYTHON) manage.py

export FLASK_APP = wsgi:app

.PHONY: install
install:
	$(PIP) install -r requirements.txt

.PHONY: run
run:
	@echo "Starting Flask development server..."
	$(MANAGE) runserver

.PHONY: run-dev
run-dev:
	@echo "Starting Flask dev server (FLASK_ENV=development)..."
	FLASK_ENV=development FLASK_DEBUG=1 $(MANAGE) runserver

.PHONY: worker
worker:
	@echo "Starting Celery worker..."
	$(MANAGE) celery-worker

.PHONY: shell
shell:
	$(MANAGE) shell

.PHONY: db-init
db-init:
	@echo "Initializing migrations..."
	-$(FLASK) db init
	-$(FLASK) db migrate -m "Initial migration"

.PHONY: migrate
migrate:
	$(FLASK) db migrate -m "Auto migration"

.PHONY: upgrade
upgrade:
	$(FLASK) db upgrade

.PHONY: release-migrate
release-migrate:
	sh ./release_migrate.sh

.PHONY: lint
lint:
	flake8 .

.PHONY: test
test:
	pytest -q
