"""Conservative byte-accurate LaTeX plain-text source unit discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from latex_word_review.canonical import sha256_canonical
from latex_word_review.discovery import ProjectDiscovery
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import FileDigest, digest_bytes, read_stable_bytes
from latex_word_review.ids import derive_unit_id
from latex_word_review.paths import resolve_within

SOURCE_UNIT_PROFILE_NAME = "conservative-plain-text"
SOURCE_UNIT_PROFILE_VERSION = "1"
TEXT_PROVENANCE_PROFILE_NAME = "normalized-text-to-utf8"
TEXT_PROVENANCE_PROFILE_VERSION = "1"

_ENVIRONMENT_RE = re.compile(r"\\(begin|end)\s*\{([A-Za-z*]+)\}")
_UNSAFE_ASCII = frozenset("\\$%{}&#_^~")
_UNSAFE_SEQUENCES = ("--", "``", "''")


@dataclass(frozen=True, slots=True)
class TextProvenanceSegment:
    """One reversible or explicitly lossy normalized-text/source span."""

    review_start: int
    review_end: int
    source_start_byte: int
    source_end_byte: int
    transformation: str
    auto_patchable: bool

    def as_contract(self) -> dict[str, object]:
        return {
            "review_start": self.review_start,
            "review_end": self.review_end,
            "source_start_byte": self.source_start_byte,
            "source_end_byte": self.source_end_byte,
            "transformation": self.transformation,
            "auto_patchable": self.auto_patchable,
        }


def _utf8_boundaries(text: str) -> tuple[int, ...]:
    boundaries = [0]
    total = 0
    for character in text:
        total += len(character.encode("utf-8"))
        boundaries.append(total)
    return tuple(boundaries)


def build_text_provenance(raw: bytes) -> tuple[str, tuple[TextProvenanceSegment, ...]]:
    """Map normalized review characters to relative UTF-8 source spans.

    Runs of Unicode whitespace intentionally remain non-patchable because the
    review representation collapses them to one ASCII space.  Non-whitespace
    runs are identity segments and can be narrowed further at ingest time.
    """

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "source unit must be valid UTF-8") from exc
    boundaries = _utf8_boundaries(text)
    tokens: list[tuple[bool, int, int]] = []
    start = 0
    while start < len(text):
        whitespace = text[start].isspace()
        end = start + 1
        while end < len(text) and text[end].isspace() == whitespace:
            end += 1
        tokens.append((whitespace, start, end))
        start = end

    review_parts: list[str] = []
    segments: list[TextProvenanceSegment] = []
    review_cursor = 0
    for whitespace, char_start, char_end in tokens:
        source_start = boundaries[char_start]
        source_end = boundaries[char_end]
        if whitespace:
            rendered = " "
            if text[char_start:char_end] == " ":
                transformation = "identity"
                auto_patchable = True
            else:
                transformation = "whitespace-collapse"
                auto_patchable = False
        else:
            rendered = text[char_start:char_end]
            transformation = "identity"
            auto_patchable = True
        review_parts.append(rendered)
        next_cursor = review_cursor + len(rendered)
        segment = TextProvenanceSegment(
            review_start=review_cursor,
            review_end=next_cursor,
            source_start_byte=source_start,
            source_end_byte=source_end,
            transformation=transformation,
            auto_patchable=auto_patchable,
        )
        if (
            segments
            and segments[-1].transformation == transformation
            and segments[-1].auto_patchable is auto_patchable
            and segments[-1].review_end == segment.review_start
            and segments[-1].source_end_byte == segment.source_start_byte
        ):
            previous = segments[-1]
            segments[-1] = TextProvenanceSegment(
                review_start=previous.review_start,
                review_end=segment.review_end,
                source_start_byte=previous.source_start_byte,
                source_end_byte=segment.source_end_byte,
                transformation=transformation,
                auto_patchable=auto_patchable,
            )
        else:
            segments.append(segment)
        review_cursor = next_cursor

    normalized = "".join(review_parts).strip()
    if normalized != normalize_review_text(text):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "text provenance normalization differs")
    if normalized != "".join(review_parts):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "source unit provenance unexpectedly contains edge whitespace",
        )
    return normalized, tuple(segments)


def text_provenance_profile() -> dict[str, str]:
    configuration = {
        "name": TEXT_PROVENANCE_PROFILE_NAME,
        "version": TEXT_PROVENANCE_PROFILE_VERSION,
        "normalization": "unicode-whitespace-runs-to-ascii-space",
        "automatic_transformations": ["identity"],
    }
    return {
        "name": TEXT_PROVENANCE_PROFILE_NAME,
        "version": TEXT_PROVENANCE_PROFILE_VERSION,
        "configuration_sha256": sha256_canonical(configuration),
    }


@dataclass(frozen=True, slots=True)
class SourceUnit:
    unit_id: str
    path: str
    start_byte: int
    end_byte: int
    slice_sha256: str
    normalized_text: str
    normalized_text_sha256: str
    newline: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    ordinal: int
    neighbor_unit_ids: tuple[str, ...] = ()
    text_provenance: tuple[TextProvenanceSegment, ...] = ()

    def source_location(self) -> dict[str, object]:
        return {
            "path": self.path,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "slice_sha256": self.slice_sha256,
            "encoding": "utf-8",
            "newline": self.newline,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_column": self.start_column,
            "end_column": self.end_column,
        }

    def as_review_unit(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "kind": "paragraph",
            "ordinal": self.ordinal,
            "parent_unit_id": None,
            "source_location": self.source_location(),
            "normalized_text": self.normalized_text,
            "normalized_text_sha256": self.normalized_text_sha256,
            "semantic_role": "body",
            "risk_class": "plain_text_low",
            "neighbor_unit_ids": list(self.neighbor_unit_ids),
            "diagnostic_ids": [],
        }


def normalize_review_text(text: str) -> str:
    """Collapse Unicode whitespace only; do not perform implicit Unicode folding."""

    return " ".join(text.split())


def source_unit_profile() -> dict[str, str]:
    configuration = {
        "name": SOURCE_UNIT_PROFILE_NAME,
        "version": SOURCE_UNIT_PROFILE_VERSION,
        "strategy": "reject-any-tex-syntax-or-structural-environment",
    }
    return {
        "name": SOURCE_UNIT_PROFILE_NAME,
        "version": SOURCE_UNIT_PROFILE_VERSION,
        "configuration_sha256": sha256_canonical(configuration),
    }


def _line_spans(data: bytes) -> tuple[tuple[int, int, int], ...]:
    spans: list[tuple[int, int, int]] = []
    start = 0
    for match in re.finditer(rb"\r\n|\n|\r", data):
        spans.append((start, match.start(), match.end()))
        start = match.end()
    if start < len(data) or not spans:
        spans.append((start, len(data), len(data)))
    return tuple(spans)


def _comment_at(line: bytes) -> int | None:
    for index, value in enumerate(line):
        if value != ord("%"):
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and line[cursor] == ord("\\"):
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return index
    return None


def _position(data: bytes, offset: int) -> tuple[int, int]:
    prefix = data[:offset]
    line = prefix.count(b"\n") + 1
    final_line = prefix.rsplit(b"\n", 1)[-1].removesuffix(b"\r")
    return line, len(final_line.decode("utf-8")) + 1


def _plain_candidate(text: str) -> bool:
    if not text or not any(character.isalpha() for character in text):
        return False
    if any(character in _UNSAFE_ASCII for character in text):
        return False
    return not any(sequence in text for sequence in _UNSAFE_SEQUENCES)


def _scan_file(
    root: Path,
    discovery: ProjectDiscovery,
    *,
    path: str,
    newline: str,
    require_document_environment: bool,
) -> list[SourceUnit]:
    source_file = next(item for item in discovery.files if item.path == path)
    source_path = resolve_within(root, path)
    data = read_stable_bytes(source_path, max_bytes=max(1, source_file.size_bytes))
    if digest_bytes(data) != FileDigest(source_file.size_bytes, source_file.sha256):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source changed before unit scan")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "TeX source must be UTF-8") from exc

    units: list[SourceUnit] = []
    block: list[tuple[int, int]] = []
    block_invalid = False
    active_environments = 0
    inside_document = not require_document_environment

    def flush() -> None:
        nonlocal block, block_invalid
        if not block or block_invalid:
            block = []
            block_invalid = False
            return
        start = block[0][0]
        end = block[-1][1]
        block = []
        block_invalid = False
        raw = data[start:end]
        normalized, provenance = build_text_provenance(raw)
        if not _plain_candidate(normalized):
            return
        slice_digest = digest_bytes(raw).sha256
        normalized_digest = digest_bytes(normalized.encode("utf-8")).sha256
        start_line, start_column = _position(data, start)
        end_line, end_column = _position(data, end)
        units.append(
            SourceUnit(
                unit_id=derive_unit_id(
                    source_tree_sha256=discovery.source_tree_sha256,
                    path=path,
                    start_byte=start,
                    end_byte=end,
                    unit_kind="paragraph",
                    slice_sha256=slice_digest,
                    profile_version=SOURCE_UNIT_PROFILE_VERSION,
                ),
                path=path,
                start_byte=start,
                end_byte=end,
                slice_sha256=slice_digest,
                normalized_text=normalized,
                normalized_text_sha256=normalized_digest,
                newline=newline,
                start_line=start_line,
                end_line=end_line,
                start_column=start_column,
                end_column=end_column,
                ordinal=0,
                text_provenance=provenance,
            )
        )

    for start, content_end, _line_end in _line_spans(data):
        raw_line = data[start:content_end]
        comment_at = _comment_at(raw_line)
        if comment_at is not None:
            block_invalid = True
            continue
        stripped = raw_line.strip()
        if not stripped:
            flush()
            continue
        line_text = raw_line.decode("utf-8")
        environments = tuple(_ENVIRONMENT_RE.finditer(line_text))
        has_document_begin = any(
            match.group(1) == "begin" and match.group(2) == "document" for match in environments
        )
        has_document_end = any(
            match.group(1) == "end" and match.group(2) == "document" for match in environments
        )
        if has_document_begin:
            flush()
            inside_document = True
            continue
        if has_document_end:
            flush()
            inside_document = False
            continue
        environment_line = bool(environments)
        if environment_line:
            if block:
                block_invalid = True
            flush()
            for match in environments:
                if match.group(2) == "document":
                    continue
                if match.group(1) == "begin":
                    active_environments += 1
                else:
                    active_environments = max(0, active_environments - 1)
            continue
        if not inside_document or active_environments:
            block_invalid = True
            continue
        leading = len(raw_line) - len(raw_line.lstrip())
        trailing = len(raw_line.rstrip())
        candidate = raw_line[leading:trailing].decode("utf-8")
        if not _plain_candidate(candidate):
            if raw_line.lstrip().startswith(b"\\"):
                if block:
                    block_invalid = True
                flush()
            else:
                block_invalid = True
            continue
        block.append((start + leading, start + trailing))
    flush()
    return units


def scan_source_units(source_root: Path, discovery: ProjectDiscovery) -> tuple[SourceUnit, ...]:
    """Return only plain prose units whose exact byte spans are safe to map."""

    if discovery.external_references:
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "blocked discovery cannot produce units")
    root = source_root.resolve(strict=True)
    units: list[SourceUnit] = []
    for source_file in discovery.files:
        if source_file.role != "tex":
            continue
        units.extend(
            _scan_file(
                root,
                discovery,
                path=source_file.path,
                newline=source_file.newline or "none",
                require_document_environment=source_file.path == discovery.main_document,
            )
        )
    ordered = sorted(units, key=lambda unit: (unit.path, unit.start_byte, unit.end_byte))
    with_neighbors: list[SourceUnit] = []
    for index, unit in enumerate(ordered):
        neighbors: list[str] = []
        if index:
            neighbors.append(ordered[index - 1].unit_id)
        if index + 1 < len(ordered):
            neighbors.append(ordered[index + 1].unit_id)
        with_neighbors.append(replace(unit, ordinal=index, neighbor_unit_ids=tuple(neighbors)))
    return tuple(with_neighbors)


def review_ir_payload(
    discovery: ProjectDiscovery,
    units: tuple[SourceUnit, ...],
    *,
    source_manifest_sha256: str,
) -> dict[str, object]:
    return {
        "source_manifest_sha256": source_manifest_sha256,
        "source_tree_sha256": discovery.source_tree_sha256,
        "normalization_profile": source_unit_profile(),
        "document_language_hints": [],
        "units": [unit.as_review_unit() for unit in units],
        "relations": [],
        "diagnostics": [],
    }


__all__ = [
    "SOURCE_UNIT_PROFILE_NAME",
    "SOURCE_UNIT_PROFILE_VERSION",
    "TEXT_PROVENANCE_PROFILE_NAME",
    "TEXT_PROVENANCE_PROFILE_VERSION",
    "SourceUnit",
    "TextProvenanceSegment",
    "build_text_provenance",
    "normalize_review_text",
    "review_ir_payload",
    "scan_source_units",
    "source_unit_profile",
    "text_provenance_profile",
]
