# code-interpreter-go

Go port of the `code-interpreter` FastAPI service. Manages execution of
arbitrary Python code; the server itself never runs user-defined Python — it
delegates to an isolated `executor` backend (Docker container or Kubernetes
pod).

## Layout

```
cmd/code-interpreter-api/   entrypoint (port of app/main.py)
internal/api/               HTTP routes + schemas (app/api/routes.py, app/models/schemas.py)
internal/bootstrap/         Docker environment setup at startup (port of entrypoint.sh)
internal/executor/          backend abstraction, Docker and Kubernetes executors
internal/storage/           uploaded-file storage (app/services/file_storage.py)
internal/config/            environment-based settings (app/app_configs.py)
internal/logging/           plain/JSON logging (app/logging_config.py)
internal/imageref/          Docker image reference normalization (app/image_ref.py)
```

## Build & run

```bash
go build ./...
go run ./cmd/code-interpreter-api   # respects HOST/PORT, defaults 0.0.0.0:8000
```

Docker image (same Docker-in-Docker / Docker-out-of-Docker support as the
Python image; the entrypoint.sh logic is built into the binary). Bases
default to Docker Hardened Images mirrored into the org namespace
(`onyxdotapp/dhi-golang`, `onyxdotapp/dhi-debian`); override the build args
to use public images:

Image variants are defined in `docker-bake.hcl`; CI builds each platform on
a native runner from the same definition and merges the manifests.

Local builds use the `local` targets, which build only your machine's native
platform (no QEMU needed) and load the result into `docker images`:

```bash
docker buildx bake local        # code-interpreter-go:local and :local-slim
docker buildx bake slim-local   # just the slim variant
# Without a DHI subscription:
BUILD_IMAGE=golang:1.26-bookworm RUNTIME_IMAGE=debian:bookworm-slim \
  docker buildx bake local
# Plain docker build works too:
docker build . -t code-interpreter-go
```

## Published images

Each release publishes two variants of `onyxdotapp/code-interpreter-go`:

- **`:<version>` (default)** — includes the Docker daemon, so every
  deployment mode works out of the box, including Docker-in-Docker. Pick
  this if unsure.
- **`:<version>-slim`** — no docker packages at all (built with
  `SKIP_NESTED_DOCKER=1`). Opt in when deploying with a mounted socket
  (Docker-out-of-Docker) or on Kubernetes; because the service talks to the
  Engine API socket directly, DooD needs no docker packages in the image —
  the Python service still required the docker CLI for that.

## Deployment modes

In order of preference:

1. **Kubernetes** (`EXECUTOR_BACKEND=kubernetes`): executors run as locked
   down pods; no Docker anywhere. Use the `-slim` image.
2. **Docker-out-of-Docker**: mount a Docker socket
   (`-v /var/run/docker.sock:/var/run/docker.sock`); executor containers are
   created on that daemon. Use the `-slim` image. Prefer mounting a
   **rootless** daemon's socket (`$XDG_RUNTIME_DIR/docker.sock`): a rootful
   socket is root-equivalent on the host, while a rootless daemon caps the
   blast radius of a service compromise at one unprivileged user and adds
   user-namespace isolation around executors. Rootless requires cgroup v2
   with systemd delegation for the memory/pids limits to apply.
3. **Docker-in-Docker** (default image, `--privileged`, no socket mounted):
   the service starts its own nested daemon. For environments where no
   daemon can be shared. Note `--privileged` disables seccomp/AppArmor and
   is effectively root on the host — rootless DooD is the safer choice when
   available.

## Test

```bash
go vet ./...
go test ./...

# Optional end-to-end test against a real Docker daemon:
CODE_INTERPRETER_GO_E2E=1 go test ./internal/executor -run TestDockerExecutorEndToEnd
```

## Configuration

Same environment variables as the Python service: `EXECUTOR_BACKEND`
(docker|kubernetes), `PYTHON_EXECUTOR_DOCKER_IMAGE/RUN_ARGS/NETWORK`,
`KUBERNETES_EXECUTOR_NAMESPACE/IMAGE/SERVICE_ACCOUNT/NET_ADMIN_LOCKDOWN`,
`MAX_EXEC_TIMEOUT_MS`, `MAX_OUTPUT_BYTES`, `CPU_TIME_LIMIT_SEC`,
`MEMORY_LIMIT_MB`, `HOST`, `PORT`, `LOG_LEVEL`, `LOG_FORMAT`,
`FILE_STORAGE_DIR`, `MAX_FILE_SIZE_MB`, `FILE_TTL_SEC`.

Docker-specific changes:

- The service talks to the Docker Engine API directly (no docker CLI
  subprocesses), so the daemon connection uses the standard `DOCKER_HOST` /
  `DOCKER_API_VERSION` / `DOCKER_CERT_PATH` / `DOCKER_TLS_VERIFY` variables,
  defaulting to the local socket. `PYTHON_EXECUTOR_DOCKER_BIN` is no longer
  read.
- `PYTHON_EXECUTOR_DOCKER_RUN_ARGS` accepts a documented subset instead of
  arbitrary `docker run` flags: `--label`, `--env`/`-e`, `--add-host`,
  `--dns`, and `--ulimit` (space or `=` separated). Any other flag fails at
  startup rather than being silently dropped.

## API

Same surface and payloads as the Python service:

- `GET /health`
- `POST /v1/execute` and `POST /v1/execute/stream` (SSE: `output`, `result`,
  `error` events)
- `POST/GET/DELETE /v1/files`, `GET /v1/files/{file_id}`
- `POST /v1/sessions`, `DELETE /v1/sessions/{session_id}`,
  `POST /v1/sessions/{session_id}/bash`

## Intentional differences from the Python implementation

- **No shell entrypoint**: the binary itself performs entrypoint.sh's job at
  startup — mode detection (Kubernetes / external `DOCKER_HOST` / mounted
  socket / Docker-in-Docker), cgroup v2 nesting setup, launching and
  supervising a nested `dockerd`, and waiting for readiness via an Engine
  API ping instead of `docker info`. The image runs the binary directly and
  needs no shell at runtime.
- **Docker Engine API instead of the docker CLI**: containers are managed via
  `github.com/docker/docker/client` (create/start/kill/list/remove, exec
  attach with demultiplexed streams, image pull), giving typed error handling
  (e.g. not-found) instead of parsing subprocess stderr. File staging and the
  workspace snapshot still run `tar` *inside* the container over the exec
  API, because `/workspace` is a tmpfs mount that the engine's copy API
  cannot see. See the configuration notes above for the
  `PYTHON_EXECUTOR_DOCKER_BIN` and `RUN_ARGS` implications.
- **Validation error bodies**: request-validation failures return
  `{"detail": "<message>"}` (a string) with HTTP 422, rather than Pydantic's
  array-of-objects `detail`. Status codes are unchanged.
- **Ephemeral container keepalive**: the idle `sleep` for one-shot executions
  is `timeout_ms/1000 + 10` seconds. The Python version computed
  `timeout_ms * 1000 + 10` (a unit slip yielding multi-day sleeps); the
  container is killed after execution either way, but a crashed API now leaks
  the container for seconds instead of days.
- **Kubernetes workspace snapshot**: streamed over SPDY, which is
  binary-safe, so the `tar | base64` round-trip the websocket client needed
  is gone — the pod-side command is plain `tar -c`.
- **Hardened base images**: the Dockerfile defaults to Docker Hardened Images
  (DHI) `-dev` variants for both build and runtime stages, overridable via
  `BUILD_IMAGE` / `RUNTIME_IMAGE` build args.
- **No OpenAPI docs endpoints**: `/docs`, `/redoc`, and `/openapi.json` are
  not served.
