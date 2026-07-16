"""P8 planning/application safety, determinism, and rollback tests."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest

import latex_word_review.applier as applier_module
from latex_word_review.applier import APPLY_MARKER, apply_patch_plan
from latex_word_review.approval import (
    create_approval_set,
    finalize_approval_set,
    record_decision,
)
from latex_word_review.canonical import seal_envelope, sha256_bytes, sha256_canonical
from latex_word_review.contracts import validate_contract
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.ids import stable_id
from latex_word_review.planner import plan_patch
from tests.test_contracts import _golden_contracts

TIME = "2026-07-16T15:00:00+09:00"
ACTOR = {"id": "synthetic-author", "display_name": "Synthetic Author"}


def _write_source(root: Path, text: str) -> str:
    root.mkdir()
    (root / "main.tex").write_text(text, encoding="utf-8", newline="")
    return discover_project(root).source_tree_sha256


def _changeset(text: str, changes: list[tuple[str, int, int, str]]) -> dict[str, Any]:
    document = copy.deepcopy(_golden_contracts()["ChangeSet"])
    raw_id = cast("str", document["payload"]["raw_events"][0]["raw_event_id"])
    data = text.encode("utf-8")
    normalized: list[dict[str, Any]] = []
    for ordinal, (kind, start, end, after) in enumerate(changes):
        before_bytes = data[start:end]
        before = before_bytes.decode("utf-8")
        change_id = stable_id("chg_", [kind, start, end, after, ordinal])
        unit_id = stable_id("unit_", ["main.tex", start, end, ordinal])
        fingerprint = sha256_canonical([kind, start, end, before, after, ordinal])
        normalized.append(
            {
                "change_id": change_id,
                "kind": kind,
                "native_kind": None,
                "raw_event_ids": [raw_id],
                "author": "Synthetic Reviewer",
                "authors": ["Synthetic Reviewer"],
                "timestamp": TIME,
                "timestamps": [TIME],
                "before": before,
                "after": after,
                "comment": None,
                "unit_id": unit_id,
                "source_location": {
                    "path": "main.tex",
                    "start_byte": start,
                    "end_byte": end,
                    "slice_sha256": sha256_bytes(before_bytes),
                    "encoding": "utf-8",
                    "newline": "lf" if "\n" in text else "none",
                    "start_line": 1,
                    "end_line": 1,
                    "start_column": 1,
                    "end_column": 1,
                },
                "resolution": {
                    "status": "exact",
                    "method": "bookmark",
                    "confidence": 1.0,
                    "candidates": [],
                },
                "safety_class": "plain_text_candidate",
                "initial_decision": "pending",
                "change_fingerprint": fingerprint,
            }
        )
    document["payload"]["changes"] = normalized
    document["payload"]["counts"]["changes"] = len(normalized)
    sealed = seal_envelope(document)
    validate_contract(sealed)
    return sealed


def _final_approval(
    changeset: dict[str, Any],
    decisions: list[tuple[str, str | None]],
) -> dict[str, Any]:
    current = create_approval_set(changeset, decided_by=ACTOR, generated_at=TIME)
    changes = cast("list[dict[str, Any]]", changeset["payload"]["changes"])
    for change, (decision, final_text) in zip(changes, decisions, strict=True):
        current = record_decision(
            changeset,
            current,
            change_id=cast("str", change["change_id"]),
            decision=cast("Any", decision),
            final_text=final_text,
            decided_at=TIME,
            generated_at=TIME,
        )
    return finalize_approval_set(changeset, current, generated_at=TIME)


@pytest.mark.parametrize(
    ("text", "change", "expected"),
    [
        ("Hello\n", ("insertion", 0, 0, "Careful "), "Careful Hello\n"),
        ("Hello\n", ("deletion", 0, 5, ""), "\n"),
        ("Hello\n", ("replacement", 0, 5, "Welcome"), "Welcome\n"),
    ],
)
def test_accepted_safe_kinds_plan_validate_and_apply(
    tmp_path: Path,
    text: str,
    change: tuple[str, int, int, str],
    expected: str,
) -> None:
    source = tmp_path / "source"
    source_tree = _write_source(source, text)
    changeset = _changeset(text, [change])
    approval = _final_approval(changeset, [("accepted", None)])
    before = (source / "main.tex").read_bytes()

    first = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )
    second = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )

    assert first == second
    assert first.document["payload"]["status"] == "ready"
    assert first.document["payload"]["mode"] == "dry_run"
    assert validate_contract(first.document).schema_name == "PatchPlan"
    assert (source / "main.tex").read_bytes() == before
    assert not list(source.glob("*.patch"))

    destination = tmp_path / "revised"
    applied = apply_patch_plan(
        source,
        destination,
        patch_plan=first.document,
        unified_diff=first.unified_diff,
        changeset=changeset,
        approval=approval,
    )
    assert applied.reused is False
    assert (destination / "main.tex").read_text(encoding="utf-8") == expected
    assert (destination / APPLY_MARKER).is_file()
    assert (source / "main.tex").read_bytes() == before


def test_rejected_change_produces_schema_valid_noop(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_tree = _write_source(source, "Hello\n")
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])
    approval = _final_approval(changeset, [("rejected", None)])

    result = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )

    assert result.document["payload"]["status"] == "noop"
    assert result.document["payload"]["excluded_changes"][0]["reason"] == "rejected"
    assert result.unified_diff == b""
    validate_contract(result.document)


@pytest.mark.parametrize("unsafe_text", [r"\\section{Injected}", "new\nparagraph", "50%"])
def test_accepted_structural_or_paragraph_edit_blocks_entire_plan(
    tmp_path: Path, unsafe_text: str
) -> None:
    source = tmp_path / "source"
    source_tree = _write_source(source, "Hello\n")
    changeset = _changeset("Hello\n", [("replacement", 0, 5, unsafe_text)])
    approval = _final_approval(changeset, [("accepted", None)])

    result = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )

    assert result.document["payload"]["status"] == "blocked"
    assert result.document["payload"]["accepted_but_blocked"] == [
        {
            "change_id": changeset["payload"]["changes"][0]["change_id"],
            "code": ErrorCode.PATCH_UNSAFE_KIND.value,
        }
    ]
    with pytest.raises(ContractError) as raised:
        apply_patch_plan(
            source,
            tmp_path / "blocked-output",
            patch_plan=result.document,
            unified_diff=result.unified_diff,
            changeset=changeset,
            approval=approval,
        )
    assert raised.value.code is ErrorCode.PATCH_ACCEPTED_BUT_BLOCKED


@pytest.mark.parametrize(
    ("text", "change"),
    [
        ("Hello\tworld\n", ("replacement", 5, 6, " ")),
        ("Hello\n", ("replacement", 0, 5, "safe\u00a0text")),
        ("Hello\n", ("replacement", 0, 5, "safe\u2028text")),
    ],
)
def test_tracked_non_u0020_whitespace_is_never_auto_patched(
    tmp_path: Path,
    text: str,
    change: tuple[str, int, int, str],
) -> None:
    source = tmp_path / "source"
    source_tree = _write_source(source, text)
    changeset = _changeset(text, [change])
    approval = _final_approval(changeset, [("accepted", None)])

    result = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )

    assert result.document["payload"]["status"] == "blocked"
    assert result.document["payload"]["accepted_but_blocked"][0]["code"] == (
        ErrorCode.PATCH_UNSAFE_KIND.value
    )


@pytest.mark.parametrize(
    "change",
    [
        ("insertion", 6, 6, " careful"),
        ("replacement", 6, 11, " revised"),
        ("replacement", 0, 5, "Hello "),
    ],
)
def test_ascii_space_created_across_source_boundary_is_blocked(
    tmp_path: Path,
    change: tuple[str, int, int, str],
) -> None:
    text = "Hello world\n"
    source = tmp_path / "source"
    source_tree = _write_source(source, text)
    changeset = _changeset(text, [change])
    approval = _final_approval(changeset, [("accepted", None)])

    result = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )

    assert result.document["payload"]["status"] == "blocked"
    assert result.document["payload"]["accepted_but_blocked"][0]["code"] == (
        ErrorCode.PATCH_UNSAFE_KIND.value
    )


def test_deleting_latex_syntax_is_blocked_even_if_changeset_claims_plain_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source_tree = _write_source(source, r"Hello \\label{unsafe}" + "\n")
    changeset = _changeset(
        r"Hello \\label{unsafe}" + "\n",
        [("deletion", 6, 20, "")],
    )
    approval = _final_approval(changeset, [("accepted", None)])

    result = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )

    assert result.document["payload"]["status"] == "blocked"
    assert result.document["payload"]["accepted_but_blocked"][0]["code"] == (
        ErrorCode.PATCH_UNSAFE_KIND.value
    )


def test_insertion_inside_utf8_codepoint_is_blocked(tmp_path: Path) -> None:
    text = "你好\n"
    source = tmp_path / "source"
    source_tree = _write_source(source, text)
    changeset = _changeset(text, [("insertion", 1, 1, "safe")])
    approval = _final_approval(changeset, [("accepted", None)])

    with pytest.raises(ContractError) as raised:
        plan_patch(
            source,
            source_tree_sha256=source_tree,
            changeset=changeset,
            approval=approval,
            generated_at=TIME,
        )
    assert raised.value.code is ErrorCode.PATCH_UNSAFE_KIND


def test_low_confidence_and_duplicate_candidates_are_blocked(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_tree = _write_source(source, "Hello\n")
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])
    change = changeset["payload"]["changes"][0]
    change["resolution"]["confidence"] = 0.98
    changeset = seal_envelope(changeset)
    approval = _final_approval(changeset, [("accepted", None)])
    low = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )
    assert low.document["payload"]["accepted_but_blocked"][0]["code"] == (
        ErrorCode.MAP_CONFIDENCE_LOW.value
    )

    candidates = copy.deepcopy(changeset)
    unit_id = candidates["payload"]["changes"][0]["unit_id"]
    candidates["payload"]["changes"][0]["resolution"] = {
        "status": "exact",
        "method": "bookmark",
        "confidence": 1.0,
        "candidates": [
            {"unit_id": unit_id, "confidence": 1.0, "evidence_sha256": sha256_bytes(b"1")},
            {"unit_id": unit_id, "confidence": 1.0, "evidence_sha256": sha256_bytes(b"2")},
        ],
    }
    candidates = seal_envelope(candidates)
    candidate_approval = _final_approval(candidates, [("accepted", None)])
    ambiguous = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=candidates,
        approval=candidate_approval,
        generated_at=TIME,
    )
    assert ambiguous.document["payload"]["accepted_but_blocked"][0]["code"] == (
        ErrorCode.MAP_AMBIGUOUS.value
    )


def test_tamper_drift_and_overlap_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_tree = _write_source(source, "Hello\n")
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])
    approval = _final_approval(changeset, [("accepted", None)])

    tampered = copy.deepcopy(changeset)
    tampered["payload"]["changes"][0]["after"] = "Tampered"
    with pytest.raises(ContractError) as integrity:
        plan_patch(
            source,
            source_tree_sha256=source_tree,
            changeset=tampered,
            approval=approval,
            generated_at=TIME,
        )
    assert integrity.value.code is ErrorCode.HASH_CHANGESET_MISMATCH

    (source / "main.tex").write_text("Drift\n", encoding="utf-8")
    with pytest.raises(ContractError) as drift:
        plan_patch(
            source,
            source_tree_sha256=source_tree,
            changeset=changeset,
            approval=approval,
            generated_at=TIME,
        )
    assert drift.value.code is ErrorCode.HASH_SOURCE_MISMATCH

    overlap_source = tmp_path / "overlap-source"
    overlap_tree = _write_source(overlap_source, "Hello\n")
    overlapping = _changeset(
        "Hello\n",
        [
            ("replacement", 0, 3, "Hey"),
            ("replacement", 2, 5, "LLO"),
        ],
    )
    overlap_approval = _final_approval(
        overlapping,
        [("accepted", None), ("accepted", None)],
    )
    with pytest.raises(ContractError) as overlap:
        plan_patch(
            overlap_source,
            source_tree_sha256=overlap_tree,
            changeset=overlapping,
            approval=overlap_approval,
            generated_at=TIME,
        )
    assert overlap.value.code is ErrorCode.PATCH_OVERLAP


def test_unicode_byte_spans_and_same_plan_idempotency(tmp_path: Path) -> None:
    text = "你好世界\n"
    source = tmp_path / "source"
    source_tree = _write_source(source, text)
    end = len("你好".encode())
    changeset = _changeset(text, [("replacement", 0, end, "您好")])
    approval = _final_approval(changeset, [("accepted", None)])
    plan = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )
    destination = tmp_path / "revised"

    first = apply_patch_plan(
        source,
        destination,
        patch_plan=plan.document,
        unified_diff=plan.unified_diff,
        changeset=changeset,
        approval=approval,
    )
    second = apply_patch_plan(
        source,
        destination,
        patch_plan=plan.document,
        unified_diff=plan.unified_diff,
        changeset=changeset,
        approval=approval,
    )

    assert first.reused is False
    assert second.reused is True
    assert (destination / "main.tex").read_text(encoding="utf-8") == "您好世界\n"


def test_apply_tamper_and_publish_failure_roll_back_without_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source_tree = _write_source(source, "Hello\n")
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])
    approval = _final_approval(changeset, [("accepted", None)])
    plan = plan_patch(
        source,
        source_tree_sha256=source_tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )
    original = (source / "main.tex").read_bytes()

    with pytest.raises(ContractError) as diff_tamper:
        apply_patch_plan(
            source,
            tmp_path / "tampered",
            patch_plan=plan.document,
            unified_diff=plan.unified_diff + b"tamper",
            changeset=changeset,
            approval=approval,
        )
    assert diff_tamper.value.code is ErrorCode.HASH_PATCHPLAN_MISMATCH

    def fail_publish(staged: Path, destination: Path) -> None:
        del staged, destination
        raise ContractError(ErrorCode.APPLY_PARTIAL_WRITE, "synthetic publish failure")

    monkeypatch.setattr(applier_module, "_publish_directory", fail_publish)
    destination = tmp_path / "rollback"
    with pytest.raises(ContractError) as rollback:
        apply_patch_plan(
            source,
            destination,
            patch_plan=plan.document,
            unified_diff=plan.unified_diff,
            changeset=changeset,
            approval=approval,
        )
    assert rollback.value.code is ErrorCode.APPLY_PARTIAL_WRITE
    assert not destination.exists()
    assert not list(tmp_path.glob(".rollback.apply-*"))
    assert (source / "main.tex").read_bytes() == original
