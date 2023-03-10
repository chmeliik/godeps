# Start by creating a venv

.PHONY: venv
venv:
	virtualenv --python=3.11 venv
	. venv/bin/activate && \
	pip install -U pip && \
	pip install -r requirements-dev.txt && \
	pip install -e .

# Everything else depends on the venv

.PHONY: fmt
fmt:
	. venv/bin/activate && \
	black src/ && \
	ruff check --fix-only src/

.PHONY: check
check: black ruff mypy

.PHONY: black
black:
	. venv/bin/activate && \
	black --check --diff src/

.PHONY: ruff
ruff:
	. venv/bin/activate && \
	ruff check src/

.PHONY: mypy
mypy:
	. venv/bin/activate && \
	mypy src/

.PHONY: pip-compile
pip-compile:
	. venv/bin/activate && \
	pip install -U pip-tools && \
	pip-compile pyproject.toml --generate-hashes && \
	pip-compile pyproject.toml --generate-hashes --extra=dev -o requirements-dev.txt

# Extras

.PHONY: submodules
submodules:
	git submodule update --init
