"""RFC 8785 canonical JSON and SHA-256 helpers.

The implementation deliberately delegates JSON Canonicalization Scheme (JCS)
to the audited ``rfc8785`` package.  There is no permissive fallback: missing
or invalid dependencies fail before authorization hashes can be produced.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

from latex_word_review.errors import ContractError, ErrorCode

try:
    import rfc8785
except ImportError as exc:  # pragma: no cover - exercised in clean-install CI
    raise RuntimeError(
        "latex-word-review requires rfc8785>=0.1.4,<0.2 for authorization hashes"
    ) from exc


SHA256_PREFIX = "sha256:"


def canonical_json(value: Any) -> bytes:
    """Serialize a JSON value with RFC 8785 JCS and return UTF-8 bytes."""

    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError) as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "value cannot be represented as RFC 8785 canonical JSON",
            details={"canonicalizer": "rfc8785"},
        ) from exc


def canonical_json_text(value: Any) -> str:
    """Return RFC 8785 canonical JSON as Unicode text."""

    return canonical_json(value).decode("utf-8")


def sha256_bytes(data: bytes | bytearray | memoryview) -> str:
    """Hash raw bytes using the contract's tagged, lowercase representation."""

    return f"{SHA256_PREFIX}{hashlib.sha256(bytes(data)).hexdigest()}"


def sha256_canonical(value: Any) -> str:
    """Hash the RFC 8785 representation of a JSON value."""

    return sha256_bytes(canonical_json(value))


def compute_payload_sha256(document_or_payload: Mapping[str, Any]) -> str:
    """Compute a payload hash from an envelope or a payload mapping."""

    payload = document_or_payload.get("payload", document_or_payload)
    if not isinstance(payload, Mapping):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "payload must be a JSON object",
            path=("payload",),
        )
    return sha256_canonical(payload)


def _document_hash_input(document: Mapping[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(document))
    integrity = candidate.get("integrity")
    if not isinstance(integrity, dict):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "integrity must be a JSON object",
            path=("integrity",),
        )
    integrity.pop("document_sha256", None)
    return candidate


def compute_document_sha256(document: Mapping[str, Any]) -> str:
    """Hash an envelope after excluding ``integrity.document_sha256`` itself."""

    return sha256_canonical(_document_hash_input(document))


def seal_envelope(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep-copied envelope with both integrity hashes populated.

    Existing integrity values are ignored, so callers cannot accidentally bind
    a new payload to stale authorization hashes.  Schema/semantic validation is
    intentionally a separate step performed by :func:`validate_contract`.
    """

    sealed = copy.deepcopy(dict(document))
    sealed["integrity"] = {
        "payload_sha256": compute_payload_sha256(sealed),
        "document_sha256": f"{SHA256_PREFIX}{'0' * 64}",
    }
    sealed["integrity"]["document_sha256"] = compute_document_sha256(sealed)
    return sealed


def verify_envelope_integrity(document: Mapping[str, Any]) -> None:
    """Verify both envelope hashes and fail closed on the first mismatch."""

    integrity = document.get("integrity")
    if not isinstance(integrity, Mapping):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "integrity must be a JSON object",
            path=("integrity",),
        )
    stored_payload = integrity.get("payload_sha256")
    stored_document = integrity.get("document_sha256")
    if not isinstance(stored_payload, str) or not isinstance(stored_document, str):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "integrity hashes must be strings",
            path=("integrity",),
        )

    computed_payload = compute_payload_sha256(document)
    if not hmac.compare_digest(stored_payload, computed_payload):
        raise ContractError(
            _integrity_error_code(document),
            "payload hash does not match canonical payload bytes",
            path=("integrity", "payload_sha256"),
            details={"expected": stored_payload, "actual": computed_payload},
        )

    computed_document = compute_document_sha256(document)
    if not hmac.compare_digest(stored_document, computed_document):
        raise ContractError(
            _integrity_error_code(document),
            "document hash does not match canonical envelope bytes",
            path=("integrity", "document_sha256"),
            details={"expected": stored_document, "actual": computed_document},
        )


def _integrity_error_code(document: Mapping[str, Any]) -> ErrorCode:
    return {
        "ChangeSet": ErrorCode.HASH_CHANGESET_MISMATCH,
        "ApprovalSet": ErrorCode.HASH_APPROVAL_MISMATCH,
        "PatchPlan": ErrorCode.HASH_PATCHPLAN_MISMATCH,
        "AuditBundle": ErrorCode.BUNDLE_HASH_MISMATCH,
    }.get(str(document.get("schema_name")), ErrorCode.HASH_INTEGRITY_MISMATCH)


__all__ = [
    "SHA256_PREFIX",
    "canonical_json",
    "canonical_json_text",
    "compute_document_sha256",
    "compute_payload_sha256",
    "seal_envelope",
    "sha256_bytes",
    "sha256_canonical",
    "verify_envelope_integrity",
]
