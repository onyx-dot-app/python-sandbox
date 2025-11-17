# Kubernetes Backend for Code Interpreter

The code-interpreter service now supports running Python code execution in Kubernetes pods as an alternative to Docker containers. This is useful when the service itself is running in a Kubernetes cluster.

## Configuration

Set the following environment variables to enable and configure the Kubernetes backend:

### Required Settings

- `EXECUTOR_BACKEND=kubernetes` - Enable the Kubernetes executor backend (default: `docker`)

### Optional Settings

- `KUBERNETES_NAMESPACE=default` - Kubernetes namespace to create pods in (default: `default`)
- `KUBERNETES_IMAGE=python-executor-sci` - Container image to use for execution pods (default: `python-executor-sci`)
- `KUBERNETES_SERVICE_ACCOUNT=` - Service account name for pods (default: empty, uses default service account)

### Common Settings (apply to both Docker and Kubernetes)

- `MAX_EXEC_TIMEOUT_MS=60000` - Maximum execution timeout in milliseconds
- `MAX_OUTPUT_BYTES=1000000` - Maximum output size in bytes
- `CPU_TIME_LIMIT_SEC=5` - CPU time limit in seconds
- `MEMORY_LIMIT_MB=256` - Memory limit in megabytes

## Deployment

### 1. Build the Docker Image with Kubernetes Support

The Kubernetes client library is already included in the dependencies, so the standard build will work:

```bash
docker build -t code-interpreter -f code-interpreter/Dockerfile .
```

### 2. Deploy to Kubernetes

Create a deployment with the necessary RBAC permissions:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: code-interpreter
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: code-interpreter
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["create", "get", "delete"]
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["create", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: code-interpreter
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: code-interpreter
subjects:
- kind: ServiceAccount
  name: code-interpreter
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: code-interpreter
spec:
  replicas: 1
  selector:
    matchLabels:
      app: code-interpreter
  template:
    metadata:
      labels:
        app: code-interpreter
    spec:
      serviceAccountName: code-interpreter
      containers:
      - name: code-interpreter
        image: code-interpreter:latest
        env:
        - name: EXECUTOR_BACKEND
          value: "kubernetes"
        - name: KUBERNETES_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        - name: HOST
          value: "0.0.0.0"
        - name: PORT
          value: "8000"
        ports:
        - containerPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: code-interpreter
spec:
  selector:
    app: code-interpreter
  ports:
  - port: 8000
    targetPort: 8000
```

## Architecture

When using the Kubernetes backend:

1. The service creates ephemeral pods for each code execution request
2. Each pod runs with security constraints (non-root user, dropped capabilities)
3. Code and files are transferred to the pod via tar archives
4. Execution happens in an isolated environment with resource limits
5. Output is streamed back and the pod is deleted after execution

## Security Considerations

The Kubernetes backend maintains the same security model as the Docker backend:

- Pods run as non-root user (UID 65532)
- All capabilities are dropped
- Resource limits are enforced (CPU, memory, PID limits)
- Pods have no network access
- Temporary filesystems are used for workspace and /tmp

## Testing

Run integration tests with the Kubernetes backend:

```bash
export EXECUTOR_BACKEND=kubernetes
pytest tests/integration_tests/test_kubernetes_executor.py
```

## Differences from Docker Backend

- **Pod startup time**: Creating Kubernetes pods may take slightly longer than Docker containers
- **Cluster resources**: Execution pods consume cluster resources and are subject to cluster policies
- **RBAC requirements**: The service needs appropriate permissions to create and manage pods
- **Namespace isolation**: Pods are created in a specific namespace

## Troubleshooting

### Permission Denied Errors

Ensure the service account has the necessary RBAC permissions:

```bash
kubectl auth can-i create pods --as=system:serviceaccount:default:code-interpreter
kubectl auth can-i create pods/exec --as=system:serviceaccount:default:code-interpreter
```

### Pod Creation Failures

Check resource quotas and limits in the namespace:

```bash
kubectl describe resourcequota -n <namespace>
kubectl describe limitrange -n <namespace>
```

### Image Pull Errors

Ensure the executor image is available in the cluster:

```bash
kubectl get events -n <namespace> | grep -i pull
```