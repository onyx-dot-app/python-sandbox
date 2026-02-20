## Development

#### Building from Source

```bash
# Standard build (executor image pulled at runtime)
docker build -t code-interpreter -f code-interpreter/Dockerfile .

# Build with pre-loaded executor image for instant DinD startup
./build-preloaded.sh code-interpreter:preloaded
```

**Build arguments:**
- `SKIP_NESTED_DOCKER=1` - Skip installing Docker entirely (only for Docker-out-of-Docker mode)
- `PYTHON_EXECUTOR_DOCKER_IMAGE=custom/image` - Use a custom executor image

#### Pre-loaded Images for Faster Startup

For production or offline environments, you can build an image with the executor pre-embedded:

```bash
# Build pre-loaded image (includes executor, ~1GB larger but instant DinD startup)
./build-preloaded.sh code-interpreter:preloaded

# Run with instant startup (no pulling needed)
docker run --rm -it \
  --privileged \
  -p 8000:8000 \
  code-interpreter:preloaded
```

This is ideal for:
- Production deployments (predictable startup times)
- Air-gapped/offline environments
- CI/CD pipelines
- Any scenario where you want instant DinD readiness

### Local Deployment

#### Prerequisites

- Python 3.11
- Docker (for execution isolation)
- [uv](https://github.com/astral-sh/uv) package manager (recommended)

#### Running the Service

```bash
# Clone the repository
git clone https://github.com/onyx-dot-app/python-sandbox.git
cd code-interpreter

# Activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
uv sync --locked
```

```bash
# Start the API server (defaults to 0.0.0.0:8000)
code-interpreter-api

# Or specify host/port via environment variables
HOST=127.0.0.1 PORT=8080 code-interpreter-api
```

### Code Quality

```bash
# Type checking (must pass strict mypy)
mypy .

# Linting
ruff check .

# Formatting
ruff format .

# Run all pre-commit hooks
pre-commit run --all-files
```

### Testing

```bash
# Run integration tests
pytest tests/integration_tests -q

# Run a specific test
pytest tests/integration_tests/test_file.py::test_function_name -v
```
