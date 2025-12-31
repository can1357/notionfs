# Default recipe
default: check

# Install in development mode
install:
    pip install -e ".[dev]"

# Run all checks (lint + typecheck + test)
check: lint typecheck test

# Run linter
lint:
    ruff check src/ tests/

# Run type checker
typecheck:
    mypy src/

# Run all tests
test:
    pytest

# Run single test (e.g., just test-one test_converter::test_roundtrip)
test-one NAME:
    pytest tests/{{NAME}}.py -v

# Run tests with coverage
coverage:
    pytest --cov=src/notionfs --cov-report=term-missing

# Format code
fmt:
    ruff format src/ tests/
    ruff check --fix src/ tests/

# Clean build artifacts and caches
clean:
    rm -rf build/ dist/ *.egg-info/
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
