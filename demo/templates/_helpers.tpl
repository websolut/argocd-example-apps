{{/*
Chart name, overridable.
*/}}
{{- define "demo-app.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name: <release>-<chart>, unless the release name already
contains the chart name (avoids "demo-app-demo-app").
*/}}
{{- define "demo-app.fullname" -}}
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

{{- define "demo-app.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Labels on every object. app.kubernetes.io/* are the Kubernetes recommended
labels; Argo CD adds its own tracking labels on top of these.
*/}}
{{- define "demo-app.labels" -}}
helm.sh/chart: {{ include "demo-app.chart" . }}
{{ include "demo-app.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: aks-lab
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels. These are immutable on a Deployment or StatefulSet once
created, so never put anything volatile (like a version) in here.
*/}}
{{- define "demo-app.selectorLabels" -}}
app.kubernetes.io/name: {{ include "demo-app.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "demo-app.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "demo-app.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Which controller are we? storage.mode = perPod needs a StatefulSet, because
volumeClaimTemplates - one PVC per pod - only exist there. The other two modes
share one volume (or none), which a Deployment handles fine.

Returns a non-empty string for "yes", empty for "no", which is how Helm does
booleans in templates.
*/}}
{{- define "demo-app.isStateful" -}}
{{- if eq .Values.storage.mode "perPod" -}}
true
{{- end -}}
{{- end }}

{{- define "demo-app.controllerKind" -}}
{{- if include "demo-app.isStateful" . }}StatefulSet{{ else }}Deployment{{ end }}
{{- end }}

{{/*
Name of the single shared PVC. Only rendered when storage.mode = shared; in
perPod mode the StatefulSet generates names like data-demo-app-0 itself.
*/}}
{{- define "demo-app.claimName" -}}
{{- printf "%s-data" (include "demo-app.fullname" .) }}
{{- end }}

{{/*
Headless service name - required by a StatefulSet for stable per-pod DNS
(demo-app-0.demo-app-headless.<ns>.svc.cluster.local). Being able to address one
specific pod is the whole reason to care: with per-pod volumes, "which replica
answered" stops being a detail and becomes the question.
*/}}
{{- define "demo-app.headlessName" -}}
{{- printf "%s-headless" (include "demo-app.fullname" .) }}
{{- end }}

{{/*
Validate the storage configuration before the cluster does it for you with a
30-minute timeout. Fails the render on combinations that cannot work.
*/}}
{{- define "demo-app.validateStorage" -}}
{{- $mode := .Values.storage.mode }}
{{- if not (has $mode (list "none" "shared" "perPod")) }}
{{- fail (printf "storage.mode must be one of none|shared|perPod, got %q" $mode) }}
{{- end }}
{{- if and (eq $mode "perPod") (has "ReadWriteMany" .Values.storage.accessModes) }}
{{- fail "storage.mode=perPod gives each pod its own volume, so it should be ReadWriteOnce - ReadWriteMany here means you probably wanted storage.mode=shared" }}
{{- end }}
{{- end }}
