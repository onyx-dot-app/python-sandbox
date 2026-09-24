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
replicaCount: 3

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
| `codeInterpreter.kubernetesExecutor.podResources` | Execution container `requests` (cpu, memory, ephemeral-storage) and `limits` (cpu, ephemeral-storage); the memory limit is `memoryLimitMb` | requests `cpu: 100m`, `memory: 64Mi`; limits `cpu: "1"` |
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
  --set replicaCount=3 \
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

`podResources.limits.memory` is not supported: the chart fails to render if you set
it. `codeInterpreter.cpuTimeLimitSec` does not apply to execution pods; the
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

The chart configures liveness and readiness probes:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 10
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /health
    port: http
  initialDelaySeconds: 5
  periodSeconds: 5
```

## Upgrading

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
