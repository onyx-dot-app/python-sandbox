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
{{- if (($executor.podResources | default dict).limits | default dict).memory -}}
{{- fail "codeInterpreter.kubernetesExecutor.podResources.limits.memory is not supported: the memory limit of execution pods is codeInterpreter.memoryLimitMb. Remove limits.memory and set memoryLimitMb instead." -}}
{{- end -}}
{{- if and (eq $mode "platform") $executor.netAdminLockdown -}}
{{- fail "codeInterpreter.kubernetesExecutor.securityContext.mode=platform requires codeInterpreter.kubernetesExecutor.netAdminLockdown=false: the lockdown init container runs as root with NET_ADMIN, which restricted admission (Pod Security \"restricted\", OpenShift restricted-v2) rejects. Enforce egress with the executor NetworkPolicy instead." -}}
{{- end -}}
{{- end }}
