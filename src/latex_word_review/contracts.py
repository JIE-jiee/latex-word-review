"""Versioned JSON Schema loading and fail-closed contract validation.

JSON Schema evaluation is delegated to ``jsonschema``'s Draft 2020-12
implementation.  This module adds only project-specific format checks,
cross-field safety invariants, integrity verification and hash binding.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache, lru_cache
from pathlib import PurePosixPath
from typing import Any

from latex_word_review.canonical import (
    compute_document_sha256,
    compute_payload_sha256,
    seal_envelope,
    sha256_canonical,
    verify_envelope_integrity,
)
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.schema_catalog import (
    COMMON_SCHEMA_FILENAME,
    SCHEMA_FILES,
    SCHEMA_MAJOR,
    SCHEMA_VERSION,
    read_schema_resource,
)

try:
    from jsonschema import Draft202012Validator, FormatChecker, ValidationError
    from referencing import Registry, Resource
except ImportError as exc:  # pragma: no cover - exercised in clean-install CI
    raise RuntimeError(
        "latex-word-review requires jsonschema>=4.26,<5 for contract validation"
    ) from exc


_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True, slots=True)
class ValidatedContract:
    """Small immutable receipt returned after complete validation."""

    schema_name: str
    schema_version: str
    object_id: str
    payload_sha256: str
    document_sha256: str


@dataclass(frozen=True, slots=True)
class HashBinding:
    """An explicit expected/actual authorization hash comparison."""

    label: str
    expected: str
    actual: str
    error_code: ErrorCode = ErrorCode.HASH_INTEGRITY_MISMATCH


_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time")
def _is_timezone_aware_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


@_FORMAT_CHECKER.checks("relative-path")
def _is_safe_relative_path(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not value or "\x00" in value or "\\" in value:
        return False
    if value.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", value):
        return False
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute()


def available_schema_names() -> tuple[str, ...]:
    """Return the twelve public v1alpha object names in deterministic order."""

    return tuple(SCHEMA_FILES)


@cache
def _read_schema_file(filename: str) -> dict[str, Any]:
    text = read_schema_resource(filename).decode("utf-8", errors="strict")
    value = _loads_json_object(text, trusted_schema=True)
    Draft202012Validator.check_schema(value)
    return value


def load_schema(schema_name: str, *, schema_version: str = SCHEMA_VERSION) -> dict[str, Any]:
    """Load a packaged Draft 2020-12 schema as an independent mapping."""

    _require_supported_version(schema_version)
    filename = SCHEMA_FILES.get(schema_name)
    if filename is None:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "unknown schema_name",
            path=("schema_name",),
            details={"schema_name": schema_name},
        )
    return copy.deepcopy(_read_schema_file(filename))


@lru_cache(maxsize=1)
def _schema_registry() -> Registry[Any]:
    schemas = [_read_schema_file(COMMON_SCHEMA_FILENAME)]
    schemas.extend(_read_schema_file(filename) for filename in SCHEMA_FILES.values())
    pairs = []
    for schema in schemas:
        uri = schema.get("$id")
        if not isinstance(uri, str):
            raise RuntimeError("packaged schema is missing an absolute $id")
        pairs.append((uri, Resource.from_contents(schema)))
    return Registry().with_resources(pairs)


def _validator(schema_name: str) -> Draft202012Validator:
    schema = _read_schema_file(SCHEMA_FILES[schema_name])
    return Draft202012Validator(
        schema,
        registry=_schema_registry(),
        format_checker=_FORMAT_CHECKER,
    )


def load_contract_json(data: str | bytes | bytearray) -> dict[str, Any]:
    """Parse an untrusted contract while rejecting duplicate keys and non-finite numbers."""

    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "contract JSON must be UTF-8") from exc
    else:
        text = data
    return _loads_json_object(text, trusted_schema=False)


def _loads_json_object(text: str, *, trusted_schema: bool) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    def reject_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        if trusted_schema:
            raise RuntimeError("packaged JSON Schema is invalid JSON") from exc
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid contract JSON") from exc
    if not isinstance(value, dict):
        if trusted_schema:
            raise RuntimeError("packaged JSON Schema root must be an object")
        raise ContractError(ErrorCode.SCHEMA_INVALID, "contract root must be a JSON object")
    return value


def make_envelope(
    *,
    schema_name: str,
    object_id: str,
    run_id: str | None,
    generated_at: str,
    producer: Mapping[str, Any],
    payload: Mapping[str, Any],
    extensions: Mapping[str, Any] | None = None,
    schema_version: str = SCHEMA_VERSION,
) -> dict[str, Any]:
    """Construct, seal and validate one versioned public envelope."""

    document: dict[str, Any] = {
        "schema_name": schema_name,
        "schema_version": schema_version,
        "object_id": object_id,
        "run_id": run_id,
        "generated_at": generated_at,
        "producer": copy.deepcopy(dict(producer)),
        "payload": copy.deepcopy(dict(payload)),
        "integrity": {
            "payload_sha256": "sha256:" + "0" * 64,
            "document_sha256": "sha256:" + "0" * 64,
        },
        "extensions": copy.deepcopy(dict(extensions or {})),
    }
    sealed = seal_envelope(document)
    validate_contract(sealed)
    return sealed


def validate_contract(
    document: Mapping[str, Any],
    *,
    verify_integrity: bool = True,
) -> ValidatedContract:
    """Validate schema, formats, semantic invariants and integrity hashes.

    The default is intentionally strict: only the exact supported prerelease is
    accepted.  Future schema objects require an explicit migration or a newer
    reader; they are never guessed into a write-capable representation.
    """

    if not isinstance(document, Mapping):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "contract root must be an object")
    schema_name = document.get("schema_name")
    if not isinstance(schema_name, str) or schema_name not in SCHEMA_FILES:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "unknown or missing schema_name",
            path=("schema_name",),
        )
    schema_version = document.get("schema_version")
    if not isinstance(schema_version, str):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "schema_version must be a SemVer string",
            path=("schema_version",),
        )
    _require_supported_version(schema_version)

    errors = sorted(_validator(schema_name).iter_errors(document), key=_validation_sort_key)
    if errors:
        raise _contract_error_from_validation(errors[0])

    _validate_semantics(schema_name, document)
    if verify_integrity:
        verify_envelope_integrity(document)

    integrity = document["integrity"]
    return ValidatedContract(
        schema_name=schema_name,
        schema_version=schema_version,
        object_id=document["object_id"],
        payload_sha256=integrity["payload_sha256"],
        document_sha256=integrity["document_sha256"],
    )


def _require_supported_version(schema_version: str) -> None:
    match = _SEMVER_RE.fullmatch(schema_version)
    if match is None:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "schema_version is not valid SemVer",
            path=("schema_version",),
        )
    if int(match.group("major")) != SCHEMA_MAJOR:
        raise ContractError(
            ErrorCode.SCHEMA_MAJOR_UNSUPPORTED,
            "schema major version is unsupported",
            path=("schema_version",),
            details={"supported_major": SCHEMA_MAJOR, "received": schema_version},
        )
    if schema_version != SCHEMA_VERSION:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "schema version is not supported by this reader",
            path=("schema_version",),
            details={"supported": SCHEMA_VERSION, "received": schema_version},
        )


def _validation_sort_key(error: ValidationError) -> tuple[tuple[str, ...], str]:
    return tuple(str(part) for part in error.absolute_path), error.message


def _contract_error_from_validation(error: ValidationError) -> ContractError:
    path = tuple(error.absolute_path)
    code = ErrorCode.SCHEMA_INVALID
    if error.validator in {"additionalProperties", "enum", "const", "propertyNames"}:
        code = ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD
    if error.validator == "format" and error.validator_value == "relative-path":
        value = error.instance
        if isinstance(value, str):
            if value.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", value):
                code = ErrorCode.PATH_ABSOLUTE
            elif ".." in value.split("/"):
                code = ErrorCode.PATH_TRAVERSAL
    return ContractError(
        code,
        "contract does not satisfy its declared JSON Schema",
        path=path,
        details={"validator": error.validator, "schema_message": error.message},
    )


def verify_hash_binding(binding: HashBinding) -> None:
    """Fail closed when an explicit upstream/downstream hash binding differs."""

    if binding.expected != binding.actual:
        raise ContractError(
            binding.error_code,
            "authorization hash binding does not match",
            details={
                "binding": binding.label,
                "expected": binding.expected,
                "actual": binding.actual,
            },
        )


def verify_payload_binding(
    downstream: Mapping[str, Any],
    payload_field: str,
    upstream: Mapping[str, Any],
    *,
    error_code: ErrorCode | None = None,
) -> None:
    """Verify a downstream payload field against an upstream semantic hash."""

    validate_contract(downstream)
    validate_contract(upstream)
    payload = downstream["payload"]
    expected = payload.get(payload_field)
    if not isinstance(expected, str):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "requested payload binding is not a hash field",
            path=("payload", payload_field),
        )
    actual = compute_payload_sha256(upstream)
    default_code = {
        "ChangeSet": ErrorCode.HASH_CHANGESET_MISMATCH,
        "ApprovalSet": ErrorCode.HASH_APPROVAL_MISMATCH,
        "PatchPlan": ErrorCode.HASH_PATCHPLAN_MISMATCH,
    }.get(str(downstream.get("schema_name")), ErrorCode.HASH_SOURCE_MISMATCH)
    verify_hash_binding(
        HashBinding(
            label=f"{downstream['schema_name']}.payload.{payload_field}",
            expected=expected,
            actual=actual,
            error_code=error_code or default_code,
        )
    )


def _validate_semantics(schema_name: str, document: Mapping[str, Any]) -> None:
    payload = document["payload"]
    _walk_source_locations(payload)
    validator = _SEMANTIC_VALIDATORS.get(schema_name)
    if validator is not None:
        validator(document, payload)


def _walk_source_locations(value: object, path: tuple[str | int, ...] = ()) -> None:
    if isinstance(value, Mapping):
        if {"start_byte", "end_byte", "slice_sha256"}.issubset(value):
            start = value["start_byte"]
            end = value["end_byte"]
            if isinstance(start, int) and isinstance(end, int) and start > end:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "source byte range must be a non-decreasing half-open interval",
                    path=path,
                )
        for key, child in value.items():
            _walk_source_locations(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_source_locations(child, (*path, index))


def _unique(items: Iterable[str], *, path: tuple[str | int, ...]) -> set[str]:
    values = list(items)
    unique = set(values)
    if len(values) != len(unique):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID, "identifier list contains duplicates", path=path
        )
    return unique


def _require_equal(
    expected: object,
    actual: object,
    *,
    path: tuple[str | int, ...],
    code: ErrorCode = ErrorCode.SCHEMA_INVALID,
    message: str = "semantic invariant does not match",
) -> None:
    if expected != actual:
        raise ContractError(
            code,
            message,
            path=path,
            details={"expected": expected, "actual": actual},
        )


def compute_source_tree_sha256(files: Sequence[Mapping[str, Any]]) -> str:
    """Compute the contract source-tree hash from normalized file records."""

    records = [
        {
            "path": item["path"],
            "role": item["role"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in sorted(files, key=lambda candidate: candidate["path"])
    ]
    return sha256_canonical(records)


def _validate_run_manifest(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    revision = payload["manifest_revision"]
    previous = payload["previous_manifest_payload_sha256"]
    if (revision == 1) != (previous is None):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "only RunManifest revision 1 may omit its previous payload hash",
            path=("payload", "previous_manifest_payload_sha256"),
        )


def _validate_source_manifest(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    _require_equal(
        document["object_id"],
        payload["source_manifest_id"],
        path=("payload", "source_manifest_id"),
    )
    artifact = payload["snapshot_artifact"]
    if not artifact["immutable"]:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source snapshot artifact must be immutable",
            path=("payload", "snapshot_artifact", "immutable"),
        )
    files = payload["files"]
    paths = [item["path"] for item in files]
    _unique(paths, path=("payload", "files"))
    if paths != sorted(paths):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "source files must be sorted by normalized relative path",
            path=("payload", "files"),
        )
    if payload["main_document"] not in paths:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "main_document is not present in the source file manifest",
            path=("payload", "main_document"),
        )
    _require_equal(
        payload["source_tree_sha256"],
        compute_source_tree_sha256(files),
        path=("payload", "source_tree_sha256"),
        code=ErrorCode.HASH_SOURCE_MISMATCH,
        message="source tree hash does not match file records",
    )


def _validate_review_ir(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    units = payload["units"]
    _unique((unit["unit_id"] for unit in units), path=("payload", "units"))
    ordinals = [unit["ordinal"] for unit in units]
    if ordinals != sorted(ordinals) or len(ordinals) != len(set(ordinals)):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "review units must have unique increasing ordinals",
            path=("payload", "units"),
        )


def _validate_source_map(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    mappings = payload["mappings"]
    _unique((item["unit_id"] for item in mappings), path=("payload", "mappings"))
    actual = {status: 0 for status in ("exact", "degraded", "unmapped", "conflict")}
    for mapping_index, item in enumerate(mappings):
        actual[item["status"]] += 1
        provenance = item["text_provenance"]
        segments = provenance["segments"]
        review_cursor = 0
        source_cursor = 0
        for segment_index, segment in enumerate(segments):
            path = (
                "payload",
                "mappings",
                mapping_index,
                "text_provenance",
                "segments",
                segment_index,
            )
            if (
                segment["review_start"] != review_cursor
                or segment["source_start_byte"] != source_cursor
                or segment["review_end"] <= segment["review_start"]
                or segment["source_end_byte"] <= segment["source_start_byte"]
            ):
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "text provenance segments must be positive and contiguous",
                    path=path,
                )
            expected_patchable = segment["transformation"] == "identity"
            if segment["auto_patchable"] is not expected_patchable:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "text provenance patchability disagrees with its transformation",
                    path=(*path, "auto_patchable"),
                )
            review_cursor = segment["review_end"]
            source_cursor = segment["source_end_byte"]
        _require_equal(
            provenance["review_length"],
            review_cursor,
            path=("payload", "mappings", mapping_index, "text_provenance", "review_length"),
        )
        location = item["source_location"]
        _require_equal(
            location["end_byte"] - location["start_byte"],
            source_cursor,
            path=("payload", "mappings", mapping_index, "text_provenance", "segments"),
        )
    coverage = payload["coverage"]
    _require_equal(coverage["total"], len(mappings), path=("payload", "coverage", "total"))
    for status, count in actual.items():
        _require_equal(coverage[status], count, path=("payload", "coverage", status))


def _validate_export_report(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    if payload["status"] == "success":
        for field in ("review_ir_sha256", "source_map_sha256", "review_docx"):
            if payload[field] is None:
                raise ContractError(
                    ErrorCode.EXPORT_SILENT_LOSS,
                    "successful export is missing a required validated artifact or binding",
                    path=("payload", field),
                )
    if payload["status"] != "failed" and payload["review_docx"] is None:
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "non-failed export must reference its DOCX artifact",
            path=("payload", "review_docx"),
        )


def _validate_raw_revision(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    _require_equal(document["object_id"], payload["raw_event_id"], path=("payload", "raw_event_id"))
    if payload["kind"] == "delete" and payload["content"]["deleted_text"] is None:
        raise ContractError(
            ErrorCode.REVISION_DELETE_TEXT_MISSING,
            "deletion event is missing w:delText evidence",
            path=("payload", "content", "deleted_text"),
        )


def _validate_changeset(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    returned = payload["returned_original"]
    if not returned["immutable"] or returned["role"] != "returned_original":
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned Word original must be an immutable returned_original artifact",
            path=("payload", "returned_original"),
        )
    raw_events = payload["raw_events"]
    raw_ids = _unique(
        (event["raw_event_id"] for event in raw_events),
        path=("payload", "raw_events"),
    )
    orders = [event["document_order"] for event in raw_events]
    if orders != sorted(orders):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "raw revision events must retain document order",
            path=("payload", "raw_events"),
        )
    for index, event in enumerate(raw_events):
        _require_equal(
            returned["sha256"],
            event["returned_docx_sha256"],
            path=("payload", "raw_events", index, "returned_docx_sha256"),
            code=ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            message="raw event is bound to a different returned Word original",
        )
        if event["kind"] == "delete" and event["content"]["deleted_text"] is None:
            raise ContractError(
                ErrorCode.REVISION_DELETE_TEXT_MISSING,
                "deletion event is missing deleted text evidence",
                path=("payload", "raw_events", index, "content", "deleted_text"),
            )
    changes = payload["changes"]
    _unique((change["change_id"] for change in changes), path=("payload", "changes"))
    for index, change in enumerate(changes):
        if not set(change["raw_event_ids"]).issubset(raw_ids):
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "change references an unknown raw revision event",
                path=("payload", "changes", index, "raw_event_ids"),
            )
        if change["safety_class"] == "plain_text_candidate":
            safe = (
                change["kind"] in {"insertion", "deletion", "replacement"}
                and change["resolution"]["status"] == "exact"
                and change["unit_id"] is not None
                and change["source_location"] is not None
                and isinstance(change["before"], str)
                and isinstance(change["after"], str)
            )
            if not safe:
                raise ContractError(
                    ErrorCode.PATCH_UNSAFE_KIND,
                    "plain_text_candidate does not satisfy the v0.1 safe subset",
                    path=("payload", "changes", index),
                )
    counts = payload["counts"]
    _require_equal(counts["raw_events"], len(raw_events), path=("payload", "counts", "raw_events"))
    _require_equal(counts["changes"], len(changes), path=("payload", "counts", "changes"))


def _validate_approval(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    _require_equal(
        document["object_id"], payload["approval_set_id"], path=("payload", "approval_set_id")
    )
    decisions = payload["decisions"]
    decided_ids = _unique(
        (decision["change_id"] for decision in decisions),
        path=("payload", "decisions"),
    )
    undecided_ids = _unique(
        payload["undecided_change_ids"], path=("payload", "undecided_change_ids")
    )
    if decided_ids & undecided_ids:
        raise ContractError(
            ErrorCode.APPROVAL_CHANGE_UNKNOWN,
            "a change cannot be both decided and undecided",
            path=("payload", "undecided_change_ids"),
        )
    if payload["status"] == "final" and undecided_ids:
        raise ContractError(
            ErrorCode.APPROVAL_NOT_FINAL,
            "final ApprovalSet cannot contain undecided changes",
            path=("payload", "undecided_change_ids"),
        )
    expected = {
        key: 0 for key in ("accepted", "accepted_with_edit", "rejected", "manual", "conflict")
    }
    for decision in decisions:
        expected[decision["decision"]] += 1
    expected["pending"] = len(undecided_ids)
    _require_equal(
        payload["decision_summary"],
        expected,
        path=("payload", "decision_summary"),
    )


def _validate_patch_plan(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    _require_equal(
        document["object_id"], payload["patch_plan_id"], path=("payload", "patch_plan_id")
    )
    operations = payload["operations"]
    _unique((item["operation_id"] for item in operations), path=("payload", "operations"))
    _unique((item["change_id"] for item in operations), path=("payload", "operations"))
    if payload["status"] == "ready" and payload["accepted_but_blocked"]:
        raise ContractError(
            ErrorCode.PATCH_ACCEPTED_BUT_BLOCKED,
            "ready PatchPlan cannot contain accepted but blocked changes",
            path=("payload", "accepted_but_blocked"),
        )
    if payload["status"] == "noop" and operations:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "noop PatchPlan cannot contain operations",
            path=("payload", "operations"),
        )
    ranges: dict[str, list[tuple[int, int]]] = {}
    for index, operation in enumerate(operations):
        target = operation["target"]
        file_ranges = ranges.setdefault(target["path"], [])
        current = target["start_byte"], target["end_byte"]
        for prior in file_ranges:
            if max(prior[0], current[0]) < min(prior[1], current[1]):
                raise ContractError(
                    ErrorCode.PATCH_OVERLAP,
                    "PatchPlan contains overlapping byte ranges",
                    path=("payload", "operations", index, "target"),
                )
        file_ranges.append(current)
    summary = payload["summary"]
    _require_equal(summary["planned"], len(operations), path=("payload", "summary", "planned"))
    _require_equal(
        summary["blocked"],
        len(payload["accepted_but_blocked"]),
        path=("payload", "summary", "blocked"),
    )
    _require_equal(
        summary["excluded"],
        len(payload["excluded_changes"]),
        path=("payload", "summary", "excluded"),
    )
    bindings = {item["kind"]: item["expected"] for item in payload["preconditions"]}
    expected_bindings = {
        "source_tree_sha256_equals": payload["source_tree_sha256"],
        "changeset_payload_sha256_equals": payload["changeset_sha256"],
        "approval_payload_sha256_equals": payload["approval_set_sha256"],
        "policy_sha256_equals": payload["policy_sha256"],
    }
    for kind, expected_hash in expected_bindings.items():
        _require_equal(
            expected_hash,
            bindings.get(kind),
            path=("payload", "preconditions"),
            code=ErrorCode.HASH_PATCHPLAN_MISMATCH,
            message=f"PatchPlan is missing or mismatches {kind}",
        )


def _validate_verification(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    if payload["status"] != "pass":
        return
    comparisons = (
        (
            "original_source",
            payload["original_source_pre_sha256"],
            payload["original_source_post_sha256"],
        ),
        (
            "returned_original",
            payload["returned_original_pre_sha256"],
            payload["returned_original_post_sha256"],
        ),
    )
    for label, before, after in comparisons:
        if before != after:
            raise ContractError(
                ErrorCode.VERIFY_ORIGINAL_MUTATED,
                f"{label} changed during the workflow",
                path=("payload", f"{label}_post_sha256"),
            )
    reconciliation = payload["patch_reconciliation"]
    if any(
        reconciliation[field] != 0
        for field in (
            "missing_count",
            "extra_count",
            "duplicate_count",
            "unapproved_modification_count",
        )
    ):
        raise ContractError(
            ErrorCode.VERIFY_DIFF_MISMATCH,
            "passing verification contains an unapplied, extra, duplicate or unapproved edit",
            path=("payload", "patch_reconciliation"),
        )
    if payload["revised_source_manifest_sha256"] is None:
        raise ContractError(
            ErrorCode.VERIFY_DIFF_MISMATCH,
            "passing verification must bind a revised source manifest",
            path=("payload", "revised_source_manifest_sha256"),
        )


def _validate_audit_bundle(document: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    entries = payload["entries"]
    paths = [entry["path"] for entry in entries]
    _unique(paths, path=("payload", "entries"))
    if paths != sorted(paths):
        raise ContractError(
            ErrorCode.BUNDLE_HASH_MISMATCH,
            "bundle entries must be in lexicographic path order",
            path=("payload", "entries"),
        )
    _require_equal(
        payload["manifest_sha256"],
        sha256_canonical(entries),
        path=("payload", "manifest_sha256"),
        code=ErrorCode.BUNDLE_HASH_MISMATCH,
        message="bundle manifest hash does not match normalized entries",
    )
    if (
        payload["content_classification"] == "public_fixture"
        and payload["privacy_scan"]["status"] != "pass"
    ):
        raise ContractError(
            ErrorCode.BUNDLE_PRIVATE_RELEASE,
            "public bundle requires a passing privacy scan",
            path=("payload", "privacy_scan", "status"),
        )


_SEMANTIC_VALIDATORS = {
    "RunManifest": _validate_run_manifest,
    "SourceManifest": _validate_source_manifest,
    "ReviewIR": _validate_review_ir,
    "SourceMap": _validate_source_map,
    "ExportReport": _validate_export_report,
    "RawRevisionEvent": _validate_raw_revision,
    "ChangeSet": _validate_changeset,
    "ApprovalSet": _validate_approval,
    "PatchPlan": _validate_patch_plan,
    "VerificationReport": _validate_verification,
    "AuditBundle": _validate_audit_bundle,
}


__all__ = [
    "HashBinding",
    "SCHEMA_FILES",
    "SCHEMA_VERSION",
    "ValidatedContract",
    "available_schema_names",
    "compute_document_sha256",
    "compute_payload_sha256",
    "compute_source_tree_sha256",
    "load_contract_json",
    "load_schema",
    "make_envelope",
    "verify_hash_binding",
    "verify_payload_binding",
    "validate_contract",
]
