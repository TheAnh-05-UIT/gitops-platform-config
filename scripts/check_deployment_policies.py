import argparse
from pathlib import Path

import yaml


def check_deployment(deployment):
    name = deployment["metadata"]["name"]
    pod_spec = deployment["spec"]["template"]["spec"]
    pod_security = pod_spec.get("securityContext") or {}
    containers = pod_spec["containers"] + (pod_spec.get("initContainers") or [])
    errors = []

    for container in containers:
        location = f"Deployment/{name}, container/{container['name']}"

        image = container.get("image") or ""
        reference, _, digest = image.partition("@")
        image_name = reference.rsplit("/", 1)[-1]
        _, _, tag = image_name.partition(":")

        if tag == "latest" or not (tag or digest):
            errors.append(
                f"{location}: use an explicit image tag or digest; "
                "'latest' is forbidden."
            )

        security = container.get("securityContext") or {}
        run_as_non_root = security.get(
            "runAsNonRoot", pod_security.get("runAsNonRoot")
        )
        run_as_user = security.get("runAsUser", pod_security.get("runAsUser"))

        if run_as_non_root is not True or run_as_user == 0:
            errors.append(
                f"{location}: require runAsNonRoot: true and forbid UID 0."
            )

        resources = container.get("resources") or {}
        for section in ("requests", "limits"):
            quantities = resources.get(section) or {}
            for resource in ("cpu", "memory"):
                if quantities.get(resource) in (None, ""):
                    errors.append(
                        f"{location}: missing resources.{section}.{resource}."
                    )

    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()

    with args.manifest.open(encoding="utf-8") as file:
        deployments = [
            document
            for document in yaml.safe_load_all(file)
            if isinstance(document, dict) and document.get("kind") == "Deployment"
        ]

    if not deployments:
        print("No Deployment found in the rendered manifest.")
        return 1

    errors = [
        error
        for deployment in deployments
        for error in check_deployment(deployment)
    ]

    if errors:
        print("\n".join(errors))
        return 1

    print(f"Deployment policies passed for {len(deployments)} Deployment(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())