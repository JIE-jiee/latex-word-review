"""Additional authorization and atomic-apply rejection tests."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import latex_word_review.applier as applier_module
import latex_word_review.planner as planner_module
from latex_word_review.applier import APPLY_MARKER, apply_patch_plan
from latex_word_review.approval import create_approval_set
from latex_word_review.canonical import canonical_json, seal_envelope
from latex_word_review.discovery import (
    DEFAULT_DISCOVERY_LIMITS,
    DiscoveryLimits,
    ProjectDiscovery,
    discover_project,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes
from latex_word_review.ids import stable_id
from latex_word_review.planner import (
    apply_operations_to_bytes,
    build_unified_diff,
    plan_patch,
    verify_plan_identity,
)
from tests.test_contracts import _golden_contracts
from tests.test_plan_apply import ACTOR, TIME, _changeset, _final_approval, _write_source


def _assert_error(code: ErrorCode, operation: Callable[[], object]) -> None:
    with pytest.raises(ContractError) as raised:
        operation()
    assert raised.value.code is code


def _ready_plan(
    tmp_path: Path,
    *,
    text: str = "Hello\n",
    reserved_marker: bool = False,
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any], bytes]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source"
    _write_source(source, text)
    if reserved_marker:
        (source / APPLY_MARKER).write_text("reserved", encoding="utf-8")
    tree = discover_project(source).source_tree_sha256
    end = len(text.rstrip("\n").encode("utf-8"))
    changeset = _changeset(text, [("replacement", 0, end, "Welcome")])
    approval = _final_approval(changeset, [("accepted", None)])
    result = plan_patch(
        source,
        source_tree_sha256=tree,
        changeset=changeset,
        approval=approval,
        generated_at=TIME,
    )
    return source, changeset, approval, result.document, result.unified_diff


def _reseal_plan(document: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(document))
    payload = cast("dict[str, Any]", result["payload"])
    without_id = copy.deepcopy(payload)
    without_id.pop("patch_plan_id")
    plan_id = stable_id("plan_", without_id)
    payload["patch_plan_id"] = plan_id
    result["object_id"] = plan_id
    return seal_envelope(result)


def test_authorization_rejects_wrong_schema_draft_and_resealed_bindings(tmp_path: Path) -> None:
    del tmp_path
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])
    draft = create_approval_set(changeset, decided_by=ACTOR, generated_at=TIME)
    final = _final_approval(changeset, [("accepted", None)])
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: planner_module._authorization_context(_golden_contracts()["SourceManifest"], final),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: planner_module._authorization_context(changeset, _golden_contracts()["ChangeSet"]),
    )
    _assert_error(
        ErrorCode.APPROVAL_NOT_FINAL,
        lambda: planner_module._authorization_context(changeset, draft),
    )

    wrong_change = copy.deepcopy(final)
    wrong_change["payload"]["changeset_sha256"] = "sha256:" + "0" * 64
    _assert_error(
        ErrorCode.HASH_CHANGESET_MISMATCH,
        lambda: planner_module._authorization_context(changeset, seal_envelope(wrong_change)),
    )
    wrong_source = copy.deepcopy(final)
    wrong_source["payload"]["source_manifest_sha256"] = "sha256:" + "0" * 64
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: planner_module._authorization_context(changeset, seal_envelope(wrong_source)),
    )
    wrong_run = copy.deepcopy(final)
    wrong_run["run_id"] = "run_019bc0ab-2400-7000-8000-000000000099"
    _assert_error(
        ErrorCode.HASH_CHANGESET_MISMATCH,
        lambda: planner_module._authorization_context(changeset, seal_envelope(wrong_run)),
    )


def test_authorization_rejects_missing_reordered_and_fingerprint_decisions() -> None:
    changeset = _changeset(
        "Hello world\n",
        [
            ("replacement", 0, 5, "Welcome"),
            ("replacement", 6, 11, "reader"),
        ],
    )
    final = _final_approval(changeset, [("accepted", None), ("accepted", None)])

    missing = copy.deepcopy(final)
    cast("list[dict[str, Any]]", missing["payload"]["decisions"]).pop()
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: planner_module._authorization_context(changeset, seal_envelope(missing)),
    )
    reordered = copy.deepcopy(final)
    cast("list[dict[str, Any]]", reordered["payload"]["decisions"]).reverse()
    _assert_error(
        ErrorCode.APPROVAL_CHANGE_UNKNOWN,
        lambda: planner_module._authorization_context(changeset, seal_envelope(reordered)),
    )
    fingerprint = copy.deepcopy(final)
    fingerprint["payload"]["decisions"][0]["change_fingerprint"] = "sha256:" + "0" * 64
    _assert_error(
        ErrorCode.HASH_CHANGESET_MISMATCH,
        lambda: planner_module._authorization_context(changeset, seal_envelope(fingerprint)),
    )


def test_resolution_policy_matrix_covers_all_fail_closed_reasons() -> None:
    base = cast(
        "dict[str, Any]",
        copy.deepcopy(
            _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])["payload"]["changes"][0]
        ),
    )
    cases: list[tuple[Callable[[dict[str, Any]], None], ErrorCode]] = [
        (lambda item: item.__setitem__("kind", "move"), ErrorCode.PATCH_UNSAFE_KIND),
        (lambda item: item.__setitem__("safety_class", "manual"), ErrorCode.PATCH_UNSAFE_KIND),
        (
            lambda item: cast("dict[str, Any]", item["resolution"]).__setitem__(
                "status", "unmapped"
            ),
            ErrorCode.MAP_UNMATCHED,
        ),
        (
            lambda item: cast("dict[str, Any]", item["resolution"]).__setitem__("confidence", 0.5),
            ErrorCode.MAP_CONFIDENCE_LOW,
        ),
        (
            lambda item: cast("dict[str, Any]", item["resolution"]).__setitem__(
                "candidates",
                [
                    {"unit_id": item["unit_id"], "confidence": 1.0},
                    {"unit_id": item["unit_id"], "confidence": 1.0},
                ],
            ),
            ErrorCode.MAP_AMBIGUOUS,
        ),
        (
            lambda item: cast("dict[str, Any]", item["resolution"]).__setitem__(
                "candidates", [{"unit_id": "other", "confidence": 1.0}]
            ),
            ErrorCode.MAP_AMBIGUOUS,
        ),
        (
            lambda item: cast("dict[str, Any]", item["resolution"]).__setitem__(
                "candidates", [{"unit_id": item["unit_id"], "confidence": 0.5}]
            ),
            ErrorCode.MAP_CONFIDENCE_LOW,
        ),
        (lambda item: item.__setitem__("unit_id", None), ErrorCode.MAP_UNMATCHED),
        (lambda item: item.__setitem__("source_location", None), ErrorCode.MAP_UNMATCHED),
    ]
    for mutate, expected in cases:
        item = copy.deepcopy(base)
        mutate(item)
        assert planner_module._resolution_block_code(item) is expected


def test_plain_text_and_range_policy_handles_insert_delete_and_boundaries() -> None:
    base = cast(
        "dict[str, Any]",
        copy.deepcopy(
            _changeset("plain", [("replacement", 0, 5, "safe")])["payload"]["changes"][0]
        ),
    )

    def eligibility(
        before: str,
        replacement: str,
        kind: str = "replacement",
        *,
        prefix: str = "L",
        suffix: str = "R",
    ) -> planner_module.PatchEligibility:
        change = copy.deepcopy(base)
        change.update({"kind": kind, "before": before, "after": replacement})
        return planner_module.evaluate_patch_eligibility(
            change,
            source_prefix=prefix,
            source_suffix=suffix,
        )

    assert eligibility("same", "same").block_code is ErrorCode.PATCH_UNSAFE_KIND
    assert eligibility("line\n", "line").block_code is ErrorCode.PATCH_UNSAFE_KIND
    assert eligibility("plain", "50%").block_code is ErrorCode.PATCH_UNSAFE_KIND
    assert eligibility("unexpected", "insert", "insertion").block_code is (
        ErrorCode.PATCH_UNSAFE_KIND
    )
    assert eligibility("delete", "replacement", "deletion").block_code is (
        ErrorCode.PATCH_UNSAFE_KIND
    )
    assert eligibility("plain", "safe").eligible

    overlap = planner_module._ranges_overlap
    assert not overlap(
        {"path": "a", "start_byte": 0, "end_byte": 1}, {"path": "b", "start_byte": 0, "end_byte": 1}
    )
    assert overlap(
        {"path": "a", "start_byte": 1, "end_byte": 1}, {"path": "a", "start_byte": 1, "end_byte": 1}
    )
    assert overlap(
        {"path": "a", "start_byte": 1, "end_byte": 1}, {"path": "a", "start_byte": 0, "end_byte": 2}
    )
    assert overlap(
        {"path": "a", "start_byte": 0, "end_byte": 2}, {"path": "a", "start_byte": 1, "end_byte": 1}
    )
    assert not overlap(
        {"path": "a", "start_byte": 0, "end_byte": 1}, {"path": "a", "start_byte": 1, "end_byte": 2}
    )


def test_make_operation_rejects_encoding_missing_path_span_slice_before_and_invalid_utf8(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_source(source, "Hello\n")
    discovery = discover_project(source)
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])
    approval = _final_approval(changeset, [("accepted", None)])
    change = cast("dict[str, Any]", changeset["payload"]["changes"][0])
    decision = cast("dict[str, Any]", approval["payload"]["decisions"][0])

    encoding = copy.deepcopy(change)
    encoding["source_location"]["encoding"] = "latin-1"
    assert (
        planner_module._make_operation(
            encoding, decision, discovery, source, DEFAULT_DISCOVERY_LIMITS
        )[1]
        is ErrorCode.PATCH_UNSAFE_KIND
    )
    mixed = copy.deepcopy(change)
    mixed["source_location"]["newline"] = "mixed"
    assert (
        planner_module._make_operation(
            mixed, decision, discovery, source, DEFAULT_DISCOVERY_LIMITS
        )[1]
        is ErrorCode.PATCH_UNSAFE_KIND
    )

    absent = copy.deepcopy(change)
    absent["source_location"]["path"] = "absent.tex"
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: planner_module._make_operation(
            absent, decision, discovery, source, DEFAULT_DISCOVERY_LIMITS
        ),
    )
    span = copy.deepcopy(change)
    span["source_location"]["end_byte"] = 999
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: planner_module._make_operation(
            span, decision, discovery, source, DEFAULT_DISCOVERY_LIMITS
        ),
    )
    sliced = copy.deepcopy(change)
    sliced["source_location"]["slice_sha256"] = "sha256:" + "0" * 64
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: planner_module._make_operation(
            sliced, decision, discovery, source, DEFAULT_DISCOVERY_LIMITS
        ),
    )
    before = copy.deepcopy(change)
    before["before"] = "Other"
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: planner_module._make_operation(
            before, decision, discovery, source, DEFAULT_DISCOVERY_LIMITS
        ),
    )

    invalid = b"\xffello\n"
    (source / "main.tex").write_bytes(invalid)
    digest = digest_bytes(invalid)
    source_file = replace(discovery.files[0], size_bytes=digest.size_bytes, sha256=digest.sha256)
    invalid_discovery = replace(discovery, files=(source_file,))
    invalid_change = copy.deepcopy(change)
    invalid_change["source_location"]["slice_sha256"] = digest_bytes(invalid[:5]).sha256
    _assert_error(
        ErrorCode.PATCH_UNSAFE_KIND,
        lambda: planner_module._make_operation(
            invalid_change,
            decision,
            invalid_discovery,
            source,
            DEFAULT_DISCOVERY_LIMITS,
        ),
    )


def test_in_memory_apply_rejects_file_slice_and_text_drift(tmp_path: Path) -> None:
    _, _, _, plan, _ = _ready_plan(tmp_path)
    operation = cast("dict[str, Any]", plan["payload"]["operations"][0])
    originals = {"main.tex": b"Hello\n"}
    file_hash = copy.deepcopy(operation)
    file_hash["target_file_sha256"] = "sha256:" + "0" * 64
    _assert_error(
        ErrorCode.PATCH_SOURCE_DRIFT,
        lambda: apply_operations_to_bytes(originals, [file_hash]),
    )
    slice_hash = copy.deepcopy(operation)
    slice_hash["expected_bytes_sha256"] = "sha256:" + "0" * 64
    _assert_error(
        ErrorCode.PATCH_SOURCE_DRIFT,
        lambda: apply_operations_to_bytes(originals, [slice_hash]),
    )
    text = copy.deepcopy(operation)
    text["before_text"] = "Other"
    _assert_error(
        ErrorCode.PATCH_SOURCE_DRIFT,
        lambda: apply_operations_to_bytes(originals, [text]),
    )
    assert build_unified_diff(originals, originals) == b""


def test_plan_rejects_invalid_diff_classification_and_detects_final_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    tree = _write_source(source, "Hello\n")
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])
    approval = _final_approval(changeset, [("accepted", None)])
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: plan_patch(
            source,
            source_tree_sha256=tree,
            changeset=changeset,
            approval=approval,
            generated_at=TIME,
            confidentiality="secret",
        ),
    )

    original_discover = discover_project
    calls = 0

    def drift(
        root: Path,
        *,
        main_document: str | None = None,
        limits: DiscoveryLimits = DEFAULT_DISCOVERY_LIMITS,
    ) -> ProjectDiscovery:
        nonlocal calls
        calls += 1
        result = original_discover(root, main_document=main_document, limits=limits)
        return replace(result, profile_sha256="sha256:" + "0" * 64) if calls == 2 else result

    monkeypatch.setattr(planner_module, "discover_project", drift)
    _assert_error(
        ErrorCode.HASH_SOURCE_MISMATCH,
        lambda: plan_patch(
            source,
            source_tree_sha256=tree,
            changeset=changeset,
            approval=approval,
            generated_at=TIME,
        ),
    )


def test_plan_identity_rejects_wrong_schema_policy_operation_hunk_and_plan_ids(
    tmp_path: Path,
) -> None:
    _, _, _, plan, _ = _ready_plan(tmp_path)
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: verify_plan_identity(_golden_contracts()["ChangeSet"]),
    )
    policy = copy.deepcopy(plan)
    policy["payload"]["policy_sha256"] = "sha256:" + "0" * 64
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH, lambda: verify_plan_identity(seal_envelope(policy))
    )
    operation = copy.deepcopy(plan)
    operation["payload"]["operations"][0]["operation_id"] = "op_" + "0" * 43
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        lambda: verify_plan_identity(seal_envelope(operation)),
    )
    hunk = copy.deepcopy(plan)
    hunk["payload"]["operations"][0]["diff_hunk_sha256"] = "sha256:" + "0" * 64
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH, lambda: verify_plan_identity(seal_envelope(hunk))
    )
    plan_id = copy.deepcopy(plan)
    plan_id["payload"]["patch_plan_id"] = "plan_" + "0" * 43
    plan_id["object_id"] = plan_id["payload"]["patch_plan_id"]
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH, lambda: verify_plan_identity(seal_envelope(plan_id))
    )


def test_apply_inventory_writable_cleanup_write_verify_and_publish_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first = root / "a.txt"
    second = root / "b.txt"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    monkeypatch.setattr(applier_module, "_is_link_or_junction", lambda path: path == first)
    _assert_error(
        ErrorCode.PATH_LINK_ESCAPE, lambda: applier_module._inventory(root, max_file_bytes=10)
    )
    monkeypatch.undo()

    monkeypatch.setattr(applier_module, "validate_relative_path", lambda value: "same")
    _assert_error(
        ErrorCode.SCHEMA_INVALID, lambda: applier_module._inventory(root, max_file_bytes=10)
    )
    monkeypatch.undo()
    monkeypatch.setattr(applier_module, "_MAX_TREE_FILES", 1)
    _assert_error(
        ErrorCode.SCHEMA_INVALID, lambda: applier_module._inventory(root, max_file_bytes=10)
    )
    monkeypatch.undo()
    monkeypatch.setattr(applier_module, "_MAX_TREE_BYTES", 1)
    _assert_error(
        ErrorCode.SCHEMA_INVALID, lambda: applier_module._inventory(root, max_file_bytes=10)
    )
    monkeypatch.undo()

    concrete_path = type(root)
    original_chmod = concrete_path.chmod

    def denied(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == first:
            raise PermissionError("denied")
        original_chmod(self, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(concrete_path, "chmod", denied)
    _assert_error(ErrorCode.APPLY_PARTIAL_WRITE, lambda: applier_module._make_writable(root))
    monkeypatch.undo()

    _assert_error(
        ErrorCode.APPLY_PARTIAL_WRITE,
        lambda: applier_module._remove_owned_tree(tmp_path / "foreign", tmp_path, ".owned-"),
    )
    applier_module._remove_owned_tree(tmp_path / ".owned-missing", tmp_path, ".owned-")
    directory_target = tmp_path / "directory"
    directory_target.mkdir()
    _assert_error(
        ErrorCode.APPLY_PARTIAL_WRITE, lambda: applier_module._write_file(directory_target, b"x")
    )

    marker = {"value": 1}
    expected = {"a.txt": b"a"}
    verify_root = tmp_path / "verify"
    verify_root.mkdir()
    (verify_root / "a.txt").write_bytes(b"a")
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        lambda: applier_module._verify_output(verify_root, marker, expected, max_file_bytes=100),
    )
    (verify_root / APPLY_MARKER).write_bytes(b"wrong")
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        lambda: applier_module._verify_output(verify_root, marker, expected, max_file_bytes=100),
    )
    (verify_root / APPLY_MARKER).write_bytes(canonical_json(marker) + b"\n")
    (verify_root / "a.txt").write_bytes(b"wrong")
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        lambda: applier_module._verify_output(verify_root, marker, expected, max_file_bytes=100),
    )

    staged = tmp_path / "staged"
    staged.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    _assert_error(
        ErrorCode.APPLY_PARTIAL_WRITE,
        lambda: applier_module._publish_directory(staged, destination),
    )


def test_apply_rejects_noop_diff_hash_size_source_drift_reserved_and_destination_conflict(
    tmp_path: Path,
) -> None:
    source, changeset, approval, plan, diff = _ready_plan(tmp_path)
    rejected_approval = _final_approval(changeset, [("rejected", None)])
    tree = discover_project(source).source_tree_sha256
    noop = plan_patch(
        source,
        source_tree_sha256=tree,
        changeset=changeset,
        approval=rejected_approval,
        generated_at=TIME,
    )
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        lambda: apply_patch_plan(
            source,
            tmp_path / "noop",
            patch_plan=noop.document,
            unified_diff=b"unexpected",
            changeset=changeset,
            approval=rejected_approval,
        ),
    )
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        lambda: apply_patch_plan(
            source,
            tmp_path / "hash",
            patch_plan=plan,
            unified_diff=diff + b"tamper",
            changeset=changeset,
            approval=approval,
        ),
    )
    size = copy.deepcopy(plan)
    size["payload"]["unified_diff"]["size_bytes"] += 1
    size = _reseal_plan(size)
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        lambda: apply_patch_plan(
            source,
            tmp_path / "size",
            patch_plan=size,
            unified_diff=diff,
            changeset=changeset,
            approval=approval,
        ),
    )

    (source / "main.tex").write_text("Drift\n", encoding="utf-8")
    _assert_error(
        ErrorCode.PATCH_SOURCE_DRIFT,
        lambda: apply_patch_plan(
            source,
            tmp_path / "drift",
            patch_plan=plan,
            unified_diff=diff,
            changeset=changeset,
            approval=approval,
        ),
    )

    source2, changeset2, approval2, plan2, diff2 = _ready_plan(
        tmp_path / "case2", reserved_marker=True
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: apply_patch_plan(
            source2,
            tmp_path / "case2-output",
            patch_plan=plan2,
            unified_diff=diff2,
            changeset=changeset2,
            approval=approval2,
        ),
    )

    source3, changeset3, approval3, plan3, diff3 = _ready_plan(tmp_path / "case3")
    conflict = tmp_path / "case3-conflict"
    conflict.write_bytes(b"competitor")
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        lambda: apply_patch_plan(
            source3,
            conflict,
            patch_plan=plan3,
            unified_diff=diff3,
            changeset=changeset3,
            approval=approval3,
        ),
    )
    assert conflict.read_bytes() == b"competitor"


def test_apply_detects_actual_diff_and_source_changes_before_and_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, changeset, approval, plan, diff = _ready_plan(tmp_path)
    monkeypatch.setattr(
        applier_module, "build_unified_diff", lambda originals, revised: b"different"
    )
    _assert_error(
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        lambda: apply_patch_plan(
            source,
            tmp_path / "actual-diff",
            patch_plan=plan,
            unified_diff=diff,
            changeset=changeset,
            approval=approval,
        ),
    )
    monkeypatch.undo()

    original_verify = applier_module._verify_output
    mutated = False

    def mutate_before_publish(
        root: Path,
        marker: Mapping[str, Any],
        expected_files: Mapping[str, bytes],
        *,
        max_file_bytes: int,
    ) -> None:
        nonlocal mutated
        original_verify(root, marker, expected_files, max_file_bytes=max_file_bytes)
        if not mutated and root.name.startswith(".before.apply-"):
            mutated = True
            (source / "main.tex").write_text("Drift\n", encoding="utf-8")

    monkeypatch.setattr(applier_module, "_verify_output", mutate_before_publish)
    _assert_error(
        ErrorCode.PATCH_SOURCE_DRIFT,
        lambda: apply_patch_plan(
            source,
            tmp_path / "before",
            patch_plan=plan,
            unified_diff=diff,
            changeset=changeset,
            approval=approval,
        ),
    )
    assert not (tmp_path / "before").exists()
    monkeypatch.undo()

    (source / "main.tex").write_bytes(b"Hello\n")
    mutated = False

    def mutate_after_publish(
        root: Path,
        marker: Mapping[str, Any],
        expected_files: Mapping[str, bytes],
        *,
        max_file_bytes: int,
    ) -> None:
        nonlocal mutated
        original_verify(root, marker, expected_files, max_file_bytes=max_file_bytes)
        if not mutated and root == tmp_path / "after":
            mutated = True
            (source / "main.tex").write_text("Drift\n", encoding="utf-8")

    monkeypatch.setattr(applier_module, "_verify_output", mutate_after_publish)
    _assert_error(
        ErrorCode.PATCH_SOURCE_DRIFT,
        lambda: apply_patch_plan(
            source,
            tmp_path / "after",
            patch_plan=plan,
            unified_diff=diff,
            changeset=changeset,
            approval=approval,
        ),
    )
    assert (tmp_path / "after").is_dir()
