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


def image_tag(text):
    values = load_yaml(text)
    require(isinstance(values, dict), "Invalid workload values")
    image = values.get("image")
    fields(image, "repository tag", "image")
    require(image["repository"] == IMAGE_REPOSITORY, "Unexpected image repository")
    require(is_sha(image["tag"]), "Image tag must be a full workload SHA")
    return image["tag"]


def patch_tag(values, new_tag):
    current = image_tag(values)
    require(is_sha(new_tag), "Invalid replacement image tag")
    document = yaml.compose(values, Loader=UniqueLoader)
    image = next(v for k, v in document.value if k.value == "image")
    tag = next(v for k, v in image.value if k.value == "tag")
    start, end = tag.start_mark.index, tag.end_mark.index
    quote = tag.style or ""
    require(quote in ("", "'", '"') and values[start:end] == f"{quote}{current}{quote}", "Unsupported image tag layout")
    updated = values[:start] + f"{quote}{new_tag}{quote}" + values[end:]
    require(image_tag(updated) == new_tag, "Updated image tag did not parse as the intended SHA")
    return updated


def guard(base, proposed, old_values, new_values, paths, rollback_targets=frozenset(), *, head_ref=None):
    old_tag, new_tag = image_tag(old_values), image_tag(new_values)
    if base == proposed and old_tag == new_tag:
        return "unchanged-image"
    if base != proposed:
        require(proposed.generation == base.generation + 1, "Control change must advance one generation")
        retained = proposed.blocks[:len(base.blocks)]
        require(len(retained) == len(base.blocks) and all(
            (old.sha, old.reason) == (new.sha, new.reason)
            and (old.digest is None or old.digest == new.digest)
            for old, new in zip(base.blocks, retained)
        ), "Historical blocks cannot be removed or rewritten")
        if old_tag == new_tag:
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
        if base.request and base.request.sha == old_tag:
            require(blocked.digest == base.request.digest, "Rollback block omits the known digest")
        require(new_tag in rollback_targets, "Rollback requires a prior image from trusted config history")
        require(not any(block.sha == new_tag for block in proposed.blocks), "Rollback target is blocked")
        require(new_values == patch_tag(old_values, new_tag), "Rollback changes unrelated values")
        return "rollback"
    require(paths == {VALUES_PATH}, "Promotion must change only the values file")
    request = base.request
    require(not base.frozen and request is not None, "No active trusted approval")
    require(head_ref == request.branch, "PR branch does not identify the current approved generation")
    require(old_tag == request.expected_tag and new_tag == request.sha, "Image does not match the trusted request")
    require(new_values == patch_tag(old_values, request.sha), "Promotion changes unrelated values")
    return "promotion"


def validate_rendered(rendered: list[dict[str, Any]], tag: str):
    images = [
        container.get("image")
        for document in rendered
        if document.get("kind") == "Deployment" and document.get("metadata", {}).get("name") == "workload-api"
        for container in document["spec"]["template"]["spec"]["containers"]
        if container.get("name") == "workload-api"
    ]
    require(images == [f"{IMAGE_REPOSITORY}:{tag}"], "Rendered workload image does not match guarded values")
