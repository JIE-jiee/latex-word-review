"""Focused safety-boundary coverage for the product application facade."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import latex_word_review.application as application_module
from latex_word_review.application import (
    ApplicationSession,
    _ApprovalVersion,
    _collect_artifact_refs,
    _ensure_real_directory,
    _Evidence,
    _is_link_or_junction,
    _PlanVersion,
    _publish_directory,
    _publish_verified_file_copy,
    _require_real_directory,
    _require_regular_file,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import FileDigest
from latex_word_review.ids import derive_artifact_id
from latex_word_review.ledger import LedgerOutput


class _BrokenLinkProbe:
    def is_symlink(self) -> bool:
        raise OSError("link metadata unavailable")


class _Status:
    def __init__(self, value: dict[str, Any]) -> None:
        self._value = value

    def as_dict(self) -> dict[str, Any]:
        return self._value


def _approval(*, status: str = "draft", revision: int = 1) -> _ApprovalVersion:
    return _ApprovalVersion(
        revision=revision,
        path=f"approvals/approval-r{revision}.json",
        document={
            "payload": {
                "revision": revision,
                "status": status,
                "decision_summary": {"pending": 0},
            }
        },
        payload_sha256=f"approval-{revision}",
    )


def _plan(
    approval: _ApprovalVersion,
    *,
    status: str = "ready",
    blocked: tuple[dict[str, Any], ...] = (),
) -> _PlanVersion:
    return _PlanVersion(
        number=1,
        directory="plans/plan-r1",
        document={
            "payload": {
                "status": status,
                "accepted_but_blocked": list(blocked),
                "summary": {},
            }
        },
        unified_diff=b"",
        approval=approval,
        payload_sha256="plan-sha",
    )


def _evidence(**overrides: Any) -> _Evidence:
    values: dict[str, Any] = {
        "workflow": {"phase": "ingested"},
        "source_manifest": {
            "run_id": "run-1",
            "payload": {"main_document": "main.tex", "source_tree_sha256": "0" * 64},
        },
        "review_docx_digest": None,
        "existing_changes_display_digest": None,
        "changeset": {"schema": "ChangeSet"},
        "approvals": (),
        "plans": (),
        "current_plan": None,
        "revised_exists": False,
        "verifications": (),
        "verification": None,
        "verification_directory": None,
        "expected_ledger": None,
        "ledger_exists": False,
        "run_manifest": None,
        "audit_exists": False,
    }
    values.update(overrides)
    return _Evidence(**values)


def _assert_code(caught: pytest.ExceptionInfo[ContractError], code: ErrorCode) -> None:
    assert caught.value.code is code


def _display_report(tmp_path: Path) -> tuple[Path, dict[str, Any], FileDigest]:
    display = tmp_path / "export" / "existing-changes-display.docx"
    display.parent.mkdir()
    data = b"sealed display Word"
    display.write_bytes(data)
    digest = FileDigest(
        size_bytes=len(data),
        sha256="sha256:" + hashlib.sha256(data).hexdigest(),
    )
    artifact = {
        "artifact_id": derive_artifact_id(digest.sha256),
        "path": "export/existing-changes-display.docx",
        "path_base": "run_root",
        "role": "latex_changes_display_docx",
        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size_bytes": digest.size_bytes,
        "sha256": digest.sha256,
        "immutable": True,
    }
    return display, {"payload": {"existing_changes_display_docx": artifact}}, digest


def test_optional_display_evidence_preserves_old_reports_and_ignores_stray_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def report(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"payload": {}}

    monkeypatch.setattr(application_module, "read_contract_file", report)
    stray = tmp_path / "export" / "existing-changes-display.docx"
    stray.parent.mkdir()
    stray.write_bytes(b"unsealed")

    assert application_module._load_existing_changes_display_digest(tmp_path, "snapshotted") is None
    assert application_module._load_existing_changes_display_digest(tmp_path, "exported") is None
    assert calls == 1


@pytest.mark.parametrize(
    ("raw_artifact", "message"),
    [
        ([], "evidence is invalid"),
        ({"path": 42}, "path is invalid"),
    ],
)
def test_optional_display_evidence_rejects_invalid_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_artifact: object,
    message: str,
) -> None:
    monkeypatch.setattr(
        application_module,
        "read_contract_file",
        lambda *_args, **_kwargs: {"payload": {"existing_changes_display_docx": raw_artifact}},
    )

    with pytest.raises(ContractError, match=message) as caught:
        application_module._load_existing_changes_display_digest(tmp_path, "exported")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "export/other.docx"),
        ("path_base", "snapshot_root"),
        ("role", "review_docx"),
        ("media_type", "application/octet-stream"),
        ("immutable", False),
    ],
)
def test_optional_display_evidence_rejects_semantic_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _display, report, _digest = _display_report(tmp_path)
    artifact = cast("dict[str, Any]", report["payload"]["existing_changes_display_docx"])
    artifact[field] = value
    monkeypatch.setattr(
        application_module,
        "read_contract_file",
        lambda *_args, **_kwargs: report,
    )

    with pytest.raises(ContractError, match="unexpected semantics") as caught:
        application_module._load_existing_changes_display_digest(tmp_path, "ingested")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size_bytes", True),
        ("size_bytes", -1),
        ("sha256", 42),
        ("artifact_id", 42),
        ("artifact_id", "artifact_wrong"),
    ],
)
def test_optional_display_evidence_rejects_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _display, report, _digest = _display_report(tmp_path)
    artifact = cast("dict[str, Any]", report["payload"]["existing_changes_display_docx"])
    artifact[field] = value
    monkeypatch.setattr(
        application_module,
        "read_contract_file",
        lambda *_args, **_kwargs: report,
    )

    with pytest.raises(ContractError, match="identity is invalid") as caught:
        application_module._load_existing_changes_display_digest(tmp_path, "exported")
    _assert_code(caught, ErrorCode.HASH_INTEGRITY_MISMATCH)


def test_optional_display_evidence_accepts_bound_file_and_rejects_digest_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display, report, digest = _display_report(tmp_path)
    monkeypatch.setattr(
        application_module,
        "read_contract_file",
        lambda *_args, **_kwargs: report,
    )

    assert application_module._load_existing_changes_display_digest(tmp_path, "exported") == digest
    display.write_bytes(b"changed display Word")
    with pytest.raises(ContractError, match="differs from its sealed evidence") as caught:
        application_module._load_existing_changes_display_digest(tmp_path, "exported")
    _assert_code(caught, ErrorCode.HASH_INTEGRITY_MISMATCH)


def test_path_guards_close_metadata_and_shape_failures(tmp_path: Path) -> None:
    with pytest.raises(ContractError) as caught:
        _is_link_or_junction(cast("Path", _BrokenLinkProbe()))
    _assert_code(caught, ErrorCode.PATH_LINK_ESCAPE)

    missing = tmp_path / "missing"
    with pytest.raises(ContractError) as caught:
        _require_real_directory(missing, "missing directory")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    regular = tmp_path / "regular.txt"
    regular.write_text("x", encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        _require_real_directory(regular, "regular file")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)
    with pytest.raises(ContractError) as caught:
        _require_regular_file(tmp_path, "directory")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    with pytest.raises(ContractError) as caught:
        _ensure_real_directory(missing / "child", "nested directory")
    _assert_code(caught, ErrorCode.INTERNAL_INVARIANT)


def test_publish_directory_rejects_existing_and_cleans_failed_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ContractError) as caught:
        _publish_directory(existing, purpose="test", writer=lambda _path: None)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    destination = tmp_path / "published"
    staged: list[Path] = []

    def writer(path: Path) -> None:
        staged.append(path)
        (path / "payload").write_text("data", encoding="utf-8")

    def fail_publish(_source: Path, _destination: Path) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(application_module, "publish_new_directory", fail_publish)
    with pytest.raises(ContractError) as caught:
        _publish_directory(destination, purpose="test", writer=writer)
    _assert_code(caught, ErrorCode.INTERNAL_INVARIANT)
    assert staged and not staged[0].exists()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileExistsError("race"), ErrorCode.SCHEMA_INVALID),
        (OSError("link failed"), ErrorCode.INTERNAL_INVARIANT),
    ],
)
def test_verified_copy_closes_publish_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    expected: ErrorCode,
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"word")
    output = tmp_path / "out"
    output.mkdir()

    def fail_link(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(ContractError) as caught:
        _publish_verified_file_copy(source, output / "copy.docx")
    _assert_code(caught, expected)
    assert not (output / "copy.docx").exists()


def test_verified_copy_detects_source_and_stage_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"word")
    output = tmp_path / "out"
    output.mkdir()
    good = FileDigest(4, "a" * 64)
    changed = FileDigest(4, "b" * 64)
    calls = 0

    def source_drift(_path: Path, *, max_bytes: int) -> FileDigest:
        nonlocal calls
        assert max_bytes > 0
        calls += 1
        return good if calls == 1 else changed

    monkeypatch.setattr(application_module, "digest_file", source_drift)
    with pytest.raises(ContractError) as caught:
        _publish_verified_file_copy(source, output / "source-drift.docx")
    _assert_code(caught, ErrorCode.HASH_SOURCE_MISMATCH)

    calls = 0

    def stage_drift(_path: Path, *, max_bytes: int) -> FileDigest:
        nonlocal calls
        assert max_bytes > 0
        calls += 1
        return changed if calls == 3 else good

    monkeypatch.setattr(application_module, "digest_file", stage_drift)
    with pytest.raises(ContractError) as caught:
        _publish_verified_file_copy(source, output / "stage-drift.docx")
    _assert_code(caught, ErrorCode.HASH_INTEGRITY_MISMATCH)


def test_verified_copy_rejects_relative_or_existing_targets(tmp_path: Path) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"word")
    with pytest.raises(ContractError) as caught:
        _publish_verified_file_copy(source, Path("relative.docx"))
    _assert_code(caught, ErrorCode.PATH_ABSOLUTE)

    existing = tmp_path / "existing.docx"
    existing.write_bytes(b"old")
    with pytest.raises(ContractError) as caught:
        _publish_verified_file_copy(source, existing)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)


def test_artifact_collection_rejects_invalid_and_conflicting_bindings() -> None:
    base: dict[str, Any] = {
        "artifact_id": "artifact",
        "path": "result.pdf",
        "path_base": "run_root",
        "role": "result",
        "size_bytes": 1,
        "sha256": "0" * 64,
        "immutable": True,
    }
    invalid = dict(base)
    invalid["path"] = 42
    with pytest.raises(ContractError) as caught:
        _collect_artifact_refs(invalid)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    conflicting = dict(base)
    conflicting["sha256"] = "1" * 64
    with pytest.raises(ContractError) as caught:
        _collect_artifact_refs([base, conflicting])
    _assert_code(caught, ErrorCode.HASH_INTEGRITY_MISMATCH)
    assert _collect_artifact_refs({"nested": [base]}) == (base,)


def test_public_actions_close_invalid_state_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ApplicationSession(tmp_path)
    result = {"phase": "unchanged"}
    monkeypatch.setattr(session, "_status_from_evidence", lambda _item: _Status(result))

    exported = _evidence(workflow={"phase": "exported"})
    monkeypatch.setattr(session, "_inspect", lambda: exported)
    assert session.export_review() == result
    with pytest.raises(ContractError) as caught:
        session.begin_approval(actor_id="actor", actor_name="Actor")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    snapshotted = _evidence(workflow={"phase": "snapshotted"}, changeset=None)
    monkeypatch.setattr(session, "_inspect", lambda: snapshotted)
    with pytest.raises(ContractError) as caught:
        session.receive_review(tmp_path / "returned.docx")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    ingested = _evidence(workflow={"phase": "ingested"})
    monkeypatch.setattr(session, "_inspect", lambda: ingested)
    assert session.receive_review(tmp_path / "returned.docx") == result


def test_approval_and_plan_guards_reject_stale_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ApplicationSession(tmp_path)
    final = _approval(status="final")
    draft = _approval(status="draft")
    blocked = _plan(final, status="blocked", blocked=({"change_id": "c1"},))

    monkeypatch.setattr(session, "_inspect", lambda: _evidence(approvals=(draft,)))
    with pytest.raises(ContractError) as caught:
        session.revise_blocked_approval(patch_plan_sha256="plan-sha")
    _assert_code(caught, ErrorCode.APPROVAL_NOT_FINAL)

    monkeypatch.setattr(session, "_inspect", lambda: _evidence(approvals=(final,)))
    with pytest.raises(ContractError) as caught:
        session.revise_blocked_approval(patch_plan_sha256="plan-sha")
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    monkeypatch.setattr(
        session,
        "_inspect",
        lambda: _evidence(approvals=(final,), plans=(blocked,), current_plan=blocked),
    )
    with pytest.raises(ContractError) as caught:
        session.apply_confirmed(patch_plan_sha256="plan-sha")
    _assert_code(caught, ErrorCode.PATCH_ACCEPTED_BUT_BLOCKED)

    monkeypatch.setattr(session, "_inspect", lambda: _evidence(approvals=(draft,)))
    with pytest.raises(ContractError) as caught:
        session.prepare_plan()
    _assert_code(caught, ErrorCode.APPROVAL_NOT_FINAL)


def test_verification_and_delivery_guards_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ApplicationSession(tmp_path)
    with pytest.raises(ContractError) as caught:
        session.verify_results()
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    with pytest.raises(ContractError) as caught:
        session.retry_verification()
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    failed = {"payload": {"status": "fail", "deliverables": []}}
    monkeypatch.setattr(session, "_inspect", lambda: _evidence(verification=failed))
    with pytest.raises(ContractError) as caught:
        session.build_ledger()
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)
    with pytest.raises(ContractError) as caught:
        session.create_bundle()
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    passing = {"payload": {"status": "pass", "deliverables": []}}
    monkeypatch.setattr(
        session,
        "_inspect",
        lambda: _evidence(verification=passing, expected_ledger=LedgerOutput({}, b"{}", b"x")),
    )
    with pytest.raises(ContractError) as caught:
        session.create_bundle()
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)


def test_loaders_reject_orphaned_and_noncontiguous_evidence(tmp_path: Path) -> None:
    approvals = tmp_path / "approval-case" / "approvals"
    approvals.mkdir(parents=True)
    session = ApplicationSession(approvals.parent)
    with pytest.raises(ContractError) as caught:
        session._load_approvals(None)
    _assert_code(caught, ErrorCode.HASH_CHANGESET_MISMATCH)

    (approvals / "unexpected.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        session._load_approvals({})
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    approvals_case = tmp_path / "approval-gap"
    (approvals_case / "approvals").mkdir(parents=True)
    (approvals_case / "approvals/approval-r2.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        ApplicationSession(approvals_case)._load_approvals({})
    _assert_code(caught, ErrorCode.HASH_APPROVAL_MISMATCH)

    plan_case = tmp_path / "plan-case"
    (plan_case / "plans").mkdir(parents=True)
    with pytest.raises(ContractError) as caught:
        ApplicationSession(plan_case)._load_plans({}, None, ())
    _assert_code(caught, ErrorCode.HASH_PATCHPLAN_MISMATCH)

    (plan_case / "plans/unexpected.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        ApplicationSession(plan_case)._load_plans({}, {}, (_approval(),))
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    gap_case = tmp_path / "plan-gap"
    (gap_case / "plans/plan-r2").mkdir(parents=True)
    with pytest.raises(ContractError) as caught:
        ApplicationSession(gap_case)._load_plans({}, {}, (_approval(),))
    _assert_code(caught, ErrorCode.HASH_PATCHPLAN_MISMATCH)


def test_verification_loader_rejects_orphaned_retry_tree(tmp_path: Path) -> None:
    (tmp_path / "verification-retries").mkdir()
    session = ApplicationSession(tmp_path)
    with pytest.raises(ContractError) as caught:
        session._load_verifications(None, None, None, False)
    _assert_code(caught, ErrorCode.HASH_INTEGRITY_MISMATCH)

    (tmp_path / "verification").mkdir()
    with pytest.raises(ContractError) as caught:
        session._load_verification_at(tmp_path / "verification", None, None, None, False)
    _assert_code(caught, ErrorCode.HASH_PATCHPLAN_MISMATCH)


def test_ledger_delivery_and_audit_validators_reject_partial_evidence(tmp_path: Path) -> None:
    session = ApplicationSession(tmp_path)
    (tmp_path / "ledger").mkdir()
    with pytest.raises(ContractError) as caught:
        session._validate_ledger(None)
    _assert_code(caught, ErrorCode.HASH_INTEGRITY_MISMATCH)

    expected = LedgerOutput({}, b"json", b"html")
    (tmp_path / "ledger/extra").write_text("x", encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        session._validate_ledger(expected)
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    (tmp_path / "delivery").mkdir()
    with pytest.raises(ContractError) as caught:
        session._load_delivery({}, None, None, None, None, None, None)
    _assert_code(caught, ErrorCode.BUNDLE_HASH_MISMATCH)

    (tmp_path / "audit.zip").write_bytes(b"zip")
    with pytest.raises(ContractError) as caught:
        session._validate_audit(None, None)
    _assert_code(caught, ErrorCode.BUNDLE_HASH_MISMATCH)


def test_publish_next_approval_rejects_core_and_readback_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    session = ApplicationSession(tmp_path)
    current = _approval()
    same_revision = {"payload": {"revision": 1}}
    with pytest.raises(ContractError) as caught:
        session._publish_next_approval({}, current, same_revision)
    _assert_code(caught, ErrorCode.HASH_APPROVAL_MISMATCH)

    noncontiguous = {"payload": {"revision": 3}}
    with pytest.raises(ContractError) as caught:
        session._publish_next_approval({}, current, noncontiguous)
    _assert_code(caught, ErrorCode.HASH_APPROVAL_MISMATCH)

    updated = {"payload": {"revision": 2}}
    monkeypatch.setattr(application_module, "write_approval_json", lambda *_args: None)
    monkeypatch.setattr(application_module, "read_contract_file", lambda *_args, **_kwargs: {})
    with pytest.raises(ContractError) as caught:
        session._publish_next_approval({}, current, updated)
    _assert_code(caught, ErrorCode.HASH_APPROVAL_MISMATCH)


def test_static_authority_guards_and_verification_directory_invariant(tmp_path: Path) -> None:
    session = ApplicationSession(tmp_path)
    with pytest.raises(ContractError) as caught:
        session._require_changeset(_evidence(changeset=None))
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)
    with pytest.raises(ContractError) as caught:
        session._require_latest_approval(_evidence())
    _assert_code(caught, ErrorCode.APPROVAL_NOT_FINAL)
    with pytest.raises(ContractError) as caught:
        session._require_not_applied(_evidence(revised_exists=True))
    _assert_code(caught, ErrorCode.SCHEMA_INVALID)

    final = _approval(status="final")
    plan = _plan(final)
    verification = {"payload": {"status": "pass", "deliverables": []}}
    evidence = _evidence(
        approvals=(final,),
        plans=(plan,),
        current_plan=plan,
        revised_exists=True,
        verification=verification,
        verification_directory=None,
    )
    with pytest.raises(ContractError) as caught:
        session._status_from_evidence(evidence)
    _assert_code(caught, ErrorCode.INTERNAL_INVARIANT)


def test_validate_bound_main_rejects_discovery_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ApplicationSession(tmp_path)
    monkeypatch.setattr(
        application_module,
        "discover_project",
        lambda *_args, **_kwargs: SimpleNamespace(
            main_document="other.tex", source_tree_sha256="1" * 64
        ),
    )
    manifest = {"payload": {"main_document": "main.tex", "source_tree_sha256": "0" * 64}}
    with pytest.raises(ContractError) as caught:
        session._validate_bound_main(manifest)
    _assert_code(caught, ErrorCode.HASH_SOURCE_MISMATCH)
