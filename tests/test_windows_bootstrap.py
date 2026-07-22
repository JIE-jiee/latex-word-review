from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "Start-Latex-Word-Review.cmd"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-windows.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-source-bootstrap.yml"


def _ascii_text(path: Path) -> str:
    data = path.read_bytes()
    assert data.isascii()
    return data.decode("ascii")


def test_double_click_launcher_is_ascii_and_bounded() -> None:
    text = _ascii_text(LAUNCHER)
    assert "%~dp0scripts\\bootstrap-windows.ps1" in text
    assert "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" in text
    assert "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File" in text
    assert "%*" not in text
    assert "Invoke-Expression" not in text


def test_bootstrap_pins_and_verifies_upstream_assets() -> None:
    text = _ascii_text(BOOTSTRAP)
    assert "0.11.16" in text
    assert "uv-x86_64-pc-windows-msvc.zip" in text
    assert "dd9d6d6554bfab265bfa98aa8e8a406c5c3a7b97582f93de1f4d48d9154a0395" in text
    assert "c5a583d5f1f6d055fc1c32c87d8eceee90edc69a5b9af5da70811befdfc04880" in text
    assert "cpython-3.12.13-windows-x86_64-none" in text
    assert "24168aff2e7d93784c6a436124c4ebb79b076a4e289bde4902c08333507b71d0" in text
    assert "[System.IO.File]::Open(" in text
    assert "[System.Security.Cryptography.SHA256]::Create()" in text
    assert "$sha256.ComputeHash($stream)" in text
    assert "[System.BitConverter]::ToString($hash)" in text
    assert "Get-FileHash" not in text
    assert "uv archive contains an unexpected member" in text
    assert "AllowAutoRedirect = $true" in text
    assert "Tls12" in text


def test_bootstrap_uses_locked_production_environment() -> None:
    text = _ascii_text(BOOTSTRAP)
    assert '"lock", "--check"' in text
    assert '"sync", "--frozen"' in text
    assert '"--no-default-groups"' in text
    assert '"--extra", "pdf-figures"' in text
    assert '"--managed-python"' in text
    assert '"--no-python-downloads"' in text
    assert "$env:UV_PROJECT_ENVIRONMENT = $venvDirectory" in text
    assert '$env:UV_PYTHON_INSTALL_REGISTRY = "0"' in text
    assert "Scripts\\latex-word-review.exe" in text
    assert text.index("$ready = $false") < text.index("$uvExe = Get-VerifiedUv")
    assert "cache clean --cache-dir $cacheDirectory" in text
    assert "Remove-OwnedFile -Root $RuntimeRoot -Path $uvExe" in text
    assert "uv run" not in text.lower()


def test_bootstrap_has_no_pipe_to_shell_or_unpinned_installer() -> None:
    text = _ascii_text(BOOTSTRAP)
    forbidden = (
        "Invoke-Expression",
        "iex ",
        "install.ps1",
        "self update",
        "allow-insecure-host",
        "trusted-host",
    )
    for token in forbidden:
        assert token.lower() not in text.lower()
    assert not re.search(r"Invoke-WebRequest.*\|", text, flags=re.IGNORECASE)


def test_bootstrap_runtime_is_ignored_and_ci_exercises_source_archive() -> None:
    assert "/.lwr-runtime/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "git archive" in workflow
    assert "Start-Latex-Word-Review.cmd" in workflow
    assert "LATEX_WORD_REVIEW_BOOTSTRAP_PREPARE_ONLY" in workflow
    assert "UV_OFFLINE" in workflow
    assert "bootstrap-evidence.json" in workflow
