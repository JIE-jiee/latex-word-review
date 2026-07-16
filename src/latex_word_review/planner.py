"""Deterministic, fail-closed planning for the v0.1 plain-text patch subset."""

from __future__ import annotations

import copy
import difflib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from latex_word_review.__about__ import __version__
from latex_word_review.canonical import sha256_canonical
from latex_word_review.contracts import make_envelope, validate_contract
from latex_word_review.discovery import (
    DEFAULT_DISCOVERY_LIMITS,
    DiscoveryLimits,
    ProjectDiscovery,
    discover_project,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes, read_stable_bytes
from latex_word_review.ids import derive_artifact_id, stable_id
from latex_word_review.paths import resolve_within, validate_relative_path
from latex_word_review.source_units import normalize_review_text

_INTERFACE_VERSION: Final = "planner-v1alpha1"
_CONFIDENCE_THRESHOLD: Final = 0.99
_STRUCTURAL_CHARACTERS: Final = frozenset("\\{}$%&#_^~")
POLICY_SHA256: Final = sha256_canonical(
    {
        "name": "v0.1-exact-plain-text-only",
        "version": "2",
        "confidence_threshold": _CONFIDENCE_THRESHOLD,
        "allowed_kinds": ["insertion", "deletion", "replacement"],
        "forbidden_replacement_characters": sorted(_STRUCTURAL_CHARACTERS),
        "whitespace": "only-u+0020-and-no-lossy-boundary-normalization",
        "paragraph_changes": "denied",
        "accepted_unsafe": "block_entire_plan",
    }
)


@dataclass(frozen=True, slots=True)
class PatchPlanResult:
    """A sealed PatchPlan plus its deterministic, not-yet-written diff bytes."""

    document: dict[str, Any]
    unified_diff: bytes


@dataclass(frozen=True, slots=True)
class PatchEligibility:
    """Shared planner/UI result for the exact plain-text auto-patch policy."""

    block_code: ErrorCode | None
    reason: str | None
    source_context_required: bool
    replacement_text: str | None

    @property
    def eligible(self) -> bool:
        """Return whether the policy has proved the edit automatically patchable."""

        return self.block_code is None and not self.source_context_required


@dataclass(frozen=True, slots=True)
class _Authorization:
    changeset_sha256: str
    approval_sha256: str
    source_manifest_sha256: str
    changes: tuple[Mapping[str, Any], ...]
    decisions: Mapping[str, Mapping[str, Any]]


def _producer() -> dict[str, Any]:
    return {
        "name": "latex-word-review",
        "version": __version__,
        "interface_version": _INTERFACE_VERSION,
        "distribution": "python-package",
        "executable_sha256": None,
        "configuration_sha256": POLICY_SHA256,
    }


def _authorization_context(
    changeset: Mapping[str, Any], approval: Mapping[str, Any]
) -> _Authorization:
    changeset_receipt = validate_contract(changeset)
    approval_receipt = validate_contract(approval)
    if changeset_receipt.schema_name != "ChangeSet":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "planner input must include a ChangeSet")
    if approval_receipt.schema_name != "ApprovalSet":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "planner input must include an ApprovalSet")

    changeset_payload = cast("Mapping[str, Any]", changeset["payload"])
    approval_payload = cast("Mapping[str, Any]", approval["payload"])
    if approval_payload["status"] != "final":
        raise ContractError(ErrorCode.APPROVAL_NOT_FINAL, "PatchPlan requires a final ApprovalSet")
    if approval_payload["changeset_sha256"] != changeset_receipt.payload_sha256:
        raise ContractError(
            ErrorCode.HASH_CHANGESET_MISMATCH,
            "ApprovalSet is bound to a different ChangeSet payload",
        )
    source_manifest_sha256 = cast("str", changeset_payload["source_manifest_sha256"])
    if approval_payload["source_manifest_sha256"] != source_manifest_sha256:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "ApprovalSet and ChangeSet source bindings differ",
        )
    if approval["run_id"] != changeset["run_id"]:
        raise ContractError(ErrorCode.HASH_CHANGESET_MISMATCH, "approval run binding differs")

    changes = tuple(cast("Sequence[Mapping[str, Any]]", changeset_payload["changes"]))
    change_index = {cast("str", item["change_id"]): item for item in changes}
    decision_items = cast("Sequence[Mapping[str, Any]]", approval_payload["decisions"])
    decisions = {cast("str", item["change_id"]): item for item in decision_items}
    if set(decisions) != set(change_index) or approval_payload["undecided_change_ids"]:
        raise ContractError(
            ErrorCode.APPROVAL_NOT_FINAL,
            "final ApprovalSet must decide every bound ChangeSet change exactly once",
        )
    if tuple(decisions) != tuple(change_index):
        raise ContractError(
            ErrorCode.APPROVAL_CHANGE_UNKNOWN,
            "ApprovalSet decisions must retain ChangeSet order",
        )
    for change_id, decision in decisions.items():
        if decision["change_fingerprint"] != change_index[change_id]["change_fingerprint"]:
            raise ContractError(
                ErrorCode.HASH_CHANGESET_MISMATCH,
                "approval decision fingerprint differs from the sealed ChangeSet",
            )
    return _Authorization(
        changeset_sha256=changeset_receipt.payload_sha256,
        approval_sha256=approval_receipt.payload_sha256,
        source_manifest_sha256=source_manifest_sha256,
        changes=changes,
        decisions=decisions,
    )


def _blocked(
    code: ErrorCode,
    reason: str,
    *,
    replacement_text: str | None = None,
) -> PatchEligibility:
    return PatchEligibility(
        block_code=code,
        reason=reason,
        source_context_required=False,
        replacement_text=replacement_text,
    )


def _resolution_block(change: Mapping[str, Any]) -> PatchEligibility | None:
    if change["kind"] not in {"insertion", "deletion", "replacement"}:
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the change kind is outside the automatic plain-text subset",
        )
    if change["safety_class"] != "plain_text_candidate":
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the safety class is not plain_text_candidate",
        )
    resolution = cast("Mapping[str, Any]", change["resolution"])
    if resolution["status"] != "exact":
        return _blocked(ErrorCode.MAP_UNMATCHED, "the source resolution is not exact")
    if float(cast("float", resolution["confidence"])) < _CONFIDENCE_THRESHOLD:
        return _blocked(
            ErrorCode.MAP_CONFIDENCE_LOW,
            "the source confidence is below 0.99",
        )
    candidates = cast("Sequence[Mapping[str, Any]]", resolution["candidates"])
    if len(candidates) > 1:
        return _blocked(
            ErrorCode.MAP_AMBIGUOUS,
            "the source resolution has multiple candidates",
        )
    if candidates:
        candidate = candidates[0]
        if candidate["unit_id"] != change["unit_id"]:
            return _blocked(
                ErrorCode.MAP_AMBIGUOUS,
                "the source candidate points to another review unit",
            )
        if float(cast("float", candidate["confidence"])) < _CONFIDENCE_THRESHOLD:
            return _blocked(
                ErrorCode.MAP_CONFIDENCE_LOW,
                "the source candidate confidence is below 0.99",
            )
    if change["unit_id"] is None or change["source_location"] is None:
        return _blocked(
            ErrorCode.MAP_UNMATCHED,
            "the source unit or byte location is missing",
        )
    return None


def _resolution_block_code(change: Mapping[str, Any]) -> ErrorCode | None:
    blocked = _resolution_block(change)
    return None if blocked is None else blocked.block_code


def _contains_non_ascii_space_whitespace(text: str) -> bool:
    return any(character != " " and character.isspace() for character in text)


def _left_review_context(prefix: str) -> str:
    """Keep the trailing ASCII-space run and one non-whitespace anchor."""

    cursor = len(prefix)
    while cursor and prefix[cursor - 1] == " ":
        cursor -= 1
    if cursor and not prefix[cursor - 1].isspace():
        cursor -= 1
    return prefix[cursor:]


def _right_review_context(suffix: str) -> str:
    """Keep the leading ASCII-space run and one non-whitespace anchor."""

    cursor = 0
    while cursor < len(suffix) and suffix[cursor] == " ":
        cursor += 1
    if cursor < len(suffix) and not suffix[cursor].isspace():
        cursor += 1
    return suffix[:cursor]


def evaluate_patch_eligibility(
    change: Mapping[str, Any],
    decision: Mapping[str, Any] | None = None,
    *,
    source_prefix: str | None = None,
    source_suffix: str | None = None,
) -> PatchEligibility:
    """Evaluate the single fail-closed auto-patch policy used by planner and UI.

    The browser has no source bytes, so an edit whose leading/trailing ASCII
    spaces need adjacent source text is reported as requiring context.  The
    planner always supplies exact decoded prefix/suffix text and makes the
    authoritative decision.
    """

    resolution_block = _resolution_block(change)
    if resolution_block is not None:
        return resolution_block

    before = change["before"]
    after = change["after"]
    if not isinstance(before, str) or not isinstance(after, str):
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the before/after payload is not plain text",
        )
    replacement = after
    if decision is not None and decision["decision"] == "accepted_with_edit":
        replacement = decision["final_text"]
        if not isinstance(replacement, str):
            return _blocked(
                ErrorCode.PATCH_UNSAFE_KIND,
                "the edited final text is missing",
            )

    kind = cast("str", change["kind"])
    if before == replacement:
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the approved replacement would not change the source",
            replacement_text=replacement,
        )
    if _contains_non_ascii_space_whitespace(before + replacement):
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the text contains Unicode whitespace other than U+0020 SPACE",
            replacement_text=replacement,
        )
    if any(character in _STRUCTURAL_CHARACTERS for character in before + replacement):
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the text contains a LaTeX structural character",
            replacement_text=replacement,
        )
    if kind == "insertion" and before:
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "an insertion unexpectedly contains before text",
            replacement_text=replacement,
        )
    if kind == "deletion" and replacement:
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "a deletion unexpectedly contains replacement text",
            replacement_text=replacement,
        )
    if "  " in before or "  " in replacement:
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the edit contains consecutive ASCII spaces that review normalization collapses",
            replacement_text=replacement,
        )

    context_sensitive = (
        not replacement
        or before.startswith(" ")
        or before.endswith(" ")
        or replacement.startswith(" ")
        or replacement.endswith(" ")
    )
    if source_prefix is None or source_suffix is None:
        if source_prefix is not None or source_suffix is not None:
            return _blocked(
                ErrorCode.PATCH_UNSAFE_KIND,
                "both source prefix and suffix are required for boundary validation",
                replacement_text=replacement,
            )
        if context_sensitive:
            return PatchEligibility(
                block_code=None,
                reason="exact source prefix/suffix context is required to prove whitespace safety",
                source_context_required=True,
                replacement_text=replacement,
            )
        return PatchEligibility(None, None, False, replacement)

    left = _left_review_context(source_prefix)
    right = _right_review_context(source_suffix)
    original_window = left + before + right
    revised_window = left + replacement + right
    if normalize_review_text(original_window) != original_window:
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the original source context changes under review normalization",
            replacement_text=replacement,
        )
    if "  " in revised_window:
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the edit would create consecutive ASCII spaces at a source boundary",
            replacement_text=replacement,
        )
    if normalize_review_text(revised_window) != revised_window:
        return _blocked(
            ErrorCode.PATCH_UNSAFE_KIND,
            "the edited source context would change under review normalization",
            replacement_text=replacement,
        )
    return PatchEligibility(None, None, False, replacement)


def _ranges_overlap(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    if first["path"] != second["path"]:
        return False
    first_start, first_end = int(first["start_byte"]), int(first["end_byte"])
    second_start, second_end = int(second["start_byte"]), int(second["end_byte"])
    if first_start == first_end and second_start == second_end:
        return first_start == second_start
    if first_start == first_end:
        return second_start <= first_start <= second_end
    if second_start == second_end:
        return first_start <= second_start <= first_end
    return max(first_start, second_start) < min(first_end, second_end)


def _operation_identity(operation_without_id: Mapping[str, Any]) -> str:
    return stable_id("op_", operation_without_id)


def _make_operation(
    change: Mapping[str, Any],
    decision: Mapping[str, Any],
    discovery: ProjectDiscovery,
    source_root: Path,
    limits: DiscoveryLimits,
) -> tuple[dict[str, Any] | None, ErrorCode | None]:
    preliminary = evaluate_patch_eligibility(change, decision)
    if preliminary.block_code is not None:
        return None, preliminary.block_code
    location = cast("Mapping[str, Any]", change["source_location"])
    if location["encoding"] != "utf-8" or location["newline"] == "mixed":
        return None, ErrorCode.PATCH_UNSAFE_KIND
    path = validate_relative_path(cast("str", location["path"]))
    source_file = next((item for item in discovery.files if item.path == path), None)
    if source_file is None:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH, "patch target is absent from source tree"
        )
    target_path = resolve_within(source_root, path)
    data = read_stable_bytes(target_path, max_bytes=limits.max_file_bytes)
    file_digest = digest_bytes(data)
    if file_digest.sha256 != source_file.sha256:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "patch target file hash drifted")
    start, end = int(location["start_byte"]), int(location["end_byte"])
    if end > len(data):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "patch target span exceeds its file")
    expected_bytes = data[start:end]
    expected_digest = digest_bytes(expected_bytes).sha256
    if expected_digest != location["slice_sha256"]:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "patch target slice hash drifted")
    try:
        data.decode("utf-8", errors="strict")
        source_prefix = data[:start].decode("utf-8", errors="strict")
        actual_before = expected_bytes.decode("utf-8", errors="strict")
        source_suffix = data[end:].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(
            ErrorCode.PATCH_UNSAFE_KIND,
            "target file or byte boundary is not valid UTF-8 text",
        ) from exc
    before = cast("str", change["before"])
    if actual_before != before:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH, "ChangeSet before text differs from source"
        )
    eligibility = evaluate_patch_eligibility(
        change,
        decision,
        source_prefix=source_prefix,
        source_suffix=source_suffix,
    )
    if eligibility.block_code is not None or eligibility.source_context_required:
        return None, eligibility.block_code or ErrorCode.PATCH_UNSAFE_KIND
    replacement = cast("str", eligibility.replacement_text)
    kind = {"insertion": "insert", "deletion": "delete", "replacement": "replace"}[
        cast("str", change["kind"])
    ]
    hunk_sha256 = sha256_canonical(
        {
            "path": path,
            "start_byte": start,
            "end_byte": end,
            "before_text": before,
            "replacement_text": replacement,
        }
    )
    operation_without_id: dict[str, Any] = {
        "change_id": change["change_id"],
        "kind": kind,
        "target": copy.deepcopy(dict(location)),
        "target_file_sha256": file_digest.sha256,
        "expected_bytes_sha256": expected_digest,
        "before_text": before,
        "replacement_text": replacement,
        "encoding": "utf-8",
        "newline_policy": "preserve",
        "unit_id": change["unit_id"],
        "unit_source_slice_sha256": location["slice_sha256"],
        "context_fingerprint": sha256_canonical(
            {
                "path": path,
                "file_sha256": file_digest.sha256,
                "prefix": digest_bytes(data[max(0, start - 32) : start]).sha256,
                "suffix": digest_bytes(data[end : end + 32]).sha256,
            }
        ),
        "confidence": cast("Mapping[str, Any]", change["resolution"])["confidence"],
        "safety_class": "plain_text_candidate",
        "safety_checks": {
            "approved": True,
            "exact_unit": True,
            "slice_hash_match": True,
            "plain_text_only": True,
            "no_overlap": True,
            "no_structural_boundary": True,
        },
        "diff_hunk_sha256": hunk_sha256,
    }
    return {
        "operation_id": _operation_identity(operation_without_id),
        **operation_without_id,
    }, None


def apply_operations_to_bytes(
    originals: Mapping[str, bytes], operations: Sequence[Mapping[str, Any]]
) -> dict[str, bytes]:
    """Apply verified operations in descending byte order to in-memory files."""

    revised = dict(originals)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for operation in operations:
        grouped[cast("str", cast("Mapping[str, Any]", operation["target"])["path"])].append(
            operation
        )
    for path, file_operations in grouped.items():
        original = originals[path]
        expected_file_hashes = {cast("str", item["target_file_sha256"]) for item in file_operations}
        if expected_file_hashes != {digest_bytes(original).sha256}:
            raise ContractError(
                ErrorCode.PATCH_SOURCE_DRIFT, "target file hash no longer matches plan"
            )
        current = original
        ordered = sorted(
            file_operations,
            key=lambda item: (
                int(cast("Mapping[str, Any]", item["target"])["start_byte"]),
                int(cast("Mapping[str, Any]", item["target"])["end_byte"]),
                cast("str", item["operation_id"]),
            ),
            reverse=True,
        )
        for operation in ordered:
            target = cast("Mapping[str, Any]", operation["target"])
            start, end = int(target["start_byte"]), int(target["end_byte"])
            actual = current[start:end]
            if digest_bytes(actual).sha256 != operation["expected_bytes_sha256"]:
                raise ContractError(
                    ErrorCode.PATCH_SOURCE_DRIFT, "target slice no longer matches plan"
                )
            if actual.decode("utf-8", errors="strict") != operation["before_text"]:
                raise ContractError(
                    ErrorCode.PATCH_SOURCE_DRIFT, "target text no longer matches plan"
                )
            replacement = cast("str", operation["replacement_text"]).encode("utf-8")
            current = current[:start] + replacement + current[end:]
        revised[path] = current
    return revised


def build_unified_diff(originals: Mapping[str, bytes], revised: Mapping[str, bytes]) -> bytes:
    """Build a path-sorted, timestamp-free unified diff from exact bytes."""

    chunks: list[bytes] = []
    for path in sorted(originals):
        if originals[path] == revised[path]:
            continue
        chunks.extend(
            difflib.diff_bytes(
                difflib.unified_diff,
                originals[path].splitlines(keepends=True),
                revised[path].splitlines(keepends=True),
                fromfile=f"a/{path}".encode(),
                tofile=f"b/{path}".encode(),
                lineterm=b"\n",
            )
        )
    return b"".join(chunks)


def _plan_id(payload_without_id: Mapping[str, Any]) -> str:
    return stable_id("plan_", payload_without_id)


def plan_patch(
    source_root: Path,
    *,
    source_tree_sha256: str,
    changeset: Mapping[str, Any],
    approval: Mapping[str, Any],
    generated_at: str,
    diff_path: str = "apply/changes.patch",
    confidentiality: str = "derived_private",
    limits: DiscoveryLimits = DEFAULT_DISCOVERY_LIMITS,
) -> PatchPlanResult:
    """Create a sealed dry-run PatchPlan and diff without writing any file."""

    authorization = _authorization_context(changeset, approval)
    discovery = discover_project(source_root, limits=limits)
    if discovery.source_tree_sha256 != source_tree_sha256:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH, "source tree differs from requested tree"
        )

    operations: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    approved_count = 0
    for change in authorization.changes:
        change_id = cast("str", change["change_id"])
        decision = authorization.decisions[change_id]
        decision_value = cast("str", decision["decision"])
        if decision_value not in {"accepted", "accepted_with_edit"}:
            excluded.append({"change_id": change_id, "reason": decision_value})
            continue
        approved_count += 1
        operation, block_code = _make_operation(change, decision, discovery, source_root, limits)
        if block_code is not None:
            blocked.append({"change_id": change_id, "code": block_code.value})
        elif operation is not None:
            operations.append(operation)

    operations.sort(
        key=lambda item: (
            cast("Mapping[str, Any]", item["target"])["path"],
            cast("Mapping[str, Any]", item["target"])["start_byte"],
            cast("Mapping[str, Any]", item["target"])["end_byte"],
            item["change_id"],
        )
    )
    for index, operation in enumerate(operations):
        for prior in operations[:index]:
            if _ranges_overlap(
                cast("Mapping[str, Any]", prior["target"]),
                cast("Mapping[str, Any]", operation["target"]),
            ):
                raise ContractError(ErrorCode.PATCH_OVERLAP, "approved patch ranges overlap")

    paths = {cast("str", cast("Mapping[str, Any]", item["target"])["path"]) for item in operations}
    originals = {
        path: read_stable_bytes(
            resolve_within(source_root, path),
            max_bytes=limits.max_file_bytes,
        )
        for path in sorted(paths)
    }
    revised = apply_operations_to_bytes(originals, operations)
    unified_diff = build_unified_diff(originals, revised)
    if operations and not unified_diff:
        raise ContractError(ErrorCode.PATCH_UNSAFE_KIND, "approved operations produce no diff")

    diff_artifact: dict[str, Any] | None = None
    if unified_diff:
        normalized_diff_path = validate_relative_path(diff_path)
        diff_sha256 = digest_bytes(unified_diff).sha256
        if confidentiality not in {"public_fixture", "local_private", "derived_private"}:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid diff confidentiality")
        diff_artifact = {
            "artifact_id": derive_artifact_id(diff_sha256),
            "path": normalized_diff_path,
            "path_base": "run_root",
            "role": "unified_diff",
            "media_type": "text/x-diff",
            "size_bytes": len(unified_diff),
            "sha256": diff_sha256,
            "immutable": True,
            "confidentiality": confidentiality,
        }

    status = "blocked" if blocked else "ready" if operations else "noop"
    preconditions: list[dict[str, str]] = [
        {"kind": "source_tree_sha256_equals", "expected": source_tree_sha256},
        {"kind": "changeset_payload_sha256_equals", "expected": authorization.changeset_sha256},
        {"kind": "approval_payload_sha256_equals", "expected": authorization.approval_sha256},
        {"kind": "policy_sha256_equals", "expected": POLICY_SHA256},
    ]
    for operation in operations:
        preconditions.extend(
            [
                {
                    "kind": "target_file_sha256_equals",
                    "expected": cast("str", operation["target_file_sha256"]),
                },
                {
                    "kind": "target_slice_sha256_equals",
                    "expected": cast("str", operation["expected_bytes_sha256"]),
                },
            ]
        )
    payload_without_id: dict[str, Any] = {
        "status": status,
        "mode": "dry_run",
        "source_manifest_sha256": authorization.source_manifest_sha256,
        "source_tree_sha256": source_tree_sha256,
        "changeset_sha256": authorization.changeset_sha256,
        "approval_set_sha256": authorization.approval_sha256,
        "policy_sha256": POLICY_SHA256,
        "planner": _producer(),
        "operations": operations,
        "accepted_but_blocked": blocked,
        "excluded_changes": excluded,
        "unified_diff": diff_artifact,
        "summary": {
            "approved": approved_count,
            "planned": len(operations),
            "blocked": len(blocked),
            "excluded": len(excluded),
            "overlaps": 0,
        },
        "preconditions": preconditions,
    }
    patch_plan_id = _plan_id(payload_without_id)
    payload = {"patch_plan_id": patch_plan_id, **payload_without_id}
    document = make_envelope(
        schema_name="PatchPlan",
        object_id=patch_plan_id,
        run_id=cast("str", changeset["run_id"]),
        generated_at=generated_at,
        producer=_producer(),
        payload=payload,
    )
    final_discovery = discover_project(source_root, limits=limits)
    if final_discovery != discovery:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source changed while planning")
    return PatchPlanResult(document=document, unified_diff=unified_diff)


def verify_plan_identity(document: Mapping[str, Any]) -> None:
    """Verify the current planner policy and content-derived operation/plan IDs."""

    receipt = validate_contract(document)
    if receipt.schema_name != "PatchPlan":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "apply input must be a PatchPlan")
    payload = cast("Mapping[str, Any]", document["payload"])
    if payload["policy_sha256"] != POLICY_SHA256:
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "PatchPlan policy is unsupported")
    for operation in cast("Sequence[Mapping[str, Any]]", payload["operations"]):
        without_id = dict(operation)
        operation_id = without_id.pop("operation_id")
        if operation_id != _operation_identity(without_id):
            raise ContractError(
                ErrorCode.HASH_PATCHPLAN_MISMATCH,
                "PatchPlan operation stable ID is invalid",
            )
        target = cast("Mapping[str, Any]", operation["target"])
        expected_hunk = sha256_canonical(
            {
                "path": target["path"],
                "start_byte": target["start_byte"],
                "end_byte": target["end_byte"],
                "before_text": operation["before_text"],
                "replacement_text": operation["replacement_text"],
            }
        )
        if operation["diff_hunk_sha256"] != expected_hunk:
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "diff hunk hash is invalid")
    without_plan_id = copy.deepcopy(dict(payload))
    plan_id = without_plan_id.pop("patch_plan_id")
    if plan_id != _plan_id(without_plan_id):
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "PatchPlan stable ID is invalid")


__all__ = [
    "POLICY_SHA256",
    "PatchEligibility",
    "PatchPlanResult",
    "apply_operations_to_bytes",
    "build_unified_diff",
    "evaluate_patch_eligibility",
    "plan_patch",
    "verify_plan_identity",
]
