"""Immutable ApprovalSet state machine bound to a sealed ChangeSet.

Approval records intent only.  This module never opens LaTeX sources, creates
patches, or modifies an existing approval JSON file.
"""

from __future__ import annotations

import copy
import os
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from latex_word_review.__about__ import __version__
from latex_word_review.canonical import canonical_json, compute_payload_sha256, sha256_canonical
from latex_word_review.contracts import make_envelope, validate_contract
from latex_word_review.domain_values import DECISION_VALUE_SET, Decision
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.ids import stable_id

DecisionSource = Literal["cli", "local_ui", "codex_skill", "imported"]
BulkOperation = Literal["accept_all_safe", "reject_selected", "mark_manual"]

_DECISIONS = DECISION_VALUE_SET
_DECISION_SOURCES: Final = {"cli", "local_ui", "codex_skill", "imported"}
_BULK_DECISIONS: Final[dict[str, str]] = {
    "accept_all_safe": "accepted",
    "reject_selected": "rejected",
    "mark_manual": "manual",
}
_INTERFACE_VERSION: Final = "approval-v1alpha2"
_CONFIGURATION_SHA256: Final = sha256_canonical(
    {
        "state_machine": "immutable-approval-revisions-v1",
        "bulk_policy": "accept-exact-text-or-explicitly-reduce-to-manual-v2",
    }
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _require_time(value: str | None) -> str:
    resolved = value or _utc_now()
    try:
        parsed = datetime.fromisoformat(
            resolved[:-1] + "+00:00" if resolved.endswith("Z") else resolved
        )
    except ValueError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "approval time is not ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "approval time must include a UTC offset")
    return resolved


def _changeset_context(
    changeset: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any], tuple[str, ...], dict[str, Mapping[str, Any]]]:
    receipt = validate_contract(changeset)
    if receipt.schema_name != "ChangeSet":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "approval input must be a ChangeSet")
    payload = cast("Mapping[str, Any]", changeset["payload"])
    changes = cast("Sequence[Mapping[str, Any]]", payload["changes"])
    order = tuple(cast("str", change["change_id"]) for change in changes)
    index = {cast("str", change["change_id"]): change for change in changes}
    return receipt.payload_sha256, payload, order, index


def _approval_id(payload_without_id: Mapping[str, Any]) -> str:
    return stable_id("apr_", payload_without_id)


def _ordered_decisions(
    order: Sequence[str], decisions: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return [
        copy.deepcopy(dict(decisions[change_id])) for change_id in order if change_id in decisions
    ]


def _summary(decisions: Sequence[Mapping[str, Any]], pending: int) -> dict[str, int]:
    counts = Counter(cast("str", item["decision"]) for item in decisions)
    return {
        "accepted": counts["accepted"],
        "accepted_with_edit": counts["accepted_with_edit"],
        "rejected": counts["rejected"],
        "manual": counts["manual"],
        "conflict": counts["conflict"],
        "pending": pending,
    }


def _producer() -> dict[str, Any]:
    return {
        "name": "latex-word-review",
        "version": __version__,
        "interface_version": _INTERFACE_VERSION,
        "distribution": "python-package",
        "executable_sha256": None,
        "configuration_sha256": _CONFIGURATION_SHA256,
    }


def _make_approval(
    changeset: Mapping[str, Any],
    *,
    decided_by: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
    status: Literal["draft", "final"],
    revision: int,
    supersedes_payload_sha256: str | None,
    bulk_operations: Sequence[Mapping[str, Any]],
    generated_at: str | None,
) -> dict[str, Any]:
    changeset_sha256, changeset_payload, order, _ = _changeset_context(changeset)
    ordered = _ordered_decisions(order, decisions)
    undecided = [change_id for change_id in order if change_id not in decisions]
    if status == "final" and undecided:
        raise ContractError(
            ErrorCode.APPROVAL_NOT_FINAL,
            "every ChangeSet change must be decided before finalization",
        )
    payload_without_id: dict[str, Any] = {
        "revision": revision,
        "supersedes_payload_sha256": supersedes_payload_sha256,
        "changeset_sha256": changeset_sha256,
        "source_manifest_sha256": changeset_payload["source_manifest_sha256"],
        "status": status,
        "decided_by": copy.deepcopy(dict(decided_by)),
        "decisions": ordered,
        "undecided_change_ids": undecided,
        "decision_summary": _summary(ordered, len(undecided)),
        "audit": {
            "bulk_operations": copy.deepcopy([dict(item) for item in bulk_operations]),
            "previous_payload_sha256": supersedes_payload_sha256,
        },
    }
    approval_set_id = _approval_id(payload_without_id)
    payload = {"approval_set_id": approval_set_id, **payload_without_id}
    return make_envelope(
        schema_name="ApprovalSet",
        object_id=approval_set_id,
        run_id=cast("str", changeset["run_id"]),
        generated_at=_require_time(generated_at),
        producer=_producer(),
        payload=payload,
    )


def _validated_current(
    changeset: Mapping[str, Any], approval: Mapping[str, Any]
) -> tuple[tuple[str, ...], dict[str, Mapping[str, Any]], Mapping[str, Any]]:
    changeset_sha256, changeset_payload, order, changes = _changeset_context(changeset)
    receipt = validate_contract(approval)
    if receipt.schema_name != "ApprovalSet":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "state input must be an ApprovalSet")
    payload = cast("Mapping[str, Any]", approval["payload"])
    if payload["changeset_sha256"] != changeset_sha256:
        raise ContractError(
            ErrorCode.HASH_CHANGESET_MISMATCH,
            "ApprovalSet is bound to a different ChangeSet payload",
        )
    if payload["source_manifest_sha256"] != changeset_payload["source_manifest_sha256"]:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "ApprovalSet is bound to a different source manifest",
        )
    if approval["run_id"] != changeset["run_id"]:
        raise ContractError(ErrorCode.HASH_CHANGESET_MISMATCH, "approval run binding differs")

    revision = cast("int", payload["revision"])
    supersedes = payload["supersedes_payload_sha256"]
    previous = cast("Mapping[str, Any]", payload["audit"])["previous_payload_sha256"]
    if (revision == 1) != (supersedes is None) or supersedes != previous:
        raise ContractError(ErrorCode.HASH_APPROVAL_MISMATCH, "approval revision chain is invalid")

    payload_without_id = copy.deepcopy(dict(payload))
    payload_without_id.pop("approval_set_id", None)
    if payload["approval_set_id"] != _approval_id(payload_without_id):
        raise ContractError(ErrorCode.HASH_APPROVAL_MISMATCH, "approval stable ID is invalid")

    decision_items = cast("Sequence[Mapping[str, Any]]", payload["decisions"])
    decision_map: dict[str, Mapping[str, Any]] = {}
    for item in decision_items:
        change_id = cast("str", item["change_id"])
        change = changes.get(change_id)
        if change is None:
            raise ContractError(
                ErrorCode.APPROVAL_CHANGE_UNKNOWN,
                "ApprovalSet references a change absent from the bound ChangeSet",
            )
        if item["change_fingerprint"] != change["change_fingerprint"]:
            raise ContractError(
                ErrorCode.HASH_CHANGESET_MISMATCH,
                "approval decision fingerprint differs from the sealed ChangeSet",
            )
        decision_map[change_id] = item
    undecided = tuple(cast("Sequence[str]", payload["undecided_change_ids"]))
    decided_order = tuple(change_id for change_id in order if change_id in decision_map)
    pending_order = tuple(change_id for change_id in order if change_id not in decision_map)
    if (
        tuple(item["change_id"] for item in decision_items) != decided_order
        or undecided != pending_order
    ):
        raise ContractError(
            ErrorCode.APPROVAL_CHANGE_UNKNOWN,
            "ApprovalSet decisions must exactly partition ChangeSet changes in source order",
        )
    return order, decision_map, payload


def create_approval_set(
    changeset: Mapping[str, Any],
    *,
    decided_by: Mapping[str, Any],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Create revision 1 as a draft with every sealed ChangeSet change pending."""

    return _make_approval(
        changeset,
        decided_by=decided_by,
        decisions={},
        status="draft",
        revision=1,
        supersedes_payload_sha256=None,
        bulk_operations=(),
        generated_at=generated_at,
    )


def _validate_decision_request(
    decision: str,
    final_text: str | None,
    decision_source: str,
) -> None:
    if decision not in _DECISIONS:
        raise ContractError(ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD, "unknown approval decision")
    if decision_source not in _DECISION_SOURCES:
        raise ContractError(ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD, "unknown decision source")
    if decision == "accepted_with_edit" and final_text is None:
        raise ContractError(
            ErrorCode.APPROVAL_FINAL_TEXT_REQUIRED,
            "accepted_with_edit requires final_text",
        )
    if decision != "accepted_with_edit" and final_text is not None:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "final_text is only valid for accepted_with_edit",
        )


def _intent_matches(
    existing: Mapping[str, Any] | None,
    *,
    decision: str,
    final_text: str | None,
    reason: str | None,
    risk_acknowledgement: str | None,
    decision_source: str,
) -> bool:
    if decision == "pending":
        return existing is None
    if existing is None:
        return False
    return all(
        (
            existing["decision"] == decision,
            existing["final_text"] == final_text,
            existing["reason"] == reason,
            existing["risk_acknowledgement"] == risk_acknowledgement,
            existing["decision_source"] == decision_source,
        )
    )


def record_decision(
    changeset: Mapping[str, Any],
    approval: Mapping[str, Any],
    *,
    change_id: str,
    decision: Decision,
    final_text: str | None = None,
    reason: str | None = None,
    risk_acknowledgement: str | None = None,
    decision_source: DecisionSource = "cli",
    decided_at: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Record or withdraw one decision as an immutable ApprovalSet revision."""

    _validate_decision_request(decision, final_text, decision_source)
    timestamp = _require_time(decided_at)
    order, prior_decisions, payload = _validated_current(changeset, approval)
    _, _, _, changes = _changeset_context(changeset)
    change = changes.get(change_id)
    if change is None:
        raise ContractError(
            ErrorCode.APPROVAL_CHANGE_UNKNOWN,
            "decision references a change absent from the bound ChangeSet",
        )
    existing = prior_decisions.get(change_id)
    if _intent_matches(
        existing,
        decision=decision,
        final_text=final_text,
        reason=reason,
        risk_acknowledgement=risk_acknowledgement,
        decision_source=decision_source,
    ):
        return copy.deepcopy(dict(approval))

    updated = {key: copy.deepcopy(dict(value)) for key, value in prior_decisions.items()}
    if decision == "pending":
        updated.pop(change_id, None)
    else:
        updated[change_id] = {
            "change_id": change_id,
            "change_fingerprint": change["change_fingerprint"],
            "decision": decision,
            "final_text": final_text,
            "reason": reason,
            "decided_at": timestamp,
            "risk_acknowledgement": risk_acknowledgement,
            "decision_source": decision_source,
        }
    del order  # ordering is re-derived from the sealed ChangeSet
    previous_hash = compute_payload_sha256(approval)
    audit = cast("Mapping[str, Any]", payload["audit"])
    return _make_approval(
        changeset,
        decided_by=cast("Mapping[str, Any]", payload["decided_by"]),
        decisions=updated,
        status="draft",
        revision=cast("int", payload["revision"]) + 1,
        supersedes_payload_sha256=previous_hash,
        bulk_operations=cast("Sequence[Mapping[str, Any]]", audit["bulk_operations"]),
        generated_at=generated_at,
    )


def record_bulk_decision(
    changeset: Mapping[str, Any],
    approval: Mapping[str, Any],
    *,
    operation: BulkOperation,
    change_ids: Sequence[str] | None = None,
    reason: str | None = None,
    risk_acknowledgement: str | None = None,
    decision_source: DecisionSource = "cli",
    decided_at: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Apply one audited bulk decision without increasing unsafe change authority."""

    desired = _BULK_DECISIONS.get(operation)
    if desired is None:
        raise ContractError(ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD, "unknown bulk operation")
    _validate_decision_request(desired, None, decision_source)
    timestamp = _require_time(decided_at)
    order, prior_decisions, payload = _validated_current(changeset, approval)
    _, _, _, changes = _changeset_context(changeset)

    def is_exact_plain_text(change_id: str) -> bool:
        change = changes[change_id]
        resolution = cast("Mapping[str, Any]", change["resolution"])
        return bool(
            change["safety_class"] == "plain_text_candidate" and resolution["status"] == "exact"
        )

    if change_ids is None:
        if operation == "accept_all_safe":
            selected = tuple(
                change_id
                for change_id in order
                if is_exact_plain_text(change_id) and change_id not in prior_decisions
            )
        elif operation == "mark_manual":
            selected = tuple(
                change_id
                for change_id in order
                if not is_exact_plain_text(change_id) and change_id not in prior_decisions
            )
        else:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "selected bulk operation requires explicit change_ids",
            )
    else:
        selected_set = set(change_ids)
        if len(selected_set) != len(change_ids):
            raise ContractError(ErrorCode.SCHEMA_INVALID, "bulk change_ids contain duplicates")
        unknown = selected_set - set(order)
        if unknown:
            raise ContractError(
                ErrorCode.APPROVAL_CHANGE_UNKNOWN,
                "bulk operation references an unknown change",
            )
        selected = tuple(
            change_id
            for change_id in order
            if change_id in selected_set and change_id not in prior_decisions
        )

    for change_id in selected:
        change = changes[change_id]
        if operation == "mark_manual":
            continue
        resolution = cast("Mapping[str, Any]", change["resolution"])
        if change["safety_class"] != "plain_text_candidate" or resolution["status"] != "exact":
            raise ContractError(
                ErrorCode.PATCH_UNSAFE_KIND,
                "bulk approval is restricted to exact plain-text candidates",
            )

    if all(
        _intent_matches(
            prior_decisions.get(change_id),
            decision=desired,
            final_text=None,
            reason=reason,
            risk_acknowledgement=risk_acknowledgement,
            decision_source=decision_source,
        )
        for change_id in selected
    ):
        return copy.deepcopy(dict(approval))

    updated = {key: copy.deepcopy(dict(value)) for key, value in prior_decisions.items()}
    for change_id in selected:
        updated[change_id] = {
            "change_id": change_id,
            "change_fingerprint": changes[change_id]["change_fingerprint"],
            "decision": desired,
            "final_text": None,
            "reason": reason,
            "decided_at": timestamp,
            "risk_acknowledgement": risk_acknowledgement,
            "decision_source": decision_source,
        }
    audit = cast("Mapping[str, Any]", payload["audit"])
    bulk_operations = [
        copy.deepcopy(dict(item))
        for item in cast("Sequence[Mapping[str, Any]]", audit["bulk_operations"])
    ]
    bulk_operations.append(
        {"operation": operation, "change_ids": list(selected), "decided_at": timestamp}
    )
    return _make_approval(
        changeset,
        decided_by=cast("Mapping[str, Any]", payload["decided_by"]),
        decisions=updated,
        status="draft",
        revision=cast("int", payload["revision"]) + 1,
        supersedes_payload_sha256=compute_payload_sha256(approval),
        bulk_operations=bulk_operations,
        generated_at=generated_at,
    )


def finalize_approval_set(
    changeset: Mapping[str, Any],
    approval: Mapping[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Seal a complete draft as final; incomplete approvals fail closed."""

    _, decisions, payload = _validated_current(changeset, approval)
    if payload["status"] == "final":
        return copy.deepcopy(dict(approval))
    if payload["undecided_change_ids"]:
        raise ContractError(
            ErrorCode.APPROVAL_NOT_FINAL,
            "every ChangeSet change must be decided before finalization",
        )
    audit = cast("Mapping[str, Any]", payload["audit"])
    return _make_approval(
        changeset,
        decided_by=cast("Mapping[str, Any]", payload["decided_by"]),
        decisions=decisions,
        status="final",
        revision=cast("int", payload["revision"]) + 1,
        supersedes_payload_sha256=compute_payload_sha256(approval),
        bulk_operations=cast("Sequence[Mapping[str, Any]]", audit["bulk_operations"]),
        generated_at=generated_at,
    )


def reopen_approval_set(
    changeset: Mapping[str, Any],
    approval: Mapping[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Create a new immutable draft from one final approval revision."""

    _, decisions, payload = _validated_current(changeset, approval)
    if payload["status"] != "final":
        raise ContractError(
            ErrorCode.APPROVAL_NOT_FINAL,
            "only a final approval can be reopened",
        )
    audit = cast("Mapping[str, Any]", payload["audit"])
    return _make_approval(
        changeset,
        decided_by=cast("Mapping[str, Any]", payload["decided_by"]),
        decisions=decisions,
        status="draft",
        revision=cast("int", payload["revision"]) + 1,
        supersedes_payload_sha256=compute_payload_sha256(approval),
        bulk_operations=cast("Sequence[Mapping[str, Any]]", audit["bulk_operations"]),
        generated_at=generated_at,
    )


def write_approval_json(
    changeset: Mapping[str, Any], approval: Mapping[str, Any], output_path: str | Path
) -> Path:
    """Atomically publish one validated approval to an explicit, unused JSON path."""

    _validated_current(changeset, approval)
    destination = Path(output_path)
    if destination.suffix.lower() != ".json" or not destination.name:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "approval output must be an explicit .json path",
        )
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "approval output parent is unavailable",
        ) from exc
    if not parent.is_dir():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "approval output parent must be a directory")
    target = parent / destination.name
    if target.exists() or target.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "approval version path already exists")

    data = canonical_json(approval) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.approval-", suffix=".tmp", dir=parent
    )
    staged = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staged, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID, "approval version path already exists"
            ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID, "approval JSON could not be atomically published"
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            staged.unlink()
    return destination


__all__ = [
    "BulkOperation",
    "Decision",
    "DecisionSource",
    "create_approval_set",
    "finalize_approval_set",
    "record_bulk_decision",
    "record_decision",
    "reopen_approval_set",
    "write_approval_json",
]
