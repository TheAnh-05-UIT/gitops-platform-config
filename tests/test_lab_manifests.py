from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from scripts.check_deployment_policies import check_deployment
from scripts.promotion_policy import image_tag, patch_tag, validate_rendered


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "apps" / "workload-api"
REPO = "https://github.com/theanh-05-uit/gitops-platform-config.git"


def render(*arguments):
    return subprocess.run(
        ["helm", "template", "workload-api", str(CHART), "--namespace", "workloads", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


class LabManifestsTest(unittest.TestCase):
    def assert_rendered(self, *arguments):
        result = render(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {
            document["kind"]: document
            for document in yaml.safe_load_all(result.stdout)
            if document
        }

    def test_default_remains_fixed_clusterip_and_preserves_placement_and_image(self):
        documents = self.assert_rendered()
        self.assertEqual(set(documents), {"Deployment", "Service"})
        deployment = documents["Deployment"]
        spec = deployment["spec"]
        self.assertEqual(spec["replicas"], 2)
        pod = spec["template"]["spec"]
        container = pod["containers"][0]
        values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
        self.assertEqual(container["image"], f"{values['image']['repository']}:{values['image']['tag']}")
        self.assertEqual(pod["nodeSelector"], values["nodeSelector"])
        self.assertEqual(container["resources"], values["resources"])
        self.assertEqual(documents["Service"]["spec"]["type"], "ClusterIP")
        self.assertEqual(documents["Service"]["spec"]["ports"][0]["targetPort"], "http")
        self.assertEqual(check_deployment(deployment), [])

    def test_promotion_patch_changes_only_the_tag_and_still_renders_the_trusted_contract(self):
        original = (CHART / "values.yaml").read_text(encoding="utf-8")
        candidate = "a" * 40
        patched = patch_tag(original, candidate)
        self.assertEqual(patch_tag(patched, image_tag(original)), original)
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text(patched, encoding="utf-8")
            documents = self.assert_rendered("-f", str(values))
        self.assertEqual(set(documents), {"Deployment", "Service"})
        validate_rendered(list(documents.values()), candidate)

    def test_restricted_pod_has_writable_tmp_and_http_probes(self):
        deployment = self.assert_rendered()["Deployment"]
        pod = deployment["spec"]["template"]["spec"]
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertEqual(pod["securityContext"]["runAsUser"], 10001)
        self.assertEqual(pod["volumes"], [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}])
        container = pod["containers"][0]
        self.assertEqual(container["securityContext"], {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        })
        self.assertEqual(container["volumeMounts"], [{"name": "tmp", "mountPath": "/tmp"}])
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertEqual(container[probe]["httpGet"], {"path": "/healthz", "port": "http"})

    def test_policy_rejects_unsafe_pod(self):
        deployment = self.assert_rendered()["Deployment"]
        pod = deployment["spec"]["template"]["spec"]
        pod["automountServiceAccountToken"] = True
        pod["hostNetwork"] = True
        pod["securityContext"]["seccompProfile"]["type"] = "Unconfined"
        pod["containers"][0]["securityContext"]["allowPrivilegeEscalation"] = True
        pod["containers"][0]["securityContext"]["capabilities"]["add"] = ["SYS_ADMIN"]
        errors = "\n".join(check_deployment(deployment))
        for field in ("automountServiceAccountToken", "hostNetwork", "seccomp", "allowPrivilegeEscalation", "capabilities"):
            self.assertIn(field, errors)

    def test_reviewed_overlay_enables_tls_and_narrow_network_policy(self):
        documents = self.assert_rendered("-f", str(CHART / "examples" / "private-lab.values.yaml"))
        ingress = documents["Ingress"]["spec"]
        host = ingress["rules"][0]["host"]
        self.assertEqual(ingress["tls"], [{"hosts": [host], "secretName": "workload-api-lab-tls"}])
        self.assertEqual(
            [(path["path"], path["pathType"]) for path in ingress["rules"][0]["http"]["paths"]],
            [("/", "Exact")],
        )
        self.assertEqual(ingress["rules"][0]["http"]["paths"][0]["backend"], {
            "service": {"name": "workload-api", "port": {"name": "http"}}
        })
        policy = documents["NetworkPolicy"]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(policy["ingress"][0]["ports"], [{"port": 8080, "protocol": "TCP"}])
        self.assertEqual(policy["ingress"][0]["from"], [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
        }])
        self.assertEqual(policy["egress"], [])
        self.assertIn("ResourceQuota", documents)
        self.assertIn("LimitRange", documents)

    def test_invalid_ingress_and_peers_fail_render(self):
        cases = [
            ("ingress.enabled=true",),
            ("networkPolicy.enabled=true", "networkPolicy.allowedIngress[0].namespace=kube-system"),
            ("ingress.enabled=true", "ingress.className=traefik", "ingress.host=*.example.test", "ingress.tlsSecretName=tls"),
            ("ingress.enabled=true", "ingress.className=traefik", "ingress.host=10.0.0.1", "ingress.tlsSecretName=tls"),
            ("ingress.enabled=true", "ingress.className=traefik", "ingress.host=workload.example.test", "ingress.tlsSecretName=tls", "networkPolicy.enabled=true"),
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                arguments = [argument for value in settings for argument in ("--set-string" if value.startswith("ingress.host=") else "--set", value)]
                self.assertNotEqual(render(*arguments).returncode, 0)

    def test_autoscaler_owns_replicas_and_pdb_requires_capacity(self):
        documents = self.assert_rendered(
            "--set", "autoscaling.enabled=true",
            "--set", "autoscaling.minReplicas=2",
            "--set", "disruptionBudget.enabled=true",
            "--set", "disruptionBudget.eligibleNodes=2",
        )
        self.assertNotIn("replicas", documents["Deployment"]["spec"])
        self.assertEqual(documents["HorizontalPodAutoscaler"]["spec"]["minReplicas"], 2)
        self.assertEqual(documents["PodDisruptionBudget"]["spec"]["minAvailable"], 1)
        self.assertEqual(documents["PodDisruptionBudget"]["spec"]["selector"],
                         documents["Deployment"]["spec"]["selector"])
        invalid = [
            ["--set", "disruptionBudget.enabled=true"],
            ["--set", "disruptionBudget.enabled=true", "--set", "disruptionBudget.eligibleNodes=2", "--set", "replicaCount=1"],
            ["--set", "autoscaling.enabled=true", "--set", "autoscaling.minReplicas=4", "--set", "autoscaling.maxReplicas=3"],
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assertNotEqual(render(*arguments).returncode, 0)

    def test_argo_uses_a_narrow_project_and_reviewed_sync(self):
        application = yaml.safe_load((ROOT / "argocd/applications/workload-api.yaml").read_text(encoding="utf-8"))
        project = yaml.safe_load((ROOT / "argocd/projects/workload-lab.yaml").read_text(encoding="utf-8"))
        namespace = yaml.safe_load((ROOT / "argocd/namespaces/workloads.yaml").read_text(encoding="utf-8"))
        spec = application["spec"]
        self.assertEqual(spec["source"], {"repoURL": REPO, "targetRevision": "main", "path": "apps/workload-api"})
        self.assertEqual(spec["project"], project["metadata"]["name"])
        self.assertIs(spec["syncPolicy"]["automated"]["enabled"], False)
        self.assertEqual(project["spec"]["sourceRepos"], [REPO])
        self.assertEqual(project["spec"]["destinations"], [spec["destination"]])
        self.assertEqual(project["spec"]["clusterResourceWhitelist"], [])
        self.assertNotIn({"group": "", "kind": "Secret"}, project["spec"]["namespaceResourceWhitelist"])
        self.assertEqual(namespace["metadata"]["name"], spec["destination"]["namespace"])
        self.assertEqual(namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")

    def test_policy_cli_accepts_render_without_live_cluster(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.yaml"
            path.write_text(result.stdout, encoding="utf-8")
            policy = subprocess.run(
                [sys.executable, str(ROOT / "scripts/check_deployment_policies.py"), str(path)],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(policy.returncode, 0, policy.stdout + policy.stderr)
