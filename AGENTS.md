# GitOps configuration repository

When the Platform checkout is available, read its [shared workspace policy](../gitops-platform/docs/WORKSPACE_POLICY.md) and manually open the relevant `../gitops-platform/.agents/skills/<name>/SKILL.md`. In a standalone clone, follow the local rules below and verify contracts with the Platform and Workload owners.

- Change managed workloads through reviewed desired state. Keep the Application source, chart path, destination, selectors and rollback behavior consistent. Require explicit authorization for live changes.
- Preserve existing changes and avoid destructive Git operations. Require explicit authorization to commit, push, open, approve or merge a remote PR, or deploy. Use the verified target `main` and treat local remote-tracking refs as cached evidence.
- Preserve `apps/workload-api/values.yaml` as the promotion and placement contract. Coordinate changes to `replicaCount`, `nodeSelector.idp.example.com/region` or their YAML representation with the portal. Verify the promoted image and preserve the health route, named port and non-root constraints.
- From this root run `helm lint ./apps/workload-api`, `helm template workload-api ./apps/workload-api`, and `python scripts/check_deployment_policies.py <rendered.yaml>` with PyYAML. Render into a temporary file outside the checkout and inspect the output and policy exit code.
- Check schedulable labels and capacity separately from rendering. Keep cluster credentials and secret values out of Git.
