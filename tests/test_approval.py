"""Approval state-machine, binding, bulk-policy, and atomic-write tests."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest

from latex_word_review.approval import (
    create_approval_set,
    finalize_approval_set,
    record_bulk_decision,
    record_decision,
    write_approval_json,
)
from latex_word_review.canonical import (
    compute_payload_sha256,
    seal_envelope,
    sha256_bytes,
)
from latex_word_review.contracts import load_contract_json, validate_contract
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_file
from latex_word_review.ids import new_run_id, stable_id
from latex_word_review.revisions import BookmarkBinding, build_changeset
from latex_word_review.source_units import build_text_provenance
from latex_word_review.word_semantics import project_word_semantics

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RETURNED_DOCX = (
    PROJECT_ROOT / "tests" / "fixtures" / "e0-minimal-paper" / "returned" / "returned-reviewed.docx"
)
BASE_DOCX = PROJECT_ROOT / "tests" / "fixtures" / "e0-minimal-paper" / "base" / "review-base.docx"
RUN_ID = new_run_id(timestamp_ms=1_750_000_000_000, random_bits=7)
HASH_A = "sha256:" + "a" * 64
ACTOR = {"id": "synthetic-author", "display_name": "Synthetic Author"}
TIME_1 = "2026-07-16T13:00:00+09:00"
TIME_2 = "2026-07-16T13:01:00+09:00"


@pytest.fixture(scope="module")
def changeset() -> dict[str, Any]:
    if not RETURNED_DOCX.is_file():
        pytest.skip("public E0 fixture is intentionally absent from this distribution")
    projection = project_word_semantics(BASE_DOCX)
    bookmark = next(
        bookmark
        for story in projection.stories
        for bookmark in story.bookmarks
        if bookmark.name == "txr_insert_001"
    )
    assert bookmark.original is not None
    raw = bookmark.original.text.encode("utf-8")
    review_text, provenance = build_text_provenance(raw)
    assert review_text == bookmark.original.text
    location = {
        "path": "source/main.tex",
        "start_byte": 0,
        "end_byte": len(raw),
        "slice_sha256": sha256_bytes(raw),
        "encoding": "utf-8",
        "newline": "lf",
        "start_line": 1,
        "end_line": 1,
        "start_column": 1,
        "end_column": 1,
    }
    binding = BookmarkBinding(
        unit_id="unit_" + "1" * 32,
        source_location=location,
        normalized_text_sha256=sha256_bytes(review_text.encode("utf-8")),
        review_length=len(review_text),
        text_provenance=provenance,
    )
    return build_changeset(
        RETURNED_DOCX,
        export_baseline_path=BASE_DOCX,
        export_baseline_sha256=digest_file(BASE_DOCX, max_bytes=128 * 1024 * 1024).sha256,
        run_id=RUN_ID,
        source_manifest_sha256=HASH_A,
        source_map_sha256=HASH_A,
        revision_reader_capabilities_sha256=HASH_A,
        returned_artifact_path="ingest/e0-returned-original.docx",
        confidentiality="public_fixture",
        bookmark_bindings={"txr_insert_001": binding},
        generated_at="2026-07-16T12:00:00+09:00",
    )


def _changes(changeset: dict[str, Any]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", changeset["payload"]["changes"])


def _safe_change(changeset: dict[str, Any]) -> dict[str, Any]:
    return next(
        change for change in _changes(changeset) if change["safety_class"] == "plain_text_candidate"
    )


def _high_risk_change(changeset: dict[str, Any]) -> dict[str, Any]:
    return next(
        change for change in _changes(changeset) if change["safety_class"] != "plain_text_candidate"
    )


def _draft(changeset: dict[str, Any]) -> dict[str, Any]:
    return create_approval_set(changeset, decided_by=ACTOR, generated_at=TIME_1)


def test_create_decide_retry_modify_and_withdraw_are_immutable(
    changeset: dict[str, Any],
) -> None:
    draft = _draft(changeset)
    receipt = validate_contract(draft)
    assert draft["payload"]["revision"] == 1
    assert draft["payload"]["status"] == "draft"
    assert draft["payload"]["changeset_sha256"] == compute_payload_sha256(changeset)
    assert draft["payload"]["decision_summary"]["pending"] == len(_changes(changeset))
    assert receipt.payload_sha256 == compute_payload_sha256(draft)

    change_id = cast("str", _safe_change(changeset)["change_id"])
    accepted = record_decision(
        changeset,
        draft,
        change_id=change_id,
        decision="accepted",
        reason="accept synthetic wording",
        decided_at=TIME_1,
        generated_at=TIME_1,
    )
    assert draft["payload"]["revision"] == 1
    assert accepted["payload"]["revision"] == 2
    assert accepted["payload"]["supersedes_payload_sha256"] == compute_payload_sha256(draft)

    retry = record_decision(
        changeset,
        accepted,
        change_id=change_id,
        decision="accepted",
        reason="accept synthetic wording",
        decided_at=TIME_2,
        generated_at=TIME_2,
    )
    assert retry == accepted

    rejected = record_decision(
        changeset,
        accepted,
        change_id=change_id,
        decision="rejected",
        reason="prefer original wording",
        decided_at=TIME_2,
        generated_at=TIME_2,
    )
    assert rejected["payload"]["revision"] == 3
    assert rejected["payload"]["supersedes_payload_sha256"] == compute_payload_sha256(accepted)

    pending = record_decision(
        changeset,
        rejected,
        change_id=change_id,
        decision="pending",
        decided_at=TIME_2,
        generated_at=TIME_2,
    )
    assert pending["payload"]["revision"] == 4
    assert change_id in pending["payload"]["undecided_change_ids"]
    assert all(item["change_id"] != change_id for item in pending["payload"]["decisions"])


@pytest.mark.parametrize(
    ("decision", "final_text"),
    [
        ("pending", None),
        ("accepted", None),
        ("accepted_with_edit", "edited text"),
        ("rejected", None),
        ("manual", None),
        ("conflict", None),
    ],
)
def test_all_decision_states_validate(
    changeset: dict[str, Any], decision: str, final_text: str | None
) -> None:
    draft = _draft(changeset)
    result = record_decision(
        changeset,
        draft,
        change_id=cast("str", _safe_change(changeset)["change_id"]),
        decision=cast("Any", decision),
        final_text=final_text,
        decided_at=TIME_1,
        generated_at=TIME_1,
    )
    validate_contract(result)
    if decision == "pending":
        assert result == draft
    else:
        assert result["payload"]["decisions"][0]["decision"] == decision


def test_final_requires_every_decision_and_edit_requires_final_text(
    changeset: dict[str, Any],
) -> None:
    draft = _draft(changeset)
    change_id = cast("str", _safe_change(changeset)["change_id"])
    with pytest.raises(ContractError) as missing_edit:
        record_decision(
            changeset,
            draft,
            change_id=change_id,
            decision="accepted_with_edit",
        )
    assert missing_edit.value.code is ErrorCode.APPROVAL_FINAL_TEXT_REQUIRED

    with pytest.raises(ContractError) as incomplete:
        finalize_approval_set(changeset, draft)
    assert incomplete.value.code is ErrorCode.APPROVAL_NOT_FINAL

    current = draft
    for change in _changes(changeset):
        current = record_decision(
            changeset,
            current,
            change_id=cast("str", change["change_id"]),
            decision="accepted" if change["safety_class"] == "plain_text_candidate" else "manual",
            decided_at=TIME_1,
            generated_at=TIME_1,
        )
    final = finalize_approval_set(changeset, current, generated_at=TIME_2)
    assert final["payload"]["status"] == "final"
    assert final["payload"]["undecided_change_ids"] == []
    assert finalize_approval_set(changeset, final, generated_at=TIME_2) == final
    validate_contract(final)

    reopened = record_decision(
        changeset,
        final,
        change_id=change_id,
        decision="rejected",
        decided_at=TIME_2,
        generated_at=TIME_2,
    )
    assert reopened["payload"]["status"] == "draft"
    assert reopened["payload"]["revision"] == final["payload"]["revision"] + 1
    assert reopened["payload"]["supersedes_payload_sha256"] == compute_payload_sha256(final)


def test_tamper_and_resealed_fingerprint_are_rejected(changeset: dict[str, Any]) -> None:
    change_id = cast("str", _safe_change(changeset)["change_id"])
    accepted = record_decision(
        changeset,
        _draft(changeset),
        change_id=change_id,
        decision="accepted",
        decided_at=TIME_1,
        generated_at=TIME_1,
    )

    damaged = copy.deepcopy(accepted)
    damaged["payload"]["decisions"][0]["reason"] = "tampered without sealing"
    with pytest.raises(ContractError) as unsealed:
        record_decision(
            changeset,
            damaged,
            change_id=change_id,
            decision="rejected",
        )
    assert unsealed.value.code is ErrorCode.HASH_APPROVAL_MISMATCH

    forged = copy.deepcopy(accepted)
    forged["payload"]["decisions"][0]["change_fingerprint"] = sha256_bytes(b"forged")
    identity = copy.deepcopy(forged["payload"])
    identity.pop("approval_set_id")
    forged_id = stable_id("apr_", identity)
    forged["payload"]["approval_set_id"] = forged_id
    forged["object_id"] = forged_id
    forged = seal_envelope(forged)
    validate_contract(forged)
    with pytest.raises(ContractError) as fingerprint:
        record_decision(
            changeset,
            forged,
            change_id=change_id,
            decision="rejected",
        )
    assert fingerprint.value.code is ErrorCode.HASH_CHANGESET_MISMATCH

    changed_changeset = copy.deepcopy(changeset)
    changed_changeset["payload"]["changes"][0]["after"] = "resealed payload"
    changed_changeset = seal_envelope(changed_changeset)
    validate_contract(changed_changeset)
    with pytest.raises(ContractError) as binding:
        record_decision(
            changed_changeset,
            accepted,
            change_id=change_id,
            decision="rejected",
        )
    assert binding.value.code is ErrorCode.HASH_CHANGESET_MISMATCH


def test_unknown_change_and_unknown_decision_fail_closed(changeset: dict[str, Any]) -> None:
    draft = _draft(changeset)
    with pytest.raises(ContractError) as unknown_change:
        record_decision(
            changeset,
            draft,
            change_id="chg_" + "f" * 32,
            decision="accepted",
        )
    assert unknown_change.value.code is ErrorCode.APPROVAL_CHANGE_UNKNOWN

    with pytest.raises(ContractError) as unknown_decision:
        record_decision(
            changeset,
            draft,
            change_id=cast("str", _safe_change(changeset)["change_id"]),
            decision=cast("Any", "approve-ish"),
        )
    assert unknown_decision.value.code is ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD


@pytest.mark.parametrize("operation", ["accept_all_safe", "reject_selected"])
def test_bulk_operations_reject_every_high_risk_change(
    changeset: dict[str, Any], operation: str
) -> None:
    with pytest.raises(ContractError) as rejected:
        record_bulk_decision(
            changeset,
            _draft(changeset),
            operation=cast("Any", operation),
            change_ids=[cast("str", _high_risk_change(changeset)["change_id"])],
            decided_at=TIME_1,
        )
    assert rejected.value.code is ErrorCode.PATCH_UNSAFE_KIND


def test_bulk_mark_manual_can_reduce_authority_for_high_risk_changes(
    changeset: dict[str, Any],
) -> None:
    draft = _draft(changeset)
    high_risk_id = cast("str", _high_risk_change(changeset)["change_id"])

    marked = record_bulk_decision(
        changeset,
        draft,
        operation="mark_manual",
        change_ids=[high_risk_id],
        reason="cannot be mapped safely",
        decided_at=TIME_1,
        generated_at=TIME_1,
    )

    decision = marked["payload"]["decisions"][0]
    assert decision["change_id"] == high_risk_id
    assert decision["decision"] == "manual"
    assert marked["payload"]["audit"]["bulk_operations"] == [
        {"operation": "mark_manual", "change_ids": [high_risk_id], "decided_at": TIME_1}
    ]


def test_mark_manual_without_ids_selects_only_unsafe_undecided_changes(
    changeset: dict[str, Any],
) -> None:
    marked = record_bulk_decision(
        changeset,
        _draft(changeset),
        operation="mark_manual",
        reason="not safe for automatic writeback",
        decided_at=TIME_1,
        generated_at=TIME_1,
    )

    decisions = marked["payload"]["decisions"]
    expected_ids = [
        change["change_id"]
        for change in _changes(changeset)
        if not (
            change["safety_class"] == "plain_text_candidate"
            and change["resolution"]["status"] == "exact"
        )
    ]
    assert [item["change_id"] for item in decisions] == expected_ids
    assert [item["decision"] for item in decisions] == ["manual"] * len(expected_ids)
    assert marked["payload"]["decision_summary"]["pending"] == (
        len(_changes(changeset)) - len(expected_ids)
    )


def test_accept_all_safe_is_audited_and_idempotent(changeset: dict[str, Any]) -> None:
    draft = _draft(changeset)
    bulk = record_bulk_decision(
        changeset,
        draft,
        operation="accept_all_safe",
        decided_at=TIME_1,
        generated_at=TIME_1,
    )
    safe_id = _safe_change(changeset)["change_id"]
    assert bulk["payload"]["revision"] == 2
    assert bulk["payload"]["decisions"][0]["change_id"] == safe_id
    assert bulk["payload"]["decisions"][0]["decision"] == "accepted"
    assert bulk["payload"]["audit"]["bulk_operations"] == [
        {"operation": "accept_all_safe", "change_ids": [safe_id], "decided_at": TIME_1}
    ]
    retry = record_bulk_decision(
        changeset,
        bulk,
        operation="accept_all_safe",
        decided_at=TIME_2,
        generated_at=TIME_2,
    )
    assert retry == bulk


@pytest.mark.parametrize(
    ("operation", "expected_decision"),
    [
        ("accept_all_safe", "accepted"),
        ("reject_selected", "rejected"),
        ("mark_manual", "manual"),
    ],
)
def test_explicit_bulk_ids_only_process_undecided_changes(
    changeset: dict[str, Any],
    operation: str,
    expected_decision: str,
) -> None:
    safe_id = cast("str", _safe_change(changeset)["change_id"])
    decided_id = cast("str", _high_risk_change(changeset)["change_id"])
    prior = record_decision(
        changeset,
        _draft(changeset),
        change_id=decided_id,
        decision="rejected",
        reason="preserve this existing decision",
        decided_at=TIME_1,
        generated_at=TIME_1,
    )
    prior_by_id = {
        item["change_id"]: item
        for item in cast("list[dict[str, Any]]", prior["payload"]["decisions"])
    }

    updated = record_bulk_decision(
        changeset,
        prior,
        operation=cast("Any", operation),
        change_ids=[decided_id, safe_id],
        reason="bulk only the undecided item",
        decided_at=TIME_2,
        generated_at=TIME_2,
    )
    updated_by_id = {
        item["change_id"]: item
        for item in cast("list[dict[str, Any]]", updated["payload"]["decisions"])
    }

    assert updated_by_id[decided_id] == prior_by_id[decided_id]
    assert updated_by_id[safe_id]["decision"] == expected_decision
    assert updated["payload"]["audit"]["bulk_operations"] == [
        {"operation": operation, "change_ids": [safe_id], "decided_at": TIME_2}
    ]


@pytest.mark.parametrize("operation", ["accept_all_safe", "reject_selected", "mark_manual"])
def test_explicit_bulk_with_only_decided_ids_is_idempotent(
    changeset: dict[str, Any],
    operation: str,
) -> None:
    safe_id = cast("str", _safe_change(changeset)["change_id"])
    decided = record_decision(
        changeset,
        _draft(changeset),
        change_id=safe_id,
        decision="accepted_with_edit",
        final_text="carefully edited text",
        decided_at=TIME_1,
        generated_at=TIME_1,
    )

    retried = record_bulk_decision(
        changeset,
        decided,
        operation=cast("Any", operation),
        change_ids=[safe_id],
        reason="must not replace the prior decision",
        decided_at=TIME_2,
        generated_at=TIME_2,
    )

    assert retried == decided


@pytest.mark.parametrize("decision", ["rejected", "manual", "accepted_with_edit"])
def test_accept_all_safe_never_overwrites_an_existing_decision(
    changeset: dict[str, Any], decision: str
) -> None:
    safe_id = cast("str", _safe_change(changeset)["change_id"])
    decided = record_decision(
        changeset,
        _draft(changeset),
        change_id=safe_id,
        decision=cast("Any", decision),
        final_text="carefully edited text" if decision == "accepted_with_edit" else None,
        decided_at=TIME_1,
        generated_at=TIME_1,
    )

    retried = record_bulk_decision(
        changeset,
        decided,
        operation="accept_all_safe",
        decided_at=TIME_2,
        generated_at=TIME_2,
    )

    assert retried == decided


def test_atomic_write_uses_explicit_new_json_path_and_never_touches_tex(
    changeset: dict[str, Any], tmp_path: Path
) -> None:
    approval = record_decision(
        changeset,
        _draft(changeset),
        change_id=cast("str", _safe_change(changeset)["change_id"]),
        decision="accepted",
        decided_at=TIME_1,
        generated_at=TIME_1,
    )
    tex = tmp_path / "source.tex"
    tex.write_bytes(b"authoritative source\n")
    destination = tmp_path / "approval-r2.json"
    assert write_approval_json(changeset, approval, destination) == destination
    parsed = load_contract_json(destination.read_bytes())
    assert parsed == approval
    validate_contract(parsed)
    assert tex.read_bytes() == b"authoritative source\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "approval-r2.json",
        "source.tex",
    ]

    original = destination.read_bytes()
    with pytest.raises(ContractError):
        write_approval_json(changeset, approval, destination)
    assert destination.read_bytes() == original
    with pytest.raises(ContractError):
        write_approval_json(changeset, approval, tex)
    assert tex.read_bytes() == b"authoritative source\n"
    with pytest.raises(ContractError):
        write_approval_json(changeset, approval, tmp_path / "missing" / "approval.json")


def test_atomic_publish_failure_leaves_no_partial_output(
    changeset: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval = _draft(changeset)
    destination = tmp_path / "approval-r1.json"

    def fail_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("synthetic link failure")

    monkeypatch.setattr("latex_word_review.approval.os.link", fail_link)
    with pytest.raises(ContractError):
        write_approval_json(changeset, approval, destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
