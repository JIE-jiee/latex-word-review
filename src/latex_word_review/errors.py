"""Stable machine-readable errors shared by every public entry point.

Human-readable messages may improve over time.  ``ErrorCode`` values and
``ExitCode`` meanings are part of the public contract and must remain stable
within a major release.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Any


class ErrorCode(StrEnum):
    """Stable diagnostic codes from the v1alpha domain contract."""

    SCHEMA_INVALID = "E_SCHEMA_INVALID"
    SCHEMA_MAJOR_UNSUPPORTED = "E_SCHEMA_MAJOR_UNSUPPORTED"
    SCHEMA_UNKNOWN_SECURITY_FIELD = "E_SCHEMA_UNKNOWN_SECURITY_FIELD"
    PATH_ABSOLUTE = "E_PATH_ABSOLUTE"
    PATH_TRAVERSAL = "E_PATH_TRAVERSAL"
    PATH_LINK_ESCAPE = "E_PATH_LINK_ESCAPE"
    HASH_INTEGRITY_MISMATCH = "E_HASH_INTEGRITY_MISMATCH"
    HASH_SOURCE_MISMATCH = "E_HASH_SOURCE_MISMATCH"
    HASH_CHANGESET_MISMATCH = "E_HASH_CHANGESET_MISMATCH"
    HASH_APPROVAL_MISMATCH = "E_HASH_APPROVAL_MISMATCH"
    HASH_PATCHPLAN_MISMATCH = "E_HASH_PATCHPLAN_MISMATCH"
    HASH_RETURNED_ORIGINAL_MISMATCH = "E_HASH_RETURNED_ORIGINAL_MISMATCH"
    TOOL_MISSING = "E_TOOL_MISSING"
    TOOL_VERSION_UNSUPPORTED = "E_TOOL_VERSION_UNSUPPORTED"
    BACKEND_CAPABILITY_MISSING = "E_BACKEND_CAPABILITY_MISSING"
    BACKEND_FAILED = "E_BACKEND_FAILED"
    EXPORT_SILENT_LOSS = "E_EXPORT_SILENT_LOSS"
    EXPORT_DEGRADED = "W_EXPORT_DEGRADED"
    REVIEW_TRACKING_DISABLED = "E_REVIEW_TRACKING_DISABLED"
    DOCX_INVALID_PACKAGE = "E_DOCX_INVALID_PACKAGE"
    DOCX_UNSAFE_RELATIONSHIP = "E_DOCX_UNSAFE_RELATIONSHIP"
    REVISION_DELETE_TEXT_MISSING = "E_REVISION_DELETE_TEXT_MISSING"
    REVISION_AUTHOR_MISSING = "W_REVISION_AUTHOR_MISSING"
    REVISION_TIMESTAMP_MISSING = "W_REVISION_TIMESTAMP_MISSING"
    REVISION_BASELINE_DRIFT = "E_REVISION_BASELINE_DRIFT"
    REVISION_VIEW_UNSUPPORTED = "E_REVISION_VIEW_UNSUPPORTED"
    REVISION_RECONCILIATION = "E_REVISION_RECONCILIATION"
    REVISION_STRUCTURED_TEXT = "W_REVISION_STRUCTURED_TEXT"
    MAP_UNMATCHED = "E_MAP_UNMATCHED"
    MAP_AMBIGUOUS = "E_MAP_AMBIGUOUS"
    MAP_CONFIDENCE_LOW = "E_MAP_CONFIDENCE_LOW"
    APPROVAL_NOT_FINAL = "E_APPROVAL_NOT_FINAL"
    APPROVAL_CHANGE_UNKNOWN = "E_APPROVAL_CHANGE_UNKNOWN"
    APPROVAL_FINAL_TEXT_REQUIRED = "E_APPROVAL_FINAL_TEXT_REQUIRED"
    PATCH_UNSAFE_KIND = "E_PATCH_UNSAFE_KIND"
    PATCH_SOURCE_DRIFT = "E_PATCH_SOURCE_DRIFT"
    PATCH_OVERLAP = "E_PATCH_OVERLAP"
    PATCH_ACCEPTED_BUT_BLOCKED = "E_PATCH_ACCEPTED_BUT_BLOCKED"
    PATCH_UNAPPROVED_CHANGE = "E_PATCH_UNAPPROVED_CHANGE"
    APPLY_PARTIAL_WRITE = "E_APPLY_PARTIAL_WRITE"
    VERIFY_COMPILE_FAILED = "E_VERIFY_COMPILE_FAILED"
    VERIFY_DIFF_MISMATCH = "E_VERIFY_DIFF_MISMATCH"
    VERIFY_ORIGINAL_MUTATED = "E_VERIFY_ORIGINAL_MUTATED"
    BUNDLE_HASH_MISMATCH = "E_BUNDLE_HASH_MISMATCH"
    BUNDLE_PRIVATE_RELEASE = "E_BUNDLE_PRIVATE_RELEASE"
    INTERNAL_INVARIANT = "E_INTERNAL_INVARIANT"


class ExitCode(IntEnum):
    """Stable process exit categories.

    The library exposes these values even though command dispatch is implemented
    elsewhere, so CLI, local UI and a future Skill can share the same mapping.
    """

    SUCCESS = 0
    USAGE_OR_SCHEMA = 2
    TOOL_OR_ENVIRONMENT = 3
    UNTRUSTED_INPUT = 4
    BACKEND_OR_EXPORT = 5
    INGEST = 6
    APPROVAL_OR_PLAN = 7
    APPLY = 8
    VERIFY_OR_BUNDLE = 9
    INTERNAL = 10


_ERROR_EXIT_CODES: dict[ErrorCode, ExitCode] = {
    ErrorCode.SCHEMA_INVALID: ExitCode.USAGE_OR_SCHEMA,
    ErrorCode.SCHEMA_MAJOR_UNSUPPORTED: ExitCode.USAGE_OR_SCHEMA,
    ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD: ExitCode.USAGE_OR_SCHEMA,
    ErrorCode.TOOL_MISSING: ExitCode.TOOL_OR_ENVIRONMENT,
    ErrorCode.TOOL_VERSION_UNSUPPORTED: ExitCode.TOOL_OR_ENVIRONMENT,
    ErrorCode.BACKEND_CAPABILITY_MISSING: ExitCode.TOOL_OR_ENVIRONMENT,
    ErrorCode.BACKEND_FAILED: ExitCode.BACKEND_OR_EXPORT,
    ErrorCode.EXPORT_SILENT_LOSS: ExitCode.BACKEND_OR_EXPORT,
    ErrorCode.EXPORT_DEGRADED: ExitCode.SUCCESS,
    ErrorCode.REVIEW_TRACKING_DISABLED: ExitCode.BACKEND_OR_EXPORT,
    ErrorCode.REVISION_DELETE_TEXT_MISSING: ExitCode.INGEST,
    ErrorCode.REVISION_AUTHOR_MISSING: ExitCode.SUCCESS,
    ErrorCode.REVISION_TIMESTAMP_MISSING: ExitCode.SUCCESS,
    ErrorCode.REVISION_BASELINE_DRIFT: ExitCode.INGEST,
    ErrorCode.REVISION_VIEW_UNSUPPORTED: ExitCode.INGEST,
    ErrorCode.REVISION_RECONCILIATION: ExitCode.INGEST,
    ErrorCode.REVISION_STRUCTURED_TEXT: ExitCode.SUCCESS,
    ErrorCode.MAP_UNMATCHED: ExitCode.INGEST,
    ErrorCode.MAP_AMBIGUOUS: ExitCode.INGEST,
    ErrorCode.MAP_CONFIDENCE_LOW: ExitCode.INGEST,
    ErrorCode.APPROVAL_NOT_FINAL: ExitCode.APPROVAL_OR_PLAN,
    ErrorCode.APPROVAL_CHANGE_UNKNOWN: ExitCode.APPROVAL_OR_PLAN,
    ErrorCode.APPROVAL_FINAL_TEXT_REQUIRED: ExitCode.APPROVAL_OR_PLAN,
    ErrorCode.PATCH_UNSAFE_KIND: ExitCode.APPROVAL_OR_PLAN,
    ErrorCode.PATCH_ACCEPTED_BUT_BLOCKED: ExitCode.APPROVAL_OR_PLAN,
    ErrorCode.PATCH_SOURCE_DRIFT: ExitCode.APPLY,
    ErrorCode.PATCH_OVERLAP: ExitCode.APPLY,
    ErrorCode.PATCH_UNAPPROVED_CHANGE: ExitCode.APPLY,
    ErrorCode.APPLY_PARTIAL_WRITE: ExitCode.APPLY,
    ErrorCode.VERIFY_COMPILE_FAILED: ExitCode.VERIFY_OR_BUNDLE,
    ErrorCode.VERIFY_DIFF_MISMATCH: ExitCode.VERIFY_OR_BUNDLE,
    ErrorCode.VERIFY_ORIGINAL_MUTATED: ExitCode.VERIFY_OR_BUNDLE,
    ErrorCode.BUNDLE_HASH_MISMATCH: ExitCode.VERIFY_OR_BUNDLE,
    ErrorCode.BUNDLE_PRIVATE_RELEASE: ExitCode.VERIFY_OR_BUNDLE,
    ErrorCode.INTERNAL_INVARIANT: ExitCode.INTERNAL,
}

for _security_code in (
    ErrorCode.PATH_ABSOLUTE,
    ErrorCode.PATH_TRAVERSAL,
    ErrorCode.PATH_LINK_ESCAPE,
    ErrorCode.HASH_INTEGRITY_MISMATCH,
    ErrorCode.HASH_SOURCE_MISMATCH,
    ErrorCode.HASH_CHANGESET_MISMATCH,
    ErrorCode.HASH_APPROVAL_MISMATCH,
    ErrorCode.HASH_PATCHPLAN_MISMATCH,
    ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
    ErrorCode.DOCX_INVALID_PACKAGE,
    ErrorCode.DOCX_UNSAFE_RELATIONSHIP,
):
    _ERROR_EXIT_CODES[_security_code] = ExitCode.UNTRUSTED_INPUT


def exit_code_for_error(code: ErrorCode | str) -> ExitCode:
    """Return the public CLI category for a machine-readable error code."""

    try:
        normalized = code if isinstance(code, ErrorCode) else ErrorCode(code)
    except ValueError:
        return ExitCode.INTERNAL
    return _ERROR_EXIT_CODES.get(normalized, ExitCode.INTERNAL)


@dataclass(frozen=True, slots=True)
class ContractViolation:
    """Serializable details for one contract failure."""

    code: ErrorCode
    message: str
    path: tuple[str | int, ...] = ()
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.details is not None and not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    @property
    def exit_code(self) -> ExitCode:
        return exit_code_for_error(self.code)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation without exposing exception internals."""

        result: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "path": list(self.path),
            "exit_code": int(self.exit_code),
        }
        if self.details:
            result["details"] = dict(self.details)
        return result


class ContractError(ValueError):
    """Raised when untrusted data violates a public, fail-closed contract."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        path: tuple[str | int, ...] = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.violation = ContractViolation(code, message, path, details)
        super().__init__(f"{code.value}: {message}")

    @property
    def code(self) -> ErrorCode:
        return self.violation.code

    @property
    def path(self) -> tuple[str | int, ...]:
        return self.violation.path

    @property
    def exit_code(self) -> ExitCode:
        return self.violation.exit_code

    def as_dict(self) -> dict[str, Any]:
        return self.violation.as_dict()


__all__ = [
    "ContractError",
    "ContractViolation",
    "ErrorCode",
    "ExitCode",
    "exit_code_for_error",
]
