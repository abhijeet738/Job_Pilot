.PHONY: lint test

lint:
	ruff check src tests

test:
	pytest