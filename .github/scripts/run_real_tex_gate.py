#!/usr/bin/env python3
"""Run the installed-LaTeX E2E case and reject missing tools or any pytest skip."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SETUP_FILENAME = "miktexsetup-5.5.0+1763023-x64.zip"
EXPECTED_SETUP_SHA256 = "0571e90f6d94353089b4f189fd82a532f9fe559a388c7e7f1102b14b3c1ae27d"
PACKAGE_MANIFEST = PROJECT_ROOT / ".github/actions/real-tex-gate/miktex-packages.txt"
EXPECTED_PACKAGE_COUNT = 29
TOOL_VERSION_COMMANDS = {
    "latexmk": ("-version",),
    "latexdiff": ("--version",),
    "miktex": ("--version",),
    "perl": ("--version",),
    "xelatex": ("--version",),
}
TOOL_VERSION_MARKERS = {
    "latexmk": "latexmk,",
    "latexdiff": "latexdiff",
    "miktex": "miktex",
    "perl": "this is perl",
    "xelatex": "xetex",
}
PACKAGE_LIST_TEMPLATE = "{id}\t{version}\t{digest}\t{isInstalled}"
PACKAGE_INVENTORY_FORMAT = "miktex-installed-package-inventory-v1"
MAX_DIAGNOSTIC_CHARS = 64 * 1024


class RealTexGateError(RuntimeError):
    """Raised when the real TeX release gate cannot prove one passing test."""


def required_miktex_packages() -> tuple[str, ...]:
    try:
        packages = tuple(PACKAGE_MANIFEST.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError) as exc:
        raise RealTexGateError("MiKTeX package manifest is unreadable") from exc
    if (
        len(packages) != EXPECTED_PACKAGE_COUNT
        or list(packages) != sorted(set(packages))
        or any(re.fullmatch(r"[a-z0-9][a-z0-9+._-]*", package) is None for package in packages)
    ):
        raise RealTexGateError("MiKTeX package manifest is malformed")
    return packages


REQUIRED_MIKTEX_PACKAGES = required_miktex_packages()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def run_captured(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RealTexGateError(f"command timed out: {command[0]}") from exc


def first_version_line(executable: str, arguments: tuple[str, ...]) -> str:
    resolved = shutil.which(executable)
    if resolved is None:
        raise RealTexGateError(f"required executable is missing: {executable}")
    result = run_captured([resolved, *arguments], timeout=30)
    if result.returncode != 0:
        raise RealTexGateError(f"version check failed for {executable}: {result.returncode}")
    lines = [line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines()]
    marker = TOOL_VERSION_MARKERS[executable]
    version = next((line for line in lines if marker in line.casefold()), "")
    if not version or len(version) > 500 or any(character in version for character in "\r\n\x00"):
        raise RealTexGateError(f"invalid version output for {executable}")
    return version


def verify_tex_resources() -> dict[str, str]:
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich is None:
        raise RealTexGateError("required executable is missing: kpsewhich")
    resources = {
        "ctex_sty": ("ctex.sty",),
        "fandol_song_regular": ("FandolSong-Regular.otf",),
        "xelatex_format": ("--engine=xetex", "--format=fmt", "xelatex.fmt"),
    }
    for arguments in resources.values():
        result = run_captured([kpsewhich, *arguments], timeout=30)
        if result.returncode != 0 or not result.stdout.strip():
            raise RealTexGateError(f"kpsewhich could not resolve {arguments[-1]}")
    return {label: "resolved_by_kpsewhich" for label in resources}


def installed_miktex_package_inventory() -> tuple[dict[str, str], ...]:
    miktex = shutil.which("miktex")
    if miktex is None:
        raise RealTexGateError("miktex is required to bind Windows TeX package evidence")
    result = run_captured(
        [
            miktex,
            "--disable-installer",
            "packages",
            "list",
            "--template",
            PACKAGE_LIST_TEMPLATE,
        ],
        timeout=60,
    )
    if result.returncode != 0:
        raise RealTexGateError("MiKTeX installed-package inventory query failed")
    lines = result.stdout.splitlines()
    if not lines:
        raise RealTexGateError("MiKTeX installed-package inventory is empty")

    seen_ids: set[str] = set()
    installed_packages: list[dict[str, str]] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 4:
            raise RealTexGateError("MiKTeX installed-package inventory is malformed")
        package_id, version, digest, installed = fields
        if re.fullmatch(r"[a-z0-9][a-z0-9+._-]*", package_id) is None or package_id in seen_ids:
            raise RealTexGateError("MiKTeX installed-package inventory has an invalid package ID")
        seen_ids.add(package_id)
        if len(version) > 100 or any(character in version for character in "\r\n\x00"):
            raise RealTexGateError("MiKTeX installed-package inventory has an invalid version")
        if re.fullmatch(r"[0-9a-fA-F]{32}", digest) is None:
            raise RealTexGateError("MiKTeX installed-package inventory has an invalid digest")
        state = installed.casefold()
        if state not in {"false", "true"}:
            raise RealTexGateError("MiKTeX installed-package inventory has an invalid state")
        if state == "true":
            installed_packages.append(
                {
                    "digest": digest.casefold(),
                    "id": package_id,
                    "version": version or "not-reported",
                }
            )
    if not installed_packages:
        raise RealTexGateError("MiKTeX installed-package inventory has no installed packages")
    return tuple(sorted(installed_packages, key=lambda package: package["id"]))


def required_miktex_package_evidence(
    inventory: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    by_id = {package["id"]: package for package in inventory}
    missing = [package_id for package_id in REQUIRED_MIKTEX_PACKAGES if package_id not in by_id]
    if missing:
        raise RealTexGateError(f"required MiKTeX package is not installed: {missing[0]}")
    return [dict(by_id[package_id]) for package_id in REQUIRED_MIKTEX_PACKAGES]


def miktex_package_inventory_evidence(
    inventory: Sequence[Mapping[str, str]],
) -> dict[str, str | int]:
    canonical = json.dumps(
        list(inventory),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return {
        "count": len(inventory),
        "format": PACKAGE_INVENTORY_FORMAT,
        "sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
    }


def require_unchanged_miktex_package_inventory(
    before: Sequence[Mapping[str, str]],
    after: Sequence[Mapping[str, str]],
) -> None:
    if before != after:
        raise RealTexGateError(
            "MiKTeX installed-package inventory changed during the real TeX test"
        )


def package_manifest_evidence() -> dict[str, str | int]:
    try:
        data = PACKAGE_MANIFEST.read_bytes()
    except OSError as exc:
        raise RealTexGateError("MiKTeX package manifest is unreadable") from exc
    if required_miktex_packages() != REQUIRED_MIKTEX_PACKAGES:
        raise RealTexGateError("MiKTeX package manifest changed during the gate")
    return {
        "count": len(REQUIRED_MIKTEX_PACKAGES),
        "path": ".github/actions/real-tex-gate/miktex-packages.txt",
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


def bootstrap_evidence() -> dict[str, str | None]:
    filename = os.environ.get("MIKTEX_SETUP_FILENAME")
    sha256 = os.environ.get("MIKTEX_SETUP_SHA256")
    in_github_actions = os.environ.get("GITHUB_ACTIONS", "").casefold() == "true"
    if in_github_actions or filename is not None or sha256 is not None:
        if filename != EXPECTED_SETUP_FILENAME or sha256 != EXPECTED_SETUP_SHA256:
            raise RealTexGateError("MiKTeX Setup Utility evidence is missing or mismatched")
        return {
            "filename": filename,
            "sha256": sha256,
            "source": "verified_official_setup_utility",
        }
    return {"filename": None, "sha256": None, "source": "preinstalled_local"}


def junit_summary(path: Path) -> dict[str, Any]:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise RealTexGateError("pytest did not produce valid JUnit XML") from exc
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    if not suites:
        raise RealTexGateError("JUnit XML contains no testsuite")
    counts = {
        key: sum(int(suite.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    cases = list(root.findall(".//testcase")) if root.tag != "testcase" else [root]
    names = [case.get("name", "") for case in cases]
    counts["selected_case_names"] = names
    return counts


def require_one_real_test(summary: dict[str, Any], returncode: int) -> None:
    expected = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    actual = {key: summary.get(key) for key in expected}
    names = summary.get("selected_case_names")
    if (
        returncode != 0
        or actual != expected
        or not isinstance(names, list)
        or len(names) != 1
        or "installed-latexmk-latexdiff" not in str(names[0])
    ):
        raise RealTexGateError(
            "real TeX pytest gate must contain exactly one installed-tools pass and zero skips: "
            + json.dumps({"returncode": returncode, **summary}, sort_keys=True)
        )


def write_json_exclusive(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> int:
    args = parse_args()
    if sys.platform != "win32":
        raise RealTexGateError("real TeX gate is supported only on Windows")
    output_dir = args.output_dir.resolve()
    try:
        relative = output_dir.relative_to(PROJECT_ROOT / "build")
    except ValueError as exc:
        raise RealTexGateError("output directory must remain inside repository build/") from exc
    if not relative.parts or output_dir.exists():
        raise RealTexGateError("real TeX output directory must be a new child of build/")
    output_dir.mkdir(parents=True)

    tool_versions = {
        name: first_version_line(name, arguments)
        for name, arguments in sorted(TOOL_VERSION_COMMANDS.items())
    }
    tex_resources = verify_tex_resources()
    package_inventory = installed_miktex_package_inventory()
    packages = required_miktex_package_evidence(package_inventory)
    package_inventory_evidence = miktex_package_inventory_evidence(package_inventory)
    junit_path = output_dir / "junit.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/test_e2e_public_roundtrip.py",
        "-k",
        "installed-latexmk-latexdiff",
        "-rA",
        f"--junitxml={junit_path.relative_to(PROJECT_ROOT).as_posix()}",
    ]
    result = run_captured(command, timeout=20 * 60)
    if result.stdout:
        print(result.stdout[-MAX_DIAGNOSTIC_CHARS:])
    if result.stderr:
        print(result.stderr[-MAX_DIAGNOSTIC_CHARS:], file=sys.stderr)
    summary = junit_summary(junit_path)
    require_one_real_test(summary, result.returncode)
    package_inventory_after = installed_miktex_package_inventory()
    require_unchanged_miktex_package_inventory(package_inventory, package_inventory_after)
    evidence = {
        "automatic_package_installation": "tex_engine_disabled_per_invocation",
        "bootstrap": bootstrap_evidence(),
        "miktex_installed_package_inventory": package_inventory_evidence,
        "miktex_packages": packages,
        "package_manifest": package_manifest_evidence(),
        "platform": "windows",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "pytest": summary,
        "schema_version": "latex-word-review-real-tex-gate-v3",
        "status": "pass",
        "tex_resources": tex_resources,
        "tool_versions": tool_versions,
    }
    write_json_exclusive(output_dir / "toolchain.json", evidence)
    print(json.dumps({"status": "pass", "pytest": summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RealTexGateError, UnicodeError, ValueError) as exc:
        print(f"real TeX gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
