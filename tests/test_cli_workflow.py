"""CLI integration through immutable snapshot, export, archive, and ingest."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from latex_word_review.cli import main
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.jsonio import read_contract_file, write_new_json
from tests.e2e_helpers import create_whole_bookmark_replacement
from tests.test_contracts import _golden_contracts
from tests.test_ledger_bundle import _seal_verification_extensions

FIXTURE = Path(__file__).parent / "fixtures/e0-minimal-paper"
RUN_ID = "run_019b0000-0000-7000-8000-000000000001"
TIME = "2026-07-16T15:00:00+09:00"


def test_cli_builds_a_hash_bound_ingest_chain_without_mutating_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    origin = tmp_path / "origin"
    returned = tmp_path / "received.docx"
    shutil.copytree(FIXTURE / "source", origin)
    origin_before = {
        path.relative_to(origin): path.read_bytes() for path in origin.rglob("*") if path.is_file()
    }
    run = tmp_path / "run"
    snapshot = run / "snapshot"
    source_manifest_path = run / "objects/source-manifest.json"

    assert (
        main(
            [
                "snapshot",
                str(origin),
                str(snapshot),
                "--main",
                "main.tex",
                "--run-id",
                RUN_ID,
                "--manifest-out",
                str(source_manifest_path),
                "--confidentiality",
                "public_fixture",
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    assert "source_manifest_payload_sha256" in capsys.readouterr().out

    review = run / "export/review.docx"
    export_objects = run / "export-objects"
    assert (
        main(
            [
                "export",
                str(snapshot),
                str(source_manifest_path),
                str(review),
                str(export_objects),
                "--confidentiality",
                "public_fixture",
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    export_stdout = capsys.readouterr().out
    assert '"status":"success"' in export_stdout
    source_map_path = export_objects / "source-map.json"
    source_map = read_contract_file(source_map_path, expected_schema="SourceMap")
    exported_sha = cast("str", cast("dict[str, Any]", source_map["payload"])["review_docx_sha256"])
    mapping = next(
        item
        for item in cast("list[dict[str, Any]]", source_map["payload"]["mappings"])
        if item["status"] == "exact"
    )
    location = cast("dict[str, Any]", mapping["source_location"])
    source_bytes = (snapshot / cast("str", location["path"])).read_bytes()
    start = cast("int", location["start_byte"])
    end = cast("int", location["end_byte"])
    before = source_bytes[start:end].decode("utf-8")
    after = before.replace(" is ", " is carefully ", 1)
    if after == before:
        after = before + " reviewed"
    bookmark_name = cast("str", cast("dict[str, Any]", mapping["docx_anchor"])["name"])
    create_whole_bookmark_replacement(
        review,
        returned,
        bookmark_name=bookmark_name,
        before=before,
        after=after,
    )
    returned_before = returned.read_bytes()

    reader_path = run / "objects/revision-reader.json"
    assert (
        main(
            [
                "reader-capabilities",
                str(reader_path),
                "--run-id",
                RUN_ID,
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    capsys.readouterr()

    archive = run / "returned"
    assert (
        main(
            [
                "archive",
                str(returned),
                str(archive),
                "--run-id",
                RUN_ID,
                "--exported-docx-sha256",
                exported_sha,
                "--confidentiality",
                "public_fixture",
            ]
        )
        == 0
    )
    capsys.readouterr()

    changeset_path = run / "objects/changeset.json"
    assert (
        main(
            [
                "ingest",
                str(archive),
                str(source_manifest_path),
                str(source_map_path),
                str(reader_path),
                str(changeset_path),
                "--baseline-docx",
                str(review),
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    assert "changeset_payload_sha256" in capsys.readouterr().out
    changeset = read_contract_file(changeset_path, expected_schema="ChangeSet")
    assert changeset["run_id"] == RUN_ID
    assert cast("dict[str, Any]", changeset["payload"])["counts"]["changes"] == 1
    baseline = changeset["payload"]["baseline_verification"]
    assert baseline["status"] == "verified_for_text_patch"
    assert baseline["automatic_patch_scope"] == "plain_text_only"
    assert baseline["manual_integrity_review_required"] is True
    assert returned.read_bytes() == returned_before
    assert {
        path.relative_to(origin): path.read_bytes() for path in origin.rglob("*") if path.is_file()
    } == origin_before


def test_cli_renders_ledger_builds_run_manifest_and_verifies_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    documents = _seal_verification_extensions(_golden_contracts())
    objects = tmp_path / "objects"
    objects.mkdir()
    paths: dict[str, Path] = {}
    for name in (
        "SourceManifest",
        "BackendCapabilities",
        "ChangeSet",
        "ApprovalSet",
        "PatchPlan",
        "VerificationReport",
    ):
        path = objects / f"{name}.json"
        write_new_json(path, documents[name], contract=True)
        paths[name] = path

    ledger_dir = tmp_path / "ledger"
    assert (
        main(
            [
                "ledger",
                str(paths["ChangeSet"]),
                str(paths["ApprovalSet"]),
                str(paths["PatchPlan"]),
                str(paths["VerificationReport"]),
                str(ledger_dir),
            ]
        )
        == 0
    )
    assert "ledger_json_sha256" in capsys.readouterr().out
    assert (ledger_dir / "ledger.json").is_file()
    assert (ledger_dir / "ledger.html").is_file()

    run_manifest = ledger_dir / "run-manifest.json"
    run_args = [
        "run-manifest",
        str(paths["SourceManifest"]),
        str(run_manifest),
        "--generated-at",
        TIME,
        "--backend",
        str(paths["BackendCapabilities"]),
        "--artifact",
        "ledger.json",
        "review_ledger_json",
        "application/json",
        "public_fixture",
        "--artifact",
        "ledger.html",
        "review_ledger_html",
        "text/html",
        "public_fixture",
    ]
    for name in ("ChangeSet", "ApprovalSet", "PatchPlan", "VerificationReport"):
        run_args.extend(("--object", str(paths[name])))
    assert main(run_args) == 0
    capsys.readouterr()
    read_contract_file(run_manifest, expected_schema="RunManifest")
    write_new_json(
        ledger_dir / "verification-report.json",
        documents["VerificationReport"],
        contract=True,
    )

    bundle = tmp_path / "audit.zip"
    assert (
        main(
            [
                "bundle",
                str(ledger_dir),
                str(bundle),
                str(run_manifest),
                str(paths["VerificationReport"]),
                "--item",
                "ledger.json",
                "review_ledger_json",
                str(run_manifest),
                "--item",
                "ledger.html",
                "review_ledger_html",
                str(run_manifest),
                "--item",
                "run-manifest.json",
                "run_manifest",
                str(run_manifest),
                "--item",
                "verification-report.json",
                "verification_report",
                str(paths["VerificationReport"]),
                "--classification",
                "public_fixture",
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    assert "audit_bundle_sha256" in capsys.readouterr().out
    assert (
        main(
            [
                "verify-bundle",
                str(bundle),
                "--run-manifest",
                str(run_manifest),
                "--verification",
                str(paths["VerificationReport"]),
            ]
        )
        == 0
    )
    assert '"status":"pass"' in capsys.readouterr().out


def test_cli_run_manifest_artifact_path_duplicate_and_drift_fail_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _golden_contracts()
    source_manifest = tmp_path / "source-manifest.json"
    write_new_json(source_manifest, documents["SourceManifest"], contract=True)
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "ledger.json").write_bytes(b"{}\n")

    unsafe_output = run_root / "unsafe.json"
    assert (
        main(
            [
                "run-manifest",
                str(source_manifest),
                str(unsafe_output),
                "--artifact",
                "../ledger.json",
                "ledger",
                "application/json",
                "public_fixture",
            ]
        )
        != 0
    )
    assert "E_PATH_TRAVERSAL" in capsys.readouterr().err
    assert not unsafe_output.exists()

    duplicate_output = run_root / "duplicate.json"
    duplicate = [
        "--artifact",
        "ledger.json",
        "ledger",
        "application/json",
        "public_fixture",
    ]
    assert (
        main(
            [
                "run-manifest",
                str(source_manifest),
                str(duplicate_output),
                *duplicate,
                *duplicate,
            ]
        )
        != 0
    )
    assert "E_SCHEMA_INVALID" in capsys.readouterr().err
    assert not duplicate_output.exists()

    def drift(_path: Path, *, max_bytes: int) -> bytes:
        del max_bytes
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "synthetic artifact drift")

    monkeypatch.setattr("latex_word_review.cli.read_stable_bytes", drift)
    drift_output = run_root / "drift.json"
    assert (
        main(
            [
                "run-manifest",
                str(source_manifest),
                str(drift_output),
                *duplicate,
            ]
        )
        != 0
    )
    assert "E_HASH_SOURCE_MISMATCH" in capsys.readouterr().err
    assert not drift_output.exists()


def test_cli_run_manifest_artifact_link_escape_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    documents = _golden_contracts()
    source_manifest = tmp_path / "source-manifest.json"
    write_new_json(source_manifest, documents["SourceManifest"], contract=True)
    run_root = tmp_path / "run"
    run_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}\n")
    linked = run_root / "linked.json"
    try:
        os.symlink(outside, linked)
    except OSError:
        pytest.skip("host policy does not permit a real symlink")
    output = run_root / "manifest.json"
    assert (
        main(
            [
                "run-manifest",
                str(source_manifest),
                str(output),
                "--artifact",
                "linked.json",
                "ledger",
                "application/json",
                "public_fixture",
            ]
        )
        != 0
    )
    assert "E_PATH_LINK_ESCAPE" in capsys.readouterr().err
    assert not output.exists()
