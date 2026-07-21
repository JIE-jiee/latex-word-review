from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_word_contract_qa.ps1"
FIELD_SCRIPT = REPO_ROOT / "src" / "latex_word_review" / "assets" / "finalize_review_fields.ps1"
PUBLIC_BASELINE = (
    REPO_ROOT / "tests" / "fixtures" / "e0-minimal-paper" / "base" / "review-base.docx"
)
WORDPROCESSINGML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _powershell() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe") or shutil.which("pwsh")


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _word_is_registered() -> bool:
    registry = shutil.which("reg.exe")
    if registry is None:
        return False
    result = subprocess.run(
        [registry, "query", r"HKCR\Word.Application\CLSID"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0


def _run_harness(
    baseline: Path,
    output: Path,
    *,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell 5.1 or 7 is not available")
    return subprocess.run(
        [
            shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-BaselineDocx",
            str(baseline),
            "-OutputDir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _document_xml(path: Path) -> ElementTree.Element:
    with ZipFile(path) as package:
        return ElementTree.fromstring(package.read("word/document.xml"))


def _tag_count(root: ElementTree.Element, local_name: str) -> int:
    return sum(1 for _ in root.iter(f"{{{WORDPROCESSINGML}}}{local_name}"))


def test_script_has_explicit_copy_only_and_com_cleanup_contract() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "[string] $BaselineDocx" in text
    assert "[string] $OutputDir" in text
    assert "Copy-Item -LiteralPath $BaselineFullPath" in text
    assert "Documents.Open($BaselineFullPath" not in text
    assert "already exists; refusing to overwrite" in text
    assert "New-Object -ComObject Word.Application" in text
    assert "$MsoAutomationSecurityForceDisable = 3" in text
    assert "$word.AutomationSecurity = $MsoAutomationSecurityForceDisable" in text
    assert "$word.DisplayAlerts = $WdAlertsNone" in text
    assert ".Close($WdDoNotSaveChanges)" in text
    save_as_call = (
        "$Document.SaveAs2(\n"
        "        $Destination,\n"
        "        $WdFormatXmlDocument,\n"
        "        [System.Type]::Missing,\n"
        "        [System.Type]::Missing,\n"
        "        $false\n"
        "    )"
    )
    assert save_as_call in text
    assert ".Save(" not in text

    snapshot_index = text.index("$winWordBefore = @(Get-WinWordProcessSnapshot)")
    create_index = text.index("$word = New-Object -ComObject Word.Application")
    identity_index = text.index(
        "$wordIdentity = Wait-ForOwnedWinWordIdentity -BeforeSnapshot $winWordBefore"
    )
    confirmed_index = text.index("$wordOwnershipConfirmed = $true")
    automation_security_index = text.index(
        "$word.AutomationSecurity = $MsoAutomationSecurityForceDisable"
    )
    assert snapshot_index < create_index < identity_index < confirmed_index
    assert confirmed_index < automation_security_index
    assert "started_filetime_utc" in text
    assert "StartTime.ToUniversalTime().ToFileTimeUtc()" in text
    assert "More than one new WINWORD process appeared; ownership is ambiguous." in text

    owned_close = text[
        text.index("function Close-OwnedWordApplication") : text.index(
            "function Close-WordDocument"
        )
    ]
    assert "if (-not $OwnershipConfirmed -or $null -eq $Identity)" in owned_close
    assert "Open-ExactWinWordProcess -Identity $Identity" in owned_close
    assert "$Word.Quit($WdDoNotSaveChanges)" in owned_close
    assert text.count(".Quit($WdDoNotSaveChanges)") == 1
    assert "Stop-ExactOwnedWinWordProcess -Identity $Identity" in owned_close
    assert "taskkill" not in text.casefold()
    assert "Stop-Process" not in text

    assert "$baselineHashAfter -cne $baselineHashBefore" in text
    assert "New-Object System.Text.UTF8Encoding($false)" in text
    assert "[System.IO.File]::WriteAllText" in text
    assert "[System.IO.Directory]::Move($StageRoot, $OutputFullPath)" in text
    assert "for ($attempt = 1; $attempt -le 30; $attempt++)" in text
    assert "Start-Sleep -Milliseconds 100" in text
    assert not re.search(r"(?m)^\s*Move-Item\b", text)

    assert '$ExtendedPathPrefix = "\\\\?\\"' in text
    assert '$DevicePathPrefix = "\\\\.\\"' in text
    assert '$NtPathPrefix = "\\??\\"' in text
    assert "Get-NormalWin32FullPath" in text
    assert "ordinary Win32 path" in text
    assert "$documents = $Word.Documents" in text
    assert "Release-ComObject -ComObject $documents" in text
    assert "function Get-WordCollectionCount" in text
    assert "Release-ComObject -ComObject $collection" in text
    assert "$comments = $Document.Comments" in text
    assert "Release-ComObject -ComObject $comments" in text
    assert "$bookmarks = $Document.Bookmarks" in text
    assert text.count(".ShowHidden = $true") == 2
    assert "Release-ComObject -ComObject $bookmarks" in text

    for case_id in (
        "roundtrip_unchanged",
        "tracked_review",
        "negative_untracked_drift",
        "negative_accepted_all",
        "negative_broken_bookmark",
    ):
        assert case_id in text

    for filename in (
        "roundtrip-unchanged.docx",
        "tracked-review.docx",
        "negative-untracked-drift.docx",
        "negative-accepted-all.docx",
        "negative-broken-bookmark.docx",
    ):
        assert filename in text

    remove_lines = [line.strip() for line in text.splitlines() if "Remove-Item" in line]
    assert remove_lines == [
        "Remove-Item -LiteralPath $workRoot -Recurse -Force",
        "Remove-Item -LiteralPath $stageRoot -Recurse -Force",
    ]
    assert not re.search(r"(?i)(?:^|[\"'])[a-z]:\\", text)
    assert "Latex论文" not in text
    assert "samples/private" not in text


@pytest.mark.parametrize(
    "script",
    (SCRIPT, FIELD_SCRIPT),
    ids=("contract-harness", "field-finalizer"),
)
def test_script_parses_in_powershell(script: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell 5.1 or 7 is not available")

    environment = os.environ.copy()
    environment["LWR_SCRIPT_TO_PARSE"] = str(script)
    command = (
        "$tokens = $null; $errors = $null; "
        "[void] [System.Management.Automation.Language.Parser]::ParseFile("
        "$env:LWR_SCRIPT_TO_PARSE, [ref] $tokens, [ref] $errors); "
        "if ($errors.Count -ne 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert result.returncode == 0, result.stderr


def test_existing_output_is_rejected_without_launching_word(tmp_path: Path) -> None:
    baseline = tmp_path / "synthetic.docx"
    baseline.write_bytes(b"not opened because output preflight fails")
    output = tmp_path / "existing-output"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    result = _run_harness(baseline, output, timeout=30)

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert sorted(output.iterdir()) == [marker]
    assert not list(tmp_path.glob(".lwr-word-contract-*.stage"))


@pytest.mark.parametrize(
    "unsafe_path",
    (
        r"\\?\C:\lwr-does-not-exist\baseline.docx",
        r"\\.\C:\lwr-does-not-exist\baseline.docx",
        r"\??\C:\lwr-does-not-exist\baseline.docx",
    ),
)
def test_device_paths_are_rejected_without_launching_word(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    output = tmp_path / "must-not-be-created"

    result = _run_harness(Path(unsafe_path), output, timeout=30)

    assert result.returncode != 0
    assert "ordinary Win32 path" in result.stderr
    assert not output.exists()
    assert not list(tmp_path.glob(".lwr-word-contract-*.stage"))


def test_real_word_contract_roundtrip_is_explicitly_opt_in(tmp_path: Path) -> None:
    if os.environ.get("LWR_RUN_WORD_COM_QA") != "1":
        pytest.skip("set LWR_RUN_WORD_COM_QA=1 to run Microsoft Word COM contract QA")
    if sys.platform != "win32":
        pytest.skip("Microsoft Word COM contract QA is Windows-only")
    if not _word_is_registered():
        pytest.skip("Microsoft Word desktop is not registered")

    baseline_hash = _sha256(PUBLIC_BASELINE)
    output = tmp_path / "word-contract-output"
    result = _run_harness(PUBLIC_BASELINE, output)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _sha256(PUBLIC_BASELINE) == baseline_hash

    manifest_path = output / "manifest.json"
    raw_manifest = manifest_path.read_bytes()
    assert not raw_manifest.startswith(b"\xef\xbb\xbf")
    manifest = cast(dict[str, object], json.loads(raw_manifest.decode("utf-8")))
    assert set(manifest) == {
        "schema_version",
        "status",
        "baseline_sha256",
        "word_version",
        "cases",
    }
    assert manifest["schema_version"] == "word-contract-qa/v1"
    assert manifest["status"] == "passed"
    assert manifest["baseline_sha256"] == baseline_hash
    assert isinstance(manifest["word_version"], str)
    assert manifest["word_version"]

    raw_cases = manifest["cases"]
    assert isinstance(raw_cases, list)
    cases: dict[str, dict[str, object]] = {}
    for raw_case in raw_cases:
        assert isinstance(raw_case, dict)
        case = cast(dict[str, object], raw_case)
        case_id = case["case_id"]
        relative_file = case["relative_file"]
        assert isinstance(case_id, str)
        assert isinstance(relative_file, str)
        assert set(case) == {
            "case_id",
            "status",
            "relative_file",
            "sha256",
            "revision_count",
            "comment_count",
            "bookmark_count",
        }
        assert Path(relative_file).name == relative_file
        assert "/" not in relative_file and "\\" not in relative_file
        assert case["status"] == "passed"
        case_path = output / relative_file
        assert case_path.is_file()
        assert case["sha256"] == _sha256(case_path)
        cases[case_id] = case

    assert set(cases) == {
        "roundtrip_unchanged",
        "tracked_review",
        "negative_untracked_drift",
        "negative_accepted_all",
        "negative_broken_bookmark",
    }
    assert not (output / "private-working-copies").exists()

    tracked_root = _document_xml(output / cast(str, cases["tracked_review"]["relative_file"]))
    assert _tag_count(tracked_root, "ins") >= 1
    assert _tag_count(tracked_root, "del") >= 1
    with ZipFile(output / cast(str, cases["tracked_review"]["relative_file"])) as package:
        assert "word/comments.xml" in package.namelist()

    for case_id in ("negative_untracked_drift", "negative_accepted_all"):
        root = _document_xml(output / cast(str, cases[case_id]["relative_file"]))
        assert _tag_count(root, "ins") == 0
        assert _tag_count(root, "del") == 0

    roundtrip_root = _document_xml(
        output / cast(str, cases["roundtrip_unchanged"]["relative_file"])
    )
    broken_root = _document_xml(
        output / cast(str, cases["negative_broken_bookmark"]["relative_file"])
    )
    assert _tag_count(broken_root, "bookmarkStart") == (
        _tag_count(roundtrip_root, "bookmarkStart") - 1
    )
