"""Deterministic, escaped review ledgers for one fully bound workflow.

The ledger is a read-only projection.  It never changes an ApprovalSet, applies
a PatchPlan, or treats a rendered HTML page as authorization evidence.
"""

from __future__ import annotations

import copy
import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from latex_word_review.canonical import canonical_json, canonical_json_text
from latex_word_review.contracts import ValidatedContract, validate_contract
from latex_word_review.errors import ContractError, ErrorCode

_LEDGER_FORMAT = "latex-word-review-ledger-v1"


@dataclass(frozen=True, slots=True)
class LedgerOutput:
    """A deterministic ledger value and its two self-contained encodings."""

    document: Mapping[str, Any]
    json_bytes: bytes
    html_bytes: bytes


@dataclass(frozen=True, slots=True)
class _LedgerContext:
    changeset_receipt: ValidatedContract
    approval_receipt: ValidatedContract
    patch_receipt: ValidatedContract
    verification_receipt: ValidatedContract
    changeset_payload: Mapping[str, Any]
    approval_payload: Mapping[str, Any]
    patch_payload: Mapping[str, Any]
    verification_payload: Mapping[str, Any]


def _require(condition: bool, code: ErrorCode, message: str) -> None:
    if not condition:
        raise ContractError(code, message)


def _receipt(document: Mapping[str, Any], expected: str) -> ValidatedContract:
    receipt = validate_contract(document)
    if receipt.schema_name != expected:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            f"ledger input must be a {expected}",
        )
    return receipt


def _validate_verification_extension(
    verification_report: Mapping[str, Any], context: _LedgerContext
) -> None:
    extensions = verification_report.get("extensions")
    if not isinstance(extensions, Mapping):
        raise ContractError(
            ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD,
            "VerificationReport security binding extension is missing",
        )
    extension = extensions.get("org.latex-word-review.verification")
    if not isinstance(extension, Mapping):
        raise ContractError(
            ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD,
            "VerificationReport security binding extension is missing",
        )
    source_sha256 = context.changeset_payload["source_manifest_sha256"]
    expected = (
        (
            "changeset_payload_sha256",
            context.changeset_receipt.payload_sha256,
            ErrorCode.HASH_CHANGESET_MISMATCH,
        ),
        (
            "approval_set_payload_sha256",
            context.approval_receipt.payload_sha256,
            ErrorCode.HASH_APPROVAL_MISMATCH,
        ),
        (
            "patch_plan_payload_sha256",
            context.patch_receipt.payload_sha256,
            ErrorCode.HASH_PATCHPLAN_MISMATCH,
        ),
        (
            "source_manifest_payload_sha256",
            source_sha256,
            ErrorCode.HASH_SOURCE_MISMATCH,
        ),
        (
            "original_source_tree_sha256",
            context.verification_payload["original_source_pre_sha256"],
            ErrorCode.VERIFY_ORIGINAL_MUTATED,
        ),
        (
            "applied_source_tree_sha256",
            context.verification_payload["revised_source_manifest_sha256"],
            ErrorCode.VERIFY_DIFF_MISMATCH,
        ),
        (
            "policy_sha256",
            context.patch_payload["policy_sha256"],
            ErrorCode.HASH_PATCHPLAN_MISMATCH,
        ),
    )
    for field, expected_hash, code in expected:
        if field not in extension:
            raise ContractError(
                ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD,
                f"VerificationReport extension is missing {field}",
            )
        _require(
            extension[field] == expected_hash,
            code,
            f"VerificationReport extension {field} binding differs",
        )
    commands = extension.get("commands")
    if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes, bytearray)):
        raise ContractError(
            ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD,
            "VerificationReport extension commands must be an array",
        )


def _validate_bindings(
    changeset: Mapping[str, Any],
    approval_set: Mapping[str, Any],
    patch_plan: Mapping[str, Any],
    verification_report: Mapping[str, Any],
) -> _LedgerContext:
    changeset_receipt = _receipt(changeset, "ChangeSet")
    approval_receipt = _receipt(approval_set, "ApprovalSet")
    patch_receipt = _receipt(patch_plan, "PatchPlan")
    verification_receipt = _receipt(verification_report, "VerificationReport")
    changeset_payload = cast("Mapping[str, Any]", changeset["payload"])
    approval_payload = cast("Mapping[str, Any]", approval_set["payload"])
    patch_payload = cast("Mapping[str, Any]", patch_plan["payload"])
    verification_payload = cast("Mapping[str, Any]", verification_report["payload"])

    run_ids = {
        cast("str", changeset["run_id"]),
        cast("str", approval_set["run_id"]),
        cast("str", patch_plan["run_id"]),
        cast("str", verification_report["run_id"]),
    }
    _require(
        len(run_ids) == 1,
        ErrorCode.HASH_INTEGRITY_MISMATCH,
        "ledger inputs are bound to different runs",
    )
    source_manifest_sha256 = cast("str", changeset_payload["source_manifest_sha256"])
    _require(
        approval_payload["changeset_sha256"] == changeset_receipt.payload_sha256,
        ErrorCode.HASH_CHANGESET_MISMATCH,
        "ApprovalSet is bound to a different ChangeSet payload",
    )
    _require(
        approval_payload["source_manifest_sha256"] == source_manifest_sha256,
        ErrorCode.HASH_SOURCE_MISMATCH,
        "ApprovalSet and ChangeSet source bindings differ",
    )
    _require(
        patch_payload["changeset_sha256"] == changeset_receipt.payload_sha256,
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        "PatchPlan is bound to a different ChangeSet payload",
    )
    _require(
        patch_payload["approval_set_sha256"] == approval_receipt.payload_sha256,
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        "PatchPlan is bound to a different ApprovalSet payload",
    )
    _require(
        patch_payload["source_manifest_sha256"] == source_manifest_sha256,
        ErrorCode.HASH_SOURCE_MISMATCH,
        "PatchPlan and ChangeSet source bindings differ",
    )
    _require(
        verification_payload["patch_plan_sha256"] == patch_receipt.payload_sha256,
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        "VerificationReport is bound to a different PatchPlan payload",
    )
    _require(
        verification_payload["source_manifest_sha256"] == source_manifest_sha256,
        ErrorCode.HASH_SOURCE_MISMATCH,
        "VerificationReport and ChangeSet source bindings differ",
    )
    _require(
        approval_payload["status"] == "final",
        ErrorCode.APPROVAL_NOT_FINAL,
        "ledger with a PatchPlan requires a final ApprovalSet",
    )
    context = _LedgerContext(
        changeset_receipt=changeset_receipt,
        approval_receipt=approval_receipt,
        patch_receipt=patch_receipt,
        verification_receipt=verification_receipt,
        changeset_payload=changeset_payload,
        approval_payload=approval_payload,
        patch_payload=patch_payload,
        verification_payload=verification_payload,
    )
    _validate_verification_extension(verification_report, context)
    return context


def _unique_index(
    values: Sequence[Mapping[str, Any]],
    field: str,
    *,
    code: ErrorCode,
    label: str,
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        key = cast("str", value[field])
        if key in result:
            raise ContractError(code, f"{label} contains duplicate {field}")
        result[key] = value
    return result


def _validate_decisions(
    context: _LedgerContext,
) -> tuple[
    tuple[Mapping[str, Any], ...],
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    changes = tuple(cast("Sequence[Mapping[str, Any]]", context.changeset_payload["changes"]))
    change_index = _unique_index(
        changes,
        "change_id",
        code=ErrorCode.HASH_CHANGESET_MISMATCH,
        label="ChangeSet",
    )
    decisions = tuple(cast("Sequence[Mapping[str, Any]]", context.approval_payload["decisions"]))
    decision_index = _unique_index(
        decisions,
        "change_id",
        code=ErrorCode.HASH_APPROVAL_MISMATCH,
        label="ApprovalSet",
    )
    change_order = tuple(change_index)
    decision_order = tuple(decision_index)
    _require(
        change_order == decision_order,
        ErrorCode.APPROVAL_CHANGE_UNKNOWN,
        "final ApprovalSet must decide every ChangeSet change in source order",
    )
    _require(
        not context.approval_payload["undecided_change_ids"],
        ErrorCode.APPROVAL_NOT_FINAL,
        "final ApprovalSet contains undecided changes",
    )
    for change_id, decision in decision_index.items():
        _require(
            decision["change_fingerprint"] == change_index[change_id]["change_fingerprint"],
            ErrorCode.HASH_CHANGESET_MISMATCH,
            "approval decision fingerprint differs from the sealed ChangeSet",
        )
    return changes, change_index, decision_index


def _validate_plan_partition(
    context: _LedgerContext,
    change_index: Mapping[str, Mapping[str, Any]],
    decision_index: Mapping[str, Mapping[str, Any]],
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    operations = tuple(cast("Sequence[Mapping[str, Any]]", context.patch_payload["operations"]))
    blocked = tuple(
        cast("Sequence[Mapping[str, Any]]", context.patch_payload["accepted_but_blocked"])
    )
    excluded = tuple(cast("Sequence[Mapping[str, Any]]", context.patch_payload["excluded_changes"]))
    planned = _unique_index(
        operations,
        "change_id",
        code=ErrorCode.HASH_PATCHPLAN_MISMATCH,
        label="PatchPlan operations",
    )
    blocked_index = _unique_index(
        blocked,
        "change_id",
        code=ErrorCode.HASH_PATCHPLAN_MISMATCH,
        label="PatchPlan blocked changes",
    )
    excluded_index = _unique_index(
        excluded,
        "change_id",
        code=ErrorCode.HASH_PATCHPLAN_MISMATCH,
        label="PatchPlan excluded changes",
    )
    partitions = (set(planned), set(blocked_index), set(excluded_index))
    _require(
        not (
            partitions[0] & partitions[1]
            or partitions[0] & partitions[2]
            or partitions[1] & partitions[2]
        ),
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        "PatchPlan change partitions overlap",
    )
    _require(
        set().union(*partitions) == set(change_index),
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        "PatchPlan does not account for every ChangeSet change exactly once",
    )
    accepted = {
        change_id
        for change_id, decision in decision_index.items()
        if decision["decision"] in {"accepted", "accepted_with_edit"}
    }
    _require(
        accepted == set(planned) | set(blocked_index),
        ErrorCode.PATCH_UNAPPROVED_CHANGE,
        "planned or blocked changes differ from accepted ApprovalSet decisions",
    )
    _require(
        set(change_index) - accepted == set(excluded_index),
        ErrorCode.PATCH_UNAPPROVED_CHANGE,
        "excluded changes differ from non-accepted ApprovalSet decisions",
    )
    for change_id, excluded_change in excluded_index.items():
        _require(
            excluded_change["reason"] == decision_index[change_id]["decision"],
            ErrorCode.HASH_PATCHPLAN_MISMATCH,
            "PatchPlan exclusion reason differs from the approval decision",
        )
    for change_id, operation in planned.items():
        change = change_index[change_id]
        decision = decision_index[change_id]
        expected_replacement = (
            decision["final_text"]
            if decision["decision"] == "accepted_with_edit"
            else change["after"]
        )
        _require(
            isinstance(change["before"], str)
            and isinstance(expected_replacement, str)
            and operation["before_text"] == change["before"]
            and operation["replacement_text"] == expected_replacement,
            ErrorCode.HASH_PATCHPLAN_MISMATCH,
            "PatchPlan operation text differs from its approved ChangeSet change",
        )
        _require(
            operation["unit_id"] == change["unit_id"]
            and operation["target"] == change["source_location"],
            ErrorCode.HASH_PATCHPLAN_MISMATCH,
            "PatchPlan operation target differs from its approved ChangeSet change",
        )
    summary = cast("Mapping[str, Any]", context.patch_payload["summary"])
    _require(
        summary["approved"] == len(accepted)
        and summary["planned"] == len(planned)
        and summary["blocked"] == len(blocked_index)
        and summary["excluded"] == len(excluded_index),
        ErrorCode.HASH_PATCHPLAN_MISMATCH,
        "PatchPlan summary differs from its change partitions",
    )
    reconciliation = cast("Mapping[str, Any]", context.verification_payload["patch_reconciliation"])
    _require(
        reconciliation["planned_count"] == len(planned),
        ErrorCode.VERIFY_DIFF_MISMATCH,
        "VerificationReport planned count differs from PatchPlan operations",
    )
    if context.verification_payload["status"] == "pass":
        _require(
            reconciliation["applied_count"] == len(planned),
            ErrorCode.VERIFY_DIFF_MISMATCH,
            "passing VerificationReport did not apply every planned operation",
        )
    return planned, blocked_index, excluded_index


def _binding(document: Mapping[str, Any], receipt: ValidatedContract) -> dict[str, str]:
    return {
        "schema_name": receipt.schema_name,
        "object_id": cast("str", document["object_id"]),
        "payload_sha256": receipt.payload_sha256,
        "document_sha256": receipt.document_sha256,
    }


def _change_status(
    change_id: str,
    decision: Mapping[str, Any],
    planned: Mapping[str, Mapping[str, Any]],
    blocked: Mapping[str, Mapping[str, Any]],
    verification_status: str,
) -> str:
    if change_id in blocked:
        return "accepted_blocked"
    if change_id in planned:
        return {
            "pass": "applied_verified",
            "fail": "verification_failed",
            "blocked": "verification_blocked",
        }[verification_status]
    return cast("str", decision["decision"])


def _evidence_summary(
    change: Mapping[str, Any], raw_index: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    raw_ids = cast("Sequence[str]", change["raw_event_ids"])
    events: list[dict[str, Any]] = []
    for raw_id in raw_ids:
        raw = raw_index.get(raw_id)
        if raw is None:
            raise ContractError(
                ErrorCode.HASH_CHANGESET_MISMATCH,
                "change references raw evidence absent from the ChangeSet",
            )
        evidence = cast("Mapping[str, Any]", raw["evidence"])
        diagnostics = cast("Sequence[Mapping[str, Any]]", raw["diagnostics"])
        events.append(
            {
                "raw_event_id": raw_id,
                "kind": raw["kind"],
                "part_uri": raw["part_uri"],
                "author": raw["author"],
                "timestamp": raw["timestamp"],
                "node_ordinal": evidence["node_ordinal"],
                "fragment_sha256": evidence["fragment_sha256"],
                "diagnostic_ids": [item["diagnostic_id"] for item in diagnostics],
            }
        )
    resolution = cast("Mapping[str, Any]", change["resolution"])
    return {
        "raw_event_ids": list(raw_ids),
        "raw_event_count": len(events),
        "events": events,
        "unit_id": change["unit_id"],
        "source_location": copy.deepcopy(change["source_location"]),
        "resolution": {
            "status": resolution["status"],
            "method": resolution["method"],
            "confidence": resolution["confidence"],
        },
    }


def _ledger_document(
    changeset: Mapping[str, Any],
    approval_set: Mapping[str, Any],
    patch_plan: Mapping[str, Any],
    verification_report: Mapping[str, Any],
) -> dict[str, Any]:
    context = _validate_bindings(changeset, approval_set, patch_plan, verification_report)
    changes, change_index, decision_index = _validate_decisions(context)
    planned, blocked, excluded = _validate_plan_partition(context, change_index, decision_index)
    raw_events = tuple(cast("Sequence[Mapping[str, Any]]", context.changeset_payload["raw_events"]))
    raw_index = _unique_index(
        raw_events,
        "raw_event_id",
        code=ErrorCode.HASH_CHANGESET_MISMATCH,
        label="ChangeSet raw events",
    )
    verification_status = cast("str", context.verification_payload["status"])
    ledger_changes: list[dict[str, Any]] = []
    for change in changes:
        change_id = cast("str", change["change_id"])
        decision = decision_index[change_id]
        ledger_changes.append(
            {
                "change_id": change_id,
                "kind": change["kind"],
                "native_kind": change["native_kind"],
                "author": change["author"],
                "authors": copy.deepcopy(change["authors"]),
                "timestamp": change["timestamp"],
                "timestamps": copy.deepcopy(change["timestamps"]),
                "decision": copy.deepcopy(dict(decision)),
                "status": _change_status(
                    change_id, decision, planned, blocked, verification_status
                ),
                "patch": {
                    "operation_id": (
                        planned[change_id]["operation_id"] if change_id in planned else None
                    ),
                    "blocked_code": blocked[change_id]["code"] if change_id in blocked else None,
                    "excluded_reason": (
                        excluded[change_id]["reason"] if change_id in excluded else None
                    ),
                },
                "content": {
                    "before": copy.deepcopy(change["before"]),
                    "after": copy.deepcopy(change["after"]),
                    "comment": change["comment"],
                },
                "evidence": _evidence_summary(change, raw_index),
            }
        )
    return {
        "format": _LEDGER_FORMAT,
        "run_id": changeset["run_id"],
        "bindings": {
            "changeset": _binding(changeset, context.changeset_receipt),
            "approval_set": _binding(approval_set, context.approval_receipt),
            "patch_plan": _binding(patch_plan, context.patch_receipt),
            "verification_report": _binding(verification_report, context.verification_receipt),
            "source_manifest_sha256": context.changeset_payload["source_manifest_sha256"],
            "returned_original_sha256": cast(
                "Mapping[str, Any]", context.changeset_payload["returned_original"]
            )["sha256"],
        },
        "status": {
            "approval": context.approval_payload["status"],
            "patch_plan": context.patch_payload["status"],
            "verification": verification_status,
        },
        "summary": {
            "changes": len(ledger_changes),
            "decisions": copy.deepcopy(context.approval_payload["decision_summary"]),
            "planned": len(planned),
            "blocked": len(blocked),
            "excluded": len(excluded),
            "verification": copy.deepcopy(context.verification_payload["patch_reconciliation"]),
        },
        "changes": ledger_changes,
    }


def _escaped(value: object) -> str:
    if value is None:
        text = "—"
    elif isinstance(value, str):
        text = value
    else:
        text = canonical_json_text(value)
    return html.escape(text, quote=True)


def render_ledger_html(document: Mapping[str, Any]) -> bytes:
    """Render one already-built ledger as self-contained, escaped UTF-8 HTML."""

    if document.get("format") != _LEDGER_FORMAT:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "unsupported ledger document format")
    rows: list[str] = []
    for item in cast("Sequence[Mapping[str, Any]]", document.get("changes", [])):
        decision = cast("Mapping[str, Any]", item["decision"])
        content = cast("Mapping[str, Any]", item["content"])
        evidence = cast("Mapping[str, Any]", item["evidence"])
        rows.append(
            "<tr>"
            f"<td><code>{_escaped(item['change_id'])}</code><br>{_escaped(item['kind'])}</td>"
            f"<td>{_escaped(item['author'])}<br><time>{_escaped(item['timestamp'])}</time></td>"
            f"<td><strong>{_escaped(decision['decision'])}</strong><br>"
            f"{_escaped(decision['decided_at'])}<br>{_escaped(decision['reason'])}</td>"
            f"<td><strong>{_escaped(item['status'])}</strong></td>"
            f"<td><pre>{_escaped(content)}</pre></td>"
            f"<td><pre>{_escaped(evidence)}</pre></td>"
            "</tr>"
        )
    status = cast("Mapping[str, Any]", document["status"])
    summary = cast("Mapping[str, Any]", document["summary"])
    bindings = cast("Mapping[str, Any]", document["bindings"])
    page = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>LaTeX Word Review Ledger</title>"
        "<style>body{font:14px/1.45 system-ui,sans-serif;margin:2rem;color:#17202a}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd1d1;"
        "padding:.55rem;vertical-align:top;text-align:left}th{background:#f4f6f7}"
        "pre{white-space:pre-wrap;word-break:break-word;max-width:34rem;margin:0}"
        "code{word-break:break-all}.meta{display:grid;grid-template-columns:repeat(3,1fr);"
        "gap:1rem;margin:1rem 0}.card{border:1px solid #ccd1d1;padding:.75rem}</style>"
        "</head><body><h1>LaTeX Word Review Ledger</h1>"
        f"<p>Run <code>{_escaped(document['run_id'])}</code></p>"
        '<div class="meta">'
        f'<div class="card"><strong>Status</strong><pre>{_escaped(status)}</pre></div>'
        f'<div class="card"><strong>Summary</strong><pre>{_escaped(summary)}</pre></div>'
        f'<div class="card"><strong>Bindings</strong><pre>{_escaped(bindings)}</pre></div>'
        "</div><table><thead><tr><th>Change</th><th>Author / time</th>"
        "<th>Decision</th><th>Status</th><th>Content</th><th>Evidence</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>\n"
    )
    return page.encode("utf-8")


def build_ledger(
    changeset: Mapping[str, Any],
    approval_set: Mapping[str, Any],
    patch_plan: Mapping[str, Any],
    verification_report: Mapping[str, Any],
) -> LedgerOutput:
    """Validate all bindings and return deterministic JSON plus escaped HTML."""

    document = _ledger_document(
        changeset,
        approval_set,
        patch_plan,
        verification_report,
    )
    return LedgerOutput(
        document=document,
        json_bytes=canonical_json(document) + b"\n",
        html_bytes=render_ledger_html(document),
    )


build_review_ledger = build_ledger


__all__ = [
    "LedgerOutput",
    "build_ledger",
    "build_review_ledger",
    "render_ledger_html",
]
