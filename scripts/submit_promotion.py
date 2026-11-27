"""Propose one reviewed image-only change using the Config promotion App."""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .check_promotion import publication_evidence
from .promote_image import ApiFailure, GitHubReader, NoRedirect, check_rejection_history, selected_request
from .promotion_policy import (
    CONFIG_REPOSITORY, CONTROL_PATH, SOURCE_REPOSITORY, VALUES_PATH, ImageIdentity,
    PolicyError, guard, image_identity, is_sha, load_yaml, patch_image, read_state, require,
)

BRANCH = re.compile(r"promotion/workload-api/g[1-9][0-9]*-[a-f0-9]{40}")
BOT_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\[bot\]")


class GitHubPromotionWriter:
    """Write only to the Config repository's promotion branch and PR."""

    def __init__(self, token, opener=None):
        require(bool(token), "Missing Config App installation token")
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open

    def _post(self, url, payload):
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            with self.opener(request, timeout=12) as response:
                raw = response.read(1024 * 1024 + 1)
            require(len(raw) <= 1024 * 1024, "Unexpected promotion API response size")
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            error.close()
            raise ApiFailure(f"Promotion write unconfirmed (HTTP {error.code}); recheck the same request") from None
        except (OSError, ValueError) as error:
            if isinstance(error, PolicyError):
                raise
            raise ApiFailure("Promotion write unconfirmed; recheck the same request") from None

    def create_branch(self, branch, base):
        require(BRANCH.fullmatch(branch) and is_sha(base), "Invalid promotion branch or base")
        self._post(GitHubReader._url(CONFIG_REPOSITORY, "git/refs"),
                   {"ref": f"refs/heads/{branch}", "sha": base})

    def commit_values(self, branch, base, values, title):
        require(BRANCH.fullmatch(branch) and is_sha(base), "Invalid promotion branch or base")
        result = self._post("https://api.github.com/graphql", {
            "query": "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }",
            "variables": {"input": {
                "branch": {"repositoryNameWithOwner": CONFIG_REPOSITORY, "branchName": branch},
                "expectedHeadOid": base, "message": {"headline": title},
                "fileChanges": {"additions": [{"path": VALUES_PATH, "contents": base64.b64encode(values.encode()).decode()}]},
            }},
        })
        require(not result.get("errors"), "Promotion commit unconfirmed; check branch head before retrying")
        return result["data"]["createCommitOnBranch"]["commit"]["oid"]

    def open_pull(self, branch, title, body):
        require(BRANCH.fullmatch(branch), "Invalid promotion branch")
        return self._post(GitHubReader._url(CONFIG_REPOSITORY, "pulls"),
                          {"base": "main", "head": branch, "title": title, "body": body, "draft": False})


def _branch_head(config, branch):
    ref = config.get(CONFIG_REPOSITORY, f"git/ref/heads/{branch}", missing=True)
    if ref is None:
        return None
    head = ref["object"]["sha"]
    require(is_sha(head), "Unexpected promotion branch SHA")
    return head


def _verify_commit(config, head, base, bot_login, desired):
    require(is_sha(head) and head != base, "Expected one image-only promotion commit")
    commit = config.get(CONFIG_REPOSITORY, f"commits/{head}")
    author = commit.get("author") or {}
    require(commit["sha"] == head and author.get("login") == bot_login and author.get("type") == "Bot"
            and [parent["sha"] for parent in commit["parents"]] == [base]
            and [(item["filename"], item["status"]) for item in commit["files"]] == [(VALUES_PATH, "modified")]
            and config.file(CONFIG_REPOSITORY, VALUES_PATH, head) == desired,
            "Promotion branch does not match the reviewed image-only commit")


def _verify_pull(pull, branch, head, base, bot_login, marker):
    author = pull.get("user") or {}
    require(type(pull["number"]) is int and pull["number"] > 0
            and pull["state"] == "open" and pull.get("draft") is False
            and author.get("login") == bot_login and author.get("type") == "Bot"
            and marker in (pull.get("body") or "").splitlines()
            and pull["head"]["ref"] == branch and pull["head"]["sha"] == head
            and pull["base"]["ref"] == "main" and pull["base"]["sha"] == base
            and all(pull[side]["repo"]["full_name"].lower() == CONFIG_REPOSITORY for side in ("head", "base")),
            "Open promotion PR does not match the reviewed request")
    return pull["number"]


def submit_promotion(config, source, writer, request_id, bot_login, *, pin_digest=False, expected_base=None):
    require(BOT_LOGIN.fullmatch(bot_login), "Expected the reviewed Config App bot login")
    base = config.head(CONFIG_REPOSITORY)
    require(expected_base is None or base == expected_base, "Config main moved since owner review")
    protected = config.get(CONFIG_REPOSITORY, "branches/main")
    require(protected.get("protected") is True and protected["commit"]["sha"] == base,
            "Config main is not the protected reviewed ref")
    state = read_state(load_yaml(config.file(CONFIG_REPOSITORY, CONTROL_PATH, base)))
    request = selected_request(state, request_id)
    branch = request.branch
    original = config.file(CONFIG_REPOSITORY, VALUES_PATH, base)
    current = image_identity(original)
    desired = patch_image(original, ImageIdentity(request.sha, request.digest if pin_digest or current.digest else None))
    now = datetime.now(timezone.utc)
    tip = publication_evidence(source, request, now)
    if original == desired:
        return None
    require(guard(state, state, original, desired, {VALUES_PATH}, head_ref=branch) == "promotion",
            "Expected an image-only promotion")
    check_rejection_history(config, request)

    def same_protected_refs():
        require(config.head(CONFIG_REPOSITORY) == base and source.head(SOURCE_REPOSITORY) == tip,
                "A protected ref moved; review a fresh request")

    marker = f"<!-- promotion-request:{request.id}:{base} -->"
    title = f"Promote workload-api {request.sha[:12]}"
    query = urllib.parse.urlencode({"state": "all", "base": "main",
                                    "head": f"{CONFIG_REPOSITORY.split('/')[0]}:{branch}"})
    pulls = config.get(CONFIG_REPOSITORY, f"pulls?{query}")
    require(isinstance(pulls, list) and len(pulls) <= 1, "Ambiguous promotion PR history")
    same_protected_refs()
    head = _branch_head(config, branch)
    if pulls:
        _verify_commit(config, head, base, bot_login, desired)
        return _verify_pull(pulls[0], branch, head, base, bot_login, marker)
    if head is None:
        writer.create_branch(branch, base)
        head = _branch_head(config, branch)
        require(head == base, "New promotion ref does not match the reviewed base")
    if head == base:
        same_protected_refs()
        head = writer.commit_values(branch, base, desired, title)
    _verify_commit(config, head, base, bot_login, desired)
    require(publication_evidence(source, request, datetime.now(timezone.utc)) == tip,
            "Publication evidence changed during proposal")
    same_protected_refs()
    require(_branch_head(config, branch) == head, "Promotion branch moved during verification")
    body = f"{marker}\n\nSource: {request.sha}\nDigest: {request.digest}\n\nIndependent approval is required."
    pull = writer.open_pull(branch, title, body)
    return _verify_pull(pull, branch, head, base, bot_login, marker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--expected-base", required=True)
    parser.add_argument("--pin-digest", action="store_true")
    parser.add_argument("--confirm-proposal", action="store_true")
    options = parser.parse_args()
    if (not options.confirm_proposal or os.environ.get("PROMOTION_PROPOSAL_ENABLED") != "true"
            or not is_sha(options.expected_base)):
        parser.error("Proposal requires explicit owner approval and the reviewed Config base SHA")
    try:
        number = submit_promotion(
            GitHubReader(os.environ.get("PROMOTION_CONFIG_READ_TOKEN")),
            GitHubReader(os.environ.get("PROMOTION_SOURCE_READ_TOKEN")),
            GitHubPromotionWriter(os.environ.get("PROMOTION_CONFIG_APP_TOKEN")),
            options.request, os.environ.get("PROMOTION_BOT_LOGIN", ""),
            expected_base=options.expected_base, pin_digest=options.pin_digest,
        )
    except PolicyError as error:
        parser.exit(1, f"Promotion proposal stopped: {error}\n")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        parser.exit(1, "Promotion proposal unconfirmed; inspect the same request before retrying\n")
    print(f"Promotion PR #{number}" if number else "Reviewed image already selected")


if __name__ == "__main__":
    main()
