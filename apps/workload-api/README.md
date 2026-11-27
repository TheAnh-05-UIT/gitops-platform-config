# Workload API chart

The base values keep the existing image and simulated-region selector, fixed replicas, a ClusterIP service and explicit CPU and memory budgets. Port `http` routes to 8080. Startup, readiness and liveness query the local `/healthz` endpoint. The Pod runs as image UID/GID 10001 with RuntimeDefault seccomp, no service-account token, no escalated privilege, no capabilities and a read-only root filesystem. A bounded temporary volume at `/tmp` supports Gunicorn. The health endpoint verifies the process. Add dependency-aware readiness if the application later acquires critical external dependencies.

## Offline validation

```powershell
helm lint apps/workload-api
helm lint apps/workload-api -f apps/workload-api/examples/private-lab.values.yaml
python -B -m unittest discover -s tests -p test_lab_manifests.py
```

Run the Config tests from the repository root with Python, PyYAML and Helm available. The example overlays are sanitized fixtures for render and review.

## Approved private ingress and isolation

The `private-lab.values.yaml` example demonstrates opt-in TLS routing with K3s Traefik's `websecure` entry point, one exact host and application path, an owner-provided existing namespace TLS secret and explicit controller namespace and pod label selectors. Health probes and metrics use internal network paths. The owner verifies the actual ingress class and controller labels, trusted certificate, private DNS, listener, and private firewall path before adoption. The chart itself does not publish a load balancer, DNS entry, certificate or private key. Use private port forwarding for the default configuration. Use of the example overlay in Argo CD requires a separate review of which values files the Application selects.

The optional NetworkPolicy selects the API pods. Each permitted source must match both the namespace and labels of a peer and can connect only on TCP 8080. With an empty ingress allow list it denies ingress. It denies Pod egress including DNS because the API only serves HTTP locally. A changed workload with dependencies needs an approved allow-list change. Verify the K3s CNI enforces the policy and that the controller uses ordinary pods with the selected labels. Kubernetes NetworkPolicy has implementation-dependent host-network handling. Other additive policies can allow additional paths. Test an approved caller and a denied caller before demonstrating isolation.

The example also enables namespace-wide ResourceQuota and LimitRange. Review existing pods, HPA range and rollout surge against the pod quota and CPU/memory requests and limits. Quota admission can stall scaling and updates or block other workloads in the same namespace. Prefer separate namespaces for unrelated apps.

## Approved elasticity

`autoscaling.enabled` opts in to CPU utilization scaling through the Resource Metrics API. Set the minimum, maximum and utilization target against the configured CPU request and available CPU and memory. The Deployment omits `spec.replicas` with autoscaling enabled. Keep the Git `replicaCount` contract for fixed mode and disable fixed-replica portal placement requests while the HPA owns replicas. A prior fixed deployment can briefly change replica count on the first sync as field ownership changes. Review live replicas, rollout and resource metrics during the transition. Test scale-up and scale-down under capped load.

`disruptionBudget.enabled` requires a verified `eligibleNodes` count and a floor above `minAvailable`. The floor is the HPA minimum or the fixed replicas. Nodes must be schedulable for the actual node selector and have sufficient capacity. The chart spreads pods on a best-effort basis. The PDB applies to qualifying voluntary disruptions and cannot guarantee availability during node failure. Keep it disabled on the initial one-worker placement unless capacity and topology support a reviewed budget. Two workers with the same placement label and a two-replica floor can be reviewed with `minAvailable: 1`.

Review the private ingress and elasticity options independently. For rollback, return to the approved values revision and review which optional resources require deletion and whether any cleanup can be safely pruned. When reverting HPA ownership, coordinate its removal and restoration of the fixed replica field in a reviewed sync. Confirm Service reachability and Ready replicas afterward.

Keep these features in separately reviewed environment overlays. The trusted image promotion checker expects the base Helm render to contain exactly the Deployment and Service and the promotion diff to change only the base image tag.
