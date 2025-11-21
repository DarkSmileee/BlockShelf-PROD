.PHONY: help test coverage test-fast test-auth test-permissions test-imports run migrate makemigrations shell createsuperuser collectstatic backup restore clean install install-dev health liveness readiness

help:
	@echo "BlockShelf Development Commands"
	@echo ""
	@echo "Testing:"
	@echo "  make test           - Run tests with pytest"
	@echo "  make coverage       - Run tests with coverage report"
	@echo "  make test-fast      - Run tests excluding slow tests"
	@echo ""
	@echo "Development:"
	@echo "  make run            - Run development server"
	@echo "  make migrate        - Run database migrations"
	@echo "  make shell          - Open Django shell"
	@echo "  make createsuperuser - Create admin user"
	@echo ""
	@echo "Maintenance:"
	@echo "  make clean          - Remove temporary files"
	@echo "  make collectstatic  - Collect static files"
	@echo "  make backup         - Backup database"

# Testing
test:
	pytest

coverage:
	pytest --cov=inventory --cov-report=html --cov-report=term

test-fast:
	pytest -m "not slow"

test-auth:
	pytest -m auth

test-permissions:
	pytest -m permissions

test-imports:
	pytest -m imports

# Development
run:
	python manage.py runserver

migrate:
	python manage.py migrate

makemigrations:
	python manage.py makemigrations

shell:
	python manage.py shell

createsuperuser:
	python manage.py createsuperuser

collectstatic:
	python manage.py collectstatic --noinput

# Database
backup:
	@if [ -f scripts/backup_database.sh ]; then \
		bash scripts/backup_database.sh; \
	else \
		echo "Backup script not found"; \
	fi

restore:
	@if [ -f scripts/restore_database.sh ]; then \
		bash scripts/restore_database.sh; \
	else \
		echo "Restore script not found"; \
	fi

# Maintenance
clean:
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '.pytest_cache' -exec rm -rf {} +
	find . -type f -name '.coverage' -delete
	find . -type d -name 'htmlcov' -exec rm -rf {} +
	find . -type f -name '*.log' -delete

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements.dev.txt

# Health checks
health:
	@curl -f http://localhost:8000/inventory/health/ || echo "Health check failed"

liveness:
	@curl -f http://localhost:8000/inventory/health/liveness/ || echo "Liveness check failed"

readiness:
	@curl -f http://localhost:8000/inventory/health/readiness/ || echo "Readiness check failed"
