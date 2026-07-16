#!/usr/bin/env python3
"""Run the installed-LaTeX E2E case and reject missing tools or any pytest skip."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_APT_PACKAGES = (
    "latexmk",
    "latexdiff",
    "texlive-lang-chinese",
    "texlive-latex-recommended",
    "texlive-xetex",
)
TOOL_VERSION_COMMANDS = {
    "latexmk": ("-version",),
    "latexdiff": ("--version",),
    "xelatex": ("--version",),
}
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
    version = next((line for line in lines if line), "")
    if not version or len(version) > 500 or any(character in version for character in "\r\n\x00"):
        raise RealTexGateError(f"invalid version output for {executable}")
    return version


def verify_ctex() -> None:
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich is None:
        raise RealTexGateError("required executable is missing: kpsewhich")
    result = run_captured([kpsewhich, "ctex.sty"], timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        raise RealTexGateError("kpsewhich could not resolve ctex.sty")


def installed_apt_packages() -> list[dict[str, str]]:
    dpkg_query = shutil.which("dpkg-query")
    if dpkg_query is None:
        raise RealTexGateError("dpkg-query is required to bind Ubuntu package versions")
    result = run_captured(
        [
            dpkg_query,
            "--show",
            "--showformat=${binary:Package}\t${Version}\\n",
            *REQUIRED_APT_PACKAGES,
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raise RealTexGateError("one or more required Ubuntu TeX packages are not installed")
    packages: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, separator, version = line.partition("\t")
        canonical = name.partition(":")[0]
        if not separator or canonical not in REQUIRED_APT_PACKAGES or not version:
            raise RealTexGateError("dpkg-query returned malformed package evidence")
        packages[canonical] = version
    if set(packages) != set(REQUIRED_APT_PACKAGES):
        raise RealTexGateError("Ubuntu package evidence is incomplete")
    return [{"name": name, "version": packages[name]} for name in sorted(packages)]


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
    verify_ctex()
    packages = installed_apt_packages()
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
        "apt_packages": packages,
        "ctex_sty": "resolved_by_kpsewhich",
        "platform": "ubuntu",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "pytest": summary,
        "schema_version": "latex-word-review-real-tex-gate-v1",
        "status": "pass",
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
