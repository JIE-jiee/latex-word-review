"""Fail-closed tests for canonical hashes, integrity envelopes, and stable IDs."""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest

from latex_word_review.canonical import (
    canonical_json,
    compute_document_sha256,
    compute_payload_sha256,
    verify_envelope_integrity,
)
from latex_word_review.errors import ContractError, ErrorCode, ExitCode, exit_code_for_error
from latex_word_review.ids import new_run_id, stable_id, stable_id_from_sha256
from tests.test_contracts import _golden_contracts


def _assert_error(code: ErrorCode, operation: Callable[[], object]) -> None:
    with pytest.raises(ContractError) as raised:
        operation()
    assert raised.value.code is code


def test_canonical_json_and_payload_hash_reject_non_json_and_non_object_values() -> None:
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: canonical_json(float("nan")))
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: compute_payload_sha256({"payload": ["not", "an", "object"]}),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: compute_document_sha256({"payload": {}}),
    )


def test_integrity_verifier_rejects_missing_and_nonstring_hashes() -> None:
    document = _golden_contracts()["SourceManifest"]
    missing = copy.deepcopy(document)
    missing.pop("integrity")
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: verify_envelope_integrity(missing))

    nonstring = copy.deepcopy(document)
    nonstring["integrity"]["payload_sha256"] = 1
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: verify_envelope_integrity(nonstring))


@pytest.mark.parametrize("prefix", ["Bad_", "missing", "1bad_"])
def test_stable_id_rejects_noncanonical_prefixes(prefix: str) -> None:
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: stable_id(prefix, {"value": 1}))


@pytest.mark.parametrize("digest_bits", [120, 129, 264])
def test_stable_id_rejects_weak_unaligned_or_oversized_digests(digest_bits: int) -> None:
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: stable_id("obj_", {"value": 1}, digest_bits=digest_bits),
    )


def test_hash_derived_id_rejects_hash_prefix_and_digest_parameters() -> None:
    valid_hash = "sha256:" + "a" * 64
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: stable_id_from_sha256("obj_", "bad"))
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: stable_id_from_sha256("Bad_", valid_hash))
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: stable_id_from_sha256("obj_", valid_hash, digest_bits=127),
    )


@pytest.mark.parametrize(
    ("timestamp_ms", "random_bits"),
    [
        (-1, 0),
        (2**48, 0),
        (0, -1),
        (0, 2**74),
    ],
)
def test_uuidv7_run_id_rejects_out_of_range_inputs(timestamp_ms: int, random_bits: int) -> None:
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: new_run_id(timestamp_ms=timestamp_ms, random_bits=random_bits),
    )


def test_unknown_error_code_maps_to_internal_exit() -> None:
    assert exit_code_for_error("E_NOT_REGISTERED") is ExitCode.INTERNAL
