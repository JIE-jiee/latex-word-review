from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "latex-word-review.spec"
INNO = ROOT / "installer" / "latex-word-review.iss"
BUILD_SCRIPT = ROOT / "scripts" / "build-windows.ps1"
LOCAL_DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-local-windows.ps1"
VERIFY_SCRIPT = ROOT / "scripts" / "verify-windows-release.ps1"
INSTALLER_SMOKE_SCRIPT = ROOT / "scripts" / "test-windows-installer.ps1"
WINDOWS_CANDIDATE_WORKFLOW = ROOT / ".github" / "workflows" / "windows-app-candidate.yml"
CLEAN_INSTALL_SCRIPT = ROOT / ".github" / "scripts" / "clean_install.py"
CPYTHON_LICENSE_BUNDLE = ROOT / "third_party" / "cpython-3.12.13-license.rst"
SCHEMA_ROOT = ROOT / "src" / "latex_word_review" / "schemas" / "v1alpha"
VERSION = "0.2.0b1"
CPYTHON_LICENSE_SHA256 = "341832873fd316a37927e79385093fbbfd40a467428480835fe435a80cadf4e5"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_synthetic_release(root: Path) -> tuple[Path, Path, Path]:
    onedir = root / "dist" / "latex-word-review"
    internal = onedir / "_internal"
    package = internal / "latex_word_review"
    (package / "schemas" / "v1alpha").mkdir(parents=True)
    (package / "assets").mkdir(parents=True)
    (internal / "pypdfium2_raw").mkdir(parents=True)
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)

    (onedir / "latex-word-review.exe").write_bytes(b"MZ" + (b"app" * 1024))
    (onedir / "LatexWordReview.exe").write_bytes(b"MZ" + (b"gui" * 1024))
    (internal / "LICENSE").write_text("synthetic license\n", encoding="utf-8")
    (internal / "LICENSE.txt").write_text("synthetic Python license\n", encoding="utf-8")
    (internal / "THIRD_PARTY_NOTICES.md").write_text("synthetic notices\n", encoding="utf-8")
    cpython_licenses = internal / "licenses" / "cpython-3.12.13"
    cpython_licenses.mkdir(parents=True)
    shutil.copyfile(
        CPYTHON_LICENSE_BUNDLE,
        cpython_licenses / "cpython-3.12.13-license.rst",
    )
    for native_name in ("libcrypto-3-x64.dll", "libffi-8.dll", "libssl-3-x64.dll"):
        (internal / native_name).write_bytes(f"synthetic-{native_name}".encode())
    (package / "py.typed").write_bytes(b"")
    for schema_path in sorted(SCHEMA_ROOT.glob("*.json")):
        shutil.copyfile(schema_path, package / "schemas" / "v1alpha" / schema_path.name)
    (package / "assets" / "app.css").write_text("body{}\n", encoding="utf-8")
    (package / "assets" / "finalize_review_fields.ps1").write_text(
        "#Requires -Version 5.1\n",
        encoding="utf-8",
    )
    (internal / "pypdfium2_raw" / "pdfium.dll").write_bytes(b"synthetic-pdfium")
    (internal / "pypdfium2_raw" / "version.json").write_text(
        '{"major":1,"minor":2,"build":3,"patch":4}\n', encoding="utf-8"
    )
    metadata_licenses = {
        "attrs-26.1.0.dist-info": ("licenses/LICENSE",),
        "jsonschema-4.26.0.dist-info": ("licenses/COPYING",),
        "jsonschema_specifications-2025.9.1.dist-info": ("licenses/COPYING",),
        "latex_word_review-0.2.0b1.dist-info": ("licenses/LICENSE",),
        "lxml-6.1.1.dist-info": ("licenses/LICENSE.txt", "licenses/LICENSES.txt"),
        "pillow-12.3.0.dist-info": ("licenses/LICENSE",),
        "pylatexenc-2.10.dist-info": ("licenses/LICENSE.txt",),
        "pypdfium2-5.12.0.dist-info": (
            "licenses/LICENSES/Apache-2.0.txt",
            "licenses/LICENSES/BSD-3-Clause.txt",
            "licenses/LICENSES/CC-BY-4.0.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/abseil.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/agg23.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/fast_float.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/freetype.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/icu.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/lcms.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/libjpeg_turbo.ijg",
            "licenses/data/windows_x64/BUILD_LICENSES/libjpeg_turbo.md",
            "licenses/data/windows_x64/BUILD_LICENSES/libopenjpeg.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/libpng.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/libtiff.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/llvm-libc.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/pdfium.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/pdfium-binaries.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/simdutf.txt",
            "licenses/data/windows_x64/BUILD_LICENSES/zlib.txt",
        ),
        "referencing-0.37.0.dist-info": ("licenses/COPYING",),
        "regex-2026.7.10.dist-info": ("licenses/LICENSE.txt",),
        "rfc8785-0.1.4.dist-info": ("LICENSE",),
        "rpds_py-2026.6.3.dist-info": ("licenses/LICENSE",),
        "tex2word-1.0.5.dist-info": ("licenses/LICENSE",),
        "typing_extensions-4.16.0.dist-info": ("licenses/LICENSE",),
    }
    for metadata_name, license_paths in metadata_licenses.items():
        metadata = internal / metadata_name
        metadata.mkdir()
        metadata_text = "Metadata-Version: 2.4\n"
        if metadata_name.startswith("latex_word_review-"):
            metadata_text += f"Name: latex-word-review\nVersion: {VERSION}\n"
        (metadata / "METADATA").write_text(metadata_text, encoding="utf-8")
        for relative_license in license_paths:
            license_path = metadata / relative_license
            license_path.parent.mkdir(parents=True, exist_ok=True)
            license_path.write_text("synthetic license evidence\n", encoding="utf-8")

    content_lines = []
    for path in sorted(item for item in onedir.rglob("*") if item.is_file()):
        relative = path.relative_to(onedir).as_posix()
        content_lines.append(f"{_sha256(path)} *{relative}")
    (onedir / "CONTENTS.sha256").write_text(
        "\n".join(content_lines) + "\n", encoding="utf-8", newline="\n"
    )

    portable = artifacts / f"latex-word-review-{VERSION}-windows-x64-portable.zip"
    with zipfile.ZipFile(portable, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in onedir.rglob("*") if item.is_file()):
            relative = path.relative_to(onedir).as_posix()
            archive.write(path, f"latex-word-review/{relative}")

    setup = artifacts / f"latex-word-review-{VERSION}-windows-x64-setup.exe"
    setup.write_bytes(b"MZ" + (b"setup" * 1024))
    (artifacts / "SHA256SUMS.txt").write_text(
        f"{_sha256(portable)} *{portable.name}\n{_sha256(setup)} *{setup.name}\n",
        encoding="utf-8",
        newline="\n",
    )
    return onedir, portable, setup


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        pytest.skip("Windows PowerShell is unavailable")
    return executable


def _run_verifier(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(VERIFY_SCRIPT),
            "-ReleaseRoot",
            os.fspath(root),
            "-Version",
            VERSION,
            "-SkipExecutableSmokeTest",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def _process_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


def test_process_output_tolerates_absent_optional_stream() -> None:
    result = subprocess.CompletedProcess(["powershell"], 1, stdout="diagnostic", stderr=None)
    assert _process_output(result) == "diagnostic"


def test_pyinstaller_recipe_is_onedir_and_collects_required_runtime_data() -> None:
    recipe = _text(SPEC)

    compile(recipe, os.fspath(SPEC), "exec")
    assert 'name="latex-word-review"' in recipe
    assert 'name="LatexWordReview"' in recipe
    assert "console=True" in recipe
    assert "console=False" in recipe
    assert recipe.count("disable_windowed_traceback=True") == 1
    assert recipe.count("disable_windowed_traceback=False") == 1
    assert "COLLECT(" in recipe
    assert "exclude_binaries=True" in recipe
    assert '"schemas/**/*.json"' in recipe
    assert '"assets/**/*"' in recipe
    assert 'distribution_version("latex-word-review")' in recipe
    assert 'PROJECT_ROOT / "LICENSE"' in recipe
    assert 'PROJECT_ROOT / "THIRD_PARTY_NOTICES.md"' in recipe
    assert '"cpython-3.12.13-license.rst"' in recipe
    assert CPYTHON_LICENSE_SHA256 in recipe
    assert "EXPECTED_CPYTHON = (3, 12, 13)" in recipe
    assert "copy_metadata" in recipe
    for distribution in (
        "attrs",
        "jsonschema",
        "jsonschema-specifications",
        "latex-word-review",
        "lxml",
        "Pillow",
        "pylatexenc",
        "pypdfium2",
        "referencing",
        "regex",
        "rfc8785",
        "rpds-py",
        "tex2word",
        "typing-extensions",
    ):
        assert f'"{distribution}"' in recipe
    for package in ("tex2word", "PIL", "pypdfium2", "pypdfium2_raw"):
        assert package in recipe
    for excluded in (
        "certifi",
        "charset_normalizer",
        "idna",
        "playwright",
        "pypandoc",
        "pytest",
        "pip",
        "requests",
        "urllib3",
    ):
        assert f'"{excluded}"' in recipe
    assert "upx=False" in recipe


def test_inno_recipe_is_fixed_per_user_and_has_one_clear_gui_entry() -> None:
    recipe = _text(INNO)
    icons = recipe.split("[Icons]", maxsplit=1)[1].split("[Run]", maxsplit=1)[0]
    run = recipe.split("[Run]", maxsplit=1)[1]

    assert "AppId={{FA0A89F6-357A-4C99-879A-A49B4576BB77}" in recipe
    assert "DefaultDirName={localappdata}\\Programs\\LatexWordReview" in recipe
    assert "PrivilegesRequired=lowest" in recipe
    assert '#define AppExeName "LatexWordReview.exe"' in recipe
    assert "ArchitecturesAllowed=x64compatible" in recipe
    assert "Uninstallable=yes" in recipe
    assert "CreateUninstallRegKey=yes" in recipe
    assert 'Name: "{autoprograms}\\{#AppName}"' in recipe
    assert 'Name: "{autodesktop}\\{#AppName}"' in recipe
    assert 'Name: "desktopicon"' in recipe
    assert "checkedonce" in recipe
    assert "latex-word-review.exe" not in icons
    assert 'Source: "{#SourceDir}\\*"' in recipe
    assert "LicenseFile={#SourceDir}\\_internal\\LICENSE" in recipe
    assert "InfoAfterFile={#SourceDir}\\_internal\\THIRD_PARTY_NOTICES.md" in recipe
    assert 'Filename: "{app}\\{#AppExeName}"' in run
    for flag in ("nowait", "postinstall", "skipifsilent"):
        assert flag in run


def test_build_script_uses_one_onedir_and_is_offline_fail_closed() -> None:
    script = _text(BUILD_SCRIPT)
    lower = script.lower()

    assert "New-PortableArchive -SourceDirectory $onedir" in script
    assert '"/DSourceDir=$onedir"' in script
    assert "verify-windows-release.ps1" in script
    assert "CONTENTS.sha256" in script
    assert "SHA256SUMS.txt" in script
    assert ".latex-word-review-build-root" in script
    assert "ExpectedPyInstallerVersion" in script
    assert "InnoTimeoutSeconds = 300" in script
    assert "$isccBuild.WaitForExit($InnoTimeoutSeconds * 1000)" in script
    assert "Stop-Process -Id $owned.Id -Force" in script
    assert "-RedirectStandardOutput $isccOutput" in script
    assert "-RedirectStandardError $isccError" in script
    assert "& $isccExe @isccArguments" not in script
    assert "[AllowEmptyString()][string]$Requested" in script
    assert '$isccProbeInfo.Arguments = "/?"' in script
    assert "Inno Setup (?<major>[0-9]+) Command-Line Compiler" in script
    assert "exact 64-bit CPython 3.12.13" in script
    assert "'micro':sys.version_info.micro" in script
    assert "$installedProjectVersion" in script
    assert "installed project metadata probe" in script
    assert "$installedReadmeMatches" in script
    assert "installed project README metadata probe" in script
    assert "stale latex-word-review README metadata" in script
    for forbidden in (
        "invoke-webrequest",
        "start-bitstransfer",
        "winget install",
        "choco install",
        "pip install",
        "/verysilent",
        "/silent",
    ):
        assert forbidden not in lower


def test_local_deploy_script_is_atomic_hash_bound_and_preserves_user_data() -> None:
    script = _text(LOCAL_DEPLOY_SCRIPT)
    lower = script.lower()

    assert script.isascii()
    assert '"output\\local-windows"' in script
    assert '"windows-release\\dist\\latex-word-review"' in script
    assert "Assert-AppManifest -AppRoot $candidate" in script
    assert "CONTENTS.sha256" in script
    assert "SHA256SUMS.txt" in script
    assert "[IO.Directory]::Move($appRoot, $rollback)" in script
    assert "[IO.Directory]::Move($stage, $appRoot)" in script
    assert "[IO.File]::Replace" in script
    assert '"user-data"' in script
    assert "user_data_preserved = $true" in script
    assert "Get-Process -Name" in script
    assert "LatexWordReview" in script
    assert "latex-word-review" in script
    assert "reparse point" in lower
    assert "unexpected entry" in lower
    for forbidden in (
        "invoke-webrequest",
        "start-process",
        "start-bitstransfer",
        "winget",
        "reg.exe",
        "remove-item -force -recurse",
    ):
        assert forbidden not in lower


def test_installer_smoke_starts_and_gracefully_stops_frozen_gui() -> None:
    script = _text(INSTALLER_SMOKE_SCRIPT)

    assert '"LatexWordReview.exe"' in script
    assert '"--no-browser"' in script
    assert "http://127.0.0.1:$guiPort" in script
    assert 'Uri "$origin/app/exit"' in script
    assert "installed_gui_http_status" in script
    assert "installed_gui_exit_code" in script


def test_windows_app_candidate_workflow_is_pinned_minimal_and_never_publishes() -> None:
    workflow = _text(WINDOWS_CANDIDATE_WORKFLOW)
    lower = workflow.lower()
    trigger_block = workflow.split("\npermissions:", maxsplit=1)[0]
    upload_block = workflow.split("uses: actions/upload-artifact@", maxsplit=1)[1]

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow
    assert "push:" not in trigger_block
    assert "tags:" not in trigger_block
    assert "release:" not in trigger_block
    assert "pull_request_target:" not in trigger_block
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "uv sync --frozen --no-default-groups --group release" in workflow
    assert "actions/setup-python@" not in workflow
    assert 'UV_MANAGED_PYTHON: "1"' in workflow
    assert 'EXACT_PYTHON_VERSION: "3.12.13"' in workflow
    assert workflow.count('uv python install "$env:EXACT_PYTHON_VERSION"') == 2
    assert workflow.count("uv python find --managed-python --no-project") == 2
    assert workflow.count("$uvIdentity = ((uv --version) | Out-String).Trim()") == 2
    assert workflow.count('"ACTUAL_UV_IDENTITY=$uvIdentity"') == 2
    assert workflow.count('"PYTHON_MANAGED_INSTALL_KEY=$installKey"') == 2
    assert workflow.count('"PYTHON_EXECUTABLE_SHA256=$pythonSha256"') == 2
    assert workflow.count('$identity -cne "$env:EXACT_PYTHON_VERSION|64|cpython"') == 2
    assert workflow.count("$identityCode = `") == 2
    assert workflow.count("$identity = ((& $pythonPath -c $identityCode)") == 2
    assert workflow.count("struct.calcsize('P')") == 2
    assert 'struct.calcsize("P")' not in workflow
    assert '--extra pdf-figures --python "$env:EXACT_PYTHON"' in workflow
    assert '--python "$env:EXACT_PYTHON" pytest tests/test_app_browser_e2e.py' in workflow
    assert "third_party/cpython-3.12.13-license.rst" in workflow
    assert "--all-groups" not in workflow
    assert "--all-extras" not in workflow
    assert 'PYINSTALLER_VERSION: "6.21.0"' in workflow
    assert 'INNO_SETUP_VERSION: "7.0.2"' in workflow
    assert "releases/download/is-7_0_2/innosetup-7.0.2-x64.exe" in workflow
    assert "5ad54ca3def786f8f4212552e54cc6d8d61329e2d24a1cfee0571d42c2684ff1" in workflow
    assert "Get-AuthenticodeSignature" in workflow
    assert "CN=Pyrsys B\\.V\\." in workflow
    assert "Start-Process -FilePath $installer -ArgumentList @(" in workflow
    assert ") -Wait -PassThru -WindowStyle Hidden" in workflow
    assert "if ($installProcess.ExitCode -ne 0)" in workflow
    assert '& $installer "/VERYSILENT"' not in workflow
    assert '"/PORTABLE=1"' in workflow
    assert '"/DIR=`"$innoRoot`""' in workflow
    assert "Inno Setup 7 Command-Line Compiler" in workflow
    assert ".\\scripts\\build-windows.ps1" in workflow
    assert ".\\scripts\\test-windows-installer.ps1" in workflow
    assert ".\\scripts\\verify-windows-release.ps1" in workflow
    assert workflow.index(".\\scripts\\build-windows.ps1") < workflow.index(
        ".\\scripts\\verify-windows-release.ps1"
    )
    assert workflow.index(".\\scripts\\verify-windows-release.ps1") < workflow.index(
        ".\\scripts\\test-windows-installer.ps1"
    )
    assert workflow.index(".\\scripts\\test-windows-installer.ps1") < workflow.index(
        "$summary = [ordered]@{"
    )
    assert "--include-untracked" in workflow
    assert "retention-days: 7" in workflow
    assert 'distribution_gate = "blocked_pending_native_dependency_license_compliance"' in (
        workflow
    )
    assert "lxml_windows_wheel_native_license_and_relinking_evidence_incomplete" in workflow
    assert "binaries_uploaded = $false" in workflow
    assert "provisioning_policy = (" in workflow
    assert "uv documents Astral" in workflow
    assert "https://docs.astral.sh/uv/concepts/python-versions/" in workflow
    assert "https://github.com/astral-sh/python-build-standalone" in workflow
    assert 'provisioner = "uv"' in workflow
    assert "actual_uv_identity = $env:ACTUAL_UV_IDENTITY" in workflow
    assert "managed_install_key = $env:PYTHON_MANAGED_INSTALL_KEY" in workflow
    assert 'executable_sha256 = "sha256:$env:PYTHON_EXECUTABLE_SHA256"' in workflow
    assert "download_archive_digest_recorded = $false" in workflow
    assert "if-no-files-found: error" in upload_block
    assert "name: windows-app-build-evidence-${{ github.sha }}" in upload_block
    assert "build/windows-app-build-evidence/candidate-summary.json" in upload_block
    assert "build/windows-app-build-evidence/CONTENTS.sha256" in upload_block
    assert "build/windows-app-build-evidence/installer-smoke.json" in upload_block
    assert "build/windows-app-build-evidence/SHA256SUMS.txt" in upload_block
    assert ".zip" not in upload_block
    assert ".exe" not in upload_block
    assert "build/windows-release/artifacts" not in upload_block
    assert "build/windows-release/dist" not in upload_block
    assert "build/**" not in upload_block
    for dependency in ("citeproc", "latex2mathml", "matplotlib", "numpy"):
        assert dependency in workflow
    for forbidden in (
        "gh release",
        "actions/create-release",
        "softprops/action-gh-release",
        "--all-groups",
        "--all-extras",
    ):
        assert forbidden not in lower

    action_references = re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", workflow)
    assert action_references
    for reference in action_references:
        if reference.startswith("./"):
            continue
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), reference


def test_pinned_cpython_license_bundle_is_exact_and_covers_native_runtime() -> None:
    assert _sha256(CPYTHON_LICENSE_BUNDLE) == CPYTHON_LICENSE_SHA256
    text = _text(CPYTHON_LICENSE_BUNDLE)
    for heading in ("OpenSSL", "libffi", "expat", "libmpdec"):
        assert f"\n{heading}\n" in text
    verifier = _text(VERIFY_SCRIPT)
    for required in (
        "cpython-3.12.13-license.rst",
        "libcrypto-3-x64.dll",
        "libffi-8.dll",
        "libssl-3-x64.dll",
    ):
        assert required in verifier


def test_clean_install_retains_fresh_roots_without_path_mutation() -> None:
    script = _text(CLEAN_INSTALL_SCRIPT)
    tree = ast.parse(script)
    forbidden_by_module = {
        "os": {"remove", "removedirs", "rename", "renames", "replace", "rmdir", "unlink"},
        "shutil": {"move", "rmtree"},
    }
    module_aliases = {"os": "os", "shutil": "shutil"}
    imported_calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in forbidden_by_module:
                    module_aliases[imported.asname or imported.name] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module in forbidden_by_module:
            for imported in node.names:
                if imported.name in forbidden_by_module[node.module]:
                    imported_calls.add(imported.asname or imported.name)

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id in imported_calls:
            violations.append(function.id)
        elif isinstance(function, ast.Attribute):
            if function.attr in {"rename", "rmdir", "unlink"}:
                violations.append(function.attr)
            if isinstance(function.value, ast.Name):
                module = module_aliases.get(function.value.id)
                if module is not None and function.attr in forbidden_by_module[module]:
                    violations.append(f"{module}.{function.attr}")

    assert violations == []
    assert script.count(".replace(") == 1
    assert 'EXPECTED_PROJECT_NAME.replace("-", "_")' in script
    for native_delete in ("DeleteFileW", "MoveFileExW", "RemoveDirectoryW", "Remove-Item"):
        assert native_delete not in script
    assert "_cleanup_owned_path" not in script
    assert '"fresh_build_roots": "retained_until_runner_teardown"' in script
    assert "must not gain a path-deletion primitive" in script


def test_release_verifier_hashes_without_powershell_module_autoloading() -> None:
    verifier = _text(VERIFY_SCRIPT)

    assert "Get-FileHash" not in verifier
    assert "[Security.Cryptography.SHA256]::Create()" in verifier
    assert "[IO.File]::Open(" in verifier
    assert "[IO.FileShare]::Read" in verifier
    assert "$sha256.Dispose()" in verifier
    assert "$stream.Dispose()" in verifier


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only release verifier")
def test_release_verifier_accepts_exact_synthetic_single_source_release(tmp_path: Path) -> None:
    _write_synthetic_release(tmp_path)

    result = _run_verifier(tmp_path)

    assert result.returncode == 0, _process_output(result)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == "verified"
    assert payload["version"] == VERSION
    assert payload["executable_smoke_test"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only release verifier")
def test_release_verifier_rejects_missing_required_runtime_asset(tmp_path: Path) -> None:
    onedir, _, _ = _write_synthetic_release(tmp_path)
    asset = onedir / "_internal" / "latex_word_review" / "assets" / "finalize_review_fields.ps1"
    asset.unlink()

    result = _run_verifier(tmp_path)

    assert result.returncode != 0
    assert "required bundled data is missing" in _process_output(result).lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only release verifier")
def test_release_verifier_rejects_rogue_runtime_asset(tmp_path: Path) -> None:
    onedir, _, _ = _write_synthetic_release(tmp_path)
    rogue = onedir / "_internal" / "latex_word_review" / "assets" / "rogue.ps1"
    rogue.write_text("Write-Output 'must be rejected'\n", encoding="utf-8")

    result = _run_verifier(tmp_path)

    assert result.returncode != 0
    assert "assets differ from the required exact set" in _process_output(result).lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only release verifier")
def test_release_verifier_rejects_missing_cataloged_schema(tmp_path: Path) -> None:
    onedir, _, _ = _write_synthetic_release(tmp_path)
    schema = (
        onedir
        / "_internal"
        / "latex_word_review"
        / "schemas"
        / "v1alpha"
        / "change-set.schema.json"
    )
    schema.unlink()

    result = _run_verifier(tmp_path)

    assert result.returncode != 0
    assert "schema payload set differs" in _process_output(result).lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only release verifier")
def test_release_verifier_rejects_rogue_schema(tmp_path: Path) -> None:
    onedir, _, _ = _write_synthetic_release(tmp_path)
    rogue = onedir / "_internal" / "latex_word_review" / "schemas" / "v1alpha" / "rogue.schema.json"
    rogue.write_text("{}\n", encoding="utf-8")

    result = _run_verifier(tmp_path)

    assert result.returncode != 0
    assert "schema payload set differs" in _process_output(result).lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only release verifier")
def test_release_verifier_rejects_stale_project_distribution_metadata(
    tmp_path: Path,
) -> None:
    onedir, _, _ = _write_synthetic_release(tmp_path)
    metadata = onedir / "_internal" / f"latex_word_review-{VERSION}.dist-info" / "METADATA"
    metadata.write_text(
        "Metadata-Version: 2.4\nName: latex-word-review\nVersion: 0.1.0b2\n",
        encoding="utf-8",
    )

    result = _run_verifier(tmp_path)

    assert result.returncode != 0
    assert "metadata name or version" in _process_output(result).lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only release verifier")
def test_release_verifier_rejects_onedir_content_tampering(tmp_path: Path) -> None:
    onedir, _, _ = _write_synthetic_release(tmp_path)
    (onedir / "_internal" / "latex_word_review" / "assets" / "app.css").write_text(
        "tampered{}\n", encoding="utf-8"
    )

    result = _run_verifier(tmp_path)

    assert result.returncode != 0
    assert "content hash mismatch" in _process_output(result).lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only release verifier")
def test_release_verifier_rejects_portable_extra_even_with_updated_outer_hash(
    tmp_path: Path,
) -> None:
    _, portable, setup = _write_synthetic_release(tmp_path)
    with zipfile.ZipFile(portable, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("latex-word-review/unexpected.txt", b"unexpected")
    (portable.parent / "SHA256SUMS.txt").write_text(
        f"{_sha256(portable)} *{portable.name}\n{_sha256(setup)} *{setup.name}\n",
        encoding="utf-8",
        newline="\n",
    )

    result = _run_verifier(tmp_path)

    assert result.returncode != 0
    assert "unexpected entry" in _process_output(result).lower()
