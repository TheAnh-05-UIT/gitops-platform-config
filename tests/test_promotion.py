import io
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.promotion_policy import (
    CONFIG_REPOSITORY, CONTROL_PATH, IMAGE_REPOSITORY, REVERTED_SHA, VALUES_PATH, PolicyError,
    guard, patch_tag, read_state, validate_rendered,
)
from scripts.check_promotion import check_merge
from scripts.promote_image import (
    ApiFailure, GitHubReader, check_rejection_history, inspect_remote, plan, verify_evidence,
)


BASE = (Path(__file__).parent / "fixtures" / "workload-values-unpinned.yaml").read_bytes().decode()
V1 = "97172600d3b87dded805e942d6e247190a0827e1"
NEXT = "a" * 40
DIGEST = "sha256:" + "b" * 64
BASE_SHA = "c" * 40
SOURCE = "theanh-05-uit/gitops-platform-workload-api"
BLOCK = [{"sha": REVERTED_SHA, "digest": None, "reason": "Config PR #3"}]


def request(sha=NEXT, generation=1, mode="promote", **overrides):
    return {
        "id": f"workload-api-g{generation}", "mode": mode, "sha": sha,
        "digest": DIGEST, "run_id": 42, "run_attempt": 1,
        "expected_tag": V1, "decision": "Reviewed lab candidate",
        "replaces_blocks": [REVERTED_SHA] if mode == "repromote" else [],
    } | overrides


def control(candidate=None, generation=0, frozen=False, blocks=BLOCK):
    return read_state({
        "generation": generation, "frozen": frozen, "request": candidate,
        "blocked_images": blocks,
    })


def evidence(candidate):
    return {
        "source_tip": candidate["sha"],
        "ancestor": False,
        "observed_digest": DIGEST,
        "receipt": {
            "version": 1, "repository": SOURCE, "workflow": ".github/workflows/ci.yml",
            "sha": candidate["sha"], "run_id": 42, "run_attempt": 1,
            "image": f"{IMAGE_REPOSITORY}:{candidate['sha']}", "digest": DIGEST,
        },
        "run": {
            "id": 42, "run_attempt": 1, "path": ".github/workflows/ci.yml",
            "head_sha": candidate["sha"], "head_branch": "main",
            "event": "push", "conclusion": "success",
            "head_repository": {"full_name": SOURCE},
        },
    }


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.candidate = request()
        self.state = control(self.candidate, 1)
        self.proof = evidence(self.candidate)

    def preview(self, values=BASE, **kwargs):
        return plan(self.state, self.candidate["id"], values, self.proof, **kwargs)

    def test_success_changes_only_the_tag_and_preserves_crlf(self):
        values = BASE.replace("\r\n", "\n").replace("\n", "\r\n")
        preview = self.preview(values)
        self.assertEqual(preview.action, "propose")
        self.assertEqual(preview.branch, f"promotion/workload-api/g1-{NEXT}")
        self.assertEqual(preview.values, values.replace(V1, NEXT))
        self.assertEqual(guard(self.state, self.state, values, preview.values, {VALUES_PATH},
                               head_ref=preview.branch), "promotion")

    def test_already_selected(self):
        self.assertEqual(self.preview(patch_tag(BASE, NEXT)).action, "no-op")

    def test_missing_invalid_and_mismatched_evidence(self):
        for change in (
            {"receipt": None}, {"receipt": {}},
            {"receipt": self.proof["receipt"] | {"sha": "d" * 40}},
            {"observed_digest": "sha256:" + "e" * 64},
            {"run": self.proof["run"] | {"conclusion": "failure"}},
            {"run": self.proof["run"] | {"run_attempt": 2}},
            {"source_tip": "e" * 40},
        ):
            with self.subTest(change=change), self.assertRaises(PolicyError):
                verify_evidence(self.state.request, self.proof | change)

    def test_duplicate_and_partial_recovery(self):
        for remote_state, expected in (
            ("open", "reuse-pr"), ("branch", "recover-pr"), ("stale-open", "refresh-branch"),
        ):
            with self.subTest(remote_state=remote_state):
                self.assertEqual(self.preview(remote_state=remote_state).action, expected)
        for remote_state in ("closed", "foreign"):
            with self.subTest(remote_state=remote_state), self.assertRaises(PolicyError):
                self.preview(remote_state=remote_state)

    def test_stale_generation_and_unexpected_config(self):
        newer = control(request(generation=2), 2)
        with self.assertRaises(PolicyError):
            plan(newer, self.candidate["id"], BASE, self.proof)
        with self.assertRaises(PolicyError):
            self.preview(patch_tag(BASE, "d" * 40))

    def test_reverted_sha_requires_explicit_repromotion(self):
        rejected = request(REVERTED_SHA)
        with self.assertRaises(PolicyError):
            control(rejected, 1)
        admitted = request(REVERTED_SHA, mode="repromote")
        state = control(admitted, 1)
        proof = evidence(admitted) | {"source_tip": NEXT, "ancestor": True}
        self.assertEqual(plan(state, admitted["id"], BASE, proof).action, "propose")
        with self.assertRaises(PolicyError):
            control(admitted | {"replaces_blocks": []}, 1)

    def test_approval_cannot_be_created_in_its_own_image_pr(self):
        with self.assertRaises(PolicyError):
            guard(control(), self.state, BASE, patch_tag(BASE, NEXT), {VALUES_PATH, CONTROL_PATH})
        self.assertEqual(guard(control(), self.state, BASE, BASE, {CONTROL_PATH}), "admission")
        with self.assertRaises(PolicyError):
            guard(self.state, self.state, BASE, patch_tag(BASE, NEXT), {VALUES_PATH, "chart.yaml"})

    def test_atomic_rollback_invalidates_old_approvals(self):
        outgoing = patch_tag(BASE, NEXT)
        blocks = BLOCK + [{"sha": NEXT, "digest": DIGEST, "reason": "Lab rollback"}]
        rollback = control(generation=2, frozen=True, blocks=blocks)
        paths = {VALUES_PATH, CONTROL_PATH}
        self.assertEqual(guard(self.state, rollback, outgoing, BASE, paths, {V1}), "rollback")
        with self.assertRaises(PolicyError):
            guard(self.state, rollback, outgoing, BASE, paths, set())
        with self.assertRaises(PolicyError):
            guard(rollback, rollback, BASE, outgoing, {VALUES_PATH})
        with self.assertRaises(PolicyError):
            guard(rollback, self.state, BASE, BASE, {CONTROL_PATH})

    def test_ambiguous_or_non_preserving_edit_fails(self):
        for candidate in (BASE + "\nimage: {}\n", BASE.replace('  tag:', '  extra: 1\n  tag:')):
            with self.subTest(candidate=candidate), self.assertRaises(PolicyError):
                self.preview(candidate)
        edited = patch_tag(BASE, NEXT).replace("replicaCount: 2", "replicaCount: 3")
        with self.assertRaises(PolicyError):
            guard(self.state, self.state, BASE, edited, {VALUES_PATH},
                  head_ref=self.state.request.branch)

    def test_missing_authentication_and_redacted_timeout(self):
        with self.assertRaises(ApiFailure):
            GitHubReader("")
        failing = Mock(side_effect=TimeoutError("sensitive transport data"))
        with self.assertRaisesRegex(ApiFailure, "^GitHub read timed out$"):
            GitHubReader("synthetic", opener=failing).get(SOURCE, "git/ref/heads/main")
        self.assertEqual(failing.call_args.args[0].get_method(), "GET")

    def test_failed_rendered_image_policy(self):
        with self.assertRaisesRegex(PolicyError, "Rendered workload image"):
            validate_rendered([], NEXT)

    def test_stale_branch_cannot_reuse_new_generation_for_same_image(self):
        new = control(request(generation=2), 2)
        with self.assertRaisesRegex(PolicyError, "generation"):
            guard(new, new, BASE, patch_tag(BASE, NEXT), {VALUES_PATH},
                  head_ref=self.state.request.branch)

    def test_repromotion_can_be_rolled_back_again(self):
        candidate = request(REVERTED_SHA, mode="repromote")
        admitted = control(candidate, 1)
        updated_blocks = [BLOCK[0] | {"digest": DIGEST}]
        frozen = control(generation=2, frozen=True, blocks=updated_blocks)
        self.assertEqual(guard(admitted, frozen, patch_tag(BASE, REVERTED_SHA), BASE,
                               {VALUES_PATH, CONTROL_PATH}, {V1}), "rollback")

    def test_prior_closed_pr_requires_reviewed_repromotion(self):
        reader = Mock()
        reader.get.side_effect = [
            [{"number": 3, "head": {"ref": self.state.request.branch,
                                    "repo": {"full_name": CONFIG_REPOSITORY}}}],
            {"merged_at": None},
        ]
        with self.assertRaisesRegex(PolicyError, "Prior closed"):
            check_rejection_history(reader, self.state.request)

    def test_merge_checks_exact_parents_and_reads_trusted_control(self):
        head, merge = "d" * 40, "e" * 40
        active = {"generation": 1, "frozen": False, "request": self.candidate,
                  "blocked_images": BLOCK}
        inactive = active | {"generation": 0, "frozen": True, "request": None}
        with patch("scripts.check_promotion.git", side_effect=[
            f"{merge} {BASE_SHA} {head}", f"{VALUES_PATH}\0{CONTROL_PATH}\0",
            BASE, patch_tag(BASE, NEXT), json.dumps(inactive), json.dumps(active),
        ]):
            with self.assertRaisesRegex(PolicyError, "Rollback"):
                check_merge(Path("."), BASE_SHA, merge, head, self.state.request.branch, CONFIG_REPOSITORY)
        with patch("scripts.check_promotion.git", return_value=f"{merge} {BASE_SHA} {head}"):
            with self.assertRaisesRegex(PolicyError, "does not match"):
                check_merge(Path("."), BASE_SHA, merge, NEXT, self.state.request.branch, CONFIG_REPOSITORY)

    def test_dry_run_cli(self):
        state = {"generation": 1, "frozen": False, "request": self.candidate,
                 "blocked_images": BLOCK}
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder) / "snapshot.json"
            fixture.write_text(json.dumps({"control": state, "values": BASE, "evidence": self.proof}))
            output = subprocess.check_output([
                sys.executable, "-B", "-m", "scripts.promote_image", "--mode", "dry-run",
                "--request", "workload-api-g1", "--snapshot", str(fixture),
            ])
        preview = json.loads(output)
        self.assertEqual(preview["action"], "propose")
        self.assertEqual(preview["branch"], self.state.request.branch)
        self.assertEqual(preview["values"], patch_tag(BASE, NEXT))
        print(f"Dry-run fixture: {preview['action']} {preview['branch']}")

    def test_receipt_archive_digest_and_run_scope(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("publication-receipt.json", json.dumps(self.proof["receipt"]))
        data = archive.getvalue()
        artifact = {"name": "publication-receipt-1", "expired": False, "id": 1,
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
        reader = GitHubReader("synthetic")
        with patch.object(reader, "get", return_value={"total_count": 1, "artifacts": [artifact]}) as listing:
            with patch.object(reader, "_read", return_value=data):
                self.assertEqual(reader.receipt(self.state.request), self.proof["receipt"])
            listing.assert_called_once_with(SOURCE, "actions/runs/42/artifacts?name=publication-receipt-1")
            with patch.object(reader, "_read", return_value=data + b"tampered"):
                with self.assertRaisesRegex(PolicyError, "ZIP digest mismatch"):
                    reader.receipt(self.state.request)

    def test_remote_recovery_checks_bot_files_and_exact_patch(self):
        head = "d" * 40
        branch = {"object": {"sha": head}}
        commit = {"author": {"login": "fixture[bot]"}, "parents": [{"sha": BASE_SHA}],
                  "files": [{"filename": VALUES_PATH}]}
        state = {"generation": 1, "frozen": False, "request": self.candidate,
                 "blocked_images": BLOCK}
        reader = Mock()
        reader.get.side_effect = [[], branch, commit]
        reader.file.side_effect = [BASE, json.dumps(state), patch_tag(BASE, NEXT)]
        self.assertEqual(inspect_remote(reader, BASE_SHA, self.state.request, "fixture[bot]"), "branch")
        reader.get.side_effect = [[], branch, commit | {"author": {"login": "other"}}]
        self.assertEqual(inspect_remote(reader, BASE_SHA, self.state.request, "fixture[bot]"), "foreign")

    def test_http_failure_does_not_log_response_body(self):
        error = urllib.error.HTTPError("https://example.invalid", 401, "redact this", {}, io.BytesIO(b""))
        with self.assertRaisesRegex(ApiFailure, r"^GitHub read failed \(HTTP 401\)$"):
            GitHubReader("synthetic", opener=Mock(side_effect=error)).get(SOURCE, "git/ref/heads/main")
