"""Fail-closed mapping from Word review offsets to LaTeX UTF-8 byte offsets.

The authoritative inputs are an immutable :class:`BookmarkBinding`, the
bookmark's reject/original text, and character offsets measured in that text.
This module deliberately does not search source text or infer offsets from a
diff.  A result is exact only when the export-time provenance proves it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import regex  # type: ignore[import-untyped]

from latex_word_review.canonical import sha256_bytes
from latex_word_review.source_units import TextProvenanceSegment, normalize_review_text

if TYPE_CHECKING:
    from latex_word_review.revisions import BookmarkBinding


class OffsetMappingDiagnosticCode(StrEnum):
    """Stable reasons why a review span cannot be patched automatically."""

    MISSING_BINDING_METADATA = "missing_binding_metadata"
    INVALID_SOURCE_LOCATION = "invalid_source_location"
    REVIEW_TEXT_NOT_NORMALIZED = "review_text_not_normalized"
    REVIEW_LENGTH_MISMATCH = "review_length_mismatch"
    REVIEW_HASH_MISMATCH = "review_hash_mismatch"
    INVALID_REVIEW_RANGE = "invalid_review_range"
    INVALID_PROVENANCE = "invalid_provenance"
    NON_PATCHABLE_PROVENANCE = "non_patchable_provenance"
    UNSAFE_UNICODE_BOUNDARY = "unsafe_unicode_boundary"


@dataclass(frozen=True, slots=True)
class OffsetMappingDiagnostic:
    """A machine-readable, approval-facing mapping failure."""

    code: OffsetMappingDiagnosticCode
    message: str
    review_start: int | None = None
    review_end: int | None = None
    segment_index: int | None = None


@dataclass(frozen=True, slots=True)
class OffsetMappingResult:
    """Either one exact absolute source byte span or one blocking diagnostic."""

    source_start_byte: int | None
    source_end_byte: int | None
    diagnostic: OffsetMappingDiagnostic | None

    def __post_init__(self) -> None:
        has_span = self.source_start_byte is not None and self.source_end_byte is not None
        if (self.source_start_byte is None) != (self.source_end_byte is None):
            raise ValueError("source byte offsets must be present together")
        if has_span == (self.diagnostic is not None):
            raise ValueError("mapping result must contain exactly one outcome")
        if (
            self.source_start_byte is not None
            and self.source_end_byte is not None
            and self.source_start_byte > self.source_end_byte
        ):
            raise ValueError("source byte span is reversed")

    @property
    def is_exact(self) -> bool:
        """Return whether the result contains an exact auto-patchable span."""

        return self.diagnostic is None


@dataclass(frozen=True, slots=True)
class _ValidatedBinding:
    source_start_byte: int
    source_end_byte: int
    segments: tuple[TextProvenanceSegment, ...]


def _failure(
    code: OffsetMappingDiagnosticCode,
    message: str,
    *,
    review_start: int | None = None,
    review_end: int | None = None,
    segment_index: int | None = None,
) -> OffsetMappingResult:
    return OffsetMappingResult(
        source_start_byte=None,
        source_end_byte=None,
        diagnostic=OffsetMappingDiagnostic(
            code=code,
            message=message,
            review_start=review_start,
            review_end=review_end,
            segment_index=segment_index,
        ),
    )


def _exact(source_start_byte: int, source_end_byte: int) -> OffsetMappingResult:
    return OffsetMappingResult(
        source_start_byte=source_start_byte,
        source_end_byte=source_end_byte,
        diagnostic=None,
    )


def _strict_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _validate_binding(
    binding: BookmarkBinding,
    review_text: str,
) -> _ValidatedBinding | OffsetMappingResult:
    if binding.review_length is None or binding.normalized_text_sha256 is None:
        return _failure(
            OffsetMappingDiagnosticCode.MISSING_BINDING_METADATA,
            "bookmark binding lacks review length or normalized-text hash",
        )

    source_start_value: object = binding.source_location.get("start_byte")
    source_end_value: object = binding.source_location.get("end_byte")
    source_start = _strict_int(source_start_value)
    source_end = _strict_int(source_end_value)
    if source_start is None or source_end is None or source_start < 0 or source_end < source_start:
        return _failure(
            OffsetMappingDiagnosticCode.INVALID_SOURCE_LOCATION,
            "bookmark binding has an invalid absolute source byte span",
        )

    if normalize_review_text(review_text) != review_text:
        return _failure(
            OffsetMappingDiagnosticCode.REVIEW_TEXT_NOT_NORMALIZED,
            "bookmark reject/original text is not in the export normalization form",
        )
    if len(review_text) != binding.review_length:
        return _failure(
            OffsetMappingDiagnosticCode.REVIEW_LENGTH_MISMATCH,
            "bookmark reject/original text length differs from the SourceMap binding",
        )
    if sha256_bytes(review_text.encode("utf-8")) != binding.normalized_text_sha256:
        return _failure(
            OffsetMappingDiagnosticCode.REVIEW_HASH_MISMATCH,
            "bookmark reject/original text hash differs from the SourceMap binding",
        )

    segments = binding.text_provenance
    source_length = source_end - source_start
    if binding.review_length == 0 and not segments:
        if source_length != 0:
            return _failure(
                OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
                "empty review text does not cover the bound source byte span",
            )
        return _ValidatedBinding(source_start, source_end, segments)
    if not segments:
        return _failure(
            OffsetMappingDiagnosticCode.MISSING_BINDING_METADATA,
            "bookmark binding lacks export-time text provenance",
        )

    review_cursor = 0
    source_cursor = 0
    for index, segment in enumerate(segments):
        if (
            segment.review_start != review_cursor
            or segment.source_start_byte != source_cursor
            or segment.review_end <= segment.review_start
            or segment.source_end_byte <= segment.source_start_byte
        ):
            return _failure(
                OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
                "text provenance is not positive, contiguous, and ordered",
                review_start=segment.review_start,
                review_end=segment.review_end,
                segment_index=index,
            )
        if segment.review_end > binding.review_length or segment.source_end_byte > source_length:
            return _failure(
                OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
                "text provenance extends beyond the bound review or source span",
                review_start=segment.review_start,
                review_end=segment.review_end,
                segment_index=index,
            )
        if segment.auto_patchable and segment.transformation != "identity":
            return _failure(
                OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
                "only identity provenance may authorize automatic patching",
                review_start=segment.review_start,
                review_end=segment.review_end,
                segment_index=index,
            )
        if segment.transformation == "identity":
            review_bytes = review_text[segment.review_start : segment.review_end].encode("utf-8")
            if len(review_bytes) != segment.source_end_byte - segment.source_start_byte:
                return _failure(
                    OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
                    "identity provenance has a UTF-8 byte-length mismatch",
                    review_start=segment.review_start,
                    review_end=segment.review_end,
                    segment_index=index,
                )
        review_cursor = segment.review_end
        source_cursor = segment.source_end_byte

    if review_cursor != binding.review_length or source_cursor != source_length:
        return _failure(
            OffsetMappingDiagnosticCode.INVALID_PROVENANCE,
            "text provenance does not cover the complete bound review and source spans",
        )
    return _ValidatedBinding(source_start, source_end, segments)


def _grapheme_boundaries(text: str) -> frozenset[int]:
    """Return every Unicode extended-grapheme boundary in ``text``.

    The maintained ``regex`` implementation's ``\\X`` follows UAX #29.  The
    coverage check is fail-closed so a future engine anomaly cannot authorize
    a byte range that was not proven to be a complete grapheme boundary.
    """

    boundaries = {0}
    cursor = 0
    for match in regex.finditer(r"\X", text, flags=regex.VERSION1):
        if match.start() != cursor or match.end() <= cursor:
            return frozenset()
        cursor = match.end()
        boundaries.add(cursor)
    if cursor != len(text):
        return frozenset()
    return frozenset(boundaries)


def _non_patchable_segment(
    segments: tuple[TextProvenanceSegment, ...],
    start: int,
    end: int,
) -> tuple[int, TextProvenanceSegment] | None:
    for index, segment in enumerate(segments):
        if segment.auto_patchable and segment.transformation == "identity":
            continue
        if start == end:
            touches_interior = segment.review_start < start < segment.review_end
        else:
            touches_interior = start < segment.review_end and end > segment.review_start
        if touches_interior:
            return index, segment
    return None


def _relative_byte_boundary(
    review_text: str,
    segments: tuple[TextProvenanceSegment, ...],
    boundary: int,
) -> int | None:
    if not segments:
        return 0 if boundary == 0 else None
    for segment in segments:
        if boundary == segment.review_start:
            return segment.source_start_byte
        if boundary == segment.review_end:
            return segment.source_end_byte
        if segment.review_start < boundary < segment.review_end:
            if not segment.auto_patchable or segment.transformation != "identity":
                return None
            prefix = review_text[segment.review_start : boundary].encode("utf-8")
            return segment.source_start_byte + len(prefix)
    return None


def map_review_span_to_source_bytes(
    binding: BookmarkBinding,
    reject_text: str,
    review_start: int,
    review_end: int,
) -> OffsetMappingResult:
    """Map a reject/original review span to exact absolute UTF-8 byte offsets.

    ``review_start`` and ``review_end`` are Python character offsets into the
    immutable bookmark reject/original text.  The returned byte offsets are
    absolute within ``binding.source_location["path"]``.  A zero-width span is
    valid for insertion.  Lossy provenance may be used only as an untouched
    boundary; a span may never enter or cross it.
    """

    validated = _validate_binding(binding, reject_text)
    if isinstance(validated, OffsetMappingResult):
        return validated
    if (
        isinstance(review_start, bool)
        or isinstance(review_end, bool)
        or not isinstance(review_start, int)
        or not isinstance(review_end, int)
        or review_start < 0
        or review_end < review_start
        or review_end > len(reject_text)
    ):
        return _failure(
            OffsetMappingDiagnosticCode.INVALID_REVIEW_RANGE,
            "review character span is reversed or outside the bookmark text",
        )

    grapheme_boundaries = _grapheme_boundaries(reject_text)
    for boundary in (review_start, review_end):
        if boundary not in grapheme_boundaries:
            return _failure(
                OffsetMappingDiagnosticCode.UNSAFE_UNICODE_BOUNDARY,
                "review boundary splits a Unicode extended grapheme cluster",
                review_start=boundary,
                review_end=boundary,
            )

    blocked = _non_patchable_segment(validated.segments, review_start, review_end)
    if blocked is not None:
        segment_index, segment = blocked
        return _failure(
            OffsetMappingDiagnosticCode.NON_PATCHABLE_PROVENANCE,
            "review span enters or crosses a lossy export-time provenance segment",
            review_start=segment.review_start,
            review_end=segment.review_end,
            segment_index=segment_index,
        )

    relative_start = _relative_byte_boundary(reject_text, validated.segments, review_start)
    relative_end = _relative_byte_boundary(reject_text, validated.segments, review_end)
    if relative_start is None or relative_end is None:
        return _failure(
            OffsetMappingDiagnosticCode.NON_PATCHABLE_PROVENANCE,
            "review boundary cannot be resolved through identity provenance",
            review_start=review_start,
            review_end=review_end,
        )
    return _exact(
        validated.source_start_byte + relative_start,
        validated.source_start_byte + relative_end,
    )


__all__ = [
    "OffsetMappingDiagnostic",
    "OffsetMappingDiagnosticCode",
    "OffsetMappingResult",
    "map_review_span_to_source_bytes",
]
