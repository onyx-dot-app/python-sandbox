# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Development Setup
```bash
# Activate virtual environment
source .venv/bin/activate

# Install/sync dependencies (using uv)
uv sync --locked
```

### Running the Service
```bash
# Start the API server (uses HOST/PORT env vars, defaults to 0.0.0.0:8000)
code-interpreter-api
```

### Code Quality Checks
```bash
# Type checking - MUST pass strict mypy
mypy .

# Linting and formatting
ruff check .
ruff format .

# Run all pre-commit hooks
pre-commit run --all-files
```

### Testing
```bash
# Run integration tests
pytest tests/integration_tests -q

# Run with environment variables from VS Code
source .venv/bin/activate && python -m dotenv -f .vscode/.env run -- pytest tests/integration_tests -q

# Run a specific test
pytest tests/integration_tests/test_file.py::test_function_name -v
```

## Architecture Overview

This is a **FastAPI-based code execution API** that safely runs Python code in sandboxed environments. The system uses Docker containers for secure isolation.

### Core Architecture Patterns

1. **Layered Architecture**:
   - API routes in `app/api/` handle HTTP requests
   - Business logic in `app/services/` manages execution backends
   - Data models in `app/models/` define request/response schemas
   - Configuration in `app/core/` handles environment-based settings

2. **Execution Backend Abstraction**:
   - The service supports swappable execution strategies via a plugin pattern
   - `executor_docker.py`: Uses Docker containers for stronger isolation
   - Selection is environment-based, allowing runtime configuration

3. **Security-First Design**:
   - All code execution happens in isolated environments with strict resource limits
   - Configurable timeouts (MAX_EXEC_TIMEOUT_MS), memory limits (MEMORY_LIMIT_MB), and output size limits (MAX_OUTPUT_BYTES)
   - No direct filesystem access; files are staged through base64 encoding

4. **Type Safety Enforcement**:
   - Strict mypy checking is mandatory - all code must pass `mypy --strict`
   - All API contracts use Pydantic models for automatic validation
   - Comprehensive type annotations throughout the codebase

### Key Implementation Details

- **Main API Endpoint**: `POST /v1/execute` accepts Python code and optional file inputs
- **Configuration**: Uses frozen dataclasses with LRU caching for immutable, efficient settings
- **Error Handling**: Structured exceptions with proper HTTP status codes and validation error details
- **File Handling**: Files are passed as base64-encoded content with specified paths for workspace staging
- **Testing**: Integration tests in `tests/integration_tests/` cover various execution scenarios

### Development Guidelines

- Always ensure mypy strict mode passes before committing
- Add integration tests for new execution scenarios
- Follow existing module structure: API routes → services → models
- Use environment variables for configuration, never hardcode values
- Maintain the security boundaries - never bypass sandbox restrictions

### Testing

There are two main kinds of tests:

#### Integration Tests

- Under the `code-interpreter/tests/integration_tests` directory
- Doesn't require anything to be running - spins up a dummy FastAPI server for each run
- Primary way of testing functionality


#### E2E Tests

- Under the `code-interpreter/tests/e2e` directory
- Requires the code-interpreter service to be running. Usually as a Docker container.
- After making changes, if you want to run these tests make sure to (1) stop existing containers, \
(2) build new images, and (3) run the new containers.
