# Demo jobs

The worker logs an availability observation every 30 seconds. The CronJob groups three observations every 15 minutes and exits nonzero on a failed request. Both use the private workload API health endpoint. Logs contain only the event kind, counts and latency. The chart is disabled by default and creates a Deployment, CronJob and scoped NetworkPolicy when explicitly enabled.

```sh
helm lint ./apps/demo-jobs
helm template demo-jobs ./apps/demo-jobs -n workloads --set enabled=true \
  --set-string imageDigest="sha256:$(printf 'a%.0s' $(seq 1 64))" --set-string placementRegion=eu-west-1
```

That digest is a render fixture. For owner gate AWS-15-DEMO, use the digest of the published, scanned image containing `workload_api.demo_jobs`. Confirm the actual node label, capacity and CoreDNS labels. In restrictive API NetworkPolicy mode, add an independently reviewed ingress peer with namespace `workloads` and label `idp.example.com/demo-probe: "true"`. The chart permits DNS and only the API Pod's HTTP port on egress. DNS and Service DNAT ordering need an actual CNI check.

Follow the [Config Argo CD gate](../../argocd/README.md). In a reviewed private copy of [the example](../../argocd/examples/demo-jobs.yaml.example), replace the two placeholders with the approved digest and placement label. Use `$DemoManifest` for its absolute path. The AppProject permits three namespaced resource kinds. The Application uses manual sync. Review the Git revision, image, label, network policy peers, resource budget and source before applying.

```powershell
# Owner only, from the approved Config root with the AWS-15 context and loopback Argo connection.
kubectl --context $Context apply -f $DemoManifest
argocd app diff demo-jobs --server localhost:8443 --revision $Revision
# Separate owner approval after reviewing the observed diff.
argocd app sync demo-jobs --server localhost:8443 --revision $Revision
kubectl --context $Context -n workloads rollout status deployment/demo-probe-worker --timeout=3m
kubectl --context $Context -n workloads get cronjob demo-availability-batch
kubectl --context $Context -n workloads logs deployment/demo-probe-worker --tail=5
```

Capture worker and next scheduled Job outcomes, relevant image digest and rollout revision without private node information. Stop if quotas, private DNS or policies fail. Roll back to the approved recorded Application settings and Git revision. Review deletion/prune separately. Jobs and logs consume the lab's existing compute and storage.
