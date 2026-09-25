# How It Works: Architecture & Security

This document provides an in-depth explanation of the code-interpreter service architecture, execution environments, and security model.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
  - [Layered Design](#layered-design)
  - [Execution Flow](#execution-flow)
- [Execution Environments](#execution-environments)
  - [Docker Executor](#docker-executor)
  - [Kubernetes Executor](#kubernetes-executor)
- [Security Model](#security-model)
  - [Isolation Mechanisms](#isolation-mechanisms)
  - [Resource Limits](#resource-limits)
  - [Attack Surface Mitigation](#attack-surface-mitigation)
- [Last-Line Interactive Mode](#last-line-interactive-mode)
- [File Management](#file-management)
- [Capacity and Overload](#capacity-and-overload)

## Overview

The code-interpreter service is a secure FastAPI-based platform for executing untrusted Python code in isolated environments. It uses a pluggable executor architecture that supports different isolation backends (Docker, Kubernetes) while maintaining consistent security guarantees.

## Architecture

### Layered Design

The service follows a clean separation of concerns across four layers:

```
┌─────────────────────────────────────────────────────┐
│  API Layer (FastAPI)                                │
│  - Request validation (Pydantic)                    │
│  - HTTP routing                                     │
│  - Error handling                                   │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│  Service Layer                                      │
│  - Executor factory (backend selection)             │
│  - File management                                  │
│  - Business logic                                   │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│  Executor Abstraction Layer                         │
│  - BaseExecutor (abstract interface)                │
│  - ExecutorProtocol (type interface)                │
│  - Common utilities (output truncation, etc.)       │
└──────────────────┬──────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        │                     │
┌───────▼───────┐    ┌────────▼────────┐
│ DockerExecutor│    │KubernetesExecutor│
│               │    │                  │
└───────────────┘    └──────────────────┘
```

**Layer Responsibilities:**

- **API Layer**: Validates requests, enforces API contracts, handles HTTP concerns
- **Service Layer**: Orchestrates business logic, manages executor lifecycle
- **Executor Layer**: Provides unified interface for different execution backends
- **Backend Implementations**: Handle environment-specific isolation and execution

### Execution Flow

1. **Request Reception**: FastAPI receives POST request at `/v1/execute`
2. **Validation**: Pydantic models validate code, files, and execution parameters
3. **Executor Selection**: Factory pattern selects backend based on `EXECUTOR_BACKEND` env var
4. **File Preparation**: Files encoded as base64 are decoded and prepared for injection
5. **Code Wrapping**: If `last_line_interactive=true`, code is wrapped to auto-print last expression
6. **Environment Creation**: Ephemeral isolated environment is created
7. **Code Execution**: Python code runs in isolation with resource limits enforced
8. **Output Collection**: stdout, stderr, exit code, and execution time are captured
9. **Workspace Extraction**: Generated files are extracted from the workspace
10. **Cleanup**: Execution environment is destroyed
11. **Response**: Results are returned to client

## Execution Environments

### Docker Executor

The Docker executor (`executor_docker.py`) provides strong isolation using Linux containers.

#### Security Architecture

**Container Lifecycle:**

```
1. Create ephemeral container (--rm, --network none)
2. Start with sleep command (detached mode)
3. Inject code + files via tar archive
4. Execute Python as unprivileged user (uid:gid 65532:65532)
5. Extract workspace artifacts
6. Kill and cleanup container
```

**Security Controls:**

| Control | Implementation | Purpose |
|---------|---------------|---------|
| **Network Isolation** | `--network none` | No network access (prevents data exfiltration) |
| **Capability Dropping** | `--cap-drop ALL`, `--cap-add CHOWN` | Minimal privileges (CHOWN only for workspace setup) |
| **No New Privileges** | `--security-opt no-new-privileges` | Prevents privilege escalation |
| **Process Limits** | `--pids-limit 64` | Prevents fork bombs |
| **Unprivileged Execution** | Run as user 65532:65532 | Non-root execution |
| **Read-Only Root** | Workspace as tmpfs | Prevents filesystem tampering |
| **Ephemeral Containers** | `--rm` flag | Automatic cleanup |
| **Memory Limits** | `--memory`, `--memory-swap` | Prevents memory exhaustion |
| **CPU Limits** | `--ulimit cpu` | Prevents CPU exhaustion |

**File System Isolation:**

```
Container Filesystem Layout:

/               (read-only root filesystem)
├── tmp/        (tmpfs, 64MB, writable)
│   └── matplotlib/  (matplotlib cache dir)
├── workspace/  (tmpfs, 100MB, owned by 65532:65532)
│   ├── __main__.py   (injected user code)
│   └── <user-files>  (injected via tar)
└── opt/
    └── executor-venv/  (pre-installed Python packages)
```

- **Workspace Injection**: Code and files are injected via tar archive streaming
- **Ownership**: All workspace files owned by unprivileged user (65532:65532)
- **Isolation**: Workspace is ephemeral tmpfs (memory-backed, no disk persistence)

**Resource Limits:**

```python
# Memory (configurable via MEMORY_LIMIT_MB)
--memory 256m --memory-swap 256m  # Hard limit, no swap

# CPU Time (configurable via CPU_TIME_LIMIT_SEC)
--ulimit cpu=5:5  # SIGKILL after 5 seconds of CPU time

# Process Count
--pids-limit 64  # Maximum 64 processes

# Filesystem
--tmpfs /tmp:size=64m        # 64MB temp storage
--tmpfs /workspace:size=100m  # 100MB workspace
```

**Execution Model:**

1. Container starts with `sleep` command (keeps container alive)
2. Tar archive with code + files streamed via stdin to `docker exec tar -x`
3. Python execution via `docker exec -u 65532:65532 python __main__.py`
4. Timeout enforcement via `subprocess.communicate(timeout=...)`
5. On timeout: `pkill -9 python` inside container, then kill container

#### Docker-out-of-Docker (Recommended)

The recommended deployment mode mounts the host's Docker socket:

```bash
docker run --rm -it \
  --user root \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  code-interpreter
```

**Security Considerations:**

- **Privilege Requirement**: Requires root inside API container to access Docker socket. Note
that the container actually running arbitrary python, does not use the root user. Also does 
not require `--privileged`.
- **Isolation Trade-off**: Executor containers run on host Docker daemon.
- **Attack Vector**: Compromised API container could spawn malicious containers
- **Mitigation**: API container itself should be isolated (network policies, resource limits)

**Advantages:**

- Simpler deployment (no Docker-in-Docker)
- Better performance (no nested virtualization)
- Standard Docker security controls apply

#### Docker-in-Docker (Alternative)

Alternatively, enable nested Docker:

```bash
docker build -f code-interpreter/Dockerfile .
docker run --privileged code-interpreter
```

**Trade-offs:**

- ⚠️ Requires `--privileged` flag
- Stronger isolation between API and executor containers
- More complex setup, potential stability issues

### Kubernetes Executor

The Kubernetes executor (`executor_kubernetes.py`) provides cloud-native, scalable isolation using Kubernetes Pods.

#### Security Architecture

**Pod Lifecycle:**

```
1. Create Pod manifest with security constraints
2. Submit Pod to Kubernetes API
3. Wait for Pod to reach Running state
4. Inject code + files via kubectl exec tar -x
5. Execute Python via kubectl exec python
6. Extract workspace artifacts via kubectl exec tar -c
7. Delete Pod (grace period 0)
```

**Security Controls:**

| Control | Implementation | Purpose |
|---------|---------------|---------|
| **RunAsNonRoot** | `securityContext.runAsNonRoot: true` | Enforces non-root execution |
| **User/Group** | `runAsUser: 65532, runAsGroup: 65532` (configurable, or platform-assigned) | Unprivileged execution |
| **No Privilege Escalation** | `allowPrivilegeEscalation: false` | Prevents setuid/setgid |
| **Capability Dropping** | `drop: ["ALL"]` | Zero Linux capabilities |
| **Seccomp** | `seccompProfile: RuntimeDefault` | Blocks dangerous syscalls |
| **Read-only Root** | `readOnlyRootFilesystem: true` (configurable) | Writes only to `/workspace` and `/tmp` |
| **Network Lockdown** | Optional NET_ADMIN init container + NetworkPolicy | Blocks egress |
| **Resource Limits** | `limits.memory`, `limits.cpu` | Prevents resource exhaustion |
| **Lifetime Bound** | `activeDeadlineSeconds` | Stops pods the service fails to delete |
| **Ephemeral Storage** | `emptyDir` volumes | No persistent storage |
| **ServiceAccount** | `automountServiceAccountToken: false` | No Kubernetes API credentials |

**Pod Security Context:**

```yaml
# Pod
automountServiceAccountToken: false
securityContext:
  runAsNonRoot: true
  fsGroup: 65532            # omitted in platform mode
  seccompProfile:
    type: RuntimeDefault
# Executor container
securityContext:
  runAsUser: 65532          # omitted in platform mode
  runAsGroup: 65532         # omitted in platform mode
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
```

**Pod Security Standards:**

- Without the network lockdown init container
  (`KUBERNETES_EXECUTOR_NET_ADMIN_LOCKDOWN=false`), executor pods pass the
  `restricted` Pod Security Standard.
- With the lockdown init container (the default), they need the `privileged` standard.
  The init container runs as root and adds `NET_ADMIN`, which `baseline` and
  `restricted` do not allow.
- OpenShift `restricted-v2` also rejects the fixed IDs. Set
  `KUBERNETES_EXECUTOR_RUN_AS_USER`, `KUBERNETES_EXECUTOR_RUN_AS_GROUP` and
  `KUBERNETES_EXECUTOR_FS_GROUP` to empty strings (chart: `securityContext.mode:
  platform`) so that OpenShift assigns them. The image then runs as an arbitrary UID;
  its only writable paths are the `/workspace` and `/tmp` emptyDirs, and `HOME` is
  `/tmp`.

**Resource Limits:**

```yaml
resources:
  limits:
    memory: "256Mi"     # MEMORY_LIMIT_MB
    cpu: "5"            # KUBERNETES_EXECUTOR_POD_RESOURCES
  requests:
    memory: "64Mi"      # KUBERNETES_EXECUTOR_POD_RESOURCES, capped at the memory limit
    cpu: "100m"         # KUBERNETES_EXECUTOR_POD_RESOURCES
```

- `KUBERNETES_EXECUTOR_POD_RESOURCES` is JSON with `requests` (cpu, memory,
  ephemeral-storage) and `limits` (cpu, ephemeral-storage). It is merged over the values
  above, one key at a time: a key you omit keeps its default, and `null` removes one.
  For example, `{"requests": {"cpu": "250m"}}` keeps the CPU limit of 5.
- The CPU limit default of 5 keeps the limit that earlier versions derived from
  `CPU_TIME_LIMIT_SEC`. Lower it (for example to `1`) to pack pods densely.
- The memory limit always comes from `MEMORY_LIMIT_MB`, the same setting that the Docker
  backend uses. The configuration cannot set it, so one setting controls it.
- `CPU_TIME_LIMIT_SEC` does not apply to executor pods. The execution timeout
  (`timeout_ms`, at most `MAX_EXEC_TIMEOUT_MS`) bounds their run time.
- With a read-only root filesystem, user code can write only to the two emptyDirs, and
  their size limits bound that. An `ephemeral-storage` limit is useful when
  `KUBERNETES_EXECUTOR_READ_ONLY_ROOT_FILESYSTEM=false`, or for scheduler accounting.

**File System Layout:**

```
Pod Filesystem:

/               (read-only container filesystem)
├── tmp/        (emptyDir, 64Mi limit: KUBERNETES_EXECUTOR_TMP_SIZE_LIMIT)
│   └── matplotlib/
├── workspace/  (emptyDir, 100Mi limit: KUBERNETES_EXECUTOR_WORKSPACE_SIZE_LIMIT)
│   ├── __main__.py
│   └── <user-files>
└── opt/
    └── executor-venv/
```

**Execution Model:**

1. Pod created with a `sleep` command and `activeDeadlineSeconds` equal to the ready
   timeout plus the execution timeout plus 120 seconds
2. Wait up to `KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC` (default 30) for Pod to reach
   Running state. Image pull errors, container config errors and terminal phases fail
   at once with the reason
3. Stream tar archive via `kubectl exec -i tar -x`
4. Execute Python via `kubectl exec python __main__.py`
5. Read output via WebSocket streams (stdout, stderr, error channel)
6. Timeout via client-side timer (kill Python process with `pkill -9` on timeout)
7. Extract files via `kubectl exec tar -c`
8. Delete Pod with `grace_period_seconds=0`

**Dedicated Sandbox Node Pools:**

Executor pods run untrusted code. To keep them apart from the service and from other
workloads, run them on a dedicated node pool. Taint the pool so that other pods do not
schedule there, then give executor pods a matching node selector and toleration.
`KUBERNETES_EXECUTOR_POD_OVERRIDES` is JSON with `nodeSelector`, `tolerations`,
`affinity`, `topologySpreadConstraints`, `priorityClassName`, `runtimeClassName`,
`labels` and `annotations`. The chart sets it from `kubernetesExecutor.pod`:

```yaml
# kubectl label node <node> onyx.app/pool=sandbox
# kubectl taint node <node> onyx.app/sandbox=true:NoSchedule
codeInterpreter:
  kubernetesExecutor:
    pod:
      nodeSelector:
        onyx.app/pool: sandbox
      tolerations:
        - key: onyx.app/sandbox
          operator: Exists
          effect: NoSchedule
      # Optional: a sandboxed runtime installed on the pool.
      runtimeClassName: gvisor
```

- The service validates the JSON at startup and does not start if it is not valid.
- The labels `app` and `component`, the `code-interpreter.expires-at` annotation and the
  ownerReference belong to the service. The configuration cannot set them.
- A pod that no node can take stays Pending. The request fails after
  `KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC` with `phase=Pending`. With a cluster
  autoscaler, set that timeout above the node start time of the pool.

**Kubernetes-Specific Security:**

- **Pod Security Standards**: `restricted` when the NET_ADMIN lockdown is off,
  `privileged` when it is on
- **Network Policies**: Can isolate Pods from cluster network
- **Resource Quotas**: Cluster-level limits on compute resources
- **RBAC**: ServiceAccount with minimal permissions
- **Admission Controllers**: Can enforce additional security constraints (e.g., PSP, OPA)

**Advantages Over Docker:**

- Native cloud orchestration
- Multi-tenancy support (namespaces)
- Built-in resource management
- Audit logging (Kubernetes API server)
- Integration with cloud-native security tools

**Trade-offs:**

- More complex deployment (requires Kubernetes cluster)
- Higher latency (Pod creation overhead ~1-5 seconds)
- Requires cluster permissions (create/delete Pods)

## Security Model

### Isolation Mechanisms

#### Process Isolation

**User Namespace Isolation:**

- All code execution happens as UID/GID 65532 (unprivileged user)
- User `65532` is the "nobody" user with zero permissions
- No ability to interact with host processes

**PID Namespace Isolation:**

- Processes cannot see host processes
- Docker: Enforced by container runtime
- Kubernetes: Enforced by container runtime + Pod isolation

#### Network Isolation

**Docker:**

- `--network none`: Complete network stack removal
- No loopback except localhost
- No external connectivity (blocks data exfiltration)

**Kubernetes:**

- Default: Pod has network access (cluster networking)
- Recommended: Apply NetworkPolicy to deny all egress
- Can allow specific destinations if needed (e.g., package registries)

Example NetworkPolicy for zero network access:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: code-interpreter-deny-all
spec:
  podSelector:
    matchLabels:
      component: executor
  policyTypes:
  - Ingress
  - Egress
  # Empty ingress/egress = deny all
```

#### Filesystem Isolation

**Read-Only Root Filesystem:**

- Container base filesystem is read-only (Docker default)
- Prevents tampering with Python interpreter, libraries

**Ephemeral Storage:**

- Workspace is tmpfs/emptyDir (memory-backed)
- Data deleted on container/Pod termination
- No persistence = no cross-execution contamination

**Path Validation:**

```python
def _validate_relative_path(self, path_str: str) -> Path:
    # Prevents path traversal attacks
    # Blocks: absolute paths, "..", empty paths
    # Ensures files stay within workspace
```

### Resource Limits

#### Memory Limits

**Docker:**

```bash
--memory 256m --memory-swap 256m
```

- Hard limit enforced by cgroups
- OOM killer terminates process if exceeded
- No swap = prevents disk thrashing

**Kubernetes:**

```yaml
resources:
  limits:
    memory: "256Mi"
```

- Hard limit enforced by kubelet
- OOM killed if exceeded

#### CPU Limits

**Docker:**

```bash
--ulimit cpu=5:5
```

- SIGKILL sent after 5 seconds of CPU time
- Measures actual CPU consumption (not wall time)
- Prevents infinite loops, crypto mining

**Kubernetes:**

```yaml
resources:
  limits:
    cpu: "5"  # KUBERNETES_EXECUTOR_POD_RESOURCES
```

- Throttling applied via CFS (Completely Fair Scheduler)
- Limits CPU rate, not CPU time. `CPU_TIME_LIMIT_SEC` does not apply
- The execution timeout stops the process

#### Timeout Enforcement

**Wall Clock Timeout:**

```python
timeout_ms = 60_000  # Default 60 seconds
proc.communicate(timeout=timeout_ms / 1000.0)
```

- Enforced by parent process
- On timeout: `SIGKILL` sent to Python process
- Protects against sleep, network waits, blocking I/O

**Combined Strategy:**

- Wall clock timeout: Protects against blocking operations
- CPU time limit: Protects against infinite loops
- Memory limit: Protects against memory bombs

#### Process Limits

```bash
--pids-limit 64  # Docker only
```

- Prevents fork bombs
- Maximum 64 processes/threads per container
- Kubernetes: Enforced by PID cgroup controller (if enabled)

#### Output Limits

```python
MAX_OUTPUT_BYTES = 1_000_000  # 1 MB default
```

- Prevents memory exhaustion in API server
- Truncates stdout/stderr at limit
- Suffix: `\n...[truncated]`

### Attack Surface Mitigation

#### Code Injection

**Protection:**

- Code is written to file (`__main__.py`), not passed via command-line
- No shell execution (`shell=False` in subprocess)
- No string interpolation into commands

**File Injection:**

```python
# Files validated before injection
validated_path = self._validate_relative_path(file_path)
# Injected via tar archive (binary safe)
tar.addfile(file_info, io.BytesIO(content))
```

#### Privilege Escalation

**Docker:**

- `--security-opt no-new-privileges`: Blocks setuid/setgid
- `--cap-drop ALL`: No Linux capabilities
- User 65532: No sudo, no setuid binaries

**Kubernetes:**

- `allowPrivilegeEscalation: false`: Blocks setuid/setgid
- `capabilities.drop: ["ALL"]`: No Linux capabilities
- `runAsNonRoot: true`: Enforced by Kubernetes

#### Resource Exhaustion

**Multi-Layer Defense:**

| Attack Vector | Defense |
|--------------|---------|
| **Fork Bomb** | `--pids-limit 64` |
| **Memory Bomb** | `--memory 256m` |
| **CPU Exhaustion** | `--ulimit cpu=5` |
| **Disk Fill** | tmpfs with size limits |
| **Infinite Loop** | Wall clock timeout + CPU limit |
| **Output Flood** | Output truncation (1MB) |

#### Container Escape

**Docker:**

- User namespace isolation (UID 65532 inside = UID 65532 outside)
- No capabilities (can't mount, modify networking, etc.)
- No new privileges (can't escalate)
- Kernel exploit still possible (use Docker-in-Docker for defense-in-depth)

**Kubernetes:**

- Pod Security Standards (`restricted` profile, with the NET_ADMIN lockdown off)
- Seccomp `RuntimeDefault` on every executor pod
- Runtime hardening (e.g., gVisor, Kata Containers)
- Node isolation (dedicated node pools for untrusted workloads)

#### Data Exfiltration

**Docker:**

- `--network none`: Complete network isolation
- No DNS, no HTTP, no external connectivity

**Kubernetes:**

- NetworkPolicy: Deny all egress (recommended)
- Without NetworkPolicy: Risk of data exfiltration via network

**Recommendation for Production:**

Always apply NetworkPolicy in Kubernetes:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: executor-deny-egress
spec:
  podSelector:
    matchLabels:
      component: executor
  policyTypes:
  - Egress
  egress: []  # Deny all egress
```

## Last-Line Interactive Mode

The service supports a Jupyter-like REPL behavior where the last expression's value is automatically printed.

### Implementation

**Code Wrapping:**

```python
def wrap_last_line_interactive(code: str) -> str:
    """
    Wraps user code to execute the last line in Python's 'single' mode.
    This mimics Jupyter notebook behavior: bare expressions print their value.
    """
    # Parses AST, executes all but last line normally
    # Last line: if it's an expression, compile with mode='single' and exec
    # Python's 'single' mode auto-prints expression values
```

**Example:**

```python
# User code
x = 10
y = 20
x + y  # Last line is an expression

# Wrapped code (simplified)
tree = ast.parse(code)
for node in tree.body[:-1]:
    exec(compile(node))  # Execute normally

last_node = tree.body[-1]
if isinstance(last_node, ast.Expr):
    exec(compile(last_node, mode='single'))  # Auto-prints value
```

**Result:**

```
stdout: "30\n"
```

**Behavior:**

- Only the **last line** is affected
- Earlier expressions are **not** printed
- Statements (assignments, imports, etc.) don't print anything
- Matches Jupyter notebook UX

**Control:**

```json
{
  "last_line_interactive": true  // Enable (default)
}
```

Set to `false` for traditional script behavior (no auto-printing).

## File Management

### File Injection

**Request Format:**

```json
{
  "code": "import pandas as pd\ndf = pd.read_csv('data.csv')\ndf.head()",
  "files": [
    {
      "path": "data.csv",
      "content": "base64-encoded-content"
    }
  ]
}
```

**Process:**

1. **Validation**: Path validated (no `..`, no absolute paths, no `__main__.py`)
2. **Directory Creation**: Parent directories created in tar archive
3. **Tar Injection**: Files added to tar with correct ownership (65532:65532)
4. **Extraction**: Tar streamed into container/Pod workspace
5. **Execution**: Code can access files via relative paths

### File Extraction

After execution, any files created in `/workspace` (except `__main__.py`) are extracted.

**Process:**

1. **Tar Creation**: `tar -c --exclude=__main__.py -C /workspace .`
2. **Streaming**: Tar archive streamed out via stdout
3. **Extraction**: Files extracted from tar, content captured
4. **Response**: Files returned as `WorkspaceEntry[]` with base64-encoded content

**Response Format:**

```json
{
  "files": [
    {
      "path": "output.png",
      "kind": "file",
      "content": "base64-encoded-image"
    },
    {
      "path": "results/",
      "kind": "directory",
      "content": null
    }
  ]
}
```

### File Storage API

The service also provides a file storage API for managing uploaded files.

**Upload:**

```bash
POST /v1/files
Content-Type: multipart/form-data
```

**Storage:**

- Files stored in `FILE_STORAGE_DIR` (default: `/tmp/code-interpreter-files`)
- UUIDs used as file identifiers
- TTL-based cleanup (default: 3600 seconds)
- Size limits enforced (default: 100MB per file)

**Usage in Execution:**

```json
{
  "code": "import pandas as pd\ndf = pd.read_csv('data.csv')",
  "files": [
    {
      "path": "data.csv",
      "file_id": "uuid-from-upload"
    }
  ]
}
```

**Security:**

- Path validation prevents directory traversal
- Size limits prevent disk exhaustion
- TTL prevents unbounded storage growth
- Files isolated per request (no cross-request access)

## Capacity and Overload

### Admission control

Each API replica admits at most `MAX_CONCURRENT_EXECUTIONS` executions at a time
(default 16). Executions are `/v1/execute`, `/v1/execute/stream`, `POST /v1/sessions`,
and `POST /v1/sessions/{id}/bash`. A session holds a slot only while its pod is created,
not for its whole TTL.

The execution routes are `async`. They get a slot on the event loop, then run the
blocking executor call in the worker thread pool. A request that waits longer than
`EXECUTION_QUEUE_TIMEOUT_SEC` (default 30s) for a slot is rejected with 429. The wait
is an async poll on the event loop, so a queued request holds no worker thread. Keep the
wait below the client's request timeout (Onyx: `timeout_ms/1000 + 10`s). The thread pool is
sized at startup to at least `MAX_CONCURRENT_EXECUTIONS + 24`, so admitted executions
never queue behind each other for a thread.

Why 16: it stays below the default thread pool of 40 with room for file routes and
stream reads. It also bounds the API replica's memory for buffered output and files, and
the executor resources one replica can request (16 x 256Mi memory limit).

### Overload contract

| Status | Reason | Meaning | `Retry-After` |
|--------|--------|---------|---------------|
| 429 | `concurrency_limit` | This replica is at `MAX_CONCURRENT_EXECUTIONS`. The cluster can still have room. | `EXECUTION_RETRY_AFTER_SEC` (2s) |
| 503 | `quota_exceeded` | Pod creation failed because the executor namespace ResourceQuota is exhausted. | `CAPACITY_RETRY_AFTER_SEC` (10s) |
| 503 | `unschedulable` | The executor pod is still Unschedulable at `KUBERNETES_EXECUTOR_READY_TIMEOUT_SEC` (for example, insufficient CPU). | `CAPACITY_RETRY_AFTER_SEC` (10s) |

429 is about one replica, so a retry can succeed at once on another replica. 503 is
about the executor backend as a whole, so clients should back off longer. Both use the
normal error body, `{"detail": "..."}`. Other 403 errors on pod creation (missing RBAC,
a quota that needs limits the pod does not set) are configuration errors. They stay
500 because a retry cannot fix them.

The scheduler marks a pod Unschedulable as soon as no node fits, before a cluster
autoscaler can add one. So the service keeps waiting until the ready timeout, and
returns 503 only if the pod is still Unschedulable then. Image pull and container
creation failures take precedence: they fail at once as a start error (500), because a
retry cannot fix them.

`/v1/execute/stream` returns 422, 429 and 503 before the event stream opens. The
executor emits an internal "started" event once the sandbox runs, and the route waits
for it before it sends the response headers. Errors after that point arrive as an SSE
`error` event, as before.

### Health and readiness

- `/health` (liveness) is `async` and answers from memory. It never calls the Docker
  daemon or the Kubernetes API, so a saturated thread pool or a slow API server cannot
  fail the liveness probe and restart a replica with runs in flight. Its `status` field
  shows the last backend check, which a background task refreshes every
  `HEALTH_CHECK_INTERVAL_SEC` (default 30s).
- `/health` returns 503 only when the checker itself is stuck: no check has completed,
  with any result, in 3 × `HEALTH_CHECK_INTERVAL_SEC` + `BACKEND_CHECK_TIMEOUT_SEC`
  (default 92.5s). A failed check counts as completed, so a Docker or Kubernetes API
  outage keeps liveness at 200 and readiness at 503.
- `/ready` (readiness) runs a fresh backend check, bounded by `BACKEND_CHECK_TIMEOUT_SEC`
  (default 2.5s), and returns 503 when it fails or times out.
- Backend checks are single-flight: while one runs, `/ready` and the background task
  wait on it instead of starting another. They run on a dedicated pool of two threads,
  so a hung check cannot starve the request thread pool, the session reaper or the
  image watchdog. Kubernetes API calls on the health path have a (connect, read)
  timeout of `BACKEND_CHECK_TIMEOUT_SEC`, so the thread itself ends.
- Both report `executor_backend` and `network_isolation`
  (`net_admin_init_container+network_policy`, `network_policy_only`, or
  `docker_network:<name>`).

### Metrics

`/metrics` exposes Prometheus metrics: active executions and the configured limit,
rejections by status and reason, completions by outcome (`ok`, `timed_out`, `error`),
admission wait time, and execution duration. A run whose user code exits non-zero
counts as `ok`: the sandbox worked.

### File storage with replicas

File storage is local to each replica. Uploads and runs are separate requests, so more
than one replica needs shared storage at `FILE_STORAGE_DIR` or sticky routing. The Helm
chart refuses to render `replicaCount > 1` without `fileStorage.shared=true`.

## Summary

The code-interpreter service provides secure, isolated Python execution through:

1. **Strong Isolation**: Docker/Kubernetes containers with restricted capabilities
2. **Resource Limits**: Memory, CPU, process, and output limits prevent exhaustion
3. **Minimal Privileges**: All code runs as unprivileged user (UID 65532)
4. **Network Isolation**: No network access by default (Docker) or via NetworkPolicy (Kubernetes)
5. **Ephemeral Storage**: No persistent data, preventing cross-execution contamination
6. **Defense in Depth**: Multiple overlapping security controls at every layer

**Recommended Deployment:**

- **Development**: Docker executor with Docker-out-of-Docker
- **Production**: Kubernetes executor with:
  - NetworkPolicy (deny all egress)
  - Pod Security Standards (restricted profile, which needs the NET_ADMIN lockdown off
    and a CNI that enforces the NetworkPolicy)
  - Dedicated node pools for untrusted workloads
  - Resource quotas and limits
  - Monitoring and alerting on execution metrics

This architecture balances security, performance, and operational simplicity while providing strong guarantees against malicious or buggy code.
