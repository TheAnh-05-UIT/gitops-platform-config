"""Validate a proposed merge using policy and approval from the trusted base."""

import argparse
import subprocess
from pathlib import Path

import yaml

from .promotion_policy import (
    CONFIG_REPOSITORY, CONTROL_PATH, VALUES_PATH, PolicyError, guard, image_tag, is_sha,
    load_yaml, read_state, require, validate_rendered,
)

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
    if before != after and after.frozen and after.request is None and image_tag(old) != image_tag(new):
        commits = git(repository, "log", "--first-parent", "--format=%H", base, "--", VALUES_PATH)
        for commit in commits.splitlines():
            try:
                rollback_targets.add(image_tag(git(repository, "show", f"{commit}:{VALUES_PATH}")))
            except PolicyError:
                continue
    operation = guard(before, after, old, new, paths, rollback_targets, head_ref=head_ref)
    if operation == "promotion":
        require(head_repository.lower() == CONFIG_REPOSITORY, "Promotion branch must belong to the config repository")
    if rendered is not None:
        manifests = [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]
        validate_rendered(manifests, image_tag(new))
    return operation


def main():
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
    print(f"Promotion guard passed: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
