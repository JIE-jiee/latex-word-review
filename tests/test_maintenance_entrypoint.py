from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "scripts" / "check.ps1"
CODE_MAP = ROOT / "docs" / "architecture" / "code-map.md"


def _powershell() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe") or shutil.which("pwsh")


def test_maintenance_entrypoint_delegates_release_verification() -> None:
    script = CHECK_SCRIPT.read_text(encoding="utf-8")

    assert 'ValidateSet("Quick", "Full", "Release")' in script
    assert 'Join-Path $PSScriptRoot "verify-windows-release.ps1"' in script
    assert "build-windows.ps1" not in script
    assert "test-windows-installer.ps1" not in script
    assert "tests/test_shared_domain_layout.py" in script


def test_code_map_names_the_maintenance_profiles_and_dependency_direction() -> None:
    code_map = CODE_MAP.read_bytes()

    assert b"scripts\\check.ps1 -Profile Quick" in code_map
    assert b"scripts\\check.ps1 -Profile Full" in code_map
    assert b"scripts\\check.ps1 -Profile Release" in code_map
    assert b"ApplicationSession" in code_map
    assert b"workflow.py" in code_map


@pytest.mark.skipif(_powershell() is None, reason="PowerShell is unavailable")
def test_quick_profile_lists_focused_revision_tests_without_external_qa() -> None:
    shell = _powershell() or "powershell.exe"
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-File",
            str(CHECK_SCRIPT),
            "-Profile",
            "Quick",
            "-ListOnly",
            "-ChangedPath",
            "src/latex_word_review/revision_macros.py",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "tests/test_revision_macros.py" in result.stdout
    assert "tests/test_revision_display_integrity.py" in result.stdout
    assert "test_app_browser_e2e.py" not in result.stdout
    assert "test_word_contract_harness.py" not in result.stdout

    blocked_result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-File",
            str(CHECK_SCRIPT),
            "-Profile",
            "Quick",
            "-ListOnly",
            "-ChangedPath",
            "tests/test_app_browser_e2e.py",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert blocked_result.returncode == 0, blocked_result.stderr
    assert "pytest --no-cov tests/test_app_browser_e2e.py" not in blocked_result.stdout
    assert "tests/test_coverage_configuration.py" in blocked_result.stdout


@pytest.mark.skipif(_powershell() is None, reason="PowerShell is unavailable")
def test_quick_profile_rejects_changed_paths_outside_repository() -> None:
    shell = _powershell() or "powershell.exe"
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-File",
            str(CHECK_SCRIPT),
            "-Profile",
            "Quick",
            "-ListOnly",
            "-ChangedPath",
            "../outside.py",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "safe repository-relative paths" in result.stderr
