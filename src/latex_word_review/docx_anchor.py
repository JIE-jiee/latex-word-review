"""Fail-closed placement of exact Word bookmarks for LaTeX source units.

The exporter may merge adjacent LaTeX paragraphs into one Word paragraph.  A
whole-paragraph equality check therefore loses otherwise exact source units.
This module permits a narrower match only when the source text has one global
normalized occurrence and that occurrence can be traced back to a contiguous,
plain Word run range without guessing.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

import regex  # type: ignore[import-untyped]
from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.source_units import SourceUnit, normalize_review_text

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
XML_NS = "http://www.w3.org/XML/1998/namespace"

_P = f"{{{W_NS}}}p"
_R = f"{{{W_NS}}}r"
_RPR = f"{{{W_NS}}}rPr"
_T = f"{{{W_NS}}}t"
_SPACE_ELEMENTS = frozenset({f"{{{W_NS}}}tab", f"{{{W_NS}}}br", f"{{{W_NS}}}cr"})
_SAFE_RUN_CHILDREN = frozenset({_RPR, _T, *_SPACE_ELEMENTS})


class AnchorReason(StrEnum):
    """Stable internal reason for an unplaced source-unit bookmark."""

    EXACT = "exact"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNSAFE_BOUNDARY = "unsafe_boundary"
    UNSAFE_STRUCTURE = "unsafe_structure"
    NON_NORMALIZED_RANGE = "non_normalized_range"
    OVERLAPPING_SOURCE_UNITS = "overlapping_source_units"


@dataclass(frozen=True, slots=True)
class AnchorPlacement:
    """Result for one source unit, in the same order supplied by the caller."""

    unit_id: str
    status: Literal["exact", "unmapped", "conflict"]
    bookmark_name: str | None
    bookmark_id: str | None
    paragraph_id: str | None
    reason: AnchorReason


@dataclass(frozen=True, slots=True)
class _RawCharacter:
    value: str
    run: etree._Element | None
    text_element: etree._Element | None
    text_offset: int | None
    safe: bool


@dataclass(frozen=True, slots=True)
class _NormalizedCharacter:
    value: str
    raw_start: int
    raw_end: int


@dataclass(frozen=True, slots=True)
class _ParagraphProjection:
    paragraph: etree._Element
    raw: tuple[_RawCharacter, ...]
    normalized: tuple[_NormalizedCharacter, ...]

    @property
    def text(self) -> str:
        return "".join(character.value for character in self.normalized)


@dataclass(frozen=True, slots=True)
class _RangeCandidate:
    paragraph: etree._Element
    paragraph_ordinal: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _BoundaryPoint:
    text_element: etree._Element
    text_offset: int


def _bookmark_name(unit_id: str) -> str:
    suffix = unit_id.removeprefix("unit_")
    return f"lwr_{suffix[:32]}"


def _safe_run(run: etree._Element, paragraph: etree._Element) -> bool:
    if run.tag != _R or run.getparent() is not paragraph:
        return False
    children = tuple(run)
    if any(child.tag not in _SAFE_RUN_CHILDREN for child in children):
        return False
    properties = [index for index, child in enumerate(children) if child.tag == _RPR]
    return len(properties) <= 1 and (not properties or properties == [0])


def _raw_characters(paragraph: etree._Element) -> tuple[_RawCharacter, ...]:
    characters: list[_RawCharacter] = []
    for element in paragraph.iter():
        if element.tag == _T:
            text = element.text or ""
            parent = element.getparent()
            run = parent if parent is not None and parent.tag == _R else None
            safe = run is not None and _safe_run(run, paragraph)
            for offset, value in enumerate(text):
                characters.append(_RawCharacter(value, run, element, offset, safe))
        elif element.tag in _SPACE_ELEMENTS:
            parent = element.getparent()
            run = parent if parent is not None and parent.tag == _R else None
            safe = run is not None and _safe_run(run, paragraph)
            characters.append(_RawCharacter(" ", run, None, None, safe))
    return tuple(characters)


def _normalized_characters(
    raw: tuple[_RawCharacter, ...],
) -> tuple[_NormalizedCharacter, ...]:
    normalized: list[_NormalizedCharacter] = []
    cursor = 0
    while cursor < len(raw):
        if raw[cursor].value.isspace():
            end = cursor + 1
            while end < len(raw) and raw[end].value.isspace():
                end += 1
            if normalized and end < len(raw):
                normalized.append(_NormalizedCharacter(" ", cursor, end))
            cursor = end
            continue
        normalized.append(_NormalizedCharacter(raw[cursor].value, cursor, cursor + 1))
        cursor += 1
    return tuple(normalized)


def _project_paragraph(paragraph: etree._Element) -> _ParagraphProjection:
    raw = _raw_characters(paragraph)
    normalized = _normalized_characters(raw)
    projection = _ParagraphProjection(paragraph, raw, normalized)
    visible = "".join(character.value for character in raw)
    if projection.text != normalize_review_text(visible):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "Word paragraph projection normalization differs",
        )
    return projection


def _occurrences(text: str, needle: str) -> tuple[int, ...]:
    starts: list[int] = []
    cursor = 0
    while cursor <= len(text) - len(needle):
        start = text.find(needle, cursor)
        if start < 0:
            break
        starts.append(start)
        cursor = start + 1
    return tuple(starts)


def _grapheme_boundaries(text: str) -> frozenset[int]:
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


def _word_character(value: str) -> bool:
    return value.isalnum() or value == "_"


def _safe_semantic_boundaries(text: str, start: int, end: int) -> bool:
    grapheme_boundaries = _grapheme_boundaries(text)
    if start not in grapheme_boundaries or end not in grapheme_boundaries:
        return False
    if start and _word_character(text[start - 1]) and _word_character(text[start]):
        return False
    return not (end < len(text) and _word_character(text[end - 1]) and _word_character(text[end]))


def _range_reason(
    projection: _ParagraphProjection,
    *,
    source_text: str,
    start: int,
    end: int,
) -> AnchorReason:
    if not _safe_semantic_boundaries(projection.text, start, end):
        return AnchorReason.UNSAFE_BOUNDARY
    start_character = projection.normalized[start]
    end_character = projection.normalized[end - 1]
    raw_start = start_character.raw_start
    raw_end = end_character.raw_end
    raw_range = projection.raw[raw_start:raw_end]
    if "".join(character.value for character in raw_range) != source_text:
        return AnchorReason.NON_NORMALIZED_RANGE
    if not raw_range or any(not character.safe for character in raw_range):
        return AnchorReason.UNSAFE_STRUCTURE
    first = raw_range[0]
    last = raw_range[-1]
    if (
        first.run is None
        or first.text_element is None
        or first.text_offset is None
        or last.run is None
        or last.text_element is None
        or last.text_offset is None
    ):
        return AnchorReason.UNSAFE_STRUCTURE
    start_child = projection.paragraph.index(first.run)
    end_child = projection.paragraph.index(last.run)
    for child in projection.paragraph[start_child : end_child + 1]:
        if not _safe_run(child, projection.paragraph):
            return AnchorReason.UNSAFE_STRUCTURE
    return AnchorReason.EXACT


def _range_points(
    projection: _ParagraphProjection,
    *,
    start: int,
    end: int,
) -> tuple[_BoundaryPoint, _BoundaryPoint]:
    first = projection.raw[projection.normalized[start].raw_start]
    last = projection.raw[projection.normalized[end - 1].raw_end - 1]
    if (
        first.text_element is None
        or first.text_offset is None
        or last.text_element is None
        or last.text_offset is None
    ):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "anchor endpoint is not Word text")
    return (
        _BoundaryPoint(first.text_element, first.text_offset),
        _BoundaryPoint(last.text_element, last.text_offset + 1),
    )


def _set_text(element: etree._Element, value: str) -> None:
    element.text = value
    if value and (value[0].isspace() or value[-1].isspace()):
        element.set(f"{{{XML_NS}}}space", "preserve")


def _run_has_content(run: etree._Element) -> bool:
    return any(child.tag != _RPR for child in run)


def _split_run_at(paragraph: etree._Element, point: _BoundaryPoint) -> int:
    text_element = point.text_element
    run = text_element.getparent()
    if run is None or not _safe_run(run, paragraph) or text_element.getparent() is not run:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "anchor boundary left its safe Word run")
    value = text_element.text or ""
    if point.text_offset < 0 or point.text_offset > len(value):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "anchor boundary is outside Word text")
    target_index = run.index(text_element)
    run_index = paragraph.index(run)
    prefix = deepcopy(run)
    suffix = deepcopy(run)

    prefix_target = prefix[target_index]
    for child in tuple(prefix)[target_index + 1 :]:
        prefix.remove(child)
    prefix_value = value[: point.text_offset]
    if prefix_value:
        _set_text(prefix_target, prefix_value)
    else:
        prefix.remove(prefix_target)

    suffix_target = suffix[target_index]
    for child in tuple(suffix)[:target_index]:
        if child.tag != _RPR:
            suffix.remove(child)
    suffix_value = value[point.text_offset :]
    if suffix_value:
        _set_text(suffix_target, suffix_value)
    else:
        suffix.remove(suffix_target)

    paragraph.remove(run)
    prefix_present = _run_has_content(prefix)
    if prefix_present:
        paragraph.insert(run_index, prefix)
    suffix_present = _run_has_content(suffix)
    if suffix_present:
        paragraph.insert(run_index + int(prefix_present), suffix)
    if not prefix_present and not suffix_present:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "Word run split removed all content")
    return int(run_index) + int(prefix_present)


def _bookmark_element(
    local_name: str, *, numeric_id: int, name: str | None = None
) -> etree._Element:
    element = etree.Element(f"{{{W_NS}}}{local_name}")
    element.set(f"{{{W_NS}}}id", str(numeric_id))
    if name is not None:
        element.set(f"{{{W_NS}}}name", name)
    return element


def _insert_range_bookmark(
    paragraph: etree._Element,
    *,
    source_text: str,
    start: int,
    end: int,
    name: str,
    numeric_id: int,
) -> None:
    initial = _project_paragraph(paragraph)
    if (
        _range_reason(initial, source_text=source_text, start=start, end=end)
        is not AnchorReason.EXACT
    ):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "anchor range changed before insertion")
    start_point, _ = _range_points(initial, start=start, end=end)
    start_index = _split_run_at(paragraph, start_point)

    after_start = _project_paragraph(paragraph)
    _, end_point = _range_points(after_start, start=start, end=end)
    end_index = _split_run_at(paragraph, end_point)
    if end_index <= start_index:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "anchor range became empty")
    for child in paragraph[start_index:end_index]:
        if not _safe_run(child, paragraph):
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "anchor crossed unsafe Word content")

    paragraph.insert(
        end_index,
        _bookmark_element("bookmarkEnd", numeric_id=numeric_id),
    )
    paragraph.insert(
        start_index,
        _bookmark_element("bookmarkStart", numeric_id=numeric_id, name=name),
    )

    inside = False
    fragments: list[str] = []
    for child in paragraph:
        if child.tag == f"{{{W_NS}}}bookmarkStart" and child.get(f"{{{W_NS}}}name") == name:
            inside = True
            continue
        if child.tag == f"{{{W_NS}}}bookmarkEnd" and child.get(f"{{{W_NS}}}id") == str(numeric_id):
            break
        if inside:
            fragments.extend(
                (element.text or "") if element.tag == _T else " "
                for element in child.iter()
                if element.tag == _T or element.tag in _SPACE_ELEMENTS
            )
    if not inside or "".join(fragments) != source_text:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "inserted bookmark text differs")


def insert_unique_source_bookmarks(
    root: etree._Element,
    units: tuple[SourceUnit, ...],
    *,
    first_numeric_id: int,
) -> tuple[AnchorPlacement, ...]:
    """Insert bookmarks for globally unique, provably editable source ranges.

    Exact whole-paragraph matches and exact substrings share the same gate.  A
    substring is accepted only when its normalized occurrence is globally
    unique, its selected Word text is already in the sealed normalization form,
    and its endpoints plus all intervening content are plain direct ``w:r``
    children.  Ambiguous or unsafe cases remain unanchored.
    """

    if first_numeric_id < 0:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "bookmark numeric id is invalid")
    paragraphs = tuple(root.iter(_P))
    projections = tuple(_project_paragraph(paragraph) for paragraph in paragraphs)
    source_counts = Counter(unit.normalized_text for unit in units)
    candidates: dict[str, _RangeCandidate] = {}
    reasons: dict[str, AnchorReason] = {}

    for unit in units:
        if source_counts[unit.normalized_text] > 1:
            reasons[unit.unit_id] = AnchorReason.AMBIGUOUS
            continue
        hits: list[_RangeCandidate] = []
        for ordinal, projection in enumerate(projections):
            for start in _occurrences(projection.text, unit.normalized_text):
                hits.append(
                    _RangeCandidate(
                        paragraph=projection.paragraph,
                        paragraph_ordinal=ordinal,
                        start=start,
                        end=start + len(unit.normalized_text),
                    )
                )
        if not hits:
            reasons[unit.unit_id] = AnchorReason.NOT_FOUND
            continue
        if len(hits) != 1:
            reasons[unit.unit_id] = AnchorReason.AMBIGUOUS
            continue
        candidate = hits[0]
        reason = _range_reason(
            projections[candidate.paragraph_ordinal],
            source_text=unit.normalized_text,
            start=candidate.start,
            end=candidate.end,
        )
        if reason is AnchorReason.EXACT:
            candidates[unit.unit_id] = candidate
        else:
            reasons[unit.unit_id] = reason

    overlapping: set[str] = set()
    candidate_items = tuple(candidates.items())
    for index, (unit_id, candidate) in enumerate(candidate_items):
        for other_id, other in candidate_items[index + 1 :]:
            if candidate.paragraph is not other.paragraph:
                continue
            if candidate.start < other.end and other.start < candidate.end:
                overlapping.update((unit_id, other_id))
    for unit_id in overlapping:
        candidates.pop(unit_id, None)
        reasons[unit_id] = AnchorReason.OVERLAPPING_SOURCE_UNITS

    placements: list[AnchorPlacement] = []
    next_id = first_numeric_id
    for unit in units:
        selected = candidates.get(unit.unit_id)
        if selected is None:
            reason = reasons[unit.unit_id]
            conflict = reason in {
                AnchorReason.AMBIGUOUS,
                AnchorReason.OVERLAPPING_SOURCE_UNITS,
            }
            placements.append(
                AnchorPlacement(
                    unit_id=unit.unit_id,
                    status="conflict" if conflict else "unmapped",
                    bookmark_name=None,
                    bookmark_id=None,
                    paragraph_id=None,
                    reason=reason,
                )
            )
            continue
        name = _bookmark_name(unit.unit_id)
        _insert_range_bookmark(
            selected.paragraph,
            source_text=unit.normalized_text,
            start=selected.start,
            end=selected.end,
            name=name,
            numeric_id=next_id,
        )
        placements.append(
            AnchorPlacement(
                unit_id=unit.unit_id,
                status="exact",
                bookmark_name=name,
                bookmark_id=str(next_id),
                paragraph_id=selected.paragraph.get(f"{{{W14_NS}}}paraId"),
                reason=AnchorReason.EXACT,
            )
        )
        next_id += 1
    return tuple(placements)


__all__ = [
    "AnchorPlacement",
    "AnchorReason",
    "insert_unique_source_bookmarks",
]
