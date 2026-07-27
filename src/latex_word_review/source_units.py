"""Conservative byte-accurate LaTeX plain-text source unit discovery."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from latex_word_review.canonical import sha256_canonical
from latex_word_review.discovery import ProjectDiscovery
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import FileDigest, digest_bytes, read_stable_bytes
from latex_word_review.ids import derive_unit_id
from latex_word_review.paths import resolve_within

SOURCE_UNIT_PROFILE_NAME = "conservative-plain-text"
SOURCE_UNIT_PROFILE_VERSION = "2"
TEXT_PROVENANCE_PROFILE_NAME = "normalized-text-to-utf8"
TEXT_PROVENANCE_PROFILE_VERSION = "1"

MAX_ISLAND_SPANS: Final = 10_000
MAX_SOURCE_UNITS: Final = 10_000
MAX_SOURCE_LINES: Final = 100_000

_ENVIRONMENT_RE = re.compile(r"\\(begin|end)\s*\{([A-Za-z*]+)\}")
_UNSAFE_ASCII = frozenset("\\$%{}&#_^~")
_SAFE_CONTROL_SYMBOLS = frozenset(
    {"\\", "%", "$", "#", "_", "{", "}", "&", "~", "^", " ", ",", ";", ":", "!", "/"}
)
_UNSAFE_SEQUENCES = ("--", "``", "''")
_INLINE_TEXT_COMMANDS = frozenset(
    {
        "emph",
        "textbf",
        "textit",
        "textnormal",
        "textrm",
        "textsc",
        "textsf",
        "textsl",
        "texttt",
        "textup",
        "underline",
    }
)

_BOUNDED_COMMAND_MANDATORY_GROUPS = {
    **{name: 1 for name in _INLINE_TEXT_COMMANDS},
    "Cref": 1,
    "acrfull": 1,
    "acrlong": 1,
    "acrshort": 1,
    "autocite": 1,
    "autoref": 1,
    "caption": 1,
    "cite": 1,
    "citeauthor": 1,
    "citep": 1,
    "citet": 1,
    "citeyear": 1,
    "cref": 1,
    "eqref": 1,
    "footcite": 1,
    "footnote": 1,
    "gls": 1,
    "href": 2,
    "includegraphics": 1,
    "index": 1,
    "label": 1,
    "nocite": 1,
    "pageref": 1,
    "parencite": 1,
    "ref": 1,
    "section": 1,
    "subsection": 1,
    "subsubsection": 1,
    "supercite": 1,
    "textcite": 1,
    "textcolor": 2,
    "url": 1,
    "vref": 1,
    # Common journal metadata and review commands. Their complete argument
    # groups are skipped and never become auto-patchable source units.
    "affiliation": 1,
    "author": 1,
    "cormark": 0,
    "cortext": 1,
    "credit": 1,
    "ead": 1,
    "linenumbers": 0,
    "maketitle": 0,
    "nolinenumbers": 0,
    "printcredits": 0,
    "shortauthors": 1,
    "shorttitle": 1,
    "title": 1,
    "bibliography": 1,
    "bibliographystyle": 1,
    "figref": 1,
    "tblref": 1,
    "added": 1,
    "deleted": 1,
    "replaced": 2,
    "add": 1,
    "delete": 1,
}


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


def _utf8_character_width(character: str) -> int:
    codepoint = ord(character)
    if codepoint <= 0x7F:
        return 1
    if codepoint <= 0x7FF:
        return 2
    if codepoint <= 0xFFFF:
        return 3
    return 4


def _utf8_byte_spans(
    text: str,
    spans: tuple[tuple[int, int], ...],
) -> Iterator[tuple[int, int]]:
    """Translate ordered character spans without allocating per-character offsets."""

    char_cursor = 0
    byte_cursor = 0
    for char_start, char_end in spans:
        if not (char_cursor <= char_start <= char_end <= len(text)):
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "inline text island character spans are not ordered",
            )
        while char_cursor < char_start:
            byte_cursor += _utf8_character_width(text[char_cursor])
            char_cursor += 1
        byte_start = byte_cursor
        while char_cursor < char_end:
            byte_cursor += _utf8_character_width(text[char_cursor])
            char_cursor += 1
        yield byte_start, byte_cursor


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

    review_parts: list[str] = []
    segments: list[TextProvenanceSegment] = []
    review_cursor = 0
    source_cursor = 0
    char_start = 0
    while char_start < len(text):
        whitespace = text[char_start].isspace()
        char_end = char_start + 1
        source_start = source_cursor
        source_cursor += _utf8_character_width(text[char_start])
        while char_end < len(text) and text[char_end].isspace() == whitespace:
            source_cursor += _utf8_character_width(text[char_end])
            char_end += 1
        source_end = source_cursor
        if whitespace:
            rendered = " "
            if char_end == char_start + 1 and text[char_start] == " ":
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
        char_start = char_end

    if source_cursor != len(raw):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "text provenance UTF-8 byte accounting differs",
        )

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
        "strategy": (
            "complete-plain-paragraphs-plus-byte-exact-inline-text-islands;"
            "reject-structural-environments-and-unbounded-commands;"
            "resume-after-closed-bounded-metadata"
        ),
    }
    return {
        "name": SOURCE_UNIT_PROFILE_NAME,
        "version": SOURCE_UNIT_PROFILE_VERSION,
        "configuration_sha256": sha256_canonical(configuration),
    }


def _line_spans(data: bytes) -> Iterator[tuple[int, int, int]]:
    start = 0
    found_newline = False
    for match in re.finditer(rb"\r\n|\n|\r", data):
        found_newline = True
        yield start, match.start(), match.end()
        start = match.end()
    if start < len(data) or not found_newline:
        yield start, len(data), len(data)


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


def _positions_for_offsets(
    data: bytes,
    offsets: tuple[int, ...],
) -> dict[int, tuple[int, int]]:
    """Resolve byte offsets in one UTF-8/newline-aware pass."""

    ordered = sorted(set(offsets))
    if not ordered:
        return {}
    if ordered[0] < 0 or ordered[-1] > len(data):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "source unit byte offset is outside the source file",
        )

    positions: dict[int, tuple[int, int]] = {}
    target_index = 0
    cursor = 0
    line = 1
    column = 1

    while target_index < len(ordered) and ordered[target_index] == cursor:
        positions[cursor] = (line, column)
        target_index += 1

    while cursor < len(data):
        first_byte = data[cursor]
        if first_byte == 0x0D:
            width = 2 if cursor + 1 < len(data) and data[cursor + 1] == 0x0A else 1
            next_line = line + 1
            next_column = 1
        elif first_byte == 0x0A:
            width = 1
            next_line = line + 1
            next_column = 1
        else:
            if first_byte <= 0x7F:
                width = 1
            elif first_byte <= 0xDF:
                width = 2
            elif first_byte <= 0xEF:
                width = 3
            else:
                width = 4
            next_line = line
            next_column = column + 1

        next_cursor = cursor + width
        if target_index < len(ordered) and ordered[target_index] < next_cursor:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "source unit offset is not a UTF-8 character or newline boundary",
            )
        cursor = next_cursor
        line = next_line
        column = next_column
        while target_index < len(ordered) and ordered[target_index] == cursor:
            positions[cursor] = (line, column)
            target_index += 1

    if target_index != len(ordered):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "source unit byte offset could not be positioned",
        )
    return positions


@dataclass(slots=True)
class _FileScanState:
    inside_document: bool
    environments: list[str] = field(default_factory=list)
    math_closer: str | None = None
    brace_depth: int = 0
    bracket_depth: int = 0
    invalid: bool = False
    stopped: bool = False
    v2_unsafe: bool = False
    pending_mandatory_groups: int = 0
    pending_group_opening: str | None = None
    pending_group_depth: int = 0
    pending_group_math_closer: str | None = None
    unit_scan_unsafe: bool = False


def _state_allows_plain_text(state: _FileScanState) -> bool:
    return (
        state.inside_document
        and not state.environments
        and state.math_closer is None
        and state.brace_depth == 0
        and state.bracket_depth == 0
        and state.pending_mandatory_groups == 0
        and state.pending_group_opening is None
        and state.pending_group_depth == 0
        and state.pending_group_math_closer is None
        and not state.unit_scan_unsafe
        and not state.invalid
        and not state.stopped
    )


def _control_sequence(text: str, start: int) -> tuple[str, int]:
    cursor = start + 1
    if cursor >= len(text):
        return "", len(text)
    if text[cursor].isalpha() or text[cursor] == "@":
        name_start = cursor
        while cursor < len(text) and (text[cursor].isalpha() or text[cursor] == "@"):
            cursor += 1
        return text[name_start:cursor], cursor
    return text[cursor], cursor + 1


def _advance_pending_command_arguments(
    text: str,
    state: _FileScanState,
    cursor: int,
) -> int:
    """Consume and conservatively validate bounded command groups across lines."""

    while state.pending_mandatory_groups > 0:
        if state.pending_group_opening is not None:
            opening = state.pending_group_opening
            closing = "}" if opening == "{" else "]"
            while cursor < len(text):
                character = text[cursor]
                math_closer = state.pending_group_math_closer
                if math_closer is not None:
                    if math_closer == "$" and text.startswith("$$", cursor):
                        state.v2_unsafe = True
                        state.pending_group_math_closer = None
                        cursor += 2
                        continue
                    if text.startswith(math_closer, cursor):
                        cursor += len(math_closer)
                        state.pending_group_math_closer = None
                        continue
                    if character == "\\":
                        name, command_end = _control_sequence(text, cursor)
                        if (
                            not name
                            or name in {"begin", "end"}
                            or name not in _SAFE_CONTROL_SYMBOLS
                        ):
                            state.v2_unsafe = True
                        cursor = command_end
                        continue
                    if character == "$":
                        state.v2_unsafe = True
                        cursor += 1
                        continue
                else:
                    if character == "\\":
                        name, command_end = _control_sequence(text, cursor)
                        if name in {"(", "["}:
                            state.pending_group_math_closer = r"\)" if name == "(" else r"\]"
                        elif not name or name in {")", "]", "begin", "end"}:
                            state.v2_unsafe = True
                        elif name not in _SAFE_CONTROL_SYMBOLS:
                            # Nested control words have unknown argument semantics here.
                            state.v2_unsafe = True
                        cursor = command_end
                        continue
                    if character == "$":
                        if text.startswith("$$", cursor):
                            state.pending_group_math_closer = "$$"
                            cursor += 2
                        else:
                            state.pending_group_math_closer = "$"
                            cursor += 1
                        continue

                if character == opening:
                    state.pending_group_depth += 1
                elif character == closing:
                    state.pending_group_depth -= 1
                    cursor += 1
                    if state.pending_group_depth == 0:
                        if state.pending_group_math_closer is not None:
                            state.v2_unsafe = True
                            state.pending_group_math_closer = None
                        state.pending_group_opening = None
                        if opening == "{":
                            state.pending_mandatory_groups -= 1
                        break
                    continue
                cursor += 1
            if state.pending_group_opening is not None:
                return cursor
            continue

        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            return cursor
        if text[cursor] in "[{":
            state.pending_group_opening = text[cursor]
            state.pending_group_depth = 0
            state.pending_group_math_closer = None
            continue

        state.v2_unsafe = True
        state.unit_scan_unsafe = True

        state.pending_mandatory_groups = 0
        return cursor
    return cursor


def _apply_environment_token(
    state: _FileScanState,
    *,
    action: str,
    environment: str,
) -> None:
    if environment == "document":
        if action == "begin":
            if state.inside_document or state.environments:
                state.invalid = True
                return
            state.inside_document = True
            return
        if not state.inside_document or state.environments:
            state.invalid = True
            return
        state.inside_document = False
        return
    if not state.inside_document:
        return
    if action == "begin":
        state.environments.append(environment)
        return
    if not state.environments or state.environments[-1] != environment:
        state.invalid = True
        return
    state.environments.pop()


def _advance_file_scan_state(text: str, state: _FileScanState) -> bool:
    """Advance bounded lexical state; return whether an environment marker occurred."""

    touched_environment = False
    cursor = 0
    while cursor < len(text) and not state.invalid and not state.stopped:
        if state.pending_mandatory_groups:
            cursor = _advance_pending_command_arguments(text, state, cursor)
            if cursor >= len(text):
                break
        if state.math_closer is not None:
            if state.math_closer == "$" and text.startswith("$$", cursor):
                state.invalid = True
                break
            if text.startswith(state.math_closer, cursor):
                cursor += len(state.math_closer)
                state.math_closer = None
                continue
            if text[cursor] == "\\" and cursor + 1 < len(text):
                cursor += 2
            else:
                cursor += 1
            continue

        character = text[cursor]
        if character == "\\":
            name, command_end = _control_sequence(text, cursor)
            if not name:
                state.invalid = True
                break
            if name in {"begin", "end"}:
                environment_match = _ENVIRONMENT_RE.match(text, cursor)
                if environment_match is None:
                    state.invalid = True
                    break
                touched_environment = True
                _apply_environment_token(
                    state,
                    action=environment_match.group(1),
                    environment=environment_match.group(2),
                )
                cursor = environment_match.end()
                continue
            if name == "endinput":
                state.stopped = True
                touched_environment = True
                break
            if state.environments or not state.inside_document:
                cursor = command_end
                continue
            if name == "[":
                state.math_closer = "\\]"
            elif name == "(":
                state.math_closer = "\\)"
            elif name in {"]", ")"}:
                state.invalid = True
            else:
                expected_mandatory = _BOUNDED_COMMAND_MANDATORY_GROUPS.get(name)
                if expected_mandatory is not None:
                    cursor = command_end
                    if cursor < len(text) and text[cursor] == "*":
                        cursor += 1
                    state.pending_mandatory_groups = expected_mandatory
                    cursor = _advance_pending_command_arguments(
                        text,
                        state,
                        cursor,
                    )
                    continue
                if name not in _SAFE_CONTROL_SYMBOLS:
                    state.v2_unsafe = True
                    state.unit_scan_unsafe = True
            cursor = command_end
            continue
        if state.environments or not state.inside_document:
            cursor += 1
            continue
        if character == "$":
            if text.startswith("$$", cursor):
                state.math_closer = "$$"
                cursor += 2
            else:
                state.math_closer = "$"
                cursor += 1
            continue
        if character == "{":
            state.brace_depth += 1
        elif character == "}":
            if state.brace_depth == 0:
                state.invalid = True
            else:
                state.brace_depth -= 1
        elif character == "[":
            state.bracket_depth += 1
        elif character == "]" and state.bracket_depth:
            state.bracket_depth -= 1
        cursor += 1
    return touched_environment


def _plain_line_eligibility(
    data: bytes,
    *,
    require_document_environment: bool,
) -> tuple[tuple[bool, ...], bool]:
    state = _FileScanState(inside_document=not require_document_environment)
    eligibility: list[bool] = []
    for start, content_end, _line_end in _line_spans(data):
        raw_line = data[start:content_end]
        comment_at = _comment_at(raw_line)
        effective_line = raw_line if comment_at is None else raw_line[:comment_at]
        start_allows_plain = _state_allows_plain_text(state)
        touched_environment = _advance_file_scan_state(
            effective_line.decode("utf-8"),
            state,
        )
        end_allows_plain = _state_allows_plain_text(state)
        line_v2_unsafe = state.v2_unsafe
        eligibility.append(
            start_allows_plain
            and end_allows_plain
            and not touched_environment
            and not line_v2_unsafe
        )
        synchronized = (
            state.pending_mandatory_groups == 0
            and state.pending_group_opening is None
            and state.pending_group_depth == 0
            and state.pending_group_math_closer is None
            and state.math_closer is None
            and not state.environments
            and state.brace_depth == 0
            and state.bracket_depth == 0
            and not state.unit_scan_unsafe
            and not state.invalid
            and not state.stopped
        )
        if line_v2_unsafe and synchronized:
            # An opaque nested construct invalidates its own line only. Once
            # every bounded group is closed, later lines can be proven again.
            state.v2_unsafe = False
        if len(eligibility) > MAX_SOURCE_LINES:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "TeX source exceeds the source line scan limit",
            )
    expected_inside_document = not require_document_environment
    document_state_is_closed = state.inside_document == expected_inside_document
    if require_document_environment and state.stopped:
        document_state_is_closed = False
    v2_allowed = (
        not state.invalid
        and not state.v2_unsafe
        and state.pending_mandatory_groups == 0
        and state.pending_group_opening is None
        and state.pending_group_depth == 0
        and state.pending_group_math_closer is None
        and not state.unit_scan_unsafe
        and state.math_closer is None
        and not state.environments
        and state.brace_depth == 0
        and state.bracket_depth == 0
        and document_state_is_closed
    )
    return tuple(eligibility), v2_allowed


def _plain_candidate(text: str) -> bool:
    if not text or not any(character.isalpha() for character in text):
        return False
    if any(character in _UNSAFE_ASCII for character in text):
        return False
    return not any(sequence in text for sequence in _UNSAFE_SEQUENCES)


def _trimmed_char_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end or not _plain_candidate(text[start:end]):
        return None
    return start, end


def _balanced_group_end(text: str, start: int) -> int | None:
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    cursor = start
    while cursor < len(text):
        character = text[cursor]
        if character == "\\" and cursor + 1 < len(text):
            escaped = text[cursor + 1]
            if escaped in {opening, closing}:
                cursor += 2
                continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return None


def _command_extent(
    text: str,
    start: int,
) -> tuple[str, int, tuple[tuple[str, int, int], ...], bool]:
    cursor = start + 1
    if cursor >= len(text):
        return "", len(text), (), False
    if text[cursor].isalpha() or text[cursor] == "@":
        name_start = cursor
        while cursor < len(text) and (text[cursor].isalpha() or text[cursor] == "@"):
            cursor += 1
        name = text[name_start:cursor]
    else:
        name = text[cursor]
        cursor += 1
        # TeX control symbols consume exactly one character.  They cannot
        # own a following bracket/group, so return before optional-argument
        # parsing (for example ``\% [\ref{...}]``).
        return name, cursor, (), False
    if cursor < len(text) and text[cursor] == "*":
        cursor += 1
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1

    groups: list[tuple[str, int, int]] = []
    while cursor < len(text) and text[cursor] in "[{":
        opening = text[cursor]
        group_end = _balanced_group_end(text, cursor)
        if group_end is None:
            return name, len(text), tuple(groups), False
        groups.append((opening, cursor + 1, group_end - 1))
        cursor = group_end
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    return name, cursor, tuple(groups), bool(groups)


def _math_extent(text: str, start: int) -> int | None:
    delimiter = "$$" if text.startswith("$$", start) else "$"
    cursor = start + len(delimiter)
    while cursor < len(text):
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text.startswith(delimiter, cursor):
            return cursor + len(delimiter)
        cursor += 1
    return None


def _plain_text_island_char_spans(text: str) -> tuple[tuple[int, int], ...] | None:
    spans: list[tuple[int, int]] = []
    cursor = 0
    plain_start = 0

    def append_plain(end: int) -> bool:
        span = _trimmed_char_span(text, plain_start, end)
        if span is not None:
            if len(spans) >= MAX_ISLAND_SPANS:
                return False
            spans.append(span)
        return True

    while cursor < len(text):
        character = text[cursor]
        if character == "\\":
            if not append_plain(cursor):
                return None
            name, command_end, groups, bounded = _command_extent(text, cursor)
            mandatory = [group for group in groups if group[0] == "{"]
            expected_mandatory = _BOUNDED_COMMAND_MANDATORY_GROUPS.get(name)
            if expected_mandatory is None:
                if name not in _SAFE_CONTROL_SYMBOLS or groups:
                    return None
            elif len(mandatory) != expected_mandatory or (expected_mandatory > 0 and not bounded):
                return None
            if name in _INLINE_TEXT_COMMANDS:
                inner = _trimmed_char_span(text, mandatory[0][1], mandatory[0][2])
                if inner is not None:
                    if len(spans) >= MAX_ISLAND_SPANS:
                        return None
                    spans.append(inner)
            cursor = command_end
            plain_start = cursor
            continue
        if character == "$":
            if not append_plain(cursor):
                return None
            math_end = _math_extent(text, cursor)
            if math_end is None:
                return None
            cursor = math_end
            plain_start = cursor
            continue
        if character in "{}&#_^~":
            if not append_plain(cursor):
                return None
            if character == "{":
                group_end = _balanced_group_end(text, cursor)
                if group_end is None:
                    return None
                cursor = group_end
            else:
                cursor += 1
            plain_start = cursor
            continue
        if any(text.startswith(sequence, cursor) for sequence in _UNSAFE_SEQUENCES):
            if not append_plain(cursor):
                return None
            cursor += 2
            plain_start = cursor
            continue
        cursor += 1
    if not append_plain(len(text)):
        return None
    return tuple(spans)


def _scan_inline_island_spans(
    data: bytes,
    *,
    eligibility: tuple[bool, ...],
    v2_allowed: bool,
) -> tuple[tuple[int, int], ...]:
    if not v2_allowed:
        return ()
    spans: list[tuple[int, int]] = []
    for line_index, (start, content_end, _line_end) in enumerate(_line_spans(data)):
        if not eligibility[line_index]:
            continue
        raw_line = data[start:content_end]
        if _comment_at(raw_line) is not None:
            continue
        if not raw_line.strip():
            continue
        line_text = raw_line.decode("utf-8")
        line_spans = _plain_text_island_char_spans(line_text)
        if line_spans is None or len(spans) + len(line_spans) > MAX_ISLAND_SPANS:
            return ()
        for byte_start, byte_end in _utf8_byte_spans(line_text, line_spans):
            spans.append((start + byte_start, start + byte_end))
    return tuple(spans)


def _source_unit_from_span(
    data: bytes,
    discovery: ProjectDiscovery,
    positions: dict[int, tuple[int, int]],
    *,
    path: str,
    newline: str,
    start: int,
    end: int,
) -> SourceUnit | None:
    raw = data[start:end]
    normalized, provenance = build_text_provenance(raw)
    if not _plain_candidate(normalized):
        return None
    slice_digest = digest_bytes(raw).sha256
    normalized_digest = digest_bytes(normalized.encode("utf-8")).sha256
    start_line, start_column = positions[start]
    end_line, end_column = positions[end]
    return SourceUnit(
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

    line_eligibility, v2_allowed = _plain_line_eligibility(
        data,
        require_document_environment=require_document_environment,
    )
    candidate_spans: list[tuple[int, int]] = []
    block: list[tuple[int, int]] = []
    block_invalid = False

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
        if len(candidate_spans) >= MAX_SOURCE_UNITS:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "TeX source exceeds the source unit limit",
            )
        candidate_spans.append((start, end))

    for line_index, (start, content_end, _line_end) in enumerate(_line_spans(data)):
        raw_line = data[start:content_end]
        comment_at = _comment_at(raw_line)
        if comment_at is not None:
            block_invalid = True
            continue
        stripped = raw_line.strip()
        if not stripped:
            flush()
            continue
        if not line_eligibility[line_index]:
            if block:
                block_invalid = True
            flush()
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
    occupied = sorted(candidate_spans)
    occupied_index = 0
    for island_start, island_end in _scan_inline_island_spans(
        data,
        eligibility=line_eligibility,
        v2_allowed=v2_allowed,
    ):
        while occupied_index < len(occupied) and occupied[occupied_index][1] <= island_start:
            occupied_index += 1
        if occupied_index < len(occupied) and island_end > occupied[occupied_index][0]:
            continue
        if len(candidate_spans) >= MAX_SOURCE_UNITS:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "TeX source exceeds the source unit limit",
            )
        candidate_spans.append((island_start, island_end))
    positions = _positions_for_offsets(
        data,
        tuple(offset for span in candidate_spans for offset in span),
    )
    units: list[SourceUnit] = []
    for unit_start, unit_end in candidate_spans:
        unit = _source_unit_from_span(
            data,
            discovery,
            positions,
            path=path,
            newline=newline,
            start=unit_start,
            end=unit_end,
        )
        if unit is not None:
            units.append(unit)
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
        file_units = _scan_file(
            root,
            discovery,
            path=source_file.path,
            newline=source_file.newline or "none",
            require_document_environment=source_file.path == discovery.main_document,
        )
        if len(units) + len(file_units) > MAX_SOURCE_UNITS:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "project exceeds the source unit limit",
            )
        units.extend(file_units)
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
