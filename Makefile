.POSIX:
PYTHON ?= python3
KILIX_CONTENT ?= ../kilix-content

.PHONY: all test lint sync install clean

all: test lint

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -t . -v

lint:
	PYTHONPATH=src $(PYTHON) -m compileall -q src tests

sync:
	uv sync --locked

install:
	uv tool install --force --with $(KILIX_CONTENT) .

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
