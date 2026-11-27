"""Validate a proposed merge using policy and approval from the trusted base."""

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .promotion_policy import (
    CONFIG_REPOSITORY, CONTROL_PATH, IMAGE_REPOSITORY, SOURCE_REPOSITORY, VALUES_PATH,
    PolicyError, guard, image_identity, is_sha, load_yaml, read_state, require, validate_rendered,
)
from .promote_image import GitHubReader, check_rejection_history, registry_digest, verify_evidence

PUBLISH_JOBS = {
    "Build and smoke": {"Build image", "Test HTTP endpoints", "Upload tested image"},
    "Scan image": {"Verify image archive", "Scan image with Trivy"},
    "Publish image": {"Verify image archive", "Load the scanned image", "Publish image to GHCR",
                       "Upload verified publication receipt"},
}
IMAGE_MANIFESTS = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
WORKLOAD_CHART = "apps/workload-api"
WORKLOAD_APPLICATION = "argocd/applications/workload-api.yaml"

def git(repository, *args):
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repository, stderr=subprocess.DEVNULL, timeout=30,
        ).decode("utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise PolicyError("Required Git objects are inaccessible") from None


def check_merge(repository, base, merge, head, head_ref, head_repository, rendered=None):
    require(all(map(is_sha, (base, merge, head))), "Expected immutable Git commit SHAs")
    parents = git(repository, "rev-list", "--parents", "-n", "1", merge).split()
    require(parents == [merge, base, head], "Merge candidate does not match the trusted base and PR head")
    paths = set(git(repository, "diff", "--no-renames", "--name-only", "-z", base, merge).split("\0")) - {""}
    old = git(repository, "show", f"{base}:{VALUES_PATH}")
    new = git(repository, "show", f"{merge}:{VALUES_PATH}")
    before = read_state(load_yaml(git(repository, "show", f"{base}:{CONTROL_PATH}")))
    after = read_state(load_yaml(git(repository, "show", f"{merge}:{CONTROL_PATH}")))
    rollback_targets = set()
    if before != after and after.frozen and after.request is None and image_identity(old) != image_identity(new):
        commits = git(repository, "log", "--first-parent", "--format=%H", base, "--", VALUES_PATH)
        for commit in commits.splitlines():
            try:
                rollback_targets.add(image_identity(git(repository, "show", f"{commit}:{VALUES_PATH}")))
            except PolicyError:
                continue
    operation = guard(before, after, old, new, paths, rollback_targets, head_ref=head_ref)
    if operation == "promotion":
        require(head_repository.lower() == CONFIG_REPOSITORY, "Promotion branch must belong to the config repository")
    if rendered is not None:
        manifests = [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]
        validate_rendered(manifests, image_identity(new))
    return operation


def approval_marker(number, base, head, merge, intent, rendered_image):
    request = intent if isinstance(intent, dict) else asdict(intent)
    binding = {
        "config": CONFIG_REPOSITORY,
        "pr": number,
        "base": base,
        "head": head,
        "merge": merge,
        "intent": request,
        "generation": int(request["id"].removeprefix("workload-api-g")),
        "source_repository": SOURCE_REPOSITORY,
        "rendered_image": rendered_image,
    }
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "promotion-approval-sha256:" + hashlib.sha256(encoded).hexdigest()


def surface_review_marker(number, base, head, merge, operation):
    # These candidates can change the effective image without changing image values.
    payload = [CONFIG_REPOSITORY, number, base, head, merge, operation]
    return "config-surface-approval-sha256:" + hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def touches_deployment_surface(repository, base, merge):
    paths = git(repository, "diff", "--no-renames", "--name-only", "-z", base, merge).split("\0")
    return any(path != VALUES_PATH and path.startswith(("apps/", "argocd/", "scripts/", ".github/workflows/", "promotion/"))
               and not path.lower().endswith(".md") for path in paths)


def verify_workload_surface(repository, merge):
    """Read only regular Git blobs and render in an empty Helm environment."""
    application = load_yaml(git(repository, "show", f"{merge}:{WORKLOAD_APPLICATION}"))
    source = application["spec"]["source"]
    require(source == {"repoURL": f"https://github.com/{CONFIG_REPOSITORY}.git", "path": WORKLOAD_CHART,
                       "targetRevision": "main"} and not application["spec"].get("sources")
            and application["spec"]["destination"] == {"namespace": "workloads", "server": "https://kubernetes.default.svc"},
            "Workload Application source or effective Helm values changed")
    entries = [row for row in git(repository, "ls-tree", "-r", "-z", merge, "--", WORKLOAD_CHART).split("\0") if row]
    require(2 <= len(entries) <= 80, "Candidate chart file count is outside the verified range")
    with tempfile.TemporaryDirectory() as isolated:
        root = Path(isolated)
        for entry in entries:
            metadata, path = entry.split("\t", 1)
            mode, kind, object_id = metadata.split()
            parts = path.split("/")
            require(mode == "100644" and kind == "blob" and is_sha(object_id)
                    and parts[:2] == WORKLOAD_CHART.split("/")
                    and all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part) for part in parts),
                    "Candidate chart contains an unsupported Git entry")
            size = int(git(repository, "cat-file", "-s", object_id))
            require(0 <= size <= 131072, "Candidate chart file is too large")
            output = root.joinpath(*parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(git(repository, "cat-file", "blob", object_id), encoding="utf-8", newline="")
        chart = root / WORKLOAD_CHART
        require(not load_yaml((chart / "Chart.yaml").read_text(encoding="utf-8")).get("dependencies"),
                "Candidate chart introduces Helm dependencies")
        values = (root / VALUES_PATH).read_text(encoding="utf-8")
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "TMP", "TEMP") if key in os.environ}
        env.update(HOME=isolated, USERPROFILE=isolated, KUBECONFIG=str(root / "absent-kubeconfig"),
                   HELM_CACHE_HOME=isolated, HELM_CONFIG_HOME=isolated, HELM_DATA_HOME=isolated)
        try:
            result = subprocess.check_output(["helm", "template", "workload-api", str(chart), "-n", "workloads"],
                                             cwd=root, env=env, timeout=30, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            raise PolicyError("Isolated candidate chart render failed") from None
    manifests = [document for document in yaml.safe_load_all(result) if isinstance(document, dict)]
    resource_ids = {(doc.get("kind"), doc.get("metadata", {}).get("name")) for doc in manifests}
    require(len(resource_ids) == len(manifests) and resource_ids
            <= {("Deployment", "workload-api"), ("Service", "workload-api"), ("Ingress", "workload-api"),
                ("HorizontalPodAutoscaler", "workload-api"), ("PodDisruptionBudget", "workload-api"),
                ("NetworkPolicy", "workload-api"), ("ResourceQuota", "workload-api"), ("LimitRange", "workload-api")},
            "Candidate chart renders an unexpected object")
    validate_rendered(manifests, image_identity(values))
    pod = next(doc["spec"]["template"]["spec"] for doc in manifests if doc["kind"] == "Deployment")
    require(len(pod["containers"]) == 1 and not pod.get("initContainers") and not pod.get("ephemeralContainers"),
            "Candidate chart renders an unexpected container")


def current_candidate(config, number, trusted_base):
    require(type(number) is int and number > 0 and is_sha(trusted_base), "Invalid protected base or PR number")
    pr = config.get(CONFIG_REPOSITORY, f"pulls/{number}")
    base, head = pr["base"], pr["head"]
    require(pr["number"] == number and pr["state"] == "open" and pr.get("mergeable") is True,
            "GitHub has not resolved an open, mergeable candidate")
    protected = config.get(CONFIG_REPOSITORY, "branches/main")
    require(base["repo"]["full_name"].lower() == CONFIG_REPOSITORY and base["ref"] == "main"
            and base["sha"] == trusted_base and protected["commit"]["sha"] == trusted_base
            and protected.get("protected") is True,
            "Config base is no longer the checked-out protected main")
    merge = pr["merge_commit_sha"]
    require(is_sha(merge) and is_sha(head["sha"]), "GitHub candidate has no immutable merge and head SHAs")
    return pr, (base["sha"], merge, head["sha"], head["ref"], head["repo"]["full_name"])


def fetch_candidate(repository, number, merge, token):
    # Fetch the Git object only. The candidate tree is never checked out.
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = os.environ | {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }
    try:
        subprocess.run(
            ["git", "fetch", "--no-tags", "--no-recurse-submodules",
             f"https://github.com/{CONFIG_REPOSITORY}.git", f"refs/pull/{number}/merge"],
            cwd=repository, env=env, capture_output=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise PolicyError("GitHub merge object is unavailable") from None
    require(git(repository, "rev-parse", "FETCH_HEAD").strip() == merge,
            "Fetched merge object does not match the GitHub candidate")


def render_promotion(repository, merge, tag):
    # check_merge limits promotion to the values file. Render the protected
    # chart with exactly the candidate values, in a minimal environment.
    values = git(repository, "show", f"{merge}:{VALUES_PATH}")
    image = image_identity(values)
    require(image.tag == tag, "Merge candidate selects a different image")
    mode = git(repository, "ls-tree", merge, VALUES_PATH).split()
    require(mode and mode[0] == "100644", "Promotion values must be a regular tracked file")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.yaml"
        path.write_text(values, encoding="utf-8", newline="")
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "TMP", "TEMP") if key in os.environ}
        env.update(HELM_CACHE_HOME=directory, HELM_CONFIG_HOME=directory, HELM_DATA_HOME=directory)
        try:
            rendered = subprocess.check_output(
                ["helm", "template", "workload-api", "./apps/workload-api", "-f", str(path)],
                cwd=repository, env=env, stderr=subprocess.DEVNULL, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            raise PolicyError("Protected Helm render failed") from None
    manifests = [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]
    require(len(manifests) == 2 and {(doc.get("kind"), doc["metadata"]["name"]) for doc in manifests}
            == {("Deployment", "workload-api"), ("Service", "workload-api")},
            "Protected chart renders an unexpected resource")
    deployment = next(doc for doc in manifests if doc["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    require(len(pod["containers"]) == 1 and not pod.get("initContainers") and not pod.get("ephemeralContainers"),
            "Protected chart renders an unexpected container")
    validate_rendered(manifests, image)
    return image.reference


def review_approved(config, number, head, marker, reviewer_ids, author_id):
    latest = {}
    for page in range(1, 11):
        reviews = config.get(CONFIG_REPOSITORY, f"pulls/{number}/reviews?per_page=100&page={page}")
        for review in reviews:
            user = review.get("user") or {}
            identity = user.get("id")
            if (type(identity) is int and identity in reviewer_ids and identity != author_id
                    and user.get("type") == "User"
                    and review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}):
                latest[identity] = review
        if len(reviews) < 100:
            return (
                all(r["state"] != "CHANGES_REQUESTED" for r in latest.values())
                and any(r["state"] == "APPROVED" and r.get("commit_id") == head
                        and (r.get("body") or "").strip() == marker for r in latest.values())
            )
    raise PolicyError("PR review history exceeds the verified scan limit")


def publication_evidence(source, request, now):
    endpoint = f"actions/runs/{request.run_id}"
    attempt = f"{endpoint}/attempts/{request.run_attempt}"
    run = source.get(SOURCE_REPOSITORY, attempt)
    current = source.get(SOURCE_REPOSITORY, endpoint)
    require(current.get("id") == request.run_id and current.get("run_attempt") == request.run_attempt
            and run.get("status") == "completed", "Publication attempt is stale or incomplete")
    jobs = source.get(SOURCE_REPOSITORY, f"{attempt}/jobs?per_page=100")
    required_jobs = [job for job in jobs["jobs"] if job["name"] in PUBLISH_JOBS]
    require(jobs["total_count"] == len(jobs["jobs"]) and len(required_jobs) == len(PUBLISH_JOBS)
            and {job["name"] for job in required_jobs} == PUBLISH_JOBS.keys()
            and all(job["conclusion"] == "success" for job in required_jobs),
            "Required build, scan or publishing job is missing, failed or skipped")
    for job in required_jobs:
        steps = [step for step in job.get("steps", []) if step["name"] in PUBLISH_JOBS[job["name"]]]
        require(len(steps) == len(PUBLISH_JOBS[job["name"]])
                and {step["name"] for step in steps} == PUBLISH_JOBS[job["name"]]
                and all(step["conclusion"] == "success" for step in steps),
                "Required validation, scan or publication step is missing, failed or skipped")
    receipt = source.receipt(request)
    require(isinstance(receipt, dict), "Missing or invalid publication receipt")
    media_type = receipt.get("media_type")
    require(media_type in IMAGE_MANIFESTS, "Promotion requires a supported single-image manifest")
    require(re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("archive_sha256", "")))
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(receipt.get("image_id", "")))
            and receipt.get("scanner_version") == "v0.76.2",
            "Publication receipt has no verified scan handoff")
    try:
        published = datetime.fromisoformat(receipt["published_at"])
        started = datetime.fromisoformat(run["run_started_at"])
        updated = datetime.fromisoformat(run["updated_at"])
        valid_time = started <= published <= updated and now - timedelta(days=30) <= published <= now
    except (TypeError, ValueError, KeyError):
        valid_time = False
    require(valid_time, "Publication receipt is stale or outside the trusted workflow attempt")
    protected = source.get(SOURCE_REPOSITORY, "branches/main")
    tip = protected["commit"]["sha"]
    require(protected.get("protected") is True, "Workload main is not protected")
    ancestor = False
    if request.mode == "repromote" and request.sha != tip:
        comparison = source.get(SOURCE_REPOSITORY, f"compare/{request.sha}...{tip}")
        ancestor = comparison["merge_base_commit"]["sha"] == request.sha
    digest = registry_digest(request, media_type=media_type, image_id=receipt["image_id"])
    verify_evidence(request, {
        "receipt": receipt, "run": run, "source_tip": tip, "ancestor": ancestor,
        "observed_digest": digest,
    })
    return tip


def verify_pull(repository, config, source, number, trusted_base, reviewer_ids, bot_id, *, now=None, fetch=None):
    pr, candidate = current_candidate(config, number, trusted_base)
    base, merge, head = candidate[:3]
    if fetch is not None:
        fetch(repository, number, merge)
    operation = check_merge(repository, *candidate)
    if operation != "promotion":
        surface_change = touches_deployment_surface(repository, base, merge)
        if operation == "rollback" or surface_change:
            author = (pr.get("user") or {}).get("id")
            marker = surface_review_marker(number, base, head, merge, operation)
            require(type(author) is int and not pr.get("draft") and reviewer_ids
                    and review_approved(config, number, head, marker, reviewer_ids, author),
                    f"Independent approval of exact Config change required. Review body: {marker}")
            verify_workload_surface(repository, merge)
        require(current_candidate(config, number, trusted_base)[1] == candidate,
                "PR candidate changed during verification")
        return operation, merge
    author = pr.get("user") or {}
    require(type(bot_id) is int and bot_id > 0 and author.get("id") == bot_id and author.get("type") == "Bot",
            "Promotion PR does not belong to the configured bot")
    require(bool(reviewer_ids) and not pr.get("draft"), "Promotion requires a non-draft PR and authorized reviewers")
    state = read_state(load_yaml(git(repository, "show", f"{base}:{CONTROL_PATH}")))
    intent = state.request
    rendered = render_promotion(repository, merge, intent.sha)
    tip = publication_evidence(source, intent, now or datetime.now(timezone.utc))
    check_rejection_history(config, intent)
    marker = approval_marker(number, base, head, merge, intent, rendered)
    require(review_approved(config, number, head, marker, reviewer_ids, bot_id),
            f"Independent approval of exact candidate required. Review body: {marker}")
    require(current_candidate(config, number, trusted_base)[1] == candidate
            and source.head(SOURCE_REPOSITORY) == tip, "Protected refs or PR candidate changed during verification")
    return operation, merge


def trusted_main(argv):
    parser = argparse.ArgumentParser(description="Verify the live GitHub merge candidate using protected policy.")
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--pr", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        expected_ref = f"{CONFIG_REPOSITORY}/.github/workflows/trusted-promotion.yml@refs/heads/main"
        base = os.environ["GITHUB_SHA"]
        require(os.environ["GITHUB_WORKFLOW_REF"].lower() == expected_ref
                and os.environ["GITHUB_WORKFLOW_SHA"] == base
                and os.environ["GITHUB_EVENT_NAME"] == "pull_request_target"
                and git(args.repository, "rev-parse", "HEAD").strip() == base,
                "Guard must run from the protected Config workflow and checkout")
        token = os.environ["CONFIG_READ_TOKEN"]
        config = GitHubReader(token)
        source = GitHubReader(os.environ.get("WORKLOAD_READ_TOKEN") or token)
        reviewer_ids = {int(value) for value in os.environ.get("PROMOTION_REVIEWER_IDS", "").split()}
        configured_bot = int(os.environ.get("PROMOTION_BOT_ID") or "0")
        operation, merge = verify_pull(
            args.repository, config, source, args.pr, base, reviewer_ids, configured_bot,
            fetch=lambda repository, number, merge: fetch_candidate(repository, number, merge, token),
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError,
            binascii.Error, zipfile.BadZipFile) as error:
        message = str(error) if isinstance(error, PolicyError) else "Invalid or inaccessible trusted verification inputs"
        parser.exit(1, f"Promotion guard blocked: {message}\n")
    print(f"Trusted validation passed: {operation}; merge candidate {merge}")
    return 0


def main():
    if sys.argv[1:2] == ["--trusted-pr"]:
        return trusted_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    parser.add_argument("--merge", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--head-ref", required=True)
    parser.add_argument("--head-repository", required=True)
    parser.add_argument("--rendered", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = check_merge(args.repository, args.base, args.merge, args.head, args.head_ref, args.head_repository,
                             args.rendered.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as error:
        message = str(error) if isinstance(error, PolicyError) else "Invalid or unreadable merge inputs"
        parser.exit(1, f"Promotion guard blocked: {message}\n")
    print(f"Local structural validation passed: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
