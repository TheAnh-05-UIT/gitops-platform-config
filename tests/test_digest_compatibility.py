from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts.check_promotion import WORKLOAD_APPLICATION, render_promotion, verify_workload_surface

from scripts.promotion_policy import (
    CONTROL_PATH, IMAGE_REPOSITORY, REVERTED_SHA, VALUES_PATH, ImageIdentity,
    PolicyError, guard, image_identity, patch_image, read_state, validate_rendered,
)

ROOT = Path(__file__).resolve().parents[1]
CURRENT = "a" * 40
CANDIDATE = "b" * 40
OLD_DIGEST = "sha256:" + "c" * 64
DIGEST = "sha256:" + "d" * 64
VALUES = f'image:\n  repository: "{IMAGE_REPOSITORY}"\n  tag: "{CURRENT}" # provenance\nreplicaCount: 2\n'
BLOCKS = [{"sha": REVERTED_SHA, "digest": None, "reason": "Historical revert"}]


def approved(candidate=CANDIDATE):
    return read_state({
        "generation": 1, "frozen": False, "blocked_images": BLOCKS,
        "request": {
            "id": "workload-api-g1", "mode": "promote", "sha": candidate, "digest": DIGEST, "run_id": 1,
            "run_attempt": 1, "expected_tag": CURRENT, "decision": "synthetic", "replaces_blocks": [],
        },
    })


class DigestCompatibilityTests(unittest.TestCase):
    def test_patch_pins_digest_and_preserves_placement_comments_and_crlf(self):
        base = VALUES.replace("\n", "\r\n")
        patched = patch_image(base, ImageIdentity(CANDIDATE, DIGEST))
        self.assertIn(f'  tag: "{CANDIDATE}" # provenance\r\n  digest: "{DIGEST}"\r\nreplicaCount: 2\r\n', patched)
        self.assertEqual(image_identity(patched).reference, f"{IMAGE_REPOSITORY}@{DIGEST}")

    def test_guard_uses_paired_source_and_digest_and_rejects_downgrade(self):
        state = approved()
        pinned = patch_image(VALUES, ImageIdentity(CURRENT, OLD_DIGEST))
        promoted = patch_image(pinned, ImageIdentity(CANDIDATE, DIGEST))
        self.assertEqual(guard(state, state, pinned, promoted, {VALUES_PATH},
                               head_ref=state.request.branch), "promotion")
        wrong = patch_image(VALUES, ImageIdentity(CANDIDATE, OLD_DIGEST))
        with self.assertRaises(PolicyError):
            guard(state, state, pinned, wrong, {VALUES_PATH}, head_ref=state.request.branch)
        tag_only = VALUES.replace(CURRENT, CANDIDATE)
        with self.assertRaises(PolicyError):
            guard(state, state, pinned, tag_only, {VALUES_PATH}, head_ref=state.request.branch)

    def test_first_pin_keeps_current_source_and_existing_pin_cannot_be_rebound(self):
        changing = approved()
        pinning = approved(CURRENT)
        pinned = patch_image(VALUES, ImageIdentity(CURRENT, DIGEST))
        self.assertEqual(guard(pinning, pinning, VALUES, pinned, {VALUES_PATH},
                               head_ref=pinning.request.branch), "promotion")
        with self.assertRaisesRegex(PolicyError, "First digest pin"):
            guard(changing, changing, VALUES, patch_image(VALUES, ImageIdentity(CANDIDATE, DIGEST)),
                  {VALUES_PATH}, head_ref=changing.request.branch)
        other = patch_image(VALUES, ImageIdentity(CURRENT, OLD_DIGEST))
        with self.assertRaisesRegex(PolicyError, "cannot be rebound"):
            guard(pinning, pinning, other, pinned, {VALUES_PATH}, head_ref=pinning.request.branch)

    def test_rollback_matches_trusted_pair_and_blocks_digest_aliases(self):
        state = approved()
        withdrawn = patch_image(VALUES, ImageIdentity(CANDIDATE, DIGEST))
        prior = ImageIdentity(CURRENT, OLD_DIGEST)
        target = patch_image(withdrawn, prior)
        paths = {CONTROL_PATH, VALUES_PATH}
        rollover = {"generation": 2, "frozen": True, "request": None,
                    "blocked_images": BLOCKS + [{"sha": CANDIDATE, "digest": DIGEST, "reason": "Synthetic rollback"}]}
        paused = read_state(rollover)
        self.assertEqual(guard(state, paused, withdrawn, target, paths, {prior}), "rollback")
        with self.assertRaises(PolicyError):
            guard(state, paused, withdrawn, target, paths, {CURRENT})
        alias = ImageIdentity(CURRENT, DIGEST)
        tempting = patch_image(withdrawn, alias)
        with self.assertRaises(PolicyError):
            guard(state, paused, withdrawn, tempting, paths, {alias})

    @unittest.skipUnless(shutil.which("helm"), "Helm required")
    def test_digest_render_and_invalid_value(self):
        with tempfile.TemporaryDirectory() as temp:
            values = Path(temp) / "values.yaml"
            values.write_text(patch_image(VALUES, ImageIdentity(CANDIDATE, DIGEST)), encoding="utf-8")
            command = ["helm", "template", "workload-api", "./apps/workload-api", "-f", str(values)]
            result = subprocess.check_output(command, cwd=ROOT)
            validate_rendered([doc for doc in yaml.safe_load_all(result) if doc], ImageIdentity(CANDIDATE, DIGEST))
            values.write_text(VALUES.replace("replicaCount", '  digest: "sha256:wrong"\nreplicaCount'), encoding="utf-8")
            self.assertNotEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 0)

    @unittest.skipUnless(shutil.which("helm"), "Helm required")
    def test_surface_render_rejects_a_literal_changed_image(self):
        rendered_source = ROOT / "apps/workload-api"
        items = [(path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))
                 for path in rendered_source.rglob("*") if path.is_file()]
        content = {f"{index:040x}": text for index, (_, text) in enumerate(items, 1)}
        sha_for_path = {path: f"{index:040x}" for index, (path, _) in enumerate(items, 1)}

        def read_object(repository, *args):
            if args[0] == "show":
                assert args[-1].endswith(WORKLOAD_APPLICATION)
                return (ROOT / WORKLOAD_APPLICATION).read_text(encoding="utf-8")
            if args[0] == "ls-tree":
                return "\0".join(f"100644 blob {sha}\t{path}" for path, sha in sha_for_path.items())
            body = content[args[-1]]
            return str(len(body.encode())) if args[1] == "-s" else body

        with patch("scripts.check_promotion.git", side_effect=read_object):
            verify_workload_surface(ROOT, CURRENT)
            values_id = sha_for_path[VALUES_PATH]
            default = content[values_id]
            for option in ("networkPolicy", "autoscaling", "namespaceBudget"):
                with self.subTest(option=option):
                    data = yaml.safe_load(default)
                    data[option]["enabled"] = True
                    content[values_id] = yaml.safe_dump(data)
                    verify_workload_surface(ROOT, CURRENT)
                    mock_with_tag = data["image"]["tag"]
                    self.assertEqual(render_promotion(ROOT, CURRENT, mock_with_tag),
                                     f"{IMAGE_REPOSITORY}:{mock_with_tag}")
            content[values_id] = default
            sha = sha_for_path["apps/workload-api/templates/deployment.yaml"]
            original = content[sha]
            content[sha] = original.replace("{{ .Values.image.repository }}:{{ .Values.image.tag }}", "fixture/wrong:v1")
            with self.assertRaisesRegex(PolicyError, "does not match guarded values"):
                verify_workload_surface(ROOT, CURRENT)
            content[sha] = original
            extra = "f" * 40
            sha_for_path["apps/workload-api/templates/extra.yaml"] = extra
            content[extra] = original.replace("- name: workload-api", "- name: alternate")
            with self.assertRaisesRegex(PolicyError, "renders an unexpected object"):
                verify_workload_surface(ROOT, CURRENT)
