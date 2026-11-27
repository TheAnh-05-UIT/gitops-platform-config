# Argo CD lab bootstrap

## AWS-15 owner gate

This gate follows AWS-14 and the manual cloud execution policy. Run it from the reviewed Config checkout as the owner, after the private kubeconfig context, node readiness, Kubernetes version, audited platform revision and approved image provenance have been verified. Confirm the AWS profile and Region previously approved for AWS-14. These commands use the private Kubernetes context from that gate. Use a private API connection, such as the approved SSM tunnel.

Argo CD 3.6.3 is pinned as the review candidate at commit `b4accc482028fd529b3e4508ef9f28977b552b50`. The supported Kubernetes range listed for that release series is 1.34–1.37. Review the upstream manifest, bundled image references, CRDs, cluster RBAC, compatibility and security advisories before approving the candidate. Download and verify the manifest bytes separately, record the SHA-256 from the approved review, and store the file outside Git. [Release](https://github.com/argoproj/argo-cd/releases/tag/v3.6.3) · [Pinned manifest](https://raw.githubusercontent.com/argoproj/argo-cd/b4accc482028fd529b3e4508ef9f28977b552b50/manifests/install.yaml) · [Installation compatibility](https://argo-cd.readthedocs.io/en/release-3.6/operator-manual/installation/#tested-versions)

The controller installation adds cluster-scoped CRDs and RBAC. The app uses only the Git source and in-cluster destination declared in `workload-lab`. Project policy permits namespaced chart resources in `workloads`. Apply that namespace separately with the Restricted Pod Security profile. Give human app operators project-scoped read and approved sync permissions and keep Application, AppProject and credential writes with the platform administrator. AppProject restrictions complement operator RBAC and admission. Limit owner access to the Argo CD server via local port forwarding. If private repository access is required, supply a dedicated credential with access to read that repository and store it outside Git. Plan bootstrap admin credential rotation, audit and eventual disablement after verifying restricted access. Keep pull credentials, bootstrap credentials and TLS keys outside commit history and logs.

### Read-only preflight

Set these values on the approved owner workstation. The install manifest and Config commit must already be reviewed.

```powershell
$Context = '<approved-private-k3s-context>'
$Revision = '<approved-config-commit-sha-on-main>'
$Manifest = '<absolute-path-to-reviewed-argocd-install.yaml>'
$ApprovedManifestSha256 = '<approved-install-manifest-sha256>'
$ManifestHash = (Get-FileHash -LiteralPath $Manifest -Algorithm SHA256).Hash
if ($ManifestHash -ne $ApprovedManifestSha256) { throw 'Argo CD manifest hash mismatch' }
if ((git rev-parse HEAD) -ne $Revision) { throw 'Config checkout mismatch' }
if (git status --porcelain) { throw 'Config checkout must be clean for this gate' }
kubectl --context $Context version
kubectl --context $Context get nodes
kubectl --context $Context get namespaces argocd workloads --ignore-not-found
```

Review the expected K3s version, the intended node count, readiness and existing Argo CD installation. Stop on a context mismatch, an unhealthy cluster or pre-existing objects whose ownership or configuration is unclear.

### Separate bootstrap mutation approval

Only after approval, create the dedicated namespace on a fresh installation. Review existing Argo CD upgrades separately. Inspect each command result before proceeding.

```powershell
kubectl --context $Context create namespace argocd
kubectl --context $Context apply --server-side -n argocd -f $Manifest
kubectl --context $Context -n argocd rollout status deployment/argocd-server --timeout=5m
kubectl --context $Context -n argocd rollout status deployment/argocd-repo-server --timeout=5m
kubectl --context $Context -n argocd rollout status statefulset/argocd-application-controller --timeout=5m
kubectl --context $Context apply -f argocd/namespaces/workloads.yaml
kubectl --context $Context apply -f argocd/projects/workload-lab.yaml
kubectl --context $Context apply -f argocd/applications/workload-api.yaml
kubectl --context $Context -n argocd get appproject workload-lab
kubectl --context $Context -n argocd get application workload-api
```

The Application remains manual. The first comparison may report OutOfSync. The project must show the reviewed source and destination, the namespace must enforce Restricted admission, and the server Service must remain private.

### Separate workload sync approval

Compare the approved revision through a loopback port-forward and the approved Argo CD operator identity. Verify the deployed image against the trusted promotion receipt. Switch terminals for CLI steps after establishing the port-forward.

```powershell
kubectl --context $Context -n argocd port-forward svc/argocd-server 8443:443 --address 127.0.0.1
```

```powershell
argocd app diff workload-api --server localhost:8443 --revision $Revision
argocd app sync workload-api --server localhost:8443 --revision $Revision
argocd app wait workload-api --server localhost:8443 --sync --health --timeout 300
kubectl --context $Context -n workloads rollout status deployment/workload-api --timeout=5m
kubectl --context $Context -n workloads get service workload-api
```

Run sync only after the owner accepts the source, target, image proof, diff and resource budget. The diff exit status may indicate changes. Inspect the comparison and stop on auth, rendering or trust problems. Use verified certificates and an authenticated CLI session. Confirm the reviewed revision is Synced and Healthy, the deployment has ready Pods and the Service remains ClusterIP. Share only product versions, revision, project and namespace, pod readiness and redacted error/status summaries. Owner approval of this gate leaves hosted publication and promotion freezes in place.

### Failure and recovery

On chart, admission, trust or health failure, keep sync manual and stop further mutation. Review sanitized Argo events, pod events and placement and resource constraints. For rollback use the last known trusted Config revision linked to the valid image evidence and review its diff before a separate sync. Reconcile Git with the approved reversion before a later sync. Pruning, uninstalling Argo CD or deleting the namespace requires a separately approved inventory of resources and recovery steps. The controller's CRDs and RBAC can affect other applications. Preserve any needed backups before decommissioning.

Controller Pods consume EC2 CPU, memory and local disk. Repository and image pulls use outbound bandwidth through the deployed profile. This gate adds no managed load balancer. Retain headroom for controller restarts, and include its consumption when reviewing whether a larger instance is needed.
