{{/*
Expand the name of the chart.
*/}}
{{- define "code-interpreter.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "code-interpreter.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "code-interpreter.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "code-interpreter.labels" -}}
helm.sh/chart: {{ include "code-interpreter.chart" . }}
{{ include "code-interpreter.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "code-interpreter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "code-interpreter.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "code-interpreter.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "code-interpreter.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Get the namespace for Kubernetes executor
*/}}
{{- define "code-interpreter.kubernetesNamespace" -}}
{{- if ne .Values.codeInterpreter.kubernetesExecutor.namespace "" }}
{{- .Values.codeInterpreter.kubernetesExecutor.namespace }}
{{- else }}
{{- .Release.Namespace }}
{{- end }}
{{- end }}

{{/*
Quoted executor pod ID for the given (list mode id). Empty in platform mode or when unset.
*/}}
{{- define "code-interpreter.executorId" -}}
{{- $mode := index . 0 -}}
{{- $id := index . 1 -}}
{{- if or (eq $mode "platform") (kindIs "invalid" $id) -}}
""
{{- else -}}
{{- int64 $id | quote -}}
{{- end -}}
{{- end }}

{{/*
Reject value combinations that cannot work.
*/}}
{{- define "code-interpreter.validateValues" -}}
{{- $executor := .Values.codeInterpreter.kubernetesExecutor -}}
{{- $mode := ($executor.securityContext | default dict).mode | default "fixed" -}}
{{- if not (has $mode (list "fixed" "platform")) -}}
{{- fail (printf "codeInterpreter.kubernetesExecutor.securityContext.mode must be \"fixed\" or \"platform\", got %q" $mode) -}}
{{- end -}}
{{- if and (eq $mode "platform") $executor.netAdminLockdown -}}
{{- fail "codeInterpreter.kubernetesExecutor.securityContext.mode=platform requires codeInterpreter.kubernetesExecutor.netAdminLockdown=false: the lockdown init container runs as root with NET_ADMIN, which restricted admission (Pod Security \"restricted\", OpenShift restricted-v2) rejects. Enforce egress with the executor NetworkPolicy instead." -}}
{{- end -}}
{{- if and (gt (int .Values.replicaCount) 1) (not .Values.fileStorage.shared) -}}
{{- fail "replicaCount > 1 needs shared file storage: uploaded files live on the local disk of one replica. Mount shared storage at FILE_STORAGE_DIR (or use sticky sessions) and set fileStorage.shared=true. See the chart README." -}}
{{- end -}}
{{- end }}

{{/*
Execution pod resources as JSON. limits.memory is dropped: the memory limit is codeInterpreter.memoryLimitMb.
Helm deletes keys set to null, so a removed service default is sent as an explicit null.
*/}}
{{- define "code-interpreter.executorPodResources" -}}
{{- $resources := deepCopy (.Values.codeInterpreter.kubernetesExecutor.podResources | default dict) -}}
{{- $limits := $resources.limits | default dict -}}
{{- $_ := unset $limits "memory" -}}
{{- $requests := $resources.requests | default dict -}}
{{- range $key := list "cpu" -}}
{{- if not (hasKey $limits $key) -}}{{- $_ := set $limits $key nil -}}{{- end -}}
{{- end -}}
{{- range $key := list "cpu" "memory" -}}
{{- if not (hasKey $requests $key) -}}{{- $_ := set $requests $key nil -}}{{- end -}}
{{- end -}}
{{- $_ := set $resources "limits" $limits -}}
{{- $_ := set $resources "requests" $requests -}}
{{- $resources | toJson -}}
{{- end }}

{{/*
Warnings about values that the chart ignores.
*/}}
{{- define "code-interpreter.warnings" -}}
{{- $limits := (.Values.codeInterpreter.kubernetesExecutor.podResources | default dict).limits | default dict -}}
{{- if $limits.memory }}

WARNING: codeInterpreter.kubernetesExecutor.podResources.limits.memory ({{ $limits.memory }}) is ignored.
  The memory limit of execution pods is codeInterpreter.memoryLimitMb ({{ .Values.codeInterpreter.memoryLimitMb }}MB).
  Remove limits.memory and set memoryLimitMb instead.
{{- end }}
{{- end }}

{{/*
Release notes. NOTES.txt includes this so that tests can render it with helm template.
*/}}
{{- define "code-interpreter.notes" -}}
1. Get the application URL by running these commands:
{{- if .Values.ingress.enabled }}
{{- range $host := .Values.ingress.hosts }}
  {{- range .paths }}
  http{{ if $.Values.ingress.tls }}s{{ end }}://{{ $host.host }}{{ .path }}
  {{- end }}
{{- end }}
{{- else if contains "NodePort" .Values.service.type }}
  export NODE_PORT=$(kubectl get --namespace {{ .Release.Namespace }} -o jsonpath="{.spec.ports[0].nodePort}" services {{ include "code-interpreter.fullname" . }})
  export NODE_IP=$(kubectl get nodes --namespace {{ .Release.Namespace }} -o jsonpath="{.items[0].status.addresses[0].address}")
  echo http://$NODE_IP:$NODE_PORT
{{- else if contains "LoadBalancer" .Values.service.type }}
     NOTE: It may take a few minutes for the LoadBalancer IP to be available.
           You can watch the status of by running 'kubectl get --namespace {{ .Release.Namespace }} svc -w {{ include "code-interpreter.fullname" . }}'
  export SERVICE_IP=$(kubectl get svc --namespace {{ .Release.Namespace }} {{ include "code-interpreter.fullname" . }} --template "{{"{{ range (index .status.loadBalancer.ingress 0) }}{{.}}{{ end }}"}}")
  echo http://$SERVICE_IP:{{ .Values.service.port }}
{{- else if contains "ClusterIP" .Values.service.type }}
  export POD_NAME=$(kubectl get pods --namespace {{ .Release.Namespace }} -l "app.kubernetes.io/name={{ include "code-interpreter.name" . }},app.kubernetes.io/instance={{ .Release.Name }}" -o jsonpath="{.items[0].metadata.name}")
  export CONTAINER_PORT=$(kubectl get pod --namespace {{ .Release.Namespace }} $POD_NAME -o jsonpath="{.spec.containers[0].ports[0].containerPort}")
  echo "Visit http://127.0.0.1:8080 to use your application"
  kubectl --namespace {{ .Release.Namespace }} port-forward $POD_NAME 8080:$CONTAINER_PORT
{{- end }}

2. Test the API endpoint:
  curl -X POST http://localhost:8080/v1/execute \
    -H "Content-Type: application/json" \
    -d '{"code": "print(\"Hello from Code Interpreter!\")", "timeout_ms": 5000}'

3. Configuration Details:
  - Executor Backend: kubernetes
  - Kubernetes Namespace: {{ include "code-interpreter.kubernetesNamespace" . }}
  - Executor Image: {{ .Values.codeInterpreter.kubernetesExecutor.image }}
  - Max Timeout: {{ .Values.codeInterpreter.maxExecTimeoutMs }}ms
  - Memory Limit: {{ .Values.codeInterpreter.memoryLimitMb }}MB

{{- if .Values.rbac.create }}

4. RBAC Status:
  ✓ ServiceAccount created: {{ include "code-interpreter.serviceAccountName" . }}
  ✓ Role and RoleBinding created for pod management

  Verify permissions:
  kubectl auth can-i create pods --as=system:serviceaccount:{{ .Release.Namespace }}:{{ include "code-interpreter.serviceAccountName" . }} -n {{ include "code-interpreter.kubernetesNamespace" . }}
{{- end }}

For more information, visit: https://github.com/your-org/code-interpreter
{{- include "code-interpreter.warnings" . }}
{{- end }}
