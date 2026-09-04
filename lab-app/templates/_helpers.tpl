{{/*
Chart name, overridable.
*/}}
{{- define "lab-app.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name: <release>-<chart>, unless the release name already
contains the chart name (avoids "lab-app-lab-app").
*/}}
{{- define "lab-app.fullname" -}}
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

{{- define "lab-app.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Labels on every object. app.kubernetes.io/* are the Kubernetes recommended
labels; Argo CD also adds its own tracking labels on top of these.
*/}}
{{- define "lab-app.labels" -}}
helm.sh/chart: {{ include "lab-app.chart" . }}
{{ include "lab-app.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: aks-lab
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels: these are immutable on a Deployment once created. Never add
anything volatile (like a version) here.
*/}}
{{- define "lab-app.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lab-app.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "lab-app.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "lab-app.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Do we need the shared log volume? Only when a sidecar wants to read the log file.
*/}}
{{- define "lab-app.needsLogVolume" -}}
{{- if .Values.sidecars.logTailer.enabled -}}
true
{{- end -}}
{{- end }}
