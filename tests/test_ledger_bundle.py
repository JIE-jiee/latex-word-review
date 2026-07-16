"""Ledger escaping/binding and deterministic audit-bundle safety tests."""

from __future__ import annotations

import copy
import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

from latex_word_review.bundle import (
    BundleItem,
    create_audit_bundle,
    object_binding,
    verify_audit_bundle,
)
from latex_word_review.canonical import (
    canonical_json,
    compute_payload_sha256,
    seal_envelope,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes
from latex_word_review.ids import derive_artifact_id
from latex_word_review.ledger import build_ledger
from tests.test_contracts import _golden_contracts

GENERATED_AT = "2026-07-16T15:00:00+09:00"
VERIFICATION_EXTENSION = "org.latex-word-review.verification"


def _seal_verification_extensions(
    documents: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    verification = copy.deepcopy(documents["VerificationReport"])
    verification["extensions"][VERIFICATION_EXTENSION] = {
        "changeset_payload_sha256": compute_payload_sha256(documents["ChangeSet"]),
        "approval_set_payload_sha256": compute_payload_sha256(documents["ApprovalSet"]),
        "patch_plan_payload_sha256": compute_payload_sha256(documents["PatchPlan"]),
        "source_manifest_payload_sha256": documents["ChangeSet"]["payload"][
            "source_manifest_sha256"
        ],
        "original_source_tree_sha256": verification["payload"]["original_source_pre_sha256"],
        "applied_source_tree_sha256": verification["payload"]["revised_source_manifest_sha256"],
        "policy_sha256": documents["PatchPlan"]["payload"]["policy_sha256"],
        "commands": [],
    }
    documents["VerificationReport"] = seal_envelope(verification)
    return documents


@pytest.fixture
def documents() -> dict[str, dict[str, Any]]:
    return _seal_verification_extensions(_golden_contracts())


def _rebind_after_changeset(
    documents: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    changeset = seal_envelope(documents["ChangeSet"])
    approval = copy.deepcopy(documents["ApprovalSet"])
    approval["payload"]["changeset_sha256"] = compute_payload_sha256(changeset)
    approval = seal_envelope(approval)
    patch = copy.deepcopy(documents["PatchPlan"])
    patch["payload"]["changeset_sha256"] = compute_payload_sha256(changeset)
    patch["payload"]["approval_set_sha256"] = compute_payload_sha256(approval)
    for condition in patch["payload"]["preconditions"]:
        if condition["kind"] == "changeset_payload_sha256_equals":
            condition["expected"] = compute_payload_sha256(changeset)
        elif condition["kind"] == "approval_payload_sha256_equals":
            condition["expected"] = compute_payload_sha256(approval)
    patch = seal_envelope(patch)
    verification = copy.deepcopy(documents["VerificationReport"])
    verification["payload"]["patch_plan_sha256"] = compute_payload_sha256(patch)
    documents.update(
        {
            "ChangeSet": changeset,
            "ApprovalSet": approval,
            "PatchPlan": patch,
            "VerificationReport": verification,
        }
    )
    return _seal_verification_extensions(documents)


def _ledger_files(
    root: Path, documents: dict[str, dict[str, Any]]
) -> tuple[Path, tuple[BundleItem, ...]]:
    ledger = build_ledger(
        documents["ChangeSet"],
        documents["ApprovalSet"],
        documents["PatchPlan"],
        documents["VerificationReport"],
    )
    artifacts = (
        _bundle_artifact("ledger.html", "review_ledger_html", ledger.html_bytes, "text/html"),
        _bundle_artifact(
            "ledger.json", "review_ledger_json", ledger.json_bytes, "application/json"
        ),
    )
    verification = documents["VerificationReport"]
    run_manifest = copy.deepcopy(documents["RunManifest"])
    run_manifest["payload"]["object_bindings"] = [object_binding(verification)]
    run_manifest["payload"]["artifacts"] = list(artifacts)
    run_manifest = seal_envelope(run_manifest)
    documents["RunManifest"] = run_manifest

    source = root / "payload"
    source.mkdir()
    (source / "ledger.json").write_bytes(ledger.json_bytes)
    (source / "ledger.html").write_bytes(ledger.html_bytes)
    (source / "run-manifest.json").write_bytes(canonical_json(run_manifest) + b"\n")
    (source / "verification-report.json").write_bytes(canonical_json(verification) + b"\n")
    (source / "not-allowlisted-private.txt").write_bytes(b"must never enter the bundle")
    allowlist = (
        BundleItem(
            "ledger.html",
            "review_ledger_html",
            run_manifest,
            artifacts[0]["artifact_id"],
        ),
        BundleItem(
            "ledger.json",
            "review_ledger_json",
            run_manifest,
            artifacts[1]["artifact_id"],
        ),
        BundleItem("run-manifest.json", "run_manifest", run_manifest, "$document"),
        BundleItem(
            "verification-report.json",
            "verification_report",
            verification,
            "$document",
        ),
    )
    return source, allowlist


def _bundle_artifact(
    path: str,
    role: str,
    data: bytes,
    media_type: str = "application/octet-stream",
) -> dict[str, Any]:
    digest = digest_bytes(data)
    return {
        "artifact_id": derive_artifact_id(digest.sha256),
        "path": path,
        "path_base": "bundle_root",
        "role": role,
        "media_type": media_type,
        "size_bytes": digest.size_bytes,
        "sha256": digest.sha256,
        "immutable": True,
        "confidentiality": "public_fixture",
    }


def test_ledger_is_deterministic_complete_and_utf8(
    documents: dict[str, dict[str, Any]],
) -> None:
    first = build_ledger(
        documents["ChangeSet"],
        documents["ApprovalSet"],
        documents["PatchPlan"],
        documents["VerificationReport"],
    )
    second = build_ledger(
        documents["ChangeSet"],
        documents["ApprovalSet"],
        documents["PatchPlan"],
        documents["VerificationReport"],
    )
    assert first.json_bytes == second.json_bytes
    assert first.html_bytes == second.html_bytes
    assert first.json_bytes == canonical_json(first.document) + b"\n"
    assert first.document["status"] == {
        "approval": "final",
        "patch_plan": "ready",
        "verification": "pass",
    }
    change = first.document["changes"][0]
    assert change["author"] == "Synthetic Reviewer"
    assert change["timestamp"] == "2026-01-16T09:00:00+08:00"
    assert change["decision"]["decision"] == "accepted"
    assert change["status"] == "applied_verified"
    assert change["evidence"]["raw_event_count"] == 1
    assert change["evidence"]["events"][0]["fragment_sha256"].startswith("sha256:")
    assert '<meta charset="utf-8">' in first.html_bytes.decode("utf-8")
    assert "<script" not in first.html_bytes.decode("utf-8").casefold()


def test_ledger_escapes_every_dynamic_html_field(
    documents: dict[str, dict[str, Any]],
) -> None:
    malicious_author = '<img src=x onerror="alert(1)">'
    malicious_text = '<script>alert("content")</script>'
    malicious_reason = '<script>alert("reason")</script>'
    documents["ChangeSet"]["payload"]["changes"][0]["author"] = malicious_author
    documents["ChangeSet"]["payload"]["changes"][0]["authors"] = [malicious_author]
    documents["ChangeSet"]["payload"]["changes"][0]["after"] = malicious_text
    documents["ChangeSet"]["payload"]["raw_events"][0]["author"] = malicious_author
    documents["ApprovalSet"]["payload"]["decisions"][0]["reason"] = malicious_reason
    documents["PatchPlan"]["payload"]["operations"][0]["replacement_text"] = malicious_text
    documents = _rebind_after_changeset(documents)

    output = build_ledger(
        documents["ChangeSet"],
        documents["ApprovalSet"],
        documents["PatchPlan"],
        documents["VerificationReport"],
    )
    rendered = output.html_bytes.decode("utf-8")
    parsed = json.loads(output.json_bytes)
    assert parsed["changes"][0]["author"] == malicious_author
    assert parsed["changes"][0]["content"]["after"] == malicious_text
    assert parsed["changes"][0]["decision"]["reason"] == malicious_reason
    assert "<script" not in rendered.casefold()
    assert "<img" not in rendered.casefold()
    assert "&lt;script&gt;" in rendered
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in rendered


def test_ledger_rejects_resealed_cross_object_hash_tamper(
    documents: dict[str, dict[str, Any]],
) -> None:
    patch = copy.deepcopy(documents["PatchPlan"])
    forged = "sha256:" + "f" * 64
    patch["payload"]["approval_set_sha256"] = forged
    for condition in patch["payload"]["preconditions"]:
        if condition["kind"] == "approval_payload_sha256_equals":
            condition["expected"] = forged
    patch = seal_envelope(patch)
    with pytest.raises(ContractError) as rejected:
        build_ledger(
            documents["ChangeSet"],
            documents["ApprovalSet"],
            patch,
            documents["VerificationReport"],
        )
    assert rejected.value.code is ErrorCode.HASH_PATCHPLAN_MISMATCH


def test_ledger_rejects_resealed_verification_extension_tamper(
    documents: dict[str, dict[str, Any]],
) -> None:
    verification = copy.deepcopy(documents["VerificationReport"])
    verification["extensions"][VERIFICATION_EXTENSION]["changeset_payload_sha256"] = (
        "sha256:" + "e" * 64
    )
    verification = seal_envelope(verification)
    with pytest.raises(ContractError) as rejected:
        build_ledger(
            documents["ChangeSet"],
            documents["ApprovalSet"],
            documents["PatchPlan"],
            verification,
        )
    assert rejected.value.code is ErrorCode.HASH_CHANGESET_MISMATCH


def test_bundle_is_allowlist_only_deterministic_and_self_verifying(
    documents: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    source, allowlist = _ledger_files(tmp_path, documents)
    first_path = tmp_path / "audit-a.zip"
    second_path = tmp_path / "audit-b.zip"
    first = create_audit_bundle(
        source,
        first_path,
        allowlist=allowlist,
        run_manifest=documents["RunManifest"],
        verification_report=documents["VerificationReport"],
        content_classification="public_fixture",
        generated_at=GENERATED_AT,
    )
    second = create_audit_bundle(
        source,
        second_path,
        allowlist=allowlist,
        run_manifest=documents["RunManifest"],
        verification_report=documents["VerificationReport"],
        content_classification="public_fixture",
        generated_at=GENERATED_AT,
    )
    assert first_path.read_bytes() == second_path.read_bytes()
    assert first.document == second.document
    assert first.file_sha256 == second.file_sha256
    verified = verify_audit_bundle(
        first_path,
        expected_run_manifest=documents["RunManifest"],
        expected_verification_report=documents["VerificationReport"],
    )
    assert verified.document == first.document
    assert verified.entry_count == 4
    assert first.document["payload"]["manifest_sha256"].startswith("sha256:")

    with zipfile.ZipFile(first_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        assert names == sorted(names)
        assert "not-allowlisted-private.txt" not in names
        assert names == [
            "ledger.html",
            "ledger.json",
            "manifest/audit-bundle.json",
            "manifest/privacy-scan.json",
            "run-manifest.json",
            "verification-report.json",
        ]
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in infos)

    original = first_path.read_bytes()
    with pytest.raises(ContractError):
        create_audit_bundle(
            source,
            first_path,
            allowlist=allowlist,
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        )
    assert first_path.read_bytes() == original


@pytest.mark.parametrize(
    ("unsafe", "code"),
    [
        ("../ledger.json", ErrorCode.PATH_TRAVERSAL),
        ("nested\\ledger.json", ErrorCode.PATH_TRAVERSAL),
        ("C:/ledger.json", ErrorCode.PATH_ABSOLUTE),
        ("/ledger.json", ErrorCode.PATH_ABSOLUTE),
    ],
)
def test_bundle_rejects_nonportable_allowlist_paths(
    documents: dict[str, dict[str, Any]],
    tmp_path: Path,
    unsafe: str,
    code: ErrorCode,
) -> None:
    source = tmp_path / "payload"
    source.mkdir()
    with pytest.raises(ContractError) as rejected:
        create_audit_bundle(
            source,
            tmp_path / "unsafe.zip",
            allowlist=[BundleItem(unsafe, "ledger", documents["ChangeSet"])],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        )
    assert rejected.value.code is code
    assert not (tmp_path / "unsafe.zip").exists()


def test_bundle_rejects_symlink_allowlist_item(
    documents: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    source = tmp_path / "payload"
    source.mkdir()
    real = source / "real.json"
    real.write_bytes(b"{}\n")
    linked = source / "linked.json"
    try:
        os.symlink(real, linked)
    except OSError:
        pytest.skip("host policy does not permit a real symlink")
    with pytest.raises(ContractError) as rejected:
        create_audit_bundle(
            source,
            tmp_path / "linked.zip",
            allowlist=[BundleItem("linked.json", "ledger", documents["ChangeSet"])],
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        )
    assert rejected.value.code is ErrorCode.PATH_LINK_ESCAPE
    assert not (tmp_path / "linked.zip").exists()


def test_public_bundle_privacy_hit_fails_without_output(
    documents: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    source = tmp_path / "payload"
    source.mkdir()
    private_bytes = b"source=C:\\Users\\Alice\\private-paper.tex\n"
    (source / "private.txt").write_bytes(private_bytes)
    artifact = _bundle_artifact("private.txt", "private", private_bytes, "text/plain")
    verification = documents["VerificationReport"]
    run_manifest = copy.deepcopy(documents["RunManifest"])
    run_manifest["payload"]["object_bindings"] = [object_binding(verification)]
    run_manifest["payload"]["artifacts"] = [artifact]
    run_manifest = seal_envelope(run_manifest)
    (source / "run-manifest.json").write_bytes(canonical_json(run_manifest) + b"\n")
    (source / "verification-report.json").write_bytes(canonical_json(verification) + b"\n")
    destination = tmp_path / "private.zip"
    with pytest.raises(ContractError) as rejected:
        create_audit_bundle(
            source,
            destination,
            allowlist=[
                BundleItem("private.txt", "private", run_manifest, artifact["artifact_id"]),
                BundleItem("run-manifest.json", "run_manifest", run_manifest, "$document"),
                BundleItem(
                    "verification-report.json",
                    "verification_report",
                    verification,
                    "$document",
                ),
            ],
            run_manifest=run_manifest,
            verification_report=verification,
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        )
    assert rejected.value.code is ErrorCode.BUNDLE_PRIVATE_RELEASE
    assert rejected.value.violation.details == {
        "finding_count": 1,
        "rule_ids": ["windows-user-profile"],
    }
    assert not destination.exists()
    assert not list(tmp_path.glob(".*.bundle-*.tmp"))


def test_bundle_publish_failure_cleans_stage_and_tamper_is_detected(
    documents: dict[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, allowlist = _ledger_files(tmp_path, documents)
    destination = tmp_path / "failed.zip"

    def fail_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("synthetic hardlink failure")

    monkeypatch.setattr("latex_word_review.bundle.os.link", fail_link)
    with pytest.raises(ContractError) as failed:
        create_audit_bundle(
            source,
            destination,
            allowlist=allowlist,
            run_manifest=documents["RunManifest"],
            verification_report=documents["VerificationReport"],
            content_classification="public_fixture",
            generated_at=GENERATED_AT,
        )
    assert failed.value.code is ErrorCode.BUNDLE_HASH_MISMATCH
    assert not destination.exists()
    assert not list(tmp_path.glob(".*.bundle-*.tmp"))

    monkeypatch.undo()
    valid = tmp_path / "valid.zip"
    create_audit_bundle(
        source,
        valid,
        allowlist=allowlist,
        run_manifest=documents["RunManifest"],
        verification_report=documents["VerificationReport"],
        content_classification="public_fixture",
        generated_at=GENERATED_AT,
    )
    tampered = tmp_path / "tampered.zip"
    damaged = bytearray(valid.read_bytes())
    damaged[len(damaged) // 2] ^= 0x01
    tampered.write_bytes(damaged)
    with pytest.raises(ContractError) as rejected:
        verify_audit_bundle(tampered)
    assert rejected.value.code is ErrorCode.BUNDLE_HASH_MISMATCH
