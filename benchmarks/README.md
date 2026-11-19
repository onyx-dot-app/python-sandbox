# Python vs Go service benchmarks

Measured comparison of the two code-interpreter implementations
(`code-interpreter/`, Python/FastAPI/uvicorn vs `code-interpreter-go/`,
Go/net/http) to inform the migration decision.

## Methodology

`harness/` spawns each service as a native process with identical
configuration (single instance, access logs suppressed on both, unique file
storage dir per run) and measures:

- **Cold start**: process spawn → first `200` from `GET /v1/files`, polled
  every 2 ms. 11 runs each, first discarded (page cache / `.pyc` warmup).
- **CPU-to-ready**: `utime+stime` from `/proc/<pid>/stat` at readiness.
- **Memory**: `VmRSS`/`VmHWM` from `/proc/<pid>/status` after a 3 s idle
  settle, and again after the load phases.
- **API-layer latency/throughput**: identical HTTP client (keep-alive,
  N workers, fixed wall-clock duration) driving storage and validation
  endpoints, which exercise routing, (de)serialization, validation, and file
  I/O without needing an executor backend.

Environment: 24-CPU shared Linux host (noisy neighbors — absolute numbers
vary run to run; ratios are stable). Two full runs are reported as ranges.
The sandbox lacks `CAP_SYS_ADMIN`, so no Docker daemon could run:
**end-to-end `/v1/execute` latency (container spin-up dominated) is not
measured here** — the Go repo's CI runs a real-daemon e2e test, and both
implementations pay the same daemon-side container costs; they differ in
orchestration overhead (Python spawns ~6 docker-CLI subprocesses per
execution, Go makes Engine API calls over one socket), which this harness
cannot isolate without a daemon.

Reproduce:

```bash
(cd code-interpreter && uv sync --locked)
(cd code-interpreter-go && CGO_ENABLED=0 go build -trimpath \
  -ldflags="-s -w" -o /tmp/code-interpreter-api ./cmd/code-interpreter-api)
cd benchmarks/harness && go run .
python3 benchmarks/imagesizes.py   # registry-reported compressed image sizes
```

## Results (2026-07, two runs, median values shown as ranges)

### Startup and memory

| Metric | Python | Go | Go advantage |
|---|---|---|---|
| Cold start to first served request | 271–763 ms | 21–30 ms | **13–25× faster** |
| CPU time to readiness | 0.26–0.73 s | 0.02–0.03 s | **~13–24× less** |
| Idle RSS | 48 MiB | 31 MiB | **~36% less** |
| RSS after load burst | 52 MiB | 43 MiB | ~17% less |

### API-layer throughput (single instance)

| Endpoint (workers) | Python req/s | Go req/s | p50 latency (Py → Go) | Speedup |
|---|---|---|---|---|
| download 100 KiB (c=1) | 1 114–2 112 | 3 709–4 083 | 0.42–0.62 ms → 0.19 ms | ~2–3.5× |
| download 100 KiB (c=16) | 1 227–1 717 | 8 330–9 870 | 7.7–11.4 ms → 1.3–1.5 ms | **~6–8×** |
| list files (c=16) | 517–590 | 14 859–18 179 | 25–27 ms → 0.6–0.8 ms | **~25–35×** |
| execute validation path (c=1) | 880–1 160 | 9 694–11 355 | 0.6–0.7 ms → 0.07 ms | **~8–13×** |
| upload 100 KiB (c=4) | 292–477 | 1 711–2 269 | 7.7–11.7 ms → 1.6–2.0 ms | ~4–6× |

The Python service plateaus under concurrency (GIL + threadpool: c=16
download throughput barely exceeds c=1); the Go service scales with cores.
Matching Go's c=16 throughput with uvicorn workers would take roughly 6–8
processes at ~48 MiB each (~300–400 MiB) vs one 43 MiB Go process.

### Sizes

Registry-reported compressed sizes (Docker Hub manifests, linux/amd64):

| Artifact | Size | Source |
|---|---|---|
| `onyxdotapp/code-interpreter:latest` (Python, with Docker daemon) | **194 MB** | measured (registry) |
| — of which docker-ce layer | 104 MB | measured (largest layer) |
| — of which `python:3.11-slim` base | 45 MB | measured (registry) |
| — of which app deps/venv + uv layers | ~38 MB | measured (remaining layers) |
| `debian:bookworm-slim` / `trixie-slim` base | 28 / 30 MB | measured (registry) |
| Go service binary (stripped, gzipped) | 13 MB | measured |
| **Go `-slim` image (base + binary + ca-certs/passwd)** | **~45 MB** | derived |
| **Go default image (adds the same docker-ce layer)** | **~150 MB** | derived |

On-disk application payload: Python needs the CPython runtime (84 MB) plus a
129 MB venv; the Go service is a single 46 MB static binary.

## Not measured

- End-to-end `/v1/execute` (needs a Docker daemon; dominated by container
  create/start either way). Run the harness on a Docker-capable host to add
  this; CI's `TestDockerExecutorEndToEnd` covers correctness.
- Kubernetes-backend performance.
- Uncompressed on-disk image sizes (needs a daemon to build; compressed
  pull sizes above are what registries report and networks transfer).
