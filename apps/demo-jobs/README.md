# Demo jobs

The worker logs an availability observation every 30 seconds. The CronJob groups three observations every 15 minutes and exits nonzero on a failed request. Both use the private workload API health endpoint. Logs contain only the event kind, counts and latency. The chart is disabled by default and creates a Deployment, CronJob and scoped NetworkPolicy when explicitly enabled.

```sh
helm lint ./apps/demo-jobs
helm template demo-jobs ./apps/demo-jobs -n workloads --set enabled=true \
  --set-string imageDigest="sha256:$(printf 'a%.0s' $(seq 1 64))" --set-string placementRegion=eu-west-1
```

That digest is a render fixture. For owner gate AWS-15-DEMO, use the digest of the published, scanned image containing `workload_api.demo_jobs`. Confirm the actual node label, capacity and CoreDNS labels. In restrictive API NetworkPolicy mode, add an independently reviewed ingress peer with namespace `workloads` and label `idp.example.com/demo-probe: "true"`. The chart permits DNS and only the API Pod's HTTP port on egress. DNS and Service DNAT ordering need an actual CNI check.

Follow the Config Argo CD gate and review a dedicated manual-sync Application targeting only this chart and `workloads`. Products outside those three resource kinds need their own scope review. From the approved private context, the owner can check `kubectl -n workloads rollout status deployment/demo-probe-worker`, `kubectl -n workloads get cronjob demo-availability-batch`, and `kubectl -n workloads logs deployment/demo-probe-worker --tail=5`. Capture the worker and Job outcomes, relevant image digest and rollout revision without private node information. Stop if quotas, private DNS or policies fail. Roll back the Git change and sync the reviewed rollback with deletion/prune reviewed separately. No separate external service is used. Jobs and logs consume the lab's existing compute and storage.
