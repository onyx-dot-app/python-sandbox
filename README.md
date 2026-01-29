# Code Interpreter

**NOTE:** Code Interpreter is currently in Alpha. Be careful with production usage.

A secure, FastAPI-based service for executing Python code in isolated Docker containers. This service provides a REST API for running untrusted Python code with strict resource limits, timeout controls, and file handling capabilities.

The goal of the project is to be the easiest, lightest weight way to add secure python execution to your AI agent.

Powers the Code Interpreter in [Onyx](https://github.com/onyx-dot-app/onyx). Checkout the implementation
[here]() as a good reference for using this in your app.

## Quick Start

### Docker Deployment

The code-interpreter service needs access to Docker to run code in isolated containers. There are two deployment modes:

#### Option 1: Docker-out-of-Docker (Recommended)

This is the recommended approach for most use cases. It shares the host's Docker daemon for better performance:

```bash
docker run --rm -it \
  --user root \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  onyxdotapp/code-interpreter
```

**When to use:**
- You have access to the host Docker socket
- You want better performance and faster startup times
- You're running in a trusted environment

**Note:** Requires `--user root` to access the Docker socket. The executor image will be pulled at runtime if not already present on the host.

#### Option 2: Docker-in-Docker

Use this when you need complete isolation or can't access the host Docker socket:

```bash
docker run --rm -it \
  --privileged \
  -p 8000:8000 \
  onyxdotapp/code-interpreter
```

**When to use:**
- You need complete isolation between the service and host
- You can't or don't want to mount the host Docker socket
- You're running in a restricted environment

**Important notes:**
- Requires `--privileged` flag
- The Docker daemon will automatically start inside the container (takes a few seconds)
- On first run, the executor image will be pulled during server startup (~30-60 seconds)
- Subsequent runs will reuse the cached image (instant startup)
- The server will not accept requests until the executor image is available

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
git clone https://github.com/onyx-dot-app/code-interpreter.git
cd code-interpreter

# Activate an virtual environment
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

## API Usage

NOTE: for full API docs, start the service up and visit `/docs`. 

### Execute Python Code

```bash
POST /v1/execute
```

**Request:**
```json
{
  "code": "print('Hello, World!')\n2 + 2",
  "stdin": null,
  "timeout_ms": 2000,
  "last_line_interactive": true,
  "files": []
}
```

**Response:**
```json
{
  "stdout": "Hello, World!\n4\n",
  "stderr": "",
  "exit_code": 0,
  "timed_out": false,
  "duration_ms": 145,
  "files": []
}
```

### File Management

Upload a file for use in code execution:

```bash
POST /v1/files
Content-Type: multipart/form-data

# Upload file
curl -X POST http://localhost:8000/v1/files \
  -F "file=@data.csv"
```

Use uploaded files in execution:

```json
{
  "code": "import pandas as pd\ndf = pd.read_csv('data.csv')\nprint(df.head())",
  "files": [
    {
      "path": "data.csv",
      "file_id": "uuid-from-upload-response"
    }
  ]
}
```

Retrieve generated files:

```bash
GET /v1/files/{file_id}
```

List all files:

```bash
GET /v1/files
```

Delete a file:

```bash
DELETE /v1/files/{file_id}
```

## Configuration

Configure the service via environment variables:

- `HOST`: Server host (default: `0.0.0.0`)
- `PORT`: Server port (default: `8000`)
- `MAX_EXEC_TIMEOUT_MS`: Maximum execution timeout in milliseconds (default: `10000`)
- `CPU_TIME_LIMIT_SEC`: CPU time limit per execution (default: `5`)
- `MEMORY_LIMIT_MB`: Memory limit per execution (default: `128`)
- `MAX_OUTPUT_BYTES`: Maximum output size (default: `1048576` = 1MB)
- `MAX_FILE_SIZE_MB`: Maximum file upload size (default: `10`)
- `FILE_STORAGE_DIR`: Directory for file storage (default: `/tmp/code-interpreter-files`)

## Development

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

## Architecture

The service follows a layered architecture:

- **API Layer** (`app/api/`): FastAPI routes and request handling
- **Service Layer** (`app/services/`): Business logic and execution backends
- **Models** (`app/models/`): Pydantic schemas for request/response validation
- **Configuration** (`app/core/`): Environment-based settings management

Execution is handled through an abstraction layer supporting multiple backends:
- **Docker Executor**: Runs code in isolated Docker containers (recommended)

## Security

- All code execution happens in isolated environments
- Strict resource limits prevent resource exhaustion
- No direct filesystem access to host system
- Configurable timeouts prevent infinite loops
- Output size limits prevent memory attacks
- File uploads are validated and size-limited

## License

MIT License - see [LICENSE](LICENSE) file for details.

Copyright (c) 2025-present DanswerAI, Inc.
