import json
from pathlib import Path
import re
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY = ROOT / "observability"


def load(name):
    return list(yaml.safe_load_all((OBSERVABILITY / name).read_text(encoding="utf-8")))


class ObservabilityTest(unittest.TestCase):
    def test_metric_target_matches_workload_service_and_isolation_overlay(self):
        rendered = subprocess.check_output(
            ["helm", "template", "workload-api", str(ROOT / "apps/workload-api"),
             "--namespace", "workloads", "-f", str(OBSERVABILITY / "metrics/private-workload.values.yaml")],
            text=True,
        )
        manifests = {doc["kind"]: doc for doc in yaml.safe_load_all(rendered) if doc}
        monitor, _ = load("metrics/workload-monitoring.yaml")
        spec = monitor["spec"]
        stack = load("metrics/stack.values.yaml")[0]
        prometheus = stack["prometheus"]["prometheusSpec"]
        self.assertEqual(spec["namespaceSelector"]["matchNames"], ["workloads"])
        self.assertEqual(spec["selector"]["matchLabels"], manifests["Service"]["metadata"]["labels"])
        endpoint = spec["endpoints"][0]
        self.assertEqual(endpoint["port"], manifests["Service"]["spec"]["ports"][0]["name"])
        self.assertEqual(endpoint["path"], "/metrics")
        self.assertEqual(endpoint["metricRelabelings"][0]["action"], "keep")
        self.assertIn(prometheus["podMetadata"]["labels"], [
            peer["podSelector"]["matchLabels"]
            for peer in manifests["NetworkPolicy"]["spec"]["ingress"][0]["from"]
        ])
        self.assertTrue(all(
            path["path"] == "/" and path["pathType"] == "Exact"
            for rule in manifests["Ingress"]["spec"]["rules"] for path in rule["http"]["paths"]
        ))

    def test_metrics_stack_uses_internal_services_bounded_retention_and_local_alerting(self):
        stack = load("metrics/stack.values.yaml")[0]
        for component in ("alertmanager", "grafana", "prometheus"):
            self.assertEqual(stack[component]["service"]["type"], "ClusterIP")
            self.assertFalse(stack[component]["ingress"]["enabled"])
        self.assertEqual(stack["grafana"]["admin"]["existingSecret"], "lab-grafana-admin")
        self.assertEqual(stack["prometheus"]["prometheusSpec"]["retention"], "48h")
        self.assertEqual(stack["prometheus"]["prometheusSpec"]["retentionSize"], "1GiB")
        config = stack["alertmanager"]["config"]
        self.assertEqual(config["receivers"], [{"name": config["route"]["receiver"]}])
        self.assertEqual(config["route"]["routes"], [])
        _, rule = load("metrics/workload-monitoring.yaml")
        self.assertEqual(rule["metadata"]["labels"], {"release": "lab-observe"})
        alerts = rule["spec"]["groups"][0]["rules"]
        self.assertEqual({a["alert"] for a in alerts},
                         {"WorkloadAPITargetUnavailable", "WorkloadAPIHighServerErrorRate"})
        self.assertTrue(all(a["labels"]["owner"] and a["annotations"]["runbook"] for a in alerts))
        dashboard_config = load("metrics/dashboard.yaml")[0]
        dashboard = json.loads(dashboard_config["data"]["workload-api.json"])
        self.assertEqual(dashboard["uid"], "workload-api-red")
        self.assertEqual(len(dashboard["panels"]), 3)

    def test_logs_have_only_namespaced_read_permissions_and_allowlisted_events(self):
        account, role, binding, configuration, deployment = load("logs/alloy.yaml")
        self.assertEqual(role["kind"], "Role")
        self.assertEqual(role["metadata"]["namespace"], "workloads")
        self.assertEqual(role["rules"], [
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        ])
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], account["metadata"]["name"])
        self.assertEqual(binding["subjects"][0]["name"], pod["serviceAccountName"])
        self.assertNotIn("hostNetwork", pod)
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertTrue(all("hostPath" not in volume for volume in pod["volumes"]))
        alloy = configuration["data"]["config.alloy"]
        self.assertIn('names = ["workloads"]', alloy)
        self.assertIn('action   = "drop"', alloy)
        self.assertIn('source = "safe_event"', alloy)
        pattern = json.loads(re.search(r'expression = ("[^"]+")', alloy).group(1))
        for ending in ("", "\n", "\r\n"):
            event = re.search(pattern, "prefixWithMetadata workload_api_request route=index status_class=2xx" + ending)
            self.assertEqual(event.group("safe_event"), "workload_api_request route=index status_class=2xx")
        for line in (
            'request /private?token=example',
            'workload_api_request route=/personal-id status_class=2xx',
            'workload_api_request route=index status_class=2xx authorization=example',
        ):
            self.assertIsNone(re.search(pattern, line))

    def test_loki_storage_and_access_are_bounded(self):
        loki = load("logs/loki.values.yaml")[0]
        self.assertEqual(loki["deploymentMode"], "Monolithic")
        self.assertEqual(loki["singleBinary"]["replicas"], 1)
        self.assertEqual(loki["loki"]["storage"]["type"], "filesystem")
        self.assertEqual(loki["loki"]["limits_config"]["retention_period"], "48h")
        self.assertEqual(loki["loki"]["compactor"]["retention_enabled"], True)
        self.assertFalse(loki["ingress"]["enabled"])
        self.assertFalse(loki["gateway"]["enabled"])
        self.assertFalse(loki["singleBinary"]["persistence"]["enableStatefulSetAutoDeletePVC"])
        policy = load("logs/loki-policy.yaml")[0]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        peers = policy["ingress"][0]["from"]
        self.assertEqual(len(peers), 2)
        self.assertTrue(all(set(peer) == {"podSelector"} for peer in peers))
        self.assertEqual(policy["ingress"][0]["ports"], [{"protocol": "TCP", "port": 3100}])
