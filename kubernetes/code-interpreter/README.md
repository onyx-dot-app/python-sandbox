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
helm repo add code-interpreter https://onyx-dot-app.github.io/code-interpreter/
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
| `codeInterpreter.kubernetesExecutor.image` | Container image used for execution pods | `python-executor-sci` |
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

- Pods run with a restricted security context
- Resource limits are enforced per execution
- Pods are cleaned up automatically after completion
- No privileged host access is required

Required RBAC permissions (automatically created when `rbac.create=true`):
- Create, get, list, watch, delete pods
- Create pod exec

## Security Considerations

1. **Network Policies**: Enable network policies to restrict traffic:
```yaml
networkPolicy:
  enabled: true
  policyTypes:
    - Ingress
    - Egress
```

2. **Pod Security Standards**: The chart follows security best practices:
   - Runs as non-root by default
   - Drops all capabilities
   - Uses read-only root filesystem where possible

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
