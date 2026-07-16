#!/usr/bin/env python3
"""Generate checksums, a runtime SBOM, and an unsigned SLSA-compatible custom statement."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_VERSION = "1"


class EvidenceError(RuntimeError):
    """Raised when release evidence cannot be generated safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repository", default="local/latex-word-review")
    parser.add_argument("--source-ref", default="local")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--run-id", default="local")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def purl(name: str, version: str) -> str:
    return f"pkg:pypi/{canonical_name(name)}@{version}"


def requirement_name(value: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", value)
    return canonical_name(match.group(1)) if match else None


def timestamp_from_epoch() -> tuple[str, int]:
    raw = os.environ.get("SOURCE_DATE_EPOCH", "")
    if not raw.isascii() or not raw.isdigit():
        raise EvidenceError("SOURCE_DATE_EPOCH must be set to an ASCII integer")
    epoch = int(raw)
    stamp = datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")
    return stamp, epoch


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")


def installed_components() -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    distributions: dict[str, importlib.metadata.Distribution] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            distributions[canonical_name(str(name))] = distribution
    root = distributions.get("latex-word-review")
    if root is None:
        raise EvidenceError("run this script with the clean-install environment Python")

    dependency_names: dict[str, set[str]] = {}
    for name, distribution in distributions.items():
        dependency_names[name] = {
            item
            for requirement in distribution.requires or []
            if (item := requirement_name(requirement)) is not None and item in distributions
        }
    reachable = {"latex-word-review"}
    pending = ["latex-word-review"]
    while pending:
        current = pending.pop()
        for dependency in sorted(dependency_names[current]):
            if dependency not in reachable:
                reachable.add(dependency)
                pending.append(dependency)

    components: list[dict[str, Any]] = []
    dependency_rows: list[dict[str, Any]] = []
    for name in sorted(reachable):
        distribution = distributions[name]
        display_name = str(distribution.metadata.get("Name", name))
        version = distribution.version
        reference = purl(display_name, version)
        component: dict[str, Any] = {
            "bom-ref": reference,
            "name": display_name,
            "purl": reference,
            "type": "application" if name == "latex-word-review" else "library",
            "version": version,
        }
        license_expression = distribution.metadata.get("License-Expression")
        if license_expression:
            component["licenses"] = [{"expression": str(license_expression)}]
        homepage = distribution.metadata.get("Home-page")
        if homepage:
            component["externalReferences"] = [{"type": "website", "url": str(homepage)}]
        if name != "latex-word-review":
            components.append(component)

        dependency_rows.append(
            {
                "ref": reference,
                "dependsOn": [
                    purl(
                        str(distributions[item].metadata.get("Name", item)),
                        distributions[item].version,
                    )
                    for item in sorted(dependency_names[name] & reachable)
                ],
            }
        )
    return components, dependency_rows, root.version


def main() -> int:
    args = parse_args()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.source_sha) is None:
        raise EvidenceError("source SHA must be 40 or 64 lowercase hexadecimal characters")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) is None:
        raise EvidenceError("repository must be an owner/name pair")
    if (
        not args.source_ref
        or len(args.source_ref) > 256
        or any(character in args.source_ref for character in "\r\n\x00")
    ):
        raise EvidenceError("source ref is invalid")
    if args.run_id != "local" and (not args.run_id.isascii() or not args.run_id.isdigit()):
        raise EvidenceError("run ID must be numeric or local")
    dist_dir = args.dist_dir.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise EvidenceError("release evidence directory already exists")
    output_dir.mkdir(parents=True)
    artifacts = sorted([*dist_dir.glob("*.whl"), *dist_dir.glob("*.tar.gz")])
    if len(artifacts) != 2:
        raise EvidenceError("expected exactly one wheel and one sdist")
    hashes = {path.name: sha256_file(path) for path in artifacts}
    timestamp, epoch = timestamp_from_epoch()
    components, dependencies, version = installed_components()

    checksums_path = output_dir / "SHA256SUMS"
    with checksums_path.open("x", encoding="ascii", newline="\n") as handle:
        for name, digest in sorted(hashes.items()):
            handle.write(f"{digest}  {name}\n")

    identity = "|".join([args.source_sha, *(f"{name}:{hashes[name]}" for name in sorted(hashes))])
    serial = uuid.uuid5(uuid.NAMESPACE_URL, identity)
    root_ref = purl("latex-word-review", version)
    sbom = {
        "bomFormat": "CycloneDX",
        "components": components,
        "dependencies": dependencies,
        "metadata": {
            "component": {
                "bom-ref": root_ref,
                "licenses": [{"expression": "Apache-2.0"}],
                "name": "latex-word-review",
                "purl": root_ref,
                "type": "application",
                "version": version,
            },
            "timestamp": timestamp,
            "tools": {
                "components": [
                    {
                        "name": "repository-release-evidence",
                        "type": "application",
                        "version": TOOL_VERSION,
                    }
                ]
            },
        },
        "serialNumber": f"urn:uuid:{serial}",
        "specVersion": "1.6",
        "version": 1,
    }
    sbom_path = output_dir / "sbom.cdx.json"
    write_json(sbom_path, sbom)

    repository_uri = f"https://github.com/{args.repository}"
    builder_id = f"{repository_uri}/.github/workflows/release-artifacts.yml@{args.source_ref}"
    invocation_id = (
        f"{repository_uri}/actions/runs/{args.run_id}" if args.run_id != "local" else "local"
    )
    resolved = [
        {"digest": {"gitCommit": args.source_sha}, "uri": f"{repository_uri}@{args.source_ref}"},
        *({"uri": component["purl"]} for component in components),
    ]
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://packaging.python.org/specifications/pyproject-toml/",
                "externalParameters": {
                    "repository": repository_uri,
                    "source_ref": args.source_ref,
                    "source_sha": args.source_sha,
                },
                "internalParameters": {
                    "build_count": 2,
                    "python": platform.python_version(),
                    "runner_os": platform.system(),
                    "source_date_epoch": epoch,
                },
                "resolvedDependencies": resolved,
            },
            "runDetails": {
                "builder": {"id": builder_id},
                "metadata": {"invocationId": invocation_id},
            },
        },
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {"digest": {"sha256": digest}, "name": name} for name, digest in sorted(hashes.items())
        ],
    }
    provenance_path = output_dir / "provenance.intoto.json"
    write_json(provenance_path, provenance)

    release_root = dist_dir.parent
    evidence_inputs = [*artifacts, checksums_path, sbom_path, provenance_path]
    manifest = {
        "files": [
            {
                "path": path.resolve().relative_to(release_root).as_posix(),
                "sha256": sha256_file(path),
            }
            for path in sorted(evidence_inputs, key=lambda item: item.as_posix())
        ],
        "schema_version": "latex-word-review-release-evidence-v1",
        "source": {"ref": args.source_ref, "sha": args.source_sha},
    }
    write_json(output_dir / "evidence-manifest.json", manifest)
    print(
        json.dumps(
            {
                "status": "pass",
                "component_version": version,
                "distribution_artifacts": sorted(hashes),
                "installed_components": len(components),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvidenceError, OSError, UnicodeError) as exc:
        print(f"release evidence failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
