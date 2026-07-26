"""Product-session reconstruction, automatic versions, and gate tests."""

from __future__ import annotations

import copy
import os
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import latex_word_review.application as application_module
from latex_word_review.application import ApplicationSession
from latex_word_review.approval import write_approval_json
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes
from latex_word_review.jsonio import read_contract_file
from latex_word_review.ledger import build_ledger
from tests.test_e2e_public_roundtrip import _fake_tools
from tests.test_workflow import FIXTURE, _make_returned

TIME = "2026-07-17T03:00:00Z"


def _exported_session(tmp_path: Path) -> ApplicationSession:
    source = tmp_path / "source"
    shutil.copytree(FIXTURE, source)
    session = ApplicationSession.create(
        source,
        tmp_path / "run",
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )
    session.export_review(confidentiality="public_fixture", generated_at=TIME)
    return session


def _review_session(tmp_path: Path) -> ApplicationSession:
    session = _exported_session(tmp_path)
    returned = tmp_path / "returned.docx"
    _make_returned(session.run_root, returned)
    session.receive_review(
        returned,
        confidentiality="public_fixture",
        generated_at=TIME,
    )
    return session


def _only_change_id(session: ApplicationSession) -> str:
    changeset = read_contract_file(
        session.run_root / "receive/changeset.json",
        expected_schema="ChangeSet",
    )
    changes = cast("list[dict[str, Any]]", changeset["payload"]["changes"])
    assert len(changes) == 1
    return cast("str", changes[0]["change_id"])


def _applied_session(tmp_path: Path) -> ApplicationSession:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    session.decide(
        change_id=_only_change_id(session),
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    ready = session.finalize_approval(generated_at=TIME)
    token = cast("str", ready["apply_confirmation"]["patch_plan_sha256"])
    session.apply_confirmed(patch_plan_sha256=token)
    return session


def test_create_load_status_uses_sealed_main_and_only_relative_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "paper.tex").write_text(
        "\\documentclass{article}\n\\begin{document}Chosen\\end{document}\n",
        encoding="utf-8",
    )
    (source / "other.tex").write_text(
        "\\documentclass{article}\n\\begin{document}Other\\end{document}\n",
        encoding="utf-8",
    )
    session = ApplicationSession.create(
        source,
        tmp_path / "run",
        main_document="paper.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    status = session.status()

    assert status["phase"] == "ready_to_export"
    assert status["step"] == 1
    assert status["next_action"] == "generate_review_word"
    assert status["can_apply"] is False
    assert status["main_document"] == "paper.tex"
    artifacts = cast("dict[str, str]", status["artifacts"])
    assert artifacts["main_document"] == "snapshot/paper.tex"
    assert all(not Path(value).is_absolute() for value in artifacts.values())
    assert all(chr(92) not in value and ":" not in value for value in artifacts.values())

    # A mutated response or arbitrary UI cache file is not authoritative.
    status["phase"] = "completed"
    (session.run_root / "ui-cache.json").write_text(
        '{"phase":"completed","can_apply":true}', encoding="utf-8"
    )
    reloaded = ApplicationSession.load(session.run_root)
    assert reloaded.status()["phase"] == "ready_to_export"
    assert reloaded.status()["can_apply"] is False


def test_load_hands_off_one_status_read_but_actions_reinspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}Review\\end{document}\n",
        encoding="utf-8",
    )
    created = ApplicationSession.create(
        source,
        tmp_path / "run",
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )
    created.status()

    original_inspect = ApplicationSession._inspect
    calls = 0

    def counted_inspect(session: ApplicationSession) -> Any:
        nonlocal calls
        calls += 1
        return original_inspect(session)

    monkeypatch.setattr(ApplicationSession, "_inspect", counted_inspect)
    loaded = ApplicationSession.load(created.run_root)
    assert calls == 1
    assert loaded.status()["phase"] == "ready_to_export"
    assert calls == 1
    assert loaded.status()["phase"] == "ready_to_export"
    assert calls == 2

    loaded_for_action = ApplicationSession.load(created.run_root)
    assert calls == 3
    monkeypatch.setattr(application_module, "export_workflow", lambda *_args, **_kwargs: {})
    loaded_for_action.export_review()
    # One validation occurs immediately before the action, then another reads
    # the post-action state. The load-time handoff cannot cross that boundary.
    assert calls == 5


def test_review_copy_is_exact_editable_and_does_not_change_sealed_baseline(
    tmp_path: Path,
) -> None:
    session = _exported_session(tmp_path)
    baseline = session.run_root / "export/review.docx"
    baseline_before = baseline.read_bytes()
    status_before = session.status()
    output_directory = tmp_path / "chosen-output"
    output_directory.mkdir()
    destination = output_directory / "paper-for-review.docx"

    saved = session.save_review_copy(destination)

    assert saved == destination.resolve()
    assert saved.read_bytes() == baseline_before
    with saved.open("r+b") as editable_stream:
        assert editable_stream.read(4) == baseline_before[:4]
    assert saved.read_bytes() == baseline_before
    assert baseline.read_bytes() == baseline_before
    assert session.status()["phase"] == status_before["phase"] == "waiting_for_return"

    existing = output_directory / "existing.docx"
    existing.write_bytes(b"existing-user-file")
    with pytest.raises(ContractError) as captured:
        session.save_review_copy(existing)
    assert captured.value.code is ErrorCode.SCHEMA_INVALID
    assert existing.read_bytes() == b"existing-user-file"
    assert baseline.read_bytes() == baseline_before


def test_review_copy_rejects_baseline_changed_after_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    baseline = run_root / "export/review.docx"
    baseline.parent.mkdir(parents=True)
    baseline_bytes = b"sealed-review-word"
    baseline.write_bytes(baseline_bytes)
    evidence = SimpleNamespace(
        workflow={"phase": "exported"},
        review_docx_digest=digest_bytes(baseline_bytes),
    )
    session = ApplicationSession(run_root)
    monkeypatch.setattr(session, "_inspect", lambda: evidence)
    output_directory = tmp_path / "race-output"
    output_directory.mkdir()

    normal_destination = output_directory / "normal-review-copy.docx"
    assert session.save_review_copy(normal_destination) == normal_destination.resolve()
    assert normal_destination.read_bytes() == baseline_bytes

    baseline.write_bytes(b"tampered-after-inspection")
    destination = output_directory / "paper-for-review.docx"

    with pytest.raises(ContractError) as captured:
        session.save_review_copy(destination)

    assert captured.value.code is ErrorCode.HASH_INTEGRITY_MISMATCH
    assert not destination.exists()
    assert not list(output_directory.glob(f".{destination.name}.publish-*"))


def test_existing_changes_display_requires_export_report_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    display_path = run_root / "export/existing-changes-display.docx"
    display_path.parent.mkdir(parents=True)
    display_bytes = b"visual-only-existing-latex-changes"
    display_path.write_bytes(display_bytes)
    report: dict[str, Any] = {"payload": {}}

    def read_report(
        path: Path,
        *,
        expected_schema: str,
        max_bytes: int,
    ) -> dict[str, Any]:
        assert path == run_root / "export/objects/export-report.json"
        assert expected_schema == "ExportReport"
        assert max_bytes > 0
        return copy.deepcopy(report)

    monkeypatch.setattr(application_module, "read_contract_file", read_report)

    assert (
        application_module._load_existing_changes_display_digest(
            run_root,
            "exported",
        )
        is None
    )

    report["payload"]["existing_changes_display_docx"] = application_module._artifact_ref(
        "export/existing-changes-display.docx",
        "latex_changes_display_docx",
        display_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert application_module._load_existing_changes_display_digest(
        run_root,
        "exported",
    ) == digest_bytes(display_bytes)


def test_existing_changes_display_copy_is_exact_and_does_not_change_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _exported_session(tmp_path)
    baseline = session.run_root / "export/review.docx"
    baseline_before = baseline.read_bytes()
    display = session.run_root / "export/existing-changes-display.docx"
    evidence = session._inspect()
    display_bytes = b"visual-only-display-copy"
    display.write_bytes(display_bytes)
    authorized = replace(
        evidence,
        existing_changes_display_digest=digest_bytes(display_bytes),
    )
    monkeypatch.setattr(ApplicationSession, "_inspect", lambda _session: authorized)
    output_directory = tmp_path / "display-output"
    output_directory.mkdir()
    destination = output_directory / "existing-changes.docx"

    saved = session.save_existing_changes_display_copy(destination)

    assert saved == destination.resolve()
    assert saved.read_bytes() == display_bytes
    assert display.read_bytes() == display_bytes
    assert baseline.read_bytes() == baseline_before
    status = session._status_from_evidence(authorized).as_dict()
    assert status["phase"] == "waiting_for_return"
    assert status["artifacts"]["review_docx"] == "export/review.docx"
    assert status["artifacts"]["existing_changes_display_docx"] == (
        "export/existing-changes-display.docx"
    )


def test_review_copy_publish_failure_leaves_no_output_or_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _exported_session(tmp_path)
    baseline = session.run_root / "export/review.docx"
    baseline_before = baseline.read_bytes()
    output_directory = tmp_path / "chosen-output"
    output_directory.mkdir()
    destination = output_directory / "paper-for-review.docx"

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("synthetic publication failure")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(ContractError) as captured:
        session.save_review_copy(destination)

    assert captured.value.code is ErrorCode.INTERNAL_INVARIANT
    assert not destination.exists()
    assert list(output_directory.iterdir()) == []
    assert baseline.read_bytes() == baseline_before
    assert session.status()["phase"] == "waiting_for_return"


def test_review_copy_requires_export_and_safe_new_docx_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}Review\\end{document}\n",
        encoding="utf-8",
    )
    session = ApplicationSession.create(
        source,
        tmp_path / "run",
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    with pytest.raises(ContractError) as not_exported:
        session.save_review_copy(tmp_path / "before-export.docx")
    assert not_exported.value.code is ErrorCode.SCHEMA_INVALID
    assert not (tmp_path / "before-export.docx").exists()

    exported = _exported_session(tmp_path / "exported-case")
    with pytest.raises(ContractError) as wrong_suffix:
        exported.save_review_copy(tmp_path / "exported-case" / "review.doc")
    assert wrong_suffix.value.code is ErrorCode.SCHEMA_INVALID
    assert not (tmp_path / "exported-case" / "review.doc").exists()


def test_approval_plan_versions_and_explicit_apply_hash_gate(tmp_path: Path) -> None:
    session = _review_session(tmp_path)
    snapshot_before = {
        path.relative_to(session.run_root / "snapshot"): path.read_bytes()
        for path in (session.run_root / "snapshot").rglob("*")
        if path.is_file()
    }

    assert session.status()["phase"] == "approval_required"
    status = session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    assert status["approval"]["revision"] == 1
    assert status["phase"] == "approval_in_progress"

    change_id = _only_change_id(session)
    status = session.decide(
        change_id=change_id,
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    assert status["approval"]["revision"] == 2
    assert status["phase"] == "approval_ready_to_finalize"

    status = session.finalize_approval(generated_at=TIME)
    assert status["approval"]["revision"] == 3
    assert status["approval"]["status"] == "final"
    assert status["plan"]["number"] == 1
    assert status["phase"] == "awaiting_apply_confirmation"
    assert status["can_apply"] is False
    confirmation = copy.deepcopy(cast("dict[str, Any]", status["apply_confirmation"]))
    assert confirmation["required"] is True
    token = cast("str", confirmation["patch_plan_sha256"])
    assert not (session.run_root / "revised-clean").exists()
    assert {
        path.relative_to(session.run_root / "snapshot"): path.read_bytes()
        for path in (session.run_root / "snapshot").rglob("*")
        if path.is_file()
    } == snapshot_before

    with pytest.raises(ContractError) as raised:
        session.apply_confirmed(patch_plan_sha256="0" * 64)
    assert raised.value.code is ErrorCode.HASH_PATCHPLAN_MISMATCH
    assert not (session.run_root / "revised-clean").exists()

    status = session.apply_confirmed(patch_plan_sha256=token)
    assert status["phase"] == "applied"
    assert status["can_apply"] is False
    assert status["artifacts"]["revised_source"] == "revised-clean"
    assert ApplicationSession.load(session.run_root).status()["phase"] == "applied"
    assert {
        path.relative_to(session.run_root / "snapshot"): path.read_bytes()
        for path in (session.run_root / "snapshot").rglob("*")
        if path.is_file()
    } == snapshot_before


def test_redirect_decisions_defer_only_the_full_status_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    change_id = _only_change_id(session)
    original_inspect = ApplicationSession._inspect
    calls = 0

    def counted_inspect(candidate: ApplicationSession) -> Any:
        nonlocal calls
        calls += 1
        return original_inspect(candidate)

    monkeypatch.setattr(ApplicationSession, "_inspect", counted_inspect)

    session.decide_without_status(
        change_id=change_id,
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    assert calls == 1
    approval_r2 = read_contract_file(
        session.run_root / "approvals/approval-r2.json",
        expected_schema="ApprovalSet",
    )
    assert approval_r2["payload"]["revision"] == 2

    redirected_status = session.status()
    assert calls == 2
    assert redirected_status["approval"]["revision"] == 2

    session.decide_bulk_without_status(
        operation="mark_manual",
        change_ids=[change_id],
        decided_at=TIME,
        generated_at=TIME,
    )
    assert calls == 3
    assert not (session.run_root / "approvals/approval-r3.json").exists()

    redirected_bulk_status = session.status()
    assert calls == 4
    assert redirected_bulk_status["approval"]["revision"] == 2

    public_status = session.decide(
        change_id=change_id,
        decision="rejected",
        decided_at=TIME,
        generated_at=TIME,
    )
    assert calls == 6
    assert public_status["approval"]["revision"] == 3


def test_redirect_decision_detects_corruption_during_publish_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    original_write = write_approval_json

    def corrupt_after_publish(
        changeset: dict[str, Any],
        approval: dict[str, Any],
        output_path: str | Path,
    ) -> Path:
        published = original_write(changeset, approval, output_path)
        published.write_text("{}", encoding="utf-8")
        return published

    monkeypatch.setattr(application_module, "write_approval_json", corrupt_after_publish)

    with pytest.raises(ContractError) as captured:
        session.decide_without_status(
            change_id=_only_change_id(session),
            decision="accepted",
            decided_at=TIME,
            generated_at=TIME,
        )

    assert captured.value.code is ErrorCode.SCHEMA_INVALID


def test_new_final_approval_allocates_plan_r2_and_invalidates_old_plan(tmp_path: Path) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    change_id = _only_change_id(session)
    session.decide(
        change_id=change_id,
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    first = session.finalize_approval(generated_at=TIME)
    first_token = cast("str", first["apply_confirmation"]["patch_plan_sha256"])

    changed = session.decide(
        change_id=change_id,
        decision="rejected",
        decided_at=TIME,
        generated_at=TIME,
    )
    assert changed["approval"]["revision"] == 4
    assert changed["plan"] is None
    assert changed["phase"] == "approval_ready_to_finalize"
    second = session.finalize_approval(generated_at=TIME)

    assert second["approval"]["revision"] == 5
    assert second["plan"]["number"] == 2
    assert second["plan"]["status"] == "noop"
    assert (session.run_root / "plans/plan-r1/patch-plan.json").is_file()
    assert (session.run_root / "plans/plan-r2/patch-plan.json").is_file()
    second_token = cast("str", second["apply_confirmation"]["patch_plan_sha256"])
    assert second_token != first_token
    with pytest.raises(ContractError, match=ErrorCode.HASH_PATCHPLAN_MISMATCH.value):
        session.apply_confirmed(patch_plan_sha256=first_token)


def test_blocked_plan_reopens_final_approval_and_invalidates_old_hash(
    tmp_path: Path,
) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    change_id = _only_change_id(session)
    session.decide(
        change_id=change_id,
        decision="accepted_with_edit",
        final_text="50%",
        decided_at=TIME,
        generated_at=TIME,
    )
    blocked = session.finalize_approval(generated_at=TIME)

    assert blocked["phase"] == "plan_blocked"
    assert blocked["plan"]["number"] == 1
    assert blocked["plan"]["status"] == "blocked"
    old_hash = cast("str", blocked["plan"]["payload_sha256"])

    with pytest.raises(ContractError, match=ErrorCode.HASH_PATCHPLAN_MISMATCH.value):
        session.revise_blocked_approval(patch_plan_sha256="sha256:" + "0" * 64)
    assert session.status() == blocked
    assert not (session.run_root / "approvals/approval-r4.json").exists()

    reopened = session.revise_blocked_approval(
        patch_plan_sha256=old_hash,
        generated_at=TIME,
    )

    assert reopened["phase"] == "approval_ready_to_finalize"
    assert reopened["approval"]["revision"] == 4
    assert reopened["approval"]["status"] == "draft"
    assert reopened["plan"] is None
    assert (session.run_root / "plans/plan-r1/patch-plan.json").is_file()
    with pytest.raises(ContractError, match=ErrorCode.HASH_PATCHPLAN_MISMATCH.value):
        session.apply_confirmed(patch_plan_sha256=old_hash)

    session.decide(
        change_id=change_id,
        decision="rejected",
        decided_at=TIME,
        generated_at=TIME,
    )
    second = session.finalize_approval(generated_at=TIME)

    assert second["approval"]["revision"] == 6
    assert second["plan"]["number"] == 2
    assert second["plan"]["status"] == "noop"
    assert second["phase"] == "awaiting_apply_confirmation"
    new_hash = cast("str", second["apply_confirmation"]["patch_plan_sha256"])
    assert new_hash != old_hash
    with pytest.raises(ContractError, match=ErrorCode.HASH_PATCHPLAN_MISMATCH.value):
        session.apply_confirmed(patch_plan_sha256=old_hash)


def test_status_rejects_approval_filename_payload_mismatch(tmp_path: Path) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    shutil.copyfile(
        session.run_root / "approvals/approval-r1.json",
        session.run_root / "approvals/approval-r2.json",
    )

    with pytest.raises(ContractError) as raised:
        session.status()
    assert raised.value.code is ErrorCode.HASH_APPROVAL_MISMATCH


def test_generate_results_wraps_verify_ledger_and_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    session.decide(
        change_id=_only_change_id(session),
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    ready = session.finalize_approval(generated_at=TIME)
    token = cast("str", ready["apply_confirmation"]["patch_plan_sha256"])
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr("latex_word_review.latex_verify.run_command", _fake_tools(calls))

    completed = session.generate_results(
        patch_plan_sha256=token,
        content_classification="public_fixture",
        generated_at=TIME,
    )

    assert completed["phase"] == "completed"
    assert completed["step"] == 4
    assert completed["can_apply"] is False
    assert completed["artifacts"]["audit_bundle"] == "audit.zip"
    assert (session.run_root / "delivery/ledger.html").is_file()
    assert (session.run_root / "audit.zip").is_file()
    assert ApplicationSession.load(session.run_root).status()["phase"] == "completed"


def test_partial_verification_retry_is_new_immutable_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _applied_session(tmp_path)
    partial = session.verify_results(
        latexmk_executable=tmp_path / "missing-latexmk.exe",
        latexdiff_executable=tmp_path / "missing-latexdiff.exe",
        generated_at=TIME,
    )

    assert partial["phase"] == "partially_completed"
    assert partial["next_action"] == "retry_verification"
    blocker = cast("list[dict[str, Any]]", partial["blockers"])[0]
    assert blocker == {
        "code": "verification_blocked",
        "attempt": 1,
        "retry_available": True,
    }
    original_attempt = {
        path.relative_to(session.run_root / "verification"): path.read_bytes()
        for path in (session.run_root / "verification").rglob("*")
        if path.is_file()
    }
    with pytest.raises(ContractError, match=ErrorCode.SCHEMA_INVALID.value):
        session.build_ledger()
    assert not (session.run_root / "ledger").exists()

    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr("latex_word_review.latex_verify.run_command", _fake_tools(calls))
    retried = session.retry_verification(generated_at=TIME)

    assert retried["phase"] == "verified"
    assert retried["artifacts"]["verification_report"] == (
        "verification-retries/retry-r1/verification-report.json"
    )
    assert (session.run_root / "verification-retries/retry-r1/verification-report.json").is_file()
    assert {
        path.relative_to(session.run_root / "verification"): path.read_bytes()
        for path in (session.run_root / "verification").rglob("*")
        if path.is_file()
    } == original_attempt
    assert calls

    old_diff = session.run_root / "verification/actual.diff"
    old_diff.write_bytes(old_diff.read_bytes() + b"tamper")
    with pytest.raises(ContractError) as raised:
        session.status()
    assert raised.value.code is ErrorCode.HASH_INTEGRITY_MISMATCH
    old_diff.write_bytes(original_attempt[Path("actual.diff")])
    assert session.status()["phase"] == "verified"

    session.build_ledger()
    completed = session.create_bundle(
        content_classification="public_fixture",
        generated_at=TIME,
    )
    assert completed["phase"] == "completed"
    assert ApplicationSession.load(session.run_root).status()["phase"] == "completed"


def test_retry_rejects_noncontiguous_attempt_history(
    tmp_path: Path,
) -> None:
    session = _applied_session(tmp_path)
    session.verify_results(
        latexmk_executable=tmp_path / "missing-latexmk.exe",
        latexdiff_executable=tmp_path / "missing-latexdiff.exe",
        generated_at=TIME,
    )
    retries = session.run_root / "verification-retries"
    (retries / "retry-r2").mkdir(parents=True)

    with pytest.raises(ContractError) as raised:
        session.status()
    assert raised.value.code is ErrorCode.HASH_INTEGRITY_MISMATCH


def test_partial_retry_is_disabled_after_legacy_downstream_evidence_is_sealed(
    tmp_path: Path,
) -> None:
    session = _applied_session(tmp_path)
    session.verify_results(
        latexmk_executable=tmp_path / "missing-latexmk.exe",
        latexdiff_executable=tmp_path / "missing-latexdiff.exe",
        generated_at=TIME,
    )
    status = session.status()
    changeset = read_contract_file(
        session.run_root / "receive/changeset.json",
        expected_schema="ChangeSet",
    )
    approval = read_contract_file(
        session.run_root / cast("str", status["artifacts"]["approval"]),
        expected_schema="ApprovalSet",
    )
    plan = read_contract_file(
        session.run_root / cast("str", status["artifacts"]["patch_plan"]),
        expected_schema="PatchPlan",
    )
    verification = read_contract_file(
        session.run_root / "verification/verification-report.json",
        expected_schema="VerificationReport",
    )
    legacy_ledger = build_ledger(changeset, approval, plan, verification)
    ledger_root = session.run_root / "ledger"
    ledger_root.mkdir()
    (ledger_root / "ledger.json").write_bytes(legacy_ledger.json_bytes)
    (ledger_root / "ledger.html").write_bytes(legacy_ledger.html_bytes)

    recovered = session.status()
    blocker = cast("list[dict[str, Any]]", recovered["blockers"])[0]
    assert recovered["phase"] == "partially_completed"
    assert recovered["next_action"] == "inspect_partial_results"
    assert blocker["retry_available"] is False
    with pytest.raises(ContractError) as raised:
        session.retry_verification()
    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert not (session.run_root / "verification-retries").exists()
