# Repository Guidelines

This guide documents the expectations for maintaining the Code Interpreter API so agents can contribute changes quickly and safely.

## Project Structure & Module Organization
- `app/main.py` boots the FastAPI service and exposes the `code-interpreter-api` entrypoint.
- `app/api/routes.py` defines the `/v1/execute` endpoint; shared validation lives in `app/models/schemas.py`.
- Execution backends reside in `app/services/` (`executor_docker.py`), and configuration utilities live in `app/core/`.
- Integration-focused pytest suites are under `tests/integration_tests/`; add new scenarios beside existing files to keep coverage consistent.

## Build, Test, and Development Commands
- Enable the virtual environment with `source .venv/bin/activate`
- Install dependencies locally: `uv sync --locked`.
- Run the API: `code-interpreter-api` (respects `HOST`/`PORT`; defaults to `127.0.0.1:8000`).
- Type check: `mypy .` (strict mode is enforced).
- Lint and format: `ruff check .`.
- Execute tests: `source .venv/bin/activate && python -m dotenv -f .vscode/.env run -- pytest tests/integration_tests -q`.
- Run hooks on demand: `pre-commit run --all-files` after `pre-commit install`.

## Coding Style & Naming Conventions
- Stick to Python 3.11 syntax with 4-space indentation.
- Maintain `line-length = 100` and satisfy Ruff’s enabled rulesets (E, F, I, UP, B, SIM, ANN, S; S603/S607 ignored).
- Prefer synchronous functions; use descriptive module-level `Final` constants where applicable.
- EVERYTHING MUST be typed.
- Name files and functions by capability (`executor_wasm`, `create_app`) to mirror existing patterns.

## Testing Guidelines
- Write pytest cases under `tests/integration_tests/` using descriptive filenames like `test_<scenario>.py`.
- Exercise both success paths and failure truncation limits; reuse fixtures from `tests/integration_tests/conftest.py` instead of redefining clients.
- Aim to keep runtime under the existing integration suite and ensure `pytest -q` passes before opening a PR.

## Commit & Pull Request Guidelines
- Follow the repo precedent of short, imperative commit subjects (e.g., `add uv.lock`). Include key modules touched in the body when needed.
- Each PR should summarize behavioral changes, note impacted WASM runtimes, and link related issues or tickets.
- Attach command output or screenshots for API responses when they help reviewers verify executor behavior, especially for timeout or limit adjustments.

## Security & Configuration Tips
- Never ship secrets; rely on environment variables for runtime configuration.
- Validate new execution paths against resource guardrails (`MAX_EXEC_TIMEOUT_MS`, `MAX_OUTPUT_BYTES`) and document deviations in the PR description.
