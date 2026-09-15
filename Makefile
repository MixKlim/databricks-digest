help:
	@echo "  cache             - Clear uv's cache"
	@echo "  audit             - Run uv audit to check for vulnerabilities"
	@echo "  sync              - Run uv sync to update dependencies"
	@echo "  eval              - Run pre-commit checks on all files"
	@echo "  test              - Run unit tests with pytest"
	@echo "  cov               - Generate coverage report and badge"
	@echo "  validate          - Validate the Databricks bundle"
	@echo "  build             - Evaluate code, run tests, and generate coverage"

cache:
	@echo "Clear uv's cache"
	uv cache clear PyPI --all --no-interaction

audit:
	@echo "Running uv audit"
	uv audit --upgrade

sync:
	@echo "Running uv sync"
	uv sync --locked --all-groups --all-extras

eval:
	@echo "Running pre-commit"
	uv run pre-commit run --all-files

test:
	@echo "Running unit test"
	uv run pytest --doctest-modules --cov=src --cov=scripts --cov-report=html

cov:
	@echo "Creating coverage badge"
	uv run pytest --doctest-modules --cov=src --cov=scripts --cov-report=term-missing
	uv run coverage report --fail-under=100
	uv run coverage xml -o ./reports/coverage/coverage.xml
	uv run coverage html
	uv run genbadge coverage --output-file reports/coverage/coverage-badge.svg

validate:
	@echo "Validating bundle"
	databricks bundle validate

build: audit sync eval test cov validate
