{{/*
The pod template, shared by deployment.yaml and statefulset.yaml.

It is defined once on purpose: the only thing that should differ between the
storage modes is the VOLUME, not the container. If you find yourself wanting to
change the app to suit a storage mode, that is usually a sign the storage mode
is wrong for the app.
*/}}
{{- define "demo-app.podTemplate" -}}
metadata:
  annotations:
    # Roll the pods whenever the app source or its config changes. Editing a
    # ConfigMap does NOT restart pods by itself - these checksums are what makes
    # a config change actually take effect.
    checksum/src: {{ include (print .Template.BasePath "/configmap-app.yaml") . | sha256sum }}
    checksum/env: {{ include (print .Template.BasePath "/configmap-env.yaml") . | sha256sum }}
    {{- with .Values.podAnnotations }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
  labels:
    {{- include "demo-app.selectorLabels" . | nindent 4 }}
    {{- with .Values.podLabels }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
spec:
  serviceAccountName: {{ include "demo-app.serviceAccountName" . }}
  automountServiceAccountToken: {{ .Values.automountServiceAccountToken }}
  {{- with .Values.imagePullSecrets }}
  imagePullSecrets:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  securityContext:
    {{- toYaml .Values.podSecurityContext | nindent 4 }}
  terminationGracePeriodSeconds: {{ .Values.terminationGracePeriodSeconds }}

  containers:
    - name: app
      image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
      imagePullPolicy: {{ .Values.image.pullPolicy }}
      command: ["python", "/app/app.py"]
      ports:
        - name: http
          containerPort: {{ .Values.app.port }}
          protocol: TCP
      envFrom:
        - configMapRef:
            name: {{ include "demo-app.fullname" . }}-env
      env:
        # Downward API: the pod tells the app who and where it is. This is what
        # makes /info and /boots useful once there is more than one replica.
        - name: POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        - name: POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        - name: POD_IP
          valueFrom:
            fieldRef:
              fieldPath: status.podIP
        - name: NODE_NAME
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
        {{- with .Values.app.extraEnv }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- if .Values.probes.startup.enabled }}
      startupProbe:
        httpGet:
          path: {{ .Values.probes.startup.path }}
          port: http
        initialDelaySeconds: {{ .Values.probes.startup.initialDelaySeconds }}
        periodSeconds: {{ .Values.probes.startup.periodSeconds }}
        failureThreshold: {{ .Values.probes.startup.failureThreshold }}
      {{- end }}
      {{- if .Values.probes.readiness.enabled }}
      readinessProbe:
        # Fails -> the pod leaves the Service endpoints but is NOT restarted.
        httpGet:
          path: {{ .Values.probes.readiness.path }}
          port: http
        periodSeconds: {{ .Values.probes.readiness.periodSeconds }}
        timeoutSeconds: {{ .Values.probes.readiness.timeoutSeconds }}
        failureThreshold: {{ .Values.probes.readiness.failureThreshold }}
      {{- end }}
      {{- if .Values.probes.liveness.enabled }}
      livenessProbe:
        # Fails -> the container is KILLED and restarted. A different job.
        httpGet:
          path: {{ .Values.probes.liveness.path }}
          port: http
        periodSeconds: {{ .Values.probes.liveness.periodSeconds }}
        timeoutSeconds: {{ .Values.probes.liveness.timeoutSeconds }}
        failureThreshold: {{ .Values.probes.liveness.failureThreshold }}
      {{- end }}
      {{- if gt (int .Values.lifecycle.preStopSleepSeconds) 0 }}
      lifecycle:
        preStop:
          # Sleep before the app sees SIGTERM so kube-proxy has time to stop
          # routing new connections here.
          exec:
            command: ["sleep", "{{ .Values.lifecycle.preStopSleepSeconds }}"]
      {{- end }}
      resources:
        {{- toYaml .Values.resources | nindent 8 }}
      securityContext:
        {{- toYaml .Values.securityContext | nindent 8 }}
      volumeMounts:
        - name: src
          mountPath: /app
          readOnly: true
        - name: tmp
          mountPath: /tmp
        # The volume this whole chart is about. The mount looks identical in all
        # three storage modes - only what is behind it changes.
        - name: data
          mountPath: {{ .Values.storage.mountPath }}
          {{- with .Values.storage.subPath }}
          subPath: {{ . | quote }}
          {{- end }}

  volumes:
    - name: src
      configMap:
        name: {{ include "demo-app.fullname" . }}-src
        defaultMode: 0555
    - name: tmp
      # Needed because readOnlyRootFilesystem is true.
      emptyDir: {}
    {{- if eq .Values.storage.mode "none" }}
    # mode=none: scratch space that dies with the pod. Restart it and /boots is
    # back to a single line - which is exactly the lesson.
    - name: data
      emptyDir:
        sizeLimit: {{ .Values.storage.emptyDirSizeLimit }}
    {{- else if eq .Values.storage.mode "shared" }}
    # mode=shared: one claim, mounted by every replica at once. Needs an access
    # mode the StorageClass actually supports - ReadWriteMany, i.e. Azure Files.
    - name: data
      persistentVolumeClaim:
        claimName: {{ include "demo-app.claimName" . }}
    {{- end }}
    {{- /*
      mode=perPod deliberately declares NO data volume here. The StatefulSet
      supplies it through volumeClaimTemplates, which is the only mechanism in
      Kubernetes that creates one PVC per replica.
    */}}

  {{- with .Values.nodeSelector }}
  nodeSelector:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with .Values.tolerations }}
  tolerations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- if .Values.affinity }}
  affinity:
    {{- toYaml .Values.affinity | nindent 4 }}
  {{- else if .Values.podAntiAffinity.enabled }}
  affinity:
    podAntiAffinity:
      {{- if eq .Values.podAntiAffinity.type "required" }}
      requiredDuringSchedulingIgnoredDuringExecution:
        - topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels:
              {{- include "demo-app.selectorLabels" . | nindent 14 }}
      {{- else }}
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          podAffinityTerm:
            topologyKey: kubernetes.io/hostname
            labelSelector:
              matchLabels:
                {{- include "demo-app.selectorLabels" . | nindent 16 }}
      {{- end }}
  {{- end }}
  {{- with .Values.topologySpreadConstraints }}
  topologySpreadConstraints:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
