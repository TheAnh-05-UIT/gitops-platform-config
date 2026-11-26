import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.check_promotion import approval_marker, fetch_candidate, render_promotion, verify_pull
from scripts.promote_image import registry_digest
from scripts.promotion_policy import CONFIG_REPOSITORY, CONTROL_PATH, VALUES_PATH, PolicyError, patch_tag
from test_promotion import BASE, BASE_SHA, BLOCK, DIGEST, NEXT, REVERTED_SHA, SOURCE, evidence, request

HEAD, MERGE = "d" * 40, "e" * 40
BOT, REVIEWER = 11, 12
NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)


class TrustedPromotionTests(unittest.TestCase):
    def setUp(self):
        self.intent = request()
        self.proof = evidence(self.intent)
        self.proof["receipt"].update(
            published_at="2026-11-25T12:02:00+00:00",
            media_type="application/vnd.oci.image.manifest.v1+json",
            archive_sha256="a" * 64, image_id=DIGEST, scanner_version="v0.76.2",
        )
        self.proof["run"].update(status="completed", run_started_at="2026-11-25T12:00:00Z",
                                 updated_at="2026-11-25T12:04:00Z")
        self.pull = {
            "number": 10, "state": "open", "draft": False, "mergeable": True,
            "merge_commit_sha": MERGE, "user": {"id": BOT, "type": "Bot"},
            "base": {"sha": BASE_SHA, "ref": "main", "repo": {"full_name": CONFIG_REPOSITORY}},
            "head": {"sha": HEAD, "ref": f"promotion/workload-api/g1-{NEXT}",
                     "repo": {"full_name": CONFIG_REPOSITORY}},
        }
        self.proposed = patch_tag(BASE, NEXT)
        self.base_control = {"generation": 1, "frozen": False, "request": self.intent,
                             "blocked_images": BLOCK}
        self.head_control = self.base_control
        self.parents = f"{MERGE} {BASE_SHA} {HEAD}"
        self.paths = VALUES_PATH
        self.jobs = [{"name": name, "conclusion": "success",
                      "steps": [{"name": step, "conclusion": "success"} for step in names]}
                     for name, names in (
                         ("Build and smoke", ["Build image", "Test HTTP endpoints", "Upload tested image"]),
                         ("Scan image", ["Verify image archive", "Scan image with Trivy"]),
                         ("Publish image", ["Verify image archive", "Load the scanned image",
                                            "Publish image to GHCR", "Upload verified publication receipt"]),
                     )]
        self.review = {
            "state": "APPROVED", "commit_id": HEAD, "user": {"id": REVIEWER, "type": "User"},
            "body": approval_marker(10, BASE_SHA, HEAD, MERGE, self.intent, f"ghcr.io/{SOURCE}:{NEXT}"),
        }

    def verify(self, reviews=None):
        config, source = Mock(), Mock()
        def config_response(repo, path):
            self.assertEqual(repo, CONFIG_REPOSITORY)
            if path == "pulls/10":
                return self.pull
            if path == "branches/main":
                return {"protected": True, "commit": {"sha": BASE_SHA}}
            if path == "pulls/10/reviews?per_page=100&page=1":
                return [self.review] if reviews is None else reviews
            if path.startswith("pulls?state=closed"):
                return []
            self.fail(f"Unexpected config API endpoint {path}")

        config.get.side_effect = config_response
        source.head.return_value = self.proof["source_tip"]
        source.receipt.return_value = self.proof["receipt"]
        source.get.side_effect = lambda repo, path: {
            "branches/main": {"protected": True, "commit": {"sha": self.proof["source_tip"]}},
            "actions/runs/42": self.proof["run"],
            "actions/runs/42/attempts/1": self.proof["run"],
            "actions/runs/42/attempts/1/jobs?per_page=100": {
                "total_count": len(self.jobs), "jobs": self.jobs,
            },
        }[path]
        objects = {
            ("rev-list", "--parents", "-n", "1", MERGE): self.parents,
            ("diff", "--no-renames", "--name-only", "-z", BASE_SHA, MERGE): self.paths,
            ("show", f"{BASE_SHA}:{VALUES_PATH}"): BASE,
            ("show", f"{MERGE}:{VALUES_PATH}"): self.proposed,
            ("show", f"{BASE_SHA}:{CONTROL_PATH}"): json.dumps(self.base_control),
            ("show", f"{MERGE}:{CONTROL_PATH}"): json.dumps(self.head_control),
        }
        with (
            patch("scripts.check_promotion.git", side_effect=lambda _, *args: objects[args]),
            patch("scripts.check_promotion.render_promotion", return_value=f"ghcr.io/{SOURCE}:{NEXT}"),
            patch("scripts.check_promotion.registry_digest", return_value=self.proof["observed_digest"]),
        ):
            return verify_pull(Path("."), config, source, 10, BASE_SHA, {REVIEWER}, BOT, now=NOW)

    def test_verified_candidate(self):
        self.assertEqual(self.verify(), ("promotion", MERGE))

    def test_approval_for_other_head_base_or_merge_is_rejected(self):
        other = "f" * 40
        for base, head, merge in ((other, HEAD, MERGE), (BASE_SHA, other, MERGE), (BASE_SHA, HEAD, other)):
            with self.subTest(base=base, head=head, merge=merge), self.assertRaises(PolicyError):
                self.verify([self.review | {"body": approval_marker(
                    10, base, head, merge, self.intent, f"ghcr.io/{SOURCE}:{NEXT}",
                )}])

    def test_unresolved_or_modified_candidate_is_rejected(self):
        self.pull["mergeable"] = None
        with self.assertRaises(PolicyError):
            self.verify()
        self.pull["mergeable"] = True
        self.parents = f"{MERGE} {BASE_SHA} {'f' * 40}"
        with self.assertRaises(PolicyError):
            self.verify()

    def test_fetched_ref_must_match_the_github_merge_object(self):
        with (
            patch("scripts.check_promotion.subprocess.run") as run,
            patch("scripts.check_promotion.git", return_value="f" * 40),
            self.assertRaisesRegex(PolicyError, "Fetched merge object"),
        ):
            fetch_candidate(Path("."), 10, MERGE, "fixture")
        self.assertEqual(run.call_args.args[0][-1], "refs/pull/10/merge")
        self.assertNotIn("fixture", " ".join(run.call_args.args[0]))

    def test_stale_base_or_wrong_review_head_is_rejected(self):
        review = self.review | {"commit_id": "f" * 40}
        with self.assertRaises(PolicyError):
            self.verify([review])
        self.pull["base"]["sha"] = "f" * 40
        with self.assertRaises(PolicyError):
            self.verify()

    def test_receipt_cannot_be_replayed_or_used_for_another_digest(self):
        self.proof["receipt"]["published_at"] = "2026-10-01T00:00:00Z"
        with self.assertRaises(PolicyError):
            self.verify()
        self.proof["receipt"]["published_at"] = "2026-11-25T12:02:00Z"
        self.proof["observed_digest"] = "sha256:" + "f" * 64
        with self.assertRaises(PolicyError):
            self.verify()

    def test_missing_failed_or_skipped_job_is_rejected(self):
        jobs = self.jobs
        for invalid in (jobs[:-1], jobs[:-1] + [jobs[-1] | {"conclusion": "failure"}],
                        [jobs[0], jobs[1] | {"conclusion": "skipped"}, jobs[2]]):
            self.jobs = invalid
            with self.subTest(jobs=invalid), self.assertRaises(PolicyError):
                self.verify()

    def test_successful_job_cannot_hide_skipped_trivy_step(self):
        self.jobs[1]["steps"][-1]["conclusion"] = "skipped"
        with self.assertRaisesRegex(PolicyError, "step"):
            self.verify()

    def test_receipt_from_superseded_workflow_attempt_is_rejected(self):
        self.proof["run"]["run_attempt"] = 2
        with self.assertRaisesRegex(PolicyError, "stale or incomplete"):
            self.verify()

    def test_historical_v2_and_same_pr_approval_are_rejected(self):
        self.base_control = self.base_control | {"request": request(REVERTED_SHA)}
        with self.assertRaises(PolicyError):
            self.verify()
        self.base_control = self.base_control | {"request": None, "frozen": True, "generation": 0}
        self.paths = f"{CONTROL_PATH}\0{VALUES_PATH}"
        with self.assertRaises(PolicyError):
            self.verify()

    def test_pr_author_or_dismissed_review_does_not_authorize(self):
        with self.assertRaises(PolicyError):
            self.verify([self.review | {"user": {"id": BOT, "type": "Bot"}}])
        with self.assertRaises(PolicyError):
            self.verify([self.review, self.review | {"state": "DISMISSED"}])

    def test_promotion_guard_workflow_checks_out_only_trusted_base(self):
        import yaml

        workflow = yaml.load(Path(".github/workflows/trusted-promotion.yml").read_bytes(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"pull_request_target"})
        trusted = workflow["jobs"]["trusted-promotion"]
        checkout = next(step for step in trusted["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(trusted["permissions"], {"contents": "read", "pull-requests": "read"})
        self.assertEqual(trusted["environment"], "promotion-verification")

    def test_registry_verifies_raw_manifest_and_scanned_config_id(self):
        media_type = self.proof["receipt"]["media_type"]
        body = json.dumps({"mediaType": media_type, "config": {"digest": DIGEST}}).encode()
        identity = Mock(sha=NEXT, digest="sha256:" + hashlib.sha256(body).hexdigest())
        result = Mock(stdout=body)
        with patch("scripts.promote_image.subprocess.run", return_value=result) as runner:
            self.assertEqual(registry_digest(identity, media_type=media_type, image_id=DIGEST), identity.digest)
            self.assertEqual(runner.call_count, 2)
            with self.assertRaisesRegex(PolicyError, "scanned image ID"):
                registry_digest(identity, media_type=media_type, image_id="sha256:" + "f" * 64)
            result.stdout = body + b" "
            with self.assertRaisesRegex(PolicyError, "manifest bytes"):
                registry_digest(identity, media_type=media_type, image_id=DIGEST)

    def test_render_binds_git_values_and_rejects_extra_workloads(self):
        def blob(_, *args):
            return self.proposed if args[0] == "show" else f"100644 blob {HEAD}\t{VALUES_PATH}"

        with patch("scripts.check_promotion.git", side_effect=blob):
            self.assertEqual(render_promotion(Path("."), MERGE, NEXT), f"ghcr.io/{SOURCE}:{NEXT}")
            with self.assertRaisesRegex(PolicyError, "different image"):
                render_promotion(Path("."), MERGE, "f" * 40)
            manifest = ("kind: Deployment\nmetadata: {name: workload-api}\n---\n"
                        "kind: Service\nmetadata: {name: workload-api}\n---\n"
                        "kind: Job\nmetadata: {name: other}\n")
            with patch("scripts.check_promotion.subprocess.check_output", return_value=manifest):
                with self.assertRaisesRegex(PolicyError, "unexpected resource"):
                    render_promotion(Path("."), MERGE, NEXT)
