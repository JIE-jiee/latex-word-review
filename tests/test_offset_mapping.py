from __future__ import annotations

from dataclasses import replace

import pytest

from latex_word_review.canonical import sha256_bytes
from latex_word_review.offset_mapping import (
    OffsetMappingDiagnosticCode,
    OffsetMappingResult,
    map_review_span_to_source_bytes,
)
from latex_word_review.revisions import BookmarkBinding
from latex_word_review.source_units import TextProvenanceSegment, build_text_provenance

_UNIT_ID = "unit_" + "a" * 26


def _binding(raw: bytes, *, source_start: int = 100) -> tuple[BookmarkBinding, str]:
    review_text, segments = build_text_provenance(raw)
    return (
        BookmarkBinding(
            unit_id=_UNIT_ID,
            source_location={
                "path": "main.tex",
                "start_byte": source_start,
                "end_byte": source_start + len(raw),
            },
            normalized_text_sha256=sha256_bytes(review_text.encode("utf-8")),
            review_length=len(review_text),
            text_provenance=segments,
        ),
        review_text,
    )


def _assert_exact(result: OffsetMappingResult, start: int, end: int) -> None:
    assert result.is_exact
    assert result.diagnostic is None
    assert (result.source_start_byte, result.source_end_byte) == (start, end)


def _assert_failure(
    result: OffsetMappingResult,
    code: OffsetMappingDiagnosticCode,
) -> None:
    assert not result.is_exact
    assert result.source_start_byte is None
    assert result.source_end_byte is None
    assert result.diagnostic is not None
    assert result.diagnostic.code is code


def test_maps_ascii_deletion_to_absolute_source_bytes() -> None:
    binding, text = _binding(b"Alpha beta gamma.")

    result = map_review_span_to_source_bytes(binding, text, 6, 10)

    _assert_exact(result, 106, 110)


def test_maps_cjk_and_emoji_by_utf8_bytes_not_character_count() -> None:
    raw = "甲乙🙂丙丁".encode()
    binding, text = _binding(raw, source_start=37)

    cjk = map_review_span_to_source_bytes(binding, text, 1, 2)
    emoji = map_review_span_to_source_bytes(binding, text, 2, 3)

    _assert_exact(cjk, 40, 43)
    _assert_exact(emoji, 43, 47)


def test_repeated_words_use_authoritative_offsets_without_searching() -> None:
    binding, text = _binding(b"same same same")

    first = map_review_span_to_source_bytes(binding, text, 0, 4)
    second = map_review_span_to_source_bytes(binding, text, 5, 9)
    third = map_review_span_to_source_bytes(binding, text, 10, 14)

    _assert_exact(first, 100, 104)
    _assert_exact(second, 105, 109)
    _assert_exact(third, 110, 114)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (0, 5, (100, 105)),
        (6, 10, (109, 113)),
        (10, 10, (113, 113)),
    ],
)
def test_multiple_independent_changes_remain_exact(
    start: int,
    end: int,
    expected: tuple[int, int],
) -> None:
    binding, text = _binding(b"alpha\r\n  beta")

    result = map_review_span_to_source_bytes(binding, text, start, end)

    _assert_exact(result, *expected)


@pytest.mark.parametrize(("start", "end"), [(5, 6), (4, 7), (0, 10)])
def test_lossy_whitespace_is_never_auto_patchable(start: int, end: int) -> None:
    binding, text = _binding(b"alpha\r\n  beta")

    result = map_review_span_to_source_bytes(binding, text, start, end)

    _assert_failure(result, OffsetMappingDiagnosticCode.NON_PATCHABLE_PROVENANCE)
    assert result.diagnostic is not None
    assert result.diagnostic.segment_index == 1


def test_both_edges_of_lossy_whitespace_are_valid_insertion_boundaries() -> None:
    binding, text = _binding(b"alpha\r\n  beta")

    before = map_review_span_to_source_bytes(binding, text, 5, 5)
    after = map_review_span_to_source_bytes(binding, text, 6, 6)

    _assert_exact(before, 105, 105)
    _assert_exact(after, 109, 109)


def test_hash_and_length_are_both_bound_to_reject_text() -> None:
    binding, text = _binding(b"original")
    wrong_length = replace(binding, review_length=len(text) + 1)
    wrong_hash = replace(binding, normalized_text_sha256=sha256_bytes(b"different"))

    _assert_failure(
        map_review_span_to_source_bytes(wrong_length, text, 0, 1),
        OffsetMappingDiagnosticCode.REVIEW_LENGTH_MISMATCH,
    )
    _assert_failure(
        map_review_span_to_source_bytes(wrong_hash, text, 0, 1),
        OffsetMappingDiagnosticCode.REVIEW_HASH_MISMATCH,
    )


def test_rejects_noncanonical_review_whitespace_even_if_binding_hash_matches() -> None:
    text = "alpha\tbeta"
    segment = TextProvenanceSegment(0, len(text), 0, len(text), "identity", True)
    binding = BookmarkBinding(
        unit_id=_UNIT_ID,
        source_location={"path": "main.tex", "start_byte": 0, "end_byte": len(text)},
        normalized_text_sha256=sha256_bytes(text.encode()),
        review_length=len(text),
        text_provenance=(segment,),
    )

    result = map_review_span_to_source_bytes(binding, text, 0, len(text))

    _assert_failure(result, OffsetMappingDiagnosticCode.REVIEW_TEXT_NOT_NORMALIZED)


@pytest.mark.parametrize(("start", "end"), [(-1, 0), (2, 1), (0, 9)])
def test_invalid_review_ranges_fail_closed(start: int, end: int) -> None:
    binding, text = _binding(b"range")

    result = map_review_span_to_source_bytes(binding, text, start, end)

    _assert_failure(result, OffsetMappingDiagnosticCode.INVALID_REVIEW_RANGE)


def test_combining_sequence_may_be_replaced_whole_but_not_split() -> None:
    binding, text = _binding("e\u0301clair".encode())

    split = map_review_span_to_source_bytes(binding, text, 0, 1)
    whole = map_review_span_to_source_bytes(binding, text, 0, 2)

    _assert_failure(split, OffsetMappingDiagnosticCode.UNSAFE_UNICODE_BOUNDARY)
    _assert_exact(whole, 100, 103)


@pytest.mark.parametrize("boundary", [1, 2])
def test_zwj_emoji_sequence_boundaries_are_unsafe(boundary: int) -> None:
    binding, text = _binding("👩\u200d💻".encode())

    result = map_review_span_to_source_bytes(binding, text, boundary, boundary)

    _assert_failure(result, OffsetMappingDiagnosticCode.UNSAFE_UNICODE_BOUNDARY)


@pytest.mark.parametrize("text", ["✈️", "👍🏽"])
def test_variation_selector_and_emoji_modifier_cannot_be_split(text: str) -> None:
    binding, review_text = _binding(text.encode())

    split = map_review_span_to_source_bytes(binding, review_text, 1, 1)
    whole = map_review_span_to_source_bytes(binding, review_text, 0, 2)

    _assert_failure(split, OffsetMappingDiagnosticCode.UNSAFE_UNICODE_BOUNDARY)
    _assert_exact(whole, 100, 100 + len(text.encode()))


def test_regional_indicators_preserve_pairs_but_allow_boundary_between_flags() -> None:
    binding, text = _binding("🇯🇵🇨🇳".encode())

    inside_first_flag = map_review_span_to_source_bytes(binding, text, 1, 1)
    between_flags = map_review_span_to_source_bytes(binding, text, 2, 2)
    inside_second_flag = map_review_span_to_source_bytes(binding, text, 3, 3)

    _assert_failure(inside_first_flag, OffsetMappingDiagnosticCode.UNSAFE_UNICODE_BOUNDARY)
    _assert_exact(between_flags, 108, 108)
    _assert_failure(inside_second_flag, OffsetMappingDiagnosticCode.UNSAFE_UNICODE_BOUNDARY)


@pytest.mark.parametrize(
    ("text", "boundary"),
    [
        ("가", 1),
        ("क्ष", 2),
        ("\U0001f3f4\U000e0067\U000e0062\U000e007f", 1),
        ("\U0001f3f4\U000e0067\U000e0062\U000e007f", 2),
    ],
)
def test_uax29_complex_script_and_emoji_tag_boundaries_are_unsafe(
    text: str,
    boundary: int,
) -> None:
    binding, review_text = _binding(text.encode())

    split = map_review_span_to_source_bytes(binding, review_text, boundary, boundary)
    whole = map_review_span_to_source_bytes(binding, review_text, 0, len(review_text))

    _assert_failure(split, OffsetMappingDiagnosticCode.UNSAFE_UNICODE_BOUNDARY)
    _assert_exact(whole, 100, 100 + len(text.encode()))


def test_missing_mapping_metadata_is_explicit() -> None:
    binding = BookmarkBinding(
        unit_id=_UNIT_ID,
        source_location={"path": "main.tex", "start_byte": 0, "end_byte": 4},
    )

    result = map_review_span_to_source_bytes(binding, "text", 0, 1)

    _assert_failure(result, OffsetMappingDiagnosticCode.MISSING_BINDING_METADATA)


def test_nonempty_binding_without_provenance_is_explicit() -> None:
    binding, text = _binding(b"text")
    missing = replace(binding, text_provenance=())

    result = map_review_span_to_source_bytes(missing, text, 0, 1)

    _assert_failure(result, OffsetMappingDiagnosticCode.MISSING_BINDING_METADATA)


def test_invalid_source_location_is_explicit() -> None:
    binding, text = _binding(b"text")
    invalid = replace(
        binding,
        source_location={"path": "main.tex", "start_byte": True, "end_byte": 4},
    )

    result = map_review_span_to_source_bytes(invalid, text, 0, 1)

    _assert_failure(result, OffsetMappingDiagnosticCode.INVALID_SOURCE_LOCATION)


def test_provenance_gap_and_identity_byte_mismatch_fail_closed() -> None:
    binding, text = _binding("甲乙".encode())
    original = binding.text_provenance[0]
    gap = replace(
        binding,
        text_provenance=(replace(original, review_start=1),),
    )
    wrong_bytes = replace(
        binding,
        text_provenance=(replace(original, source_end_byte=5),),
        source_location={"path": "main.tex", "start_byte": 100, "end_byte": 105},
    )

    _assert_failure(
        map_review_span_to_source_bytes(gap, text, 0, 1),
        OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
    )
    _assert_failure(
        map_review_span_to_source_bytes(wrong_bytes, text, 0, 1),
        OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
    )


def test_provenance_must_stay_within_and_cover_the_bound_source_span() -> None:
    binding, text = _binding(b"text")
    segment = binding.text_provenance[0]
    extends = replace(
        binding,
        text_provenance=(replace(segment, source_end_byte=5),),
    )
    incomplete = replace(
        binding,
        source_location={"path": "main.tex", "start_byte": 100, "end_byte": 105},
    )

    _assert_failure(
        map_review_span_to_source_bytes(extends, text, 0, 1),
        OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
    )
    _assert_failure(
        map_review_span_to_source_bytes(incomplete, text, 0, 1),
        OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
    )


def test_auto_patchable_nonidentity_transformation_is_rejected() -> None:
    binding, text = _binding(b"text")
    invalid = replace(
        binding,
        text_provenance=(replace(binding.text_provenance[0], transformation="magic"),),
    )

    result = map_review_span_to_source_bytes(invalid, text, 0, 1)

    _assert_failure(result, OffsetMappingDiagnosticCode.INVALID_PROVENANCE)


def test_empty_binding_supports_an_exact_zero_width_insertion() -> None:
    binding = BookmarkBinding(
        unit_id=_UNIT_ID,
        source_location={"path": "main.tex", "start_byte": 12, "end_byte": 12},
        normalized_text_sha256=sha256_bytes(b""),
        review_length=0,
        text_provenance=(),
    )

    result = map_review_span_to_source_bytes(binding, "", 0, 0)

    _assert_exact(result, 12, 12)


def test_empty_review_cannot_hide_nonempty_source_bytes() -> None:
    binding = BookmarkBinding(
        unit_id=_UNIT_ID,
        source_location={"path": "main.tex", "start_byte": 12, "end_byte": 13},
        normalized_text_sha256=sha256_bytes(b""),
        review_length=0,
        text_provenance=(),
    )

    result = map_review_span_to_source_bytes(binding, "", 0, 0)

    _assert_failure(result, OffsetMappingDiagnosticCode.INVALID_PROVENANCE)


def test_result_rejects_ambiguous_internal_state() -> None:
    with pytest.raises(ValueError, match="exactly one outcome"):
        OffsetMappingResult(source_start_byte=None, source_end_byte=None, diagnostic=None)

    with pytest.raises(ValueError, match="present together"):
        OffsetMappingResult(source_start_byte=1, source_end_byte=None, diagnostic=None)

    with pytest.raises(ValueError, match="reversed"):
        OffsetMappingResult(source_start_byte=2, source_end_byte=1, diagnostic=None)
