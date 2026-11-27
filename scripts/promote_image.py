"""Plan a promotion from offline evidence or read-only GitHub/GHCR data."""

import argparse
import base64
import binascii
import hashlib
import io
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .promotion_policy import (
    CONFIG_REPOSITORY, CONTROL_PATH, IMAGE_REPOSITORY, SOURCE_REPOSITORY,
    VALUES_PATH, WORKFLOW, ImageIdentity, PolicyError, image_identity, is_sha, load_yaml, patch_image,
    read_state, require,
)


class ApiFailure(PolicyError):
    pass


def verify_evidence(request, evidence):
    require(isinstance(evidence, dict), "Invalid publication evidence")
    receipt, run = evidence.get("receipt"), evidence.get("run")
    require(isinstance(receipt, dict), "Missing or invalid publication receipt")
    require(isinstance(run, dict), "Missing trusted workflow attempt")
    expected = {
        "version": 1, "repository": SOURCE_REPOSITORY, "workflow": WORKFLOW,
        "sha": request.sha, "run_id": request.run_id, "run_attempt": request.run_attempt,
        "image": f"{IMAGE_REPOSITORY}:{request.sha}", "digest": request.digest,
    }
    require(all(type(receipt.get(k)) is type(v) and receipt[k] == v for k, v in expected.items()),
            "Publication receipt does not match the approval")
    run_fields = {
        "id": request.run_id, "run_attempt": request.run_attempt, "path": WORKFLOW,
        "head_sha": request.sha, "head_branch": "main", "event": "push", "conclusion": "success",
    }
    repository = run.get("head_repository")
    require(isinstance(repository, dict) and str(repository.get("full_name", "")).lower() == SOURCE_REPOSITORY
            and all(type(run.get(k)) is type(v) and run[k] == v for k, v in run_fields.items()),
            "Workflow attempt is not the approved successful source publication")
    tip = evidence.get("source_tip")
    require(is_sha(tip), "Missing workload main SHA")
    require(tip == request.sha or (request.mode == "repromote" and evidence.get("ancestor") is True),
            "Approved source is stale or has no verified ancestry")
    require(evidence.get("observed_digest") == request.digest, "Missing image or registry digest mismatch")


@dataclass(frozen=True)
class Preview:
    action: str
    branch: str
    values: str


def selected_request(state, request_id):
    request = state.request
    require(not state.frozen and request is not None, "Promotion is frozen or has no active request")
    require(request.id == request_id, "Request has been superseded")
    return request


def plan(state, request_id, values, evidence, *, remote_state="absent", pin_digest=False):
    request = selected_request(state, request_id)
    verify_evidence(request, evidence)
    current = image_identity(values)
    target = ImageIdentity(request.sha, request.digest if pin_digest or current.digest else None)
    if current == target:
        return Preview("no-op", request.branch, values)
    require(current.tag == request.expected_tag, "Current config image differs from approved base")
    require(not pin_digest or current.digest is not None or target.tag == current.tag,
            "First digest pin must keep the current source SHA")
    require(current.tag != request.sha or current.digest in (None, request.digest),
            "An existing pin for the source SHA cannot be rebound")
    actions = {
        "absent": "propose", "open": "reuse-pr", "branch": "recover-pr",
        "stale-open": "refresh-branch", "stale-branch": "refresh-and-recover",
    }
    require(isinstance(remote_state, str) and remote_state in actions,
            "Closed, conflicting or unverified promotion requires owner review")
    return Preview(actions[remote_state], request.branch, patch_image(values, target))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubReader:
    """GET only. Redirects cannot carry the GitHub token to artifact storage."""

    def __init__(self, token, opener=None):
        if not token:
            raise ApiFailure("Missing GitHub read authentication")
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open

    def _request(self, url, authenticated):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        return self.opener(urllib.request.Request(url, headers=headers, method="GET"), timeout=12)

    def _read(self, url, *, missing=False, artifact=False):
        try:
            try:
                response = self._request(url, True)
            except urllib.error.HTTPError as error:
                error.close()
                if missing and error.code == 404:
                    return None
                if not artifact or error.code not in (301, 302, 303, 307, 308):
                    raise
                target = urllib.parse.urlsplit(error.headers.get("Location", ""))
                host = target.hostname or ""
                require(target.scheme == "https" and not target.username and not target.password
                        and target.port in (None, 443)
                        and (host.endswith(".blob.core.windows.net") or host.endswith(".actions.githubusercontent.com")),
                        "Unexpected GitHub artifact redirect")
                response = self._request(target.geturl(), False)
            with response:
                data = response.read(4 * 1024 * 1024 + 1)
            require(len(data) <= 4 * 1024 * 1024, "GitHub response exceeds preview limit")
            return data
        except TimeoutError:
            raise ApiFailure("GitHub read timed out") from None
        except urllib.error.HTTPError as error:
            if error.code in (403, 429) and (
                error.code == 429 or error.headers.get("X-RateLimit-Remaining") == "0"
                or error.headers.get("Retry-After")
            ):
                raise ApiFailure("GitHub rate limit reached; retry after the service limit resets") from None
            raise ApiFailure(f"GitHub read failed (HTTP {error.code})") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ApiFailure("GitHub read timed out") from None
            raise ApiFailure("GitHub read unavailable") from None
        except OSError:
            raise ApiFailure("GitHub read unavailable") from None

    @staticmethod
    def _url(repository, endpoint):
        require(repository in (SOURCE_REPOSITORY, CONFIG_REPOSITORY), "Unexpected GitHub repository")
        return f"https://api.github.com/repos/{repository}/{endpoint}"

    def get(self, repository, endpoint, *, missing=False):
        data = self._read(self._url(repository, endpoint), missing=missing)
        return None if data is None else json.loads(data)

    def head(self, repository):
        sha = self.get(repository, "git/ref/heads/main")["object"]["sha"]
        require(is_sha(sha), "Unexpected GitHub main SHA")
        return sha

    def file(self, repository, path, ref):
        require(is_sha(ref) and path in (CONTROL_PATH, VALUES_PATH), "Unexpected config file or ref")
        item = self.get(repository, f"contents/{path}?ref={ref}")
        require(item["type"] == "file" and item["encoding"] == "base64", "Unsupported GitHub file response")
        return base64.b64decode("".join(item["content"].split()), validate=True).decode("utf-8")

    def receipt(self, request):
        name = f"publication-receipt-{request.run_attempt}"
        listing = self.get(SOURCE_REPOSITORY, f"actions/runs/{request.run_id}/artifacts?name={name}")
        artifacts = [
            item for item in listing["artifacts"]
            if item["name"] == name and item["expired"] is False
        ]
        require(len(artifacts) == 1 and listing["total_count"] == 1, "Missing or ambiguous publication artifact")
        artifact = artifacts[0]
        require(type(artifact["id"]) is int and artifact["id"] > 0, "Invalid artifact ID")
        endpoint = f"actions/artifacts/{artifact['id']}/zip"
        data = self._read(self._url(SOURCE_REPOSITORY, endpoint), artifact=True)
        require(artifact.get("digest") == "sha256:" + hashlib.sha256(data).hexdigest(),
                "Publication artifact ZIP digest mismatch")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            require(len(entries) == 1 and entries[0].filename == "publication-receipt.json"
                    and entries[0].file_size <= 16384, "Unexpected receipt archive layout")
            return json.loads(archive.read(entries[0]))


def registry_digest(request, *, media_type=None, image_id=None):
    # Registry inspection may use Docker's credential store. Do not pass
    # GitHub API credentials to the Docker CLI or its Buildx plugin.
    env = os.environ.copy()
    for key in ("CONFIG_READ_TOKEN", "WORKLOAD_READ_TOKEN", "PROMOTION_SOURCE_READ_TOKEN",
                "PROMOTION_CONFIG_READ_TOKEN"):
        env.pop(key, None)

    def descriptor(ref):
        try:
            result = subprocess.run(
                ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"],
                capture_output=True, text=True, check=True, timeout=20, env=env,
            )
            return json.loads(result.stdout)["digest"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            raise PolicyError("Registry descriptor could not be read") from None

    tagged = f"{IMAGE_REPOSITORY}:{request.sha}"
    pinned = f"{IMAGE_REPOSITORY}@{request.digest}"
    if media_type is not None:
        for ref in (tagged, pinned):
            try:
                result = subprocess.run(
                    ["docker", "buildx", "imagetools", "inspect", ref, "--raw"],
                    capture_output=True, check=True, timeout=20, env=env,
                )
                manifest = json.loads(result.stdout)
                digest = "sha256:" + hashlib.sha256(result.stdout).hexdigest()
                require(digest == request.digest and manifest["mediaType"] == media_type
                        and manifest["config"]["digest"] == image_id,
                        "Registry manifest bytes, media type or scanned image ID do not match")
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
                if isinstance(error, PolicyError):
                    raise
                raise PolicyError("Registry manifest could not be verified") from None
        return request.digest
    digest = descriptor(tagged)
    require(digest == request.digest, "Registry tag maps to an unexpected digest")
    require(descriptor(pinned) == digest, "Digest is not available by reference")
    return digest


def inspect_remote(reader, base_sha, request, bot_login):
    query = urllib.parse.urlencode({"state": "all", "base": "main", "head": f"theanh-05-uit:{request.branch}"})
    pulls = reader.get(CONFIG_REPOSITORY, f"pulls?{query}")
    require(len(pulls) <= 1, "Ambiguous existing promotion PR")
    pull = pulls[0] if pulls else None
    if pull and pull["state"] != "open":
        return "closed"
    ref = reader.get(CONFIG_REPOSITORY, f"git/ref/heads/{request.branch}", missing=True)
    if ref is None:
        return "foreign" if pull else "absent"
    head = ref["object"]["sha"]
    require(is_sha(head), "Invalid promotion branch head SHA")
    if pull and (pull["head"]["sha"] != head
                 or pull["head"]["repo"]["full_name"].lower() != CONFIG_REPOSITORY):
        return "foreign"
    commit = reader.get(CONFIG_REPOSITORY, f"commits/{head}")
    author = commit.get("author") or {}
    if not bot_login or author.get("login") != bot_login or len(commit["parents"]) != 1:
        return "foreign"
    parent = commit["parents"][0]["sha"]
    if [entry["filename"] for entry in commit["files"]] != [VALUES_PATH]:
        return "foreign"
    base_values = reader.file(CONFIG_REPOSITORY, VALUES_PATH, parent)
    original = read_state(load_yaml(reader.file(CONFIG_REPOSITORY, CONTROL_PATH, parent)))
    if original.request != request or image_identity(base_values).tag != request.expected_tag:
        return "foreign"
    head_values = reader.file(CONFIG_REPOSITORY, VALUES_PATH, head)
    previous = image_identity(base_values)
    target = image_identity(head_values)
    expected = ImageIdentity(request.sha, request.digest if previous.digest else None)
    if (target != expected
            or (previous.tag == target.tag and previous.digest not in (None, target.digest))
            or head_values != patch_image(base_values, target)):
        return "foreign"
    if parent != base_sha:
        return "stale-open" if pull else "stale-branch"
    return "open" if pull else "branch"


def check_rejection_history(reader, request):
    """Stop routine preview after a prior closed proposal for the same SHA."""
    if request.mode == "repromote":
        return
    for page in range(1, 11):
        pulls = reader.get(CONFIG_REPOSITORY, f"pulls?state=closed&base=main&per_page=100&page={page}")
        for pull in pulls:
            head = pull["head"]
            ref = head["ref"]
            repository = head.get("repo") or {}
            if (str(repository.get("full_name", "")).lower() == CONFIG_REPOSITORY
                    and re.fullmatch(r"promotion/workload-api/g[1-9][0-9]*-[0-9a-f]{40}", ref)
                    and ref.endswith(f"-{request.sha}")):
                status = reader.get(CONFIG_REPOSITORY, f"pulls/{int(pull['number'])}")
                if not status["merged_at"]:
                    raise PolicyError("Prior closed promotion requires a recorded block and fresh re-promotion")
        if len(pulls) < 100:
            return
    raise PolicyError("Promotion rejection history exceeds the verified scan limit")


def github_preview(request_id, bot_login):
    source = GitHubReader(os.environ.get("PROMOTION_SOURCE_READ_TOKEN"))
    config = GitHubReader(os.environ.get("PROMOTION_CONFIG_READ_TOKEN"))
    base = config.head(CONFIG_REPOSITORY)
    state = read_state(load_yaml(config.file(CONFIG_REPOSITORY, CONTROL_PATH, base)))
    request = selected_request(state, request_id)
    values = config.file(CONFIG_REPOSITORY, VALUES_PATH, base)
    tip = source.head(SOURCE_REPOSITORY)
    ancestor = False
    if request.mode == "repromote" and request.sha != tip:
        diff = source.get(SOURCE_REPOSITORY, f"compare/{request.sha}...{tip}")
        ancestor = diff["merge_base_commit"]["sha"] == request.sha
    run = source.get(SOURCE_REPOSITORY, f"actions/runs/{request.run_id}/attempts/{request.run_attempt}")
    evidence = {
        "source_tip": tip, "ancestor": ancestor, "run": run,
        "receipt": source.receipt(request), "observed_digest": registry_digest(request),
    }
    preview = plan(state, request_id, values, evidence)
    if preview.action != "no-op":
        check_rejection_history(config, request)
        remote = inspect_remote(config, base, request, bot_login)
        preview = plan(state, request_id, values, evidence, remote_state=remote)
    require(config.head(CONFIG_REPOSITORY) == base and source.head(SOURCE_REPOSITORY) == tip,
            "A protected ref moved during preview; obtain fresh evidence")
    return preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry-run", "read-only"), default="dry-run")
    parser.add_argument("--request", required=True)
    parser.add_argument("--snapshot", type=Path, help="Offline JSON fixture, never hosted proof")
    parser.add_argument("--pin-digest", action="store_true", help="Preview initial pinning from offline evidence")
    parser.add_argument("--bot-login", help="Verified bot login for read-only branch recovery checks")
    args = parser.parse_args()
    if args.mode == "dry-run" and args.snapshot is None:
        parser.error("offline dry-run requires --snapshot")
    if args.mode == "read-only" and args.snapshot is not None:
        parser.error("read-only mode uses GitHub reads, not --snapshot")
    if args.mode != "dry-run" and args.pin_digest:
        parser.error("initial digest pinning requires an offline fixture and separate review")
    if not re.fullmatch(r"workload-api-g[1-9][0-9]*", args.request):
        parser.error("invalid request ID")
    try:
        if args.snapshot:
            sample = json.loads(args.snapshot.read_bytes())
            preview = plan(read_state(sample["control"]), args.request, sample["values"],
                           sample["evidence"], remote_state=sample.get("remote_state", "absent"),
                           pin_digest=args.pin_digest)
        else:
            preview = github_preview(args.request, args.bot_login)
    except PolicyError as error:
        parser.exit(1, f"Promotion preview blocked: {error}\n")
    except (OSError, ValueError, TypeError, KeyError, binascii.Error, zipfile.BadZipFile):
        parser.exit(1, "Promotion preview blocked: invalid or inaccessible evidence\n")
    result = asdict(preview)
    if args.mode == "read-only":
        result.pop("values")
    print(json.dumps({"mode": args.mode, "request": args.request} | result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
