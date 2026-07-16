#!/usr/bin/env python3
"""Run the installed-LaTeX E2E case and reject missing tools or any pytest skip."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SETUP_FILENAME = "miktexsetup-5.5.0+1763023-x64.zip"
EXPECTED_SETUP_SHA256 = "0571e90f6d94353089b4f189fd82a532f9fe559a388c7e7f1102b14b3c1ae27d"
REQUIRED_MIKTEX_PACKAGES = (
    "amsmath",
    "booktabs",
    "ctex",
    "fandol",
    "graphics",
    "hyperref",
    "latexdiff",
    "latexmk",
    "xetex",
)
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
PACKAGE_INFO_TEMPLATE = "{id}\t{version}\t{digest}\t{isInstalled}\n"
MAX_DIAGNOSTIC_CHARS = 64 * 1024


class RealTexGateError(RuntimeError):
    """Raised when the real TeX release gate cannot prove one passing test."""


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
        "ctex_sty": "ctex.sty",
        "fandol_song_regular": "FandolSong-Regular.otf",
    }
    for filename in resources.values():
        result = run_captured([kpsewhich, filename], timeout=30)
        if result.returncode != 0 or not result.stdout.strip():
            raise RealTexGateError(f"kpsewhich could not resolve {filename}")
    return {label: "resolved_by_kpsewhich" for label in resources}


def installed_miktex_packages() -> list[dict[str, str]]:
    miktex = shutil.which("miktex")
    if miktex is None:
        raise RealTexGateError("miktex is required to bind Windows TeX package evidence")
    packages: list[dict[str, str]] = []
    for package_id in REQUIRED_MIKTEX_PACKAGES:
        result = run_captured(
            [
                miktex,
                "packages",
                "info",
                "--template",
                PACKAGE_INFO_TEMPLATE,
                package_id,
            ],
            timeout=60,
        )
        if result.returncode != 0:
            raise RealTexGateError(f"MiKTeX package query failed: {package_id}")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise RealTexGateError(f"MiKTeX package evidence is malformed: {package_id}")
        fields = lines[0].split("\t")
        if len(fields) != 4:
            raise RealTexGateError(f"MiKTeX package evidence is malformed: {package_id}")
        observed_id, version, digest, installed = fields
        if observed_id != package_id or installed.casefold() != "true":
            raise RealTexGateError(f"required MiKTeX package is not installed: {package_id}")
        if len(digest) != 32 or any(
            character not in "0123456789abcdefABCDEF" for character in digest
        ):
            raise RealTexGateError(f"MiKTeX package digest is invalid: {package_id}")
        if len(version) > 100 or any(character in version for character in "\r\n\x00"):
            raise RealTexGateError(f"MiKTeX package version is invalid: {package_id}")
        packages.append(
            {
                "digest": digest.casefold(),
                "id": package_id,
                "version": version or "not-reported",
            }
        )
    return packages


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
    packages = installed_miktex_packages()
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
    evidence = {
        "bootstrap": bootstrap_evidence(),
        "miktex_packages": packages,
        "platform": "windows",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "pytest": summary,
        "schema_version": "latex-word-review-real-tex-gate-v2",
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
