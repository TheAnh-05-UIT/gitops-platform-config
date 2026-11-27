# Private lab observability

The ServiceMonitor discovers each API pod through the named HTTP Service port, and drops unrecognized metric families. The optional isolation overlay permits the selected ingress controller and Prometheus pods. The Grafana dashboard displays requests by status class, p95 and healthy scrape targets.

| Signal | Starting lab objective | Measurement |
| --- | --- | --- |
| Success | At least 99% non-5xx for index requests over a populated 30-minute window | Compare the 5xx and total index request counter increases across pods. An empty interval has insufficient traffic. |
| Latency | At least 95% of index requests finish within 250 ms in the capped exercise | Divide the increases of the 250 ms and total buckets. The histogram covers requests reaching the process. |
| Scrape health | At least one reachable workload metrics target | Check the maximum of per-pod scrape health and perform an independent application response check. |

Alertmanager has a local receiver with no external notification integration. The platform operator owns alerts and the exercise. The server-error alert requires 20 observed index requests in five minutes and fires after five minutes above the threshold. Diagnose readiness using Kubernetes events and rollout status alongside telemetry.

Prometheus retains data for up to 48 hours with a 1 GiB TSDB retention target. Measure WAL, compaction and node disk use independently. One Loki replica uses filesystem storage, 48-hour compactor retention and a 4 GiB K3s local-path claim request. Local-path requests do not impose a disk quota. Pod and node replacement can lose metrics or interrupt writes. Check node disk, ingestion and retained claims at cleanup.

Alloy reads Pod logs through Kubernetes with namespaced permissions and explicit application discovery. Namespaced RBAC can read logs of all Pods in that namespace. The Alloy allow list only forwards the fixed request event, omits dynamic labels and drops other lines. Prevent secrets and personal data at the log source. Loki uses plain HTTP within the lab and relies on an enforced NetworkPolicy restricting ingress to collector and Grafana Pods. Restrict deployment and label writes in the observability namespace to authorized operators. All Services are private. Review any additional policies that could broaden Loki access.

## Offline verification and owner review

Run `python -B -m unittest discover -s tests` from Config. The tests check the local workload render, scrape and access policy contracts, dashboard JSON, alerts and log scope.

Obtain the pinned upstream chart artifacts for review and verify their provenance and hashes. Keep chart archives outside Git. Review Kubernetes compatibility, images, resource budgets, RBAC, webhooks, account tokens and host mounts in the actual render. The monitoring chart adds cluster CRDs and RBAC. Node Exporter uses host metrics and may require a reviewed observability namespace policy exception. Workloads retain Restricted Pod Security admission. [kube-prometheus-stack 85.0.0](https://github.com/prometheus-community/helm-charts/releases/tag/kube-prometheus-stack-85.0.0) · [Loki 18.7.6](https://github.com/grafana-community/helm-charts/releases/tag/loki-18.7.6) · [Alloy v1.22.0](https://github.com/grafana/alloy/releases/tag/v1.22.0)

```powershell
$MetricsChart = '<absolute-path-to-reviewed-kube-prometheus-stack-85.0.0.tgz>'
$LogsChart = '<absolute-path-to-reviewed-loki-18.7.6.tgz>'
helm template lab-observe $MetricsChart --namespace observability --include-crds -f observability/metrics/stack.values.yaml
helm template lab-loki $LogsChart --namespace observability -f observability/logs/loki.values.yaml
helm template workload-api ./apps/workload-api --namespace workloads -f observability/metrics/private-workload.values.yaml
```

Run `promtool check rules <extracted-rules.yaml>` on the PrometheusRule groups and `alloy validate <extracted-config.alloy>` on the Alloy configuration. Inspect the load script from Workload with `k6 inspect tests/load/workload-api.js`. Verify the rendered Loki port and pod labels match the ingress policy. Verify Service types, network selectors, compatible CPU architecture and node headroom.

## AWS-15-OBS1 · Owner metrics gate

Run from the approved Config checkout after AWS-15, a trusted promotion of the workload image exposing metrics, and separate approval of the monitoring change and CPU, memory and storage budget. Verify the previously approved AWS profile and Region and private cluster context. Provision Grafana's approved namespace Secret through the owner's private credential workflow. Record only credential existence as evidence.

```powershell
$Context = '<approved-private-k3s-context>'
kubectl --context $Context apply -f observability/namespace.yaml
# Pause to provision the approved lab-grafana-admin Secret securely.
helm upgrade --install lab-observe $MetricsChart --namespace observability --kube-context $Context -f observability/metrics/stack.values.yaml --wait
kubectl --context $Context apply -f observability/metrics/workload-monitoring.yaml -f observability/metrics/dashboard.yaml
kubectl --context $Context -n observability get pods,services,servicemonitors,prometheusrules
```

The admin Secret uses `admin-user` and `admin-password` keys. If the workload uses network isolation, apply the approved Prometheus peer through the workload overlay and reviewed Argo sync. Verify each workload target, node and Kubernetes state metrics, alert state and dashboard over private loopback port forwarding. Share a redacted dashboard and target and alert summary. Stop before enabling logs if verification fails. Inspect private events and roll back the inspected prior Helm revision under separate approval. For a failed first install, review the release, CRDs and cluster RBAC before cleanup.

## AWS-15-OBS2 · Owner logs and exercise gate

Run from the same approved Config checkout and private context after AWS-15-OBS1. Verify storage class and policy, approved chart and image versions, allowed events and retention. Confirm capacity for the additional local storage, ingestion and workloads.

```powershell
kubectl --context $Context get storageclass local-path
kubectl --context $Context apply -f observability/logs/loki-policy.yaml
helm upgrade --install lab-loki $LogsChart --namespace observability --kube-context $Context -f observability/logs/loki.values.yaml --wait
kubectl --context $Context apply -f observability/logs/alloy.yaml
kubectl --context $Context -n observability rollout status deployment/lab-alloy --timeout=3m
helm upgrade lab-observe $MetricsChart --namespace observability --kube-context $Context -f observability/metrics/stack.values.yaml -f observability/logs/grafana-loki.values.yaml --wait
kubectl --context $Context -n observability get pods,services,pvc
```

Verify Grafana returns only fixed request events for `{app="workload-api"}`. Verify a disallowed Pod cannot reach Loki. On policy or collection failure, stop exercise traffic and suspend collection after review. Inspect private events, restore the reviewed Grafana values and consider release rollback. Share only redacted event and service status evidence.

### Incident exercise and recovery

Use a disposable approved lab and retain a known-good Config revision. Open a local-only port-forward from the Config workstation and run the bounded load script from Workload in a second terminal after separate approval. The script uses three virtual users for 30 seconds, caps request duration at two seconds and disables redirects. Stop manually if latency or capacity degrades.

```powershell
# Config checkout
kubectl --context $Context -n workloads port-forward svc/workload-api 18080:80 --address 127.0.0.1
```

```powershell
# Workload checkout
$env:WORKLOAD_LOOPBACK = 'http://127.0.0.1:18080'
k6 run tests/load/workload-api.js
```

Record load duration, response distribution and p95. Under a separate approved fault exercise, make scraping temporarily unavailable with a reversible lab network policy change. Stop the load, record the alert time and check readiness, targets and policy. Restore the last approved Config revision with manual sync, verify endpoint health, confirm alert resolution and inspect one allowed log line. Record redacted detection and recovery times. Before cleanup, review persistent claims, Secrets, cluster RBAC and CRDs and export allowed telemetry evidence. Removing shared CRDs can affect other releases.
