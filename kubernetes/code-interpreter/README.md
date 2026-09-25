# Code Interpreter Helm Chart

This Helm chart deploys the Code Interpreter service on a Kubernetes cluster. The service provides a FastAPI-based API for executing Python code in secure, isolated environments.

## Prerequisites

- Kubernetes 1.19+
- Helm 3.8.0+
- PV provisioner support in the underlying infrastructure (if persistence is needed)
- Container image for code-interpreter built and available

## Installation

### Add the repository (if published)

```bash
helm repo add code-interpreter https://onyx-dot-app.github.io/python-sandbox/
helm repo update
```

### Install from local chart

```bash
# From the project root
helm install code-interpreter ./kubernetes/code-interpreter
```

### Install with custom values

```bash
# Create a custom values file
cat > my-values.yaml <<EOF
replicaCount: 1

image:
  repository: my-registry.com/code-interpreter
  tag: v1.0.0

codeInterpreter:
  maxExecTimeoutMs: 30000
  memoryLimitMb: 512
  kubernetes:
    image: my-registry.com/python-executor-sci:v1.0.0

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: code-interpreter.example.com
      paths:
        - path: /
          pathType: Prefix

EOF

# Install with custom values
helm install code-interpreter ./code-interpreter -f my-values.yaml
```

## Configuration

### Key Configuration Options

| Parameter | Description | Default |
|-----------|-------------|---------|
| `replicaCount` | Number of replicas | `1` |
| `image.repository` | Container image repository | `code-interpreter` |
| `image.tag` | Container image tag | `""` (uses chart appVersion) |
| `codeInterpreter.maxExecTimeoutMs` | Maximum execution timeout in milliseconds | `60000` |
| `codeInterpreter.memoryLimitMb` | Memory limit for code execution in MB | `256` |
| `codeInterpreter.kubernetesExecutor.image` | Container image used for execution pods | `""` (`onyxdotapp/python-executor-sci`, untagged) |
| `codeInterpreter.kubernetesExecutor.imagePullPolicy` | Pull policy for execution pods; empty follows Kubernetes (`Always` for `:latest`, else `IfNotPresent`) | `""` |
| `codeInterpreter.kubernetesExecutor.readyTimeoutSec` | Seconds to wait for an execution pod to reach Running (1-600) | `30` |
| `codeInterpreter.kubernetesExecutor.netAdminLockdown` | Add the root NET_ADMIN init container that blocks egress with iptables | `true` |
| `codeInterpreter.kubernetesExecutor.setOwnerReferences` | Give execution pods an ownerReference to this chart's Deployment | `true` |
| `codeInterpreter.kubernetesExecutor.securityContext.mode` | `fixed` uses the IDs below; `platform` lets the platform assign them | `fixed` |
| `codeInterpreter.kubernetesExecutor.securityContext.runAsUser` / `runAsGroup` / `fsGroup` | Execution pod IDs in `fixed` mode; `null` omits one | `65532` |
| `codeInterpreter.kubernetesExecutor.securityContext.readOnlyRootFilesystem` | Mount the execution container root filesystem read-only | `true` |
| `codeInterpreter.kubernetesExecutor.podResources` | Execution container `requests` (cpu, memory, ephemeral-storage) and `limits` (cpu, ephemeral-storage); the memory limit is `memoryLimitMb` | requests `cpu: 100m`, `memory: 64Mi`; limits `cpu: "5"` |
| `codeInterpreter.kubernetesExecutor.workspaceSizeLimit` | Size limit of the `/workspace` emptyDir | `100Mi` |
| `codeInterpreter.kubernetesExecutor.tmpSizeLimit` | Size limit of the `/tmp` emptyDir | `64Mi` |
| `codeInterpreter.kubernetesExecutor.pod.nodeSelector` | Node selector of execution pods | `{}` |
| `codeInterpreter.kubernetesExecutor.pod.tolerations` | Tolerations of execution pods | `[]` |
| `codeInterpreter.kubernetesExecutor.pod.affinity` | Affinity of execution pods | `{}` |
| `codeInterpreter.kubernetesExecutor.pod.topologySpreadConstraints` | Topology spread constraints of execution pods | `[]` |
| `codeInterpreter.kubernetesExecutor.pod.priorityClassName` | Priority class of execution pods | `""` |
| `codeInterpreter.kubernetesExecutor.pod.runtimeClassName` | Runtime class of execution pods, e.g. `gvisor` | `""` |
| `codeInterpreter.kubernetesExecutor.pod.labels` / `annotations` | Extra metadata of execution pods; `app`, `component` and the expiry annotation are reserved | `{}` |
| `service.type` | Kubernetes service type | `ClusterIP` |
| `ingress.enabled` | Enable ingress | `false` |
| `rbac.create` | Create RBAC resources | `true` |
| `capacity.maxConcurrentExecutions` | In-flight executions per replica before 429 | `16` |
| `capacity.queueTimeoutSec` | Wait for a free slot before 429 | `30` |
| `capacity.retryAfterSec` | `Retry-After` sent with 429 | `2` |
| `capacity.capacityRetryAfterSec` | `Retry-After` sent with 503 | `10` |
| `executorResourceQuota.enabled` | Create a ResourceQuota in the executor namespace | `false` |
| `metrics.serviceMonitor.enabled` | Create a Prometheus Operator ServiceMonitor | `false` |
| `fileStorage.shared` | Confirm that all replicas share `FILE_STORAGE_DIR` (needed for `replicaCount > 1`) | `false` |

See [values.yaml](values.yaml) for the full list of configurable parameters.

## Usage Examples

### Basic Installation

```bash
helm install code-interpreter ./code-interpreter \
  --set image.repository=my-registry/code-interpreter \
  --set image.tag=latest \
  --set codeInterpreter.kubernetesExecutor.image=my-registry/python-executor-sci:latest
```

### Production Setup with Ingress

```bash
helm install code-interpreter ./code-interpreter \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set "ingress.hosts[0].host=api.example.com" \
  --set "ingress.hosts[0].paths[0].path=/" \
  --set "ingress.hosts[0].paths[0].pathType=Prefix" \
  --set codeInterpreter.kubernetesExecutor.namespace=code-execution \
  --set resources.requests.cpu=500m \
  --set resources.requests.memory=256Mi
```

## Kubernetes Executor

The chart always uses the Kubernetes executor to run ephemeral pods for code execution:

- Pods run with seccomp `RuntimeDefault`, all capabilities dropped, no privilege
  escalation, a read-only root filesystem and no service account token
- Resource limits are enforced per execution
- Pods are cleaned up automatically after completion. `activeDeadlineSeconds` also
  stops a pod that the service fails to delete: execution pods after the ready
  timeout plus the execution timeout plus 120 seconds, session pods at their TTL
- No privileged host access is required
- A pod that cannot start (image pull error, container config error, failed phase)
  fails the request at once with the reason

Required RBAC permissions (automatically created when `rbac.create=true`):
- Create, get, list, watch, delete pods
- Create pod exec
- Get deployments, when `codeInterpreter.kubernetesExecutor.setOwnerReferences=true`

### Owner references

With `codeInterpreter.kubernetesExecutor.setOwnerReferences=true` (the default), each
execution pod carries an ownerReference to this chart's Deployment. This gives two
benefits:

- Kubernetes garbage-collects execution pods that the service fails to clean up, for
  example after an OOM kill.
- Monitoring can tell short-lived execution pods apart from long-lived workloads,
  because the standard "pod has an owner" test now applies to them.

The service reads its own Deployment once at startup to build the reference. It skips
the reference, and logs why, when the read is not permitted or when
`codeInterpreter.kubernetesExecutor.namespace` names a different namespace than the
service: Kubernetes does not honour ownerReferences across namespaces, and would treat
the owner as already deleted.

### Image pinning

By default, execution pods use `onyxdotapp/python-executor-sci` with no tag, which
means `:latest` and pull policy `Always`. Each execution pod then asks the registry
for the current digest. Executor images are published with a version tag, so for
reproducible installs pin one:

```yaml
codeInterpreter:
  kubernetesExecutor:
    image: onyxdotapp/python-executor-sci:0.4.7
```

A pinned tag uses pull policy `IfNotPresent`. Set `imagePullPolicy` to override it,
for example `IfNotPresent` or `Never` for nodes that cannot reach the registry.

### Dedicated node pool

The top-level `nodeSelector`, `tolerations` and `affinity` apply only to the service
pod. To run execution pods, which run user code, on their own tainted node pool, use
`codeInterpreter.kubernetesExecutor.pod`:

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
```

The service validates these values at startup and does not start if they are not
valid. It does not let them change the `app` and `component` labels, the expiry
annotation or the ownerReference. A pod that no node can take fails the request after
`readyTimeoutSec` with `phase=Pending`.

### Execution pod resources

```yaml
codeInterpreter:
  memoryLimitMb: 512          # memory limit of execution pods
  kubernetesExecutor:
    podResources:
      requests:
        cpu: 250m
        memory: 256Mi         # capped at memoryLimitMb
        ephemeral-storage: 256Mi
      limits:
        cpu: "2"              # null removes the CPU limit
        ephemeral-storage: 1Gi
    workspaceSizeLimit: 500Mi
    tmpSizeLimit: 128Mi
```

The CPU limit default of `"5"` keeps the limit that earlier versions derived from
`cpuTimeLimitSec`. Lower it (for example to `"1"`) to pack pods densely.

`podResources.limits.memory` is not supported. The chart ignores it and prints a
warning in the release notes; set `memoryLimitMb` instead. `codeInterpreter.cpuTimeLimitSec` does not apply to execution pods; the
execution timeout bounds their run time. With the read-only root filesystem, the
emptyDir size limits bound what user code can write. Kubelet evicts a pod that goes
over a limit, so the write itself does not fail at once.

### Restricted Pod Security and OpenShift

Execution pods satisfy the Kubernetes `restricted` Pod Security Standard only when
`netAdminLockdown` is `false`. The lockdown init container runs as root with
`NET_ADMIN`, which `restricted` does not allow. Without it, the executor
NetworkPolicy (`templates/networkpolicy.yaml`) is the only egress control, so your
CNI must enforce NetworkPolicies.

```yaml
# Namespace labelled pod-security.kubernetes.io/enforce=restricted
codeInterpreter:
  kubernetesExecutor:
    netAdminLockdown: false
```

OpenShift `restricted-v2` also assigns the user and fsGroup from the namespace
range, and rejects the fixed ID `65532`. Use `platform` mode, and let OpenShift
assign the IDs of the service pod too:

```yaml
podSecurityContext:
  runAsUser: null
  fsGroup: null
securityContext:
  runAsUser: null
codeInterpreter:
  kubernetesExecutor:
    netAdminLockdown: false
    securityContext:
      mode: platform
```

The chart fails to render when `mode: platform` is set with `netAdminLockdown: true`.
`platform` mode needs an admission controller that assigns a user ID, because the
executor image runs as root by default. On other clusters, keep `fixed` mode and set
IDs that your policy allows.

## Security Considerations

1. **Network Policies**: Enable network policies to restrict traffic:
```yaml
networkPolicy:
  enabled: true
  policyTypes:
    - Ingress
    - Egress
```

2. **Pod Security Standards**: The service pod meets the `restricted` standard.
   Execution pods meet it when `netAdminLockdown` is `false` (see above).
   - Runs as non-root by default
   - Drops all capabilities and uses seccomp `RuntimeDefault`
   - Execution pods use a read-only root filesystem

3. **Resource Limits**: Always set appropriate resource limits:
```yaml
resources:
  limits:
    cpu: 1000m
    memory: 512Mi
  requests:
    cpu: 100m
    memory: 128Mi
```

## Health Checks

| Endpoint | Probe | Behavior |
|----------|-------|----------|
| `/health` | liveness | Answers from memory. It never calls the Kubernetes API, so a slow API server or a saturated replica does not get the pod restarted during runs. `status` shows the last background backend check. HTTP 503 only when the checker is stuck: no check has completed, with any result, in 3 × `HEALTH_CHECK_INTERVAL_SEC` + `BACKEND_CHECK_TIMEOUT_SEC` (default 92.5s). An API outage alone keeps it at 200. |
| `/ready` | readiness | Runs a fresh backend check (can the service account create executor pods). HTTP 503 when the check fails or takes longer than `BACKEND_CHECK_TIMEOUT_SEC`. Checks are single-flight, and their API calls time out, so a hung API server cannot pile up threads. |

Both payloads include `executor_backend` and `network_isolation`. For the Kubernetes
backend, `network_isolation` is `net_admin_init_container+network_policy` when
`kubernetesExecutor.netAdminLockdown=true`, and `network_policy_only` when it is false.
In the second case, the executor NetworkPolicy is the only network control, so your CNI
must enforce it.

Readiness does not fail when the replica is busy. Busy replicas answer 429 (see below).

## Capacity and Overload

Each replica admits at most `capacity.maxConcurrentExecutions` executions at a time.
This covers `/v1/execute`, `/v1/execute/stream`, session creation, and session bash
commands. A request waits up to `capacity.queueTimeoutSec` (30s) for a free slot, and
gets 429 only if no slot frees up in that time. A waiting request holds no worker thread.
Keep `capacity.queueTimeoutSec` below the client's request timeout (Onyx: `timeout_ms/1000 + 10`s, 70s by default).

| Status | Meaning | Client action |
|--------|---------|---------------|
| `429` + `Retry-After` | This replica has no free slot. | Retry after the delay. Another replica can take the retry. |
| `503` + `Retry-After` | The cluster has no room for an executor pod: the executor namespace ResourceQuota is exhausted, or the pod is unschedulable. | Retry after the delay, or add cluster capacity. |

`/v1/execute/stream` returns these statuses before the event stream opens, so clients
see a real HTTP status, not an SSE `error` event. The body uses the usual
`{"detail": "..."}` shape.

The default of 16 slots per replica keeps each replica below the server's default
thread pool (40) with headroom for file routes and stream reads. At the default
executor size (256Mi memory limit, 100m CPU request), 16 runs need about 4Gi of memory
limits and 1.6 CPU of requests. Raise the value only when the cluster can schedule that
many executor pods per replica.

### Executor ResourceQuota

Set `executorResourceQuota.enabled=true` to cap what executor pods can use. The quota
applies to every pod in its namespace, so use a dedicated executor namespace:

```yaml
codeInterpreter:
  kubernetesExecutor:
    namespace: code-execution   # must exist
executorResourceQuota:
  enabled: true
  hard:
    pods: "32"
    requests.cpu: "4"
    requests.memory: 4Gi
    limits.memory: 12Gi
```

Count session pods in `pods`: each open session holds one pod for its whole TTL.
Executor pods set no ephemeral-storage requests. If you add ephemeral-storage keys to
`hard`, also set `executorResourceQuota.limitRange.enabled=true` to give containers a
default. Rendering fails if the quota would go into the release namespace, unless you
set `executorResourceQuota.allowReleaseNamespace=true`.

### Metrics

The service serves Prometheus metrics at `/metrics` on the `http` port. Set
`metrics.serviceMonitor.enabled=true` to create a ServiceMonitor (this needs the
Prometheus Operator CRDs).

| Metric | Type | Labels |
|--------|------|--------|
| `code_interpreter_executions_active` | gauge | `operation` |
| `code_interpreter_executions_limit` | gauge | |
| `code_interpreter_executions_rejected_total` | counter | `operation`, `status` (429/503), `reason` (`concurrency_limit`, `quota_exceeded`, `unschedulable`) |
| `code_interpreter_executions_completed_total` | counter | `operation`, `outcome` (`ok`, `timed_out`, `error`) |
| `code_interpreter_admission_wait_seconds` | histogram | `operation` |
| `code_interpreter_execution_duration_seconds` | histogram | `operation` |

## File Storage and Replicas

Uploaded files and execution outputs are stored on the local disk of the replica that
received them (`FILE_STORAGE_DIR`, default `/tmp/code-interpreter-files`). The Onyx
client uploads a file and then runs code in a separate request. With more than one
replica and no sticky routing, the run can land on a replica that does not have the
file, and the run fails with 404.

For this reason, the chart fails to render when `replicaCount > 1` unless you set
`fileStorage.shared=true`. Set it only after you do one of these:

- Mount shared storage (a ReadWriteMany volume) at `FILE_STORAGE_DIR` on every replica,
  with `volumes`, `volumeMounts` and, if needed, a `FILE_STORAGE_DIR` entry in
  `extraEnvVars`.
- Route each client to one replica (sticky sessions on your ingress or service mesh).

## Upgrading

### Notes for this release

- The readiness probe now uses `/ready`. If you override `readinessProbe`, point it at
  `/ready`.
- Rendering fails for `replicaCount > 1` unless `fileStorage.shared=true`. See
  [File Storage and Replicas](#file-storage-and-replicas).
- Executions over `capacity.maxConcurrentExecutions` (16) per replica wait up to
  `capacity.queueTimeoutSec` (30s) for a slot, then get 429 with `Retry-After`.

### Upgrade the deployment

```bash
helm upgrade my-code-interpreter ./code-interpreter \
  --set image.tag=v2.0.0
```

### Upgrade with new values

```bash
helm upgrade my-code-interpreter ./code-interpreter \
  -f my-values.yaml \
  --set image.tag=v2.0.0
```

## Uninstallation

```bash
helm uninstall my-code-interpreter
```

## Troubleshooting

### Check pod status

```bash
kubectl get pods -l app.kubernetes.io/name=code-interpreter
kubectl describe pod <pod-name>
kubectl logs <pod-name>
```

### Verify RBAC permissions (Kubernetes backend)

```bash
kubectl auth can-i create pods \
  --as=system:serviceaccount:<namespace>:<serviceaccount-name>
```

### Test the API

```bash
# Port-forward to test locally
k port-forward deployment/code-interpreter 8000:8000

# Test execution
curl -X POST http://localhost:8000/v1/execute \
  -H "Content-Type: application/json" \
  -d '{
    "code": "print(\"Hello, World!\")",
    "timeout_ms": 5000
  }'
```

## Advanced Configuration

### Using External Secrets

```yaml
extraEnvFrom:
  - secretRef:
      name: my-api-secrets
  - configMapRef:
      name: my-config
```

### Custom Volume Mounts

```yaml
volumes:
  - name: custom-config
    configMap:
      name: my-custom-config

volumeMounts:
  - name: custom-config
    mountPath: /etc/custom
    readOnly: true
```

## Development

### Testing the chart

```bash
# Lint the chart
helm lint ./code-interpreter

# Dry run to see generated manifests
helm install my-code-interpreter ./code-interpreter --dry-run --debug

# Template to generate YAML
helm template my-code-interpreter ./code-interpreter > generated.yaml
```

### Package the chart

```bash
helm package ./code-interpreter
```

## Support

For issues and feature requests, please open an issue in the GitHub repository.

## License

This chart is provided under the same license as the Code Interpreter project.
