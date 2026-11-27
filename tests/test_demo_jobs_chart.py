from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "apps/demo-jobs"
DIGEST = "sha256:" + "a" * 64


@unittest.skipUnless(shutil.which("helm"), "Helm is required for chart render tests")
class DemoJobsChart(unittest.TestCase):
    def render(self, *settings):
        return subprocess.run(["helm", "template", "demo-jobs", str(CHART), "-n", "workloads", *settings],
                              capture_output=True, text=True, check=False)

    def test_default_disabled_and_missing_digest_fails(self):
        default = self.render()
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(default.stdout.strip(), "")
        self.assertNotEqual(self.render("--set", "enabled=true").returncode, 0)

    def test_private_worker_and_bounded_scheduled_batch(self):
        rendered = self.render("--set", f"enabled=true,imageDigest={DIGEST},placementRegion=eu-west-1")
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        docs = {doc["kind"]: doc for doc in yaml.safe_load_all(rendered.stdout)}
        self.assertEqual(set(docs), {"Deployment", "CronJob", "NetworkPolicy"})
        worker = docs["Deployment"]["spec"]["template"]["spec"]
        cron = docs["CronJob"]["spec"]
        self.assertEqual(cron["concurrencyPolicy"], "Forbid")
        job = cron["jobTemplate"]["spec"]
        self.assertEqual(job["backoffLimit"], 0)
        for role, pod in (("worker", worker), ("batch", job["template"]["spec"])):
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["nodeSelector"], {"idp.example.com/region": "eu-west-1"})
            container = pod["containers"][0]
            self.assertTrue(container["image"].endswith("@" + DIGEST))
            self.assertIn(role, container["args"])
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(docs["NetworkPolicy"]["spec"]["policyTypes"], ["Ingress", "Egress"])

    def test_opt_in_application_is_scoped_and_manual(self):
        path = ROOT / "argocd/examples/demo-jobs.yaml.example"
        project, app = yaml.safe_load_all(path.read_text(encoding="utf-8"))
        self.assertEqual(app["spec"]["project"], project["metadata"]["name"])
        self.assertEqual([app["spec"]["destination"]], project["spec"]["destinations"])
        self.assertEqual([app["spec"]["source"]["repoURL"]], project["spec"]["sourceRepos"])
        self.assertEqual(app["spec"]["source"]["path"], "apps/demo-jobs")
        self.assertFalse(app["spec"]["syncPolicy"]["automated"]["enabled"])
        self.assertEqual(project["spec"]["clusterResourceWhitelist"], [])
        self.assertEqual({item["kind"] for item in project["spec"]["namespaceResourceWhitelist"]},
                         {"CronJob", "Deployment", "NetworkPolicy"})
        self.assertFalse((ROOT / "argocd/applications/demo-jobs.yaml").exists())
