"""Stable, content-derived identifiers for public contract objects."""

from __future__ import annotations

import re
import secrets
import time
import uuid
from collections.abc import Sequence
from typing import Any

from latex_word_review.canonical import sha256_canonical
from latex_word_review.errors import ContractError, ErrorCode

_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]*_$")
_SHA256_RE = re.compile(r"^sha256:([0-9a-f]{64})$")


def stable_id(prefix: str, semantic_input: Any, *, digest_bits: int = 128) -> str:
    """Derive a lowercase ID from canonical semantic input.

    ``digest_bits`` must be a byte-aligned value of at least 128 bits.  Collision
    handling is deterministic: callers may recompute with a longer digest; they
    must never append a random ordinal.
    """

    if not _PREFIX_RE.fullmatch(prefix):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "stable ID prefix must be lowercase alphanumeric and end in '_'",
            details={"prefix": prefix},
        )
    if digest_bits < 128 or digest_bits > 256 or digest_bits % 8:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "stable ID digest length must be byte-aligned and between 128 and 256 bits",
            details={"digest_bits": digest_bits},
        )
    digest = sha256_canonical(semantic_input).removeprefix("sha256:")
    return f"{prefix}{digest[: digest_bits // 4]}"


def stable_id_from_sha256(prefix: str, tagged_sha256: str, *, digest_bits: int = 128) -> str:
    """Derive an ID directly from a previously verified SHA-256 string."""

    match = _SHA256_RE.fullmatch(tagged_sha256)
    if match is None:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "content hash must use the 'sha256:' lowercase representation",
        )
    if not _PREFIX_RE.fullmatch(prefix):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid stable ID prefix")
    if digest_bits < 128 or digest_bits > 256 or digest_bits % 8:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid stable ID digest length")
    return f"{prefix}{match.group(1)[: digest_bits // 4]}"


def new_run_id(*, timestamp_ms: int | None = None, random_bits: int | None = None) -> str:
    """Create a UUIDv7 run identifier without requiring Python 3.14's uuid7 API.

    The injectable arguments exist for deterministic tests.  Production callers
    should leave both unset so timestamp and random bits come from trusted local
    primitives.
    """

    milliseconds = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    randomness = secrets.randbits(74) if random_bits is None else random_bits
    if not 0 <= milliseconds < 2**48:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "UUIDv7 timestamp is out of range")
    if not 0 <= randomness < 2**74:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "UUIDv7 random value is out of range")

    rand_a = randomness >> 62
    rand_b = randomness & ((1 << 62) - 1)
    value = milliseconds << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return f"run_{uuid.UUID(int=value)}"


def derive_source_manifest_id(source_tree_sha256: str) -> str:
    return stable_id_from_sha256("src_", source_tree_sha256)


def derive_artifact_id(file_sha256: str) -> str:
    return stable_id_from_sha256("art_", file_sha256)


def derive_unit_id(
    *,
    source_tree_sha256: str,
    path: str,
    start_byte: int,
    end_byte: int,
    unit_kind: str,
    slice_sha256: str,
    profile_version: str,
) -> str:
    return stable_id(
        "unit_",
        [
            source_tree_sha256,
            path,
            start_byte,
            end_byte,
            unit_kind,
            slice_sha256,
            profile_version,
        ],
    )


def derive_raw_event_id(
    *,
    returned_docx_sha256: str,
    part_uri: str,
    kind: str,
    native_id: str | None,
    document_order: int,
    fragment_sha256: str,
) -> str:
    return stable_id(
        "rev_",
        [
            returned_docx_sha256,
            part_uri,
            kind,
            native_id,
            document_order,
            fragment_sha256,
        ],
    )


def derive_change_id(raw_event_ids: Sequence[str], *, profile_version: str) -> str:
    return stable_id("chg_", [list(raw_event_ids), profile_version])


def derive_diagnostic_id(
    *, code: str, phase: str, location_or_evidence_fingerprint: str | None
) -> str:
    return stable_id("diag_", [code, phase, location_or_evidence_fingerprint])


__all__ = [
    "derive_artifact_id",
    "derive_change_id",
    "derive_diagnostic_id",
    "derive_raw_event_id",
    "derive_source_manifest_id",
    "derive_unit_id",
    "new_run_id",
    "stable_id",
    "stable_id_from_sha256",
]
