{{- define "demo-jobs.pod" -}}
automountServiceAccountToken: false
nodeSelector:
  idp.example.com/region: {{ .values.placementRegion | quote }}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  seccompProfile:
    type: RuntimeDefault
containers:
  - name: {{ .role }}
    image: "ghcr.io/theanh-05-uit/gitops-platform-workload-api@{{ .values.imageDigest }}"
    command: ["python", "-B", "-m", "workload_api.demo_jobs"]
    args: [{{ .role | quote }}]
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: [ALL]
    resources:
      requests: {cpu: 30m, memory: 40Mi}
      limits: {cpu: 100m, memory: 80Mi}
{{- end -}}
