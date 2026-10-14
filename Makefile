.PHONY: install format lint typecheck security contracts test integration check helm terraform image run clean

install:
	uv sync

format:
	uv run black src tests
	uv run ruff check src tests --fix

lint:
	uv run ruff check src tests
	uv run black --check src tests

typecheck:
	uv run mypy

security:
	uv run python -m bandit -q -r src -c pyproject.toml

contracts:
	uv run python -m patient_access_agents.contracts --check

test:
	uv run pytest

integration:
	uv run pytest tests/integration -m integration --no-cov

check: lint typecheck security contracts test

helm:
	helm lint deployments/helm/patient-access-agents -f deployments/helm/patient-access-agents/ci/test-values.yaml

terraform:
	terraform fmt -recursive -check deployments/terraform
	for c in aws azure gcp; do terraform -chdir=deployments/terraform/$$c init -backend=false -input=false && terraform -chdir=deployments/terraform/$$c validate; done

image:
	docker build -t patient-access-agents:dev .

run:
	docker compose up --build

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml
