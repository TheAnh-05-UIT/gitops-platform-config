import base64
import json
import unittest
from unittest.mock import Mock, patch

from scripts.promotion_policy import CONFIG_REPOSITORY, CONTROL_PATH, VALUES_PATH
from scripts.submit_promotion import GitHubPromotionWriter, main, submit_promotion
from test_promotion import BASE, BASE_SHA, BLOCK, NEXT, request

HEAD = "d" * 40
BOT = "fixture-app[bot]"


class PromotionWriterTests(unittest.TestCase):
    def setUp(self):
        self.config, self.source, self.writer = Mock(), Mock(), Mock()
        self.request = request()
        self.control = {"generation": 1, "frozen": False, "request": self.request, "blocked_images": BLOCK}
        self.config.file.side_effect = self.read_file
        self.source.head.return_value = NEXT
        self.branch = f"promotion/workload-api/g1-{NEXT}"
        self.expected = BASE.replace(self.request["expected_tag"], NEXT)
        self.config.head.return_value = BASE_SHA
        self.config.get.side_effect = self.get
        self.ref = None
        self.pull = None

    def read_file(self, repo, path, ref):
        import json
        if path == CONTROL_PATH:
            return json.dumps(self.control)
        return BASE if ref == BASE_SHA else self.expected

    def get(self, repo, endpoint, **kwargs):
        self.assertEqual(repo, CONFIG_REPOSITORY)
        if endpoint == "branches/main":
            return {"protected": True, "commit": {"sha": BASE_SHA}}
        if endpoint.startswith("pulls?"):
            if "state=closed" in endpoint:
                return []
            return [self.pull] if self.pull else []
        if endpoint.startswith("git/ref/heads/"):
            return {"object": {"sha": self.ref}} if self.ref else None
        if endpoint == f"commits/{HEAD}":
            return {"sha": HEAD, "author": {"login": BOT, "type": "Bot"}, "parents": [{"sha": BASE_SHA}],
                    "files": [{"filename": VALUES_PATH, "status": "modified"}]}
        self.fail(f"Unexpected read {endpoint}")

    def saved_branch(self, branch, base):
        self.assertEqual((branch, base), (self.branch, BASE_SHA))
        self.ref = base

    def write_commit(self, branch, base, values, title):
        self.assertEqual((branch, base, values), (self.branch, BASE_SHA, self.expected))
        self.ref = HEAD
        return HEAD

    def make_pull(self, branch, title, body):
        self.pull = {
            "number": 17, "state": "open", "draft": False, "user": {"login": BOT, "type": "Bot"},
            "head": {"ref": self.branch, "sha": HEAD, "repo": {"full_name": CONFIG_REPOSITORY}},
            "base": {"ref": "main", "sha": BASE_SHA, "repo": {"full_name": CONFIG_REPOSITORY}}, "body": body,
        }
        return self.pull

    def submit(self):
        self.writer.create_branch.side_effect = self.saved_branch
        self.writer.commit_values.side_effect = self.write_commit
        self.writer.open_pull.side_effect = self.make_pull
        with patch("scripts.submit_promotion.publication_evidence", return_value=NEXT) as evidence:
            number = submit_promotion(self.config, self.source, self.writer, self.request["id"], BOT)
        self.assertGreaterEqual(evidence.call_count, 1)
        return number

    def test_creates_image_only_then_reuses_exact_open_request(self):
        self.assertEqual(self.submit(), 17)
        self.assertEqual(self.submit(), 17)
        self.writer.create_branch.assert_called_once()
        self.writer.commit_values.assert_called_once()
        self.writer.open_pull.assert_called_once()

    def test_recovers_only_expected_empty_branch(self):
        self.ref = BASE_SHA
        self.assertEqual(self.submit(), 17)
        self.writer.create_branch.assert_not_called()
        self.writer.commit_values.assert_called_once()

    def test_frozen_control_and_moved_base_prevent_write(self):
        self.control.update(frozen=True, request=None)
        with self.assertRaisesRegex(ValueError, "frozen"):
            self.submit()
        self.control.update(frozen=False, request=self.request)
        self.config.head.side_effect = [BASE_SHA, "b" * 40]
        with self.assertRaisesRegex(ValueError, "moved"):
            self.submit()
        self.writer.create_branch.assert_not_called()
        self.writer.open_pull.assert_not_called()

    def test_foreign_branch_prevents_pr_even_if_it_uses_the_same_name(self):
        self.ref = HEAD
        self.expected = self.expected.replace("replicaCount: 2", "replicaCount: 3")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.submit()
        self.writer.open_pull.assert_not_called()

    def test_writer_transmits_single_file_with_compare_and_swap(self):
        from io import BytesIO
        opener = Mock(return_value=BytesIO(json.dumps(
            {"data": {"createCommitOnBranch": {"commit": {"oid": HEAD}}}}
        ).encode()))
        writer = GitHubPromotionWriter("synthetic-app", opener=opener)
        self.assertEqual(writer.commit_values(self.branch, BASE_SHA, self.expected, "Review image"), HEAD)
        http_request = opener.call_args.args[0]
        data = json.loads(http_request.data)["variables"]["input"]
        self.assertEqual(http_request.full_url, "https://api.github.com/graphql")
        self.assertEqual(data["expectedHeadOid"], BASE_SHA)
        addition, = data["fileChanges"]["additions"]
        self.assertEqual(addition["path"], VALUES_PATH)
        self.assertEqual(base64.b64decode(addition["contents"]).decode(), self.expected)

    def test_cli_closed_gate_fails_before_constructing_a_client(self):
        with patch.dict("os.environ", {"PROMOTION_PROPOSAL_ENABLED": "false"}), \
             patch("sys.argv", ["submit_promotion", "--request", self.request["id"], "--expected-base",
                                BASE_SHA, "--confirm-proposal"]), \
             patch("scripts.submit_promotion.GitHubReader") as reader, self.assertRaises(SystemExit):
            main()
        reader.assert_not_called()
