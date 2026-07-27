from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "Start-Latex-Word-Review.cmd"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-windows.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-source-bootstrap.yml"
READMES = (
    ROOT / "README.md",
    ROOT / "README.en.md",
    ROOT / "README.ja.md",
)


def _ascii_text(path: Path) -> str:
    data = path.read_bytes()
    assert data.isascii()
    return data.decode("ascii")


def test_double_click_launcher_is_utf8_bilingual_and_bounded() -> None:
    data = LAUNCHER.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.count(b"\r\n") == data.count(b"\n")
    assert b"\r" not in data.replace(b"\r\n", b"")
    text = data.decode("utf-8")

    assert "chcp 65001 >nul" in text
    assert r"%~dp0output\local-windows" in text
    assert r"%FROZEN_ROOT%\app\LatexWordReview.exe" in text
    assert r"%FROZEN_ROOT%\app\latex-word-review.exe" in text
    assert r"%FROZEN_ROOT%\app\CONTENTS.sha256" in text
    assert r"%FROZEN_ROOT%\SHA256SUMS.txt" in text
    assert r"%FROZEN_ROOT%\app\_internal" in text
    assert 'call "Start-Latex-Word-Review.cmd"' in text
    assert "%~dp0scripts\\bootstrap-windows.ps1" in text
    assert "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" in text
    assert "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File" in text
    for code in (
        "E_LAUNCHER_FROZEN_FAILED",
        "E_LAUNCHER_BOOTSTRAP_FAILED",
        "E_LAUNCHER_FILES_MISSING",
        "E_LAUNCHER_POWERSHELL_MISSING",
    ):
        assert text.count(f"[{code}]") == 2
    assert "启动未完成" in text
    assert "Startup did not finish" in text
    assert "项目文件不完整" in text
    assert "Project files are incomplete" in text
    assert text.index('call "Start-Latex-Word-Review.cmd"') < text.index('"%POWERSHELL%" -NoLogo')
    assert "%*" not in text
    assert "Invoke-Expression" not in text


def test_double_click_launcher_prefers_complete_local_frozen_app(tmp_path: Path) -> None:
    launcher = tmp_path / LAUNCHER.name
    launcher.write_bytes(LAUNCHER.read_bytes())
    frozen_root = tmp_path / "output" / "local-windows"
    app_root = frozen_root / "app"
    internal_root = app_root / "_internal"
    internal_root.mkdir(parents=True)
    (app_root / "LatexWordReview.exe").write_bytes(b"gui")
    (app_root / "latex-word-review.exe").write_bytes(b"cli")
    (app_root / "CONTENTS.sha256").write_text("manifest", encoding="ascii")
    (frozen_root / "SHA256SUMS.txt").write_text("sums", encoding="ascii")
    (frozen_root / LAUNCHER.name).write_bytes(
        b'@echo off\r\n>"%~dp0frozen-marker.txt" echo frozen\r\nexit /b 0\r\n'
    )

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0
    assert (frozen_root / "frozen-marker.txt").read_text(encoding="ascii").strip() == "frozen"
    assert not (tmp_path / "bootstrap-marker.txt").exists()


def test_double_click_launcher_falls_back_when_local_frozen_app_is_incomplete(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / LAUNCHER.name
    launcher.write_bytes(LAUNCHER.read_bytes())
    frozen_root = tmp_path / "output" / "local-windows"
    frozen_root.mkdir(parents=True)
    (frozen_root / LAUNCHER.name).write_bytes(b"@echo off\r\nexit /b 91\r\n")
    scripts_root = tmp_path / "scripts"
    scripts_root.mkdir()
    (scripts_root / "bootstrap-windows.ps1").write_text(
        "[System.IO.File]::WriteAllText("
        '(Join-Path (Split-Path -Parent $PSScriptRoot) "bootstrap-marker.txt"),'
        ' "source")\n',
        encoding="ascii",
    )

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0
    assert (tmp_path / "bootstrap-marker.txt").read_text(encoding="ascii") == "source"


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


def test_bootstrap_has_bilingual_status_and_stable_failure_codes() -> None:
    text = _ascii_text(BOOTSTRAP)

    assert "function Convert-FromUtf8Base64" in text
    assert "function Write-BilingualStatus" in text
    encoded_statuses = re.findall(r'-ChineseBase64 "([A-Za-z0-9+/=]+)"', text)
    assert len(encoded_statuses) == 10
    decoded_statuses = [
        base64.b64decode(value, validate=True).decode("utf-8") for value in encoded_statuses
    ]
    assert all(any(ord(character) > 127 for character in value) for value in decoded_statuses)
    chinese_messages = {
        "5q2j5Zyo5LiL6L295bm25qCh6aqM5ZCv5Yqo5bel5YW344CC": "正在下载并校验启动工具。",
        "5q2j5Zyo5YeG5aSH5LiT55SoIFB5dGhvbiDov5DooYznjq/looPjgII=": (
            "正在准备专用 Python 运行环境。"
        ),
        "5ZCv5Yqo5aSx6LSl44CC5Y6f5aeLIExhVGVYIOWSjCBXb3JkIOaWh+S7tuayoeacieiiq+S/ruaUueOAgg==": (
            "启动失败。原始 LaTeX 和 Word 文件没有被修改。"
        ),
    }
    for encoded, expected in chinese_messages.items():
        assert encoded in text
        assert base64.b64decode(encoded).decode("utf-8") == expected

    error_codes = (
        "E_BOOTSTRAP_PRECHECK",
        "E_BOOTSTRAP_UV_SETUP",
        "E_BOOTSTRAP_PYTHON_SETUP",
        "E_BOOTSTRAP_DEPENDENCY_SETUP",
        "E_BOOTSTRAP_APP_START",
    )
    for code in error_codes:
        assert f'"{code}"' in text
    assert "-Code $FailureCode" in text
    assert text.index('$FailureCode = "E_BOOTSTRAP_UV_SETUP"') < text.index(
        "$uvExe = Get-VerifiedUv"
    )
    assert text.index('$FailureCode = "E_BOOTSTRAP_APP_START"') < text.index(
        "Start-ReviewApplication `"
    )


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
    assert "Launch the prepared application and use the protected exit" in workflow
    assert "scripts\\bootstrap-windows.ps1" in workflow
    assert "WindowsPowerShell\\v1.0\\powershell.exe" in workflow
    assert "$homeResponse = $null" in workflow
    assert "$home = $null" not in workflow
    assert '"http://127.0.0.1:$port"' in workflow
    assert '"lwr_app_session_$port"' in workflow
    assert 'Origin = "http://127.0.0.1:1"' in workflow
    assert "Origin = $origin" in workflow
    assert '"Sec-Fetch-Site" = "same-origin"' in workflow
    assert '"$origin/app/exit"' in workflow
    assert "application_http_reachable = $true" in workflow
    assert "cross_origin_exit_rejected = $true" in workflow
    assert "protected_exit_passed = $true" in workflow


def test_public_readmes_frontload_the_exact_supported_windows_shell() -> None:
    required_fragments = {
        "README.md": (
            "仅支持 64 位 Windows 与 64 位 Windows PowerShell 5.1",
            "PowerShell 7",
            "32 位 Windows PowerShell",
        ),
        "README.en.md": (
            "64-bit Windows and 64-bit Windows PowerShell 5.1 only",
            "PowerShell 7",
            "32-bit Windows PowerShell",
        ),
        "README.ja.md": (
            "64 ビット版 Windows と 64 ビット版 Windows PowerShell 5.1 のみ",
            "PowerShell 7",
            "32 ビット版 Windows PowerShell",
        ),
    }

    for path in READMES:
        text = path.read_text(encoding="utf-8")
        first_section = text.split("\n## ", 1)[0]
        for fragment in required_fragments[path.name]:
            assert fragment in first_section, f"{path.name} does not frontload {fragment!r}"
