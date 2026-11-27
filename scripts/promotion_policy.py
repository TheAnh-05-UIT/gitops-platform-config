"""Trusted parser and transition rules for workload-api promotion."""

import re
from dataclasses import dataclass
from typing import Any

import yaml

SOURCE_REPOSITORY = "theanh-05-uit/gitops-platform-workload-api"
CONFIG_REPOSITORY = "theanh-05-uit/gitops-platform-config"
IMAGE_REPOSITORY = f"ghcr.io/{SOURCE_REPOSITORY}"
WORKFLOW = ".github/workflows/ci.yml"
VALUES_PATH = "apps/workload-api/values.yaml"
CONTROL_PATH = "promotion/workload-api.yaml"
REVERTED_SHA = "2dd86b7d8237da0ae37878ae99bc9cf983e3a9d0"


class PolicyError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PolicyError(message)


def is_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            require(isinstance(key, str) and key not in result, "Non-string or duplicate YAML key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_yaml(text):
    try:
        require(
            not any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
                    for token in yaml.scan(text)),
            "YAML anchors and aliases are unsupported in promotion inputs",
        )
        return yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError:
        raise PolicyError("Invalid YAML syntax") from None


def fields(value, names, name):
    require(isinstance(value, dict) and value.keys() == set(names.split()), f"Invalid {name} fields")


@dataclass(frozen=True)
class Block:
    sha: str
    digest: str | None
    reason: str


@dataclass(frozen=True)
class Request:
    id: str
    mode: str
    sha: str
    digest: str
    run_id: int
    run_attempt: int
    expected_tag: str
    decision: str
    replaces_blocks: tuple[str, ...]

    @property
    def branch(self):
        return f"promotion/workload-api/{self.id.removeprefix('workload-api-')}-{self.sha}"


@dataclass(frozen=True)
class State:
    generation: int
    frozen: bool
    request: Request | None
    blocks: tuple[Block, ...]


def read_state(value):
    fields(value, "generation frozen request blocked_images", "control")
    generation, frozen = value["generation"], value["frozen"]
    require(type(generation) is int and generation >= 0, "Invalid generation")
    require(type(frozen) is bool, "Invalid freeze state")
    require(isinstance(value["blocked_images"], list), "Invalid block list")
    blocks = []
    for row in value["blocked_images"]:
        fields(row, "sha digest reason", "block")
        require(is_sha(row["sha"]), "Invalid blocked SHA")
        require(row["digest"] is None or is_digest(row["digest"]), "Invalid blocked digest")
        require(isinstance(row["reason"], str) and row["reason"].strip(), "Missing block reason")
        blocks.append(Block(**row))
    require(len({block.sha for block in blocks}) == len(blocks), "Duplicate block")
    require(any(block.sha == REVERTED_SHA for block in blocks), "Historical v2 block is required")
    candidate = value["request"]
    if candidate is None:
        return State(generation, frozen, None, tuple(blocks))
    fields(candidate, "id mode sha digest run_id run_attempt expected_tag decision replaces_blocks", "request")
    require(not frozen and generation > 0, "Frozen or initial state cannot authorize a promotion")
    require(candidate["id"] == f"workload-api-g{generation}", "Request ID must match generation")
    require(candidate["mode"] in ("promote", "repromote"), "Invalid request mode")
    require(is_sha(candidate["sha"]) and is_sha(candidate["expected_tag"]), "Invalid request SHA tag")
    require(is_digest(candidate["digest"]), "Invalid request digest")
    for key in ("run_id", "run_attempt"):
        require(type(candidate[key]) is int and candidate[key] > 0, f"Invalid {key}")
    require(isinstance(candidate["decision"], str) and candidate["decision"].strip(), "Missing decision")
    replacements = candidate["replaces_blocks"]
    require(isinstance(replacements, list) and all(map(is_sha, replacements)), "Invalid block references")
    matched = {
        b.sha for b in blocks if b.sha == candidate["sha"] or b.digest == candidate["digest"]
    }
    if candidate["mode"] == "promote":
        require(not matched and not replacements, "Candidate is blocked or requests an override")
    else:
        require(matched and sorted(replacements) == sorted(matched), "Re-promotion must name every applicable block")
        require(all(b.sha != candidate["sha"] or b.digest in (None, candidate["digest"]) for b in blocks),
                "Re-promotion cannot rebind a blocked source SHA to another digest")
    req = Request(**(candidate | {"replaces_blocks": tuple(replacements)}))
    return State(generation, frozen, req, tuple(blocks))


@dataclass(frozen=True)
class ImageIdentity:
    tag: str
    digest: str | None = None

    @property
    def reference(self):
        suffix = f"@{self.digest}" if self.digest else f":{self.tag}"
        return IMAGE_REPOSITORY + suffix


def image_identity(text):
    values = load_yaml(text)
    require(isinstance(values, dict), "Invalid workload values")
    image = values.get("image")
    require(isinstance(image, dict) and image.keys() in ({"repository", "tag"}, {"repository", "tag", "digest"}),
            "Invalid image fields")
    require(image["repository"] == IMAGE_REPOSITORY, "Unexpected image repository")
    require(is_sha(image["tag"]), "Image tag must be a full workload SHA")
    require("digest" not in image or is_digest(image["digest"]), "Invalid image digest")
    return ImageIdentity(image["tag"], image.get("digest"))


def image_tag(text):
    return image_identity(text).tag


def patch_tag(values, new_tag):
    require(image_identity(values).digest is None, "Digest-pinned images must use a paired source SHA and digest")
    return patch_image(values, ImageIdentity(new_tag))


def patch_image(values, target):
    current = image_identity(values)
    require(is_sha(target.tag) and (target.digest is None or is_digest(target.digest)), "Invalid replacement image")
    require(current.digest is None or target.digest is not None, "Cannot remove digest pinning")
    document = yaml.compose(values, Loader=UniqueLoader)
    image = next(v for k, v in document.value if k.value == "image")
    nodes = {key.value: (key, value) for key, value in image.value}
    edits = []
    for key, old, new in (("tag", current.tag, target.tag), ("digest", current.digest, target.digest)):
        if key in nodes:
            _, node = nodes[key]
            start, end = node.start_mark.index, node.end_mark.index
            quote = node.style or ""
            require(quote in ("", "'", '"') and values[start:end] == f"{quote}{old}{quote}",
                    "Unsupported image scalar layout")
            edits.append((start, end, f"{quote}{new}{quote}"))
    if current.digest is None and target.digest is not None:
        key, node = nodes["tag"]
        require(not image.flow_style and key.start_mark.line == node.end_mark.line, "Unsupported image mapping layout")
        end = values.find("\n", node.end_mark.index)
        indent = " " * key.start_mark.column
        newline = "\r\n" if end > 0 and values[end - 1] == "\r" else "\n"
        entry = f'{indent}digest: "{target.digest}"'
        edits.append((end + 1, end + 1, entry + newline) if end >= 0
                     else (len(values), len(values), newline + entry))
    updated = values
    for start, end, replacement in sorted(edits, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    require(image_identity(updated) == target, "Updated fields do not select the intended image")
    return updated


def guard(base, proposed, old_values, new_values, paths, rollback_targets=frozenset(), *, head_ref=None):
    old_image, new_image = image_identity(old_values), image_identity(new_values)
    old_tag, new_tag = old_image.tag, new_image.tag
    if base == proposed and old_image == new_image:
        return "unchanged-image"
    if base != proposed:
        require(proposed.generation == base.generation + 1, "Control change must advance one generation")
        retained = proposed.blocks[:len(base.blocks)]
        require(len(retained) == len(base.blocks) and all(
            (old.sha, old.reason) == (new.sha, new.reason)
            and (old.digest is None or old.digest == new.digest)
            for old, new in zip(base.blocks, retained)
        ), "Historical blocks cannot be removed or rewritten")
        if old_image == new_image:
            require(paths == {CONTROL_PATH}, "Admission must change only the control file")
            if proposed.request:
                require(proposed.request.expected_tag == old_tag, "Admission targets a stale image")
            return "admission"
        require(paths == {CONTROL_PATH, VALUES_PATH}, "Rollback must change only image and control")
        require(proposed.frozen and proposed.request is None, "Rollback must freeze and invalidate admission")
        added = proposed.blocks[len(base.blocks):]
        require(all(old == new or old.sha == old_tag for old, new in zip(base.blocks, retained)),
                "Rollback cannot update unrelated blocks")
        previous_block = any(block.sha == old_tag for block in base.blocks)
        require(tuple(block.sha for block in added) == (() if previous_block else (old_tag,)),
                "Rollback must block the withdrawn image")
        blocked = next(b for b in proposed.blocks if b.sha == old_tag)
        if old_image.digest:
            require(blocked.digest == old_image.digest, "Rollback block omits the withdrawn pinned digest")
        if base.request and base.request.sha == old_tag:
            require(blocked.digest == base.request.digest, "Rollback block omits the known digest")
        require(new_image in rollback_targets or (new_image.digest is None and new_tag in rollback_targets),
                "Rollback requires a prior image from trusted config history")
        require(not any(block.sha == new_tag or (new_image.digest is not None and block.digest == new_image.digest)
                        for block in proposed.blocks), "Rollback target is blocked")
        require(new_values == patch_image(old_values, new_image), "Rollback changes unrelated values")
        return "rollback"
    require(paths == {VALUES_PATH}, "Promotion must change only the values file")
    request = base.request
    require(not base.frozen and request is not None, "No active trusted approval")
    require(head_ref == request.branch, "PR branch does not identify the current approved generation")
    require(old_tag == request.expected_tag and new_tag == request.sha, "Image does not match the trusted request")
    require(new_image.digest is None or new_image.digest == request.digest, "Pin differs from approved digest")
    require(old_image.digest is not None or new_image.digest is None or old_tag == new_tag,
            "First digest pin must keep the current source SHA")
    require(old_tag != new_tag or old_image.digest in (None, new_image.digest),
            "An existing pin for the source SHA cannot be rebound")
    require(new_values == patch_image(old_values, new_image), "Promotion changes unrelated values")
    return "promotion"


def validate_rendered(rendered: list[dict[str, Any]], image: str | ImageIdentity):
    identity = ImageIdentity(image) if isinstance(image, str) else image
    images = [
        container.get("image")
        for document in rendered
        if document.get("kind") == "Deployment" and document.get("metadata", {}).get("name") == "workload-api"
        for container in document["spec"]["template"]["spec"]["containers"]
        if container.get("name") == "workload-api"
    ]
    require(images == [identity.reference], "Rendered workload image does not match guarded values")
