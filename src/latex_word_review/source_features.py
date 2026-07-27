"""Bounded LaTeX feature inventory and post-export structure reconciliation.

This module deliberately does not expand arbitrary TeX.  It records a
conservative, statically provable lower bound from the already-discovered
UTF-8 source files, then compares that bound with independently observable
DOCX structure and explicit backend fallback counters.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from latex_word_review.backends.base import BackendResult
from latex_word_review.backends.tex2word import (
    SUPPORTED_TEX2WORD_VERSION,
    TEX2WORD_INTERFACE_VERSION,
)
from latex_word_review.canonical import sha256_bytes, sha256_canonical
from latex_word_review.discovery import ProjectDiscovery, SourceFile
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_models import ExportFeatureResult, ExportFinding
from latex_word_review.hashing import read_stable_bytes
from latex_word_review.inspection import DocxInspection
from latex_word_review.paths import resolve_within

INVENTORY_PROFILE_NAME: Final = "conservative-source-features"
INVENTORY_PROFILE_VERSION: Final = 1
_MAX_FEATURE_OCCURRENCES: Final = 100_000
_MAX_STATIC_KEY_LENGTH: Final = 512

_SINGLE_ROW_MATH_ENVIRONMENTS: Final = frozenset({"equation", "equation*", "displaymath"})
_MULTI_ROW_MATH_ENVIRONMENTS: Final = frozenset(
    {
        "align",
        "align*",
        "alignat",
        "alignat*",
        "eqnarray",
        "eqnarray*",
        "flalign",
        "flalign*",
        "gather",
        "gather*",
        "multline",
        "multline*",
    }
)
_MATH_ENVIRONMENTS: Final = _SINGLE_ROW_MATH_ENVIRONMENTS | _MULTI_ROW_MATH_ENVIRONMENTS
_EQUIVALENT_TABLE_ENVIRONMENTS: Final = frozenset({"tabular", "tabular*"})
_NON_EQUIVALENT_TABLE_ENVIRONMENTS: Final = frozenset(
    {"array", "longtable", "tabularx", "tabulary", "tblr"}
)
_VERBATIM_ENVIRONMENTS: Final = frozenset(
    {"Verbatim", "alltt", "lstlisting", "minted", "verbatim", "verbatim*"}
)
_FIELD_REFERENCE_COMMANDS: Final = frozenset(
    {
        "Cref",
        "Crefrange",
        "autoref",
        "cref",
        "crefrange",
        "eqref",
        "labelcref",
        "pageref",
        "ref",
        "vref",
    }
)
_NON_EQUIVALENT_REFERENCE_COMMANDS: Final = frozenset({"Nameref", "hyperref", "nameref"})
_RANGE_REFERENCE_COMMANDS: Final = frozenset({"Crefrange", "crefrange"})
_CITATION_COMMANDS: Final = frozenset(
    {
        "Cite",
        "Citeauthor",
        "Parencite",
        "Textcite",
        "autocite",
        "cite",
        "citealp",
        "citealt",
        "citeauthor",
        "citep",
        "citeyear",
        "citeyearpar",
        "citenum",
        "citenumber",
        "citet",
        "footcite",
        "parencite",
        "smartcite",
        "textcite",
    }
)
_NEW_COMMAND_DEFINITIONS: Final = frozenset(
    {
        "DeclareRobustCommand",
        "newcommand",
        "providecommand",
        "renewcommand",
    }
)
_XPARSE_COMMAND_DEFINITIONS: Final = frozenset(
    {
        "DeclareDocumentCommand",
        "NewDocumentCommand",
        "ProvideDocumentCommand",
        "RenewDocumentCommand",
    }
)
_ENVIRONMENT_DEFINITIONS: Final = frozenset({"newenvironment", "newtheorem", "renewenvironment"})
_PRIMITIVE_DEFINITIONS: Final = frozenset({"def", "edef", "gdef", "xdef"})

_GROUP_CONDITIONAL_ARGUMENTS: Final = {
    "IfFileExists": 3,
    "InputIfFileExists": 3,
    "ifthenelse": 3,
}
FeatureKind = Literal["math", "tables", "references", "labels", "images", "citations"]
RevisionFeatureView = Literal["source", "clean", "display"]
RevisionAlias = Literal["add", "delete"]
_CANONICAL_REVISION_COMMANDS: Final = frozenset({"added", "deleted", "replaced"})
_REVISION_COMMAND_ARITY: Final = {
    "added": 1,
    "deleted": 1,
    "replaced": 2,
    "add": 1,
    "delete": 1,
}


@dataclass(frozen=True, slots=True)
class FeatureSourceLocation:
    """Schema-compatible location for the first observed source construct."""

    path: str
    start_byte: int
    end_byte: int
    slice_sha256: str
    newline: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int

    def as_contract(self) -> dict[str, object]:
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


@dataclass(frozen=True, slots=True)
class SourceFeatureInventory:
    """Conservative source-side counts created before backend conversion."""

    source_tree_sha256: str
    tex_files: int
    inline_math_instances: int
    display_math_instances: int
    math_objects: int
    table_instances: int
    non_equivalent_table_instances: int
    reference_instances: int
    non_equivalent_reference_instances: int
    label_instances: int
    dynamic_label_instances: int
    citation_instances: int
    image_instances: int
    skipped_dynamic_regions: int
    static_labels: tuple[str, ...]
    reference_warning_constructs: tuple[str, ...]
    first_locations: tuple[tuple[FeatureKind, FeatureSourceLocation], ...]

    def as_metrics(self) -> dict[str, int]:
        return {
            "source_feature_inventory_version": INVENTORY_PROFILE_VERSION,
            "source_tex_files": self.tex_files,
            "inline_math_instances": self.inline_math_instances,
            "display_math_instances": self.display_math_instances,
            "math_objects": self.math_objects,
            "table_instances": self.table_instances,
            "non_equivalent_table_instances": self.non_equivalent_table_instances,
            "reference_instances": self.reference_instances,
            "non_equivalent_reference_instances": self.non_equivalent_reference_instances,
            "label_instances": self.label_instances,
            "dynamic_label_instances": self.dynamic_label_instances,
            "citation_instances": self.citation_instances,
            "image_instances": self.image_instances,
            "skipped_dynamic_regions": self.skipped_dynamic_regions,
        }

    def location_for(self, feature: FeatureKind) -> FeatureSourceLocation | None:
        return dict(self.first_locations).get(feature)


@dataclass(frozen=True, slots=True)
class FeatureReconciliation:
    feature_results: tuple[ExportFeatureResult, ...]
    findings: tuple[ExportFinding, ...]

    @property
    def failed(self) -> bool:
        return any(not finding.recoverable for finding in self.findings)


@dataclass(slots=True)
class _MutableInventory:
    inline_math_instances: int = 0
    display_math_instances: int = 0
    math_objects: int = 0
    table_instances: int = 0
    non_equivalent_table_instances: int = 0
    reference_instances: int = 0
    non_equivalent_reference_instances: int = 0
    dynamic_label_instances: int = 0
    citation_instances: int = 0
    skipped_dynamic_regions: int = 0
    static_labels: list[str] = field(default_factory=list)
    reference_warning_constructs: set[str] = field(default_factory=set)
    first_locations: dict[FeatureKind, FeatureSourceLocation] = field(default_factory=dict)

    @property
    def occurrences(self) -> int:
        return (
            self.math_objects
            + self.table_instances
            + self.non_equivalent_table_instances
            + self.reference_instances
            + self.non_equivalent_reference_instances
            + len(self.static_labels)
            + self.dynamic_label_instances
            + self.citation_instances
        )


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _skip_comment(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline + 1


def _command(text: str, index: int) -> tuple[str, int]:
    cursor = index + 1
    if cursor >= len(text):
        return "", cursor
    if text[cursor].isascii() and (text[cursor].isalpha() or text[cursor] == "@"):
        end = cursor + 1
        while end < len(text) and text[end].isascii() and (text[end].isalpha() or text[end] == "@"):
            end += 1
        return text[cursor:end], end
    return text[cursor], cursor + 1


def _skip_space_and_comments(text: str, index: int) -> int:
    cursor = index
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        if text[cursor] == "%" and not _is_escaped(text, cursor):
            cursor = _skip_comment(text, cursor)
            continue
        break
    return cursor


def _balanced(
    text: str,
    index: int,
    *,
    opening: str,
    closing: str,
) -> tuple[str, int] | None:
    if index >= len(text) or text[index] != opening:
        return None
    depth = 1
    cursor = index + 1
    while cursor < len(text):
        character = text[cursor]
        if character == "%" and not _is_escaped(text, cursor):
            cursor = _skip_comment(text, cursor)
            continue
        if character == "\\":
            cursor += 2
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return text[index + 1 : cursor], cursor + 1
        cursor += 1
    raise ContractError(
        ErrorCode.SCHEMA_INVALID,
        "recognized LaTeX feature has an unbalanced group",
        details={"opening": opening, "closing": closing, "start_character": index},
    )


def _optional_groups(text: str, index: int, *, limit: int = 2) -> int:
    cursor = _skip_space_and_comments(text, index)
    for _ in range(limit):
        if cursor >= len(text) or text[cursor] != "[":
            break
        result = _balanced(text, cursor, opening="[", closing="]")
        if result is None:
            return len(text)
        _, cursor = result
        cursor = _skip_space_and_comments(text, cursor)
    return cursor


def _mandatory_group(text: str, index: int) -> tuple[str, int] | None:
    cursor = _skip_space_and_comments(text, index)
    return _balanced(text, cursor, opening="{", closing="}")


def _definition_target(text: str, index: int) -> int | None:
    cursor = _skip_space_and_comments(text, index)
    if cursor < len(text) and text[cursor] == "{":
        target = _balanced(text, cursor, opening="{", closing="}")
        return None if target is None else target[1]
    if cursor < len(text) and text[cursor] == "\\":
        _, cursor = _command(text, cursor)
        return cursor
    return None


def _skip_definition(text: str, command: str, index: int) -> int:
    cursor = _skip_space_and_comments(text, index)
    if cursor < len(text) and text[cursor] == "*":
        cursor = _skip_space_and_comments(text, cursor + 1)
    if command in _PRIMITIVE_DEFINITIONS:
        while cursor < len(text) and text[cursor] != "{":
            if text[cursor] == "%" and not _is_escaped(text, cursor):
                cursor = _skip_comment(text, cursor)
            else:
                cursor += 1
        body = _balanced(text, cursor, opening="{", closing="}")
        return len(text) if body is None else body[1]
    if command in _NEW_COMMAND_DEFINITIONS:
        target_end = _definition_target(text, cursor)
        if target_end is None:
            return len(text)
        cursor = target_end
        cursor = _optional_groups(text, cursor, limit=2)
        body = _mandatory_group(text, cursor)
        return len(text) if body is None else body[1]
    if command in _XPARSE_COMMAND_DEFINITIONS:
        target_end = _definition_target(text, cursor)
        if target_end is None:
            return len(text)
        cursor = target_end
        for _ in range(2):
            group = _mandatory_group(text, cursor)
            if group is None:
                return len(text)
            _, cursor = group
        return cursor
    group = _mandatory_group(text, cursor)
    if group is None:
        return len(text)
    _, cursor = group
    if command in {"newenvironment", "renewenvironment"}:
        cursor = _optional_groups(text, cursor, limit=2)
        group_count = 2
    else:
        cursor = _optional_groups(text, cursor, limit=1)
        group_count = 1
    for _ in range(group_count):
        group = _mandatory_group(text, cursor)
        if group is None:
            return len(text)
        _, cursor = group
    if command == "newtheorem":
        cursor = _optional_groups(text, cursor, limit=1)
    return cursor


def _skip_conditional(text: str, index: int) -> int:
    depth = 1
    cursor = index
    while cursor < len(text):
        if text[cursor] == "%" and not _is_escaped(text, cursor):
            cursor = _skip_comment(text, cursor)
            continue
        if text[cursor] != "\\":
            cursor += 1
            continue
        command, end = _command(text, cursor)
        if command.startswith("if") and command not in {"ifthenelse", "IfFileExists"}:
            depth += 1
        elif command == "fi":
            depth -= 1
            if depth == 0:
                return end
        cursor = end
    return len(text)


def _skip_group_conditional(text: str, index: int, *, arguments: int) -> int:
    cursor = index
    for _ in range(arguments):
        group = _mandatory_group(text, cursor)
        if group is None:
            return len(text)
        _, cursor = group
    return cursor


def _group_span(
    text: str,
    index: int,
    *,
    opening: str,
    closing: str,
) -> tuple[int, int] | None:
    start = _skip_space_and_comments(text, index)
    result = _balanced(text, start, opening=opening, closing=closing)
    if result is None:
        return None
    return start, result[1]


def _revision_projection_exclusions(
    text: str,
    *,
    main_document: bool,
    revision_view: Literal["clean", "display"],
    revision_aliases: tuple[RevisionAlias, ...],
) -> tuple[tuple[int, int], ...]:
    """Locate source spans that a revision view intentionally does not render.

    The ranges stay in original character coordinates. They are used only to
    mask the feature-inventory scan; source bytes and source-map offsets never
    change.
    """

    revision_commands = _CANONICAL_REVISION_COMMANDS | frozenset(revision_aliases)
    exclusions: list[tuple[int, int]] = []
    excluded_starts: dict[int, int] = {}
    projection_calls = 0
    active = not main_document
    cursor = 0
    while cursor < len(text):
        excluded_end = excluded_starts.get(cursor)
        if excluded_end is not None:
            cursor = excluded_end
            continue
        character = text[cursor]
        if character == "%" and not _is_escaped(text, cursor):
            cursor = _skip_comment(text, cursor)
            continue
        if character != "\\":
            cursor += 1
            continue
        command_start = cursor
        command, command_end = _command(text, cursor)
        cursor = command_end
        if command == "endinput":
            break
        if (
            command
            in _NEW_COMMAND_DEFINITIONS
            | _XPARSE_COMMAND_DEFINITIONS
            | _ENVIRONMENT_DEFINITIONS
            | _PRIMITIVE_DEFINITIONS
        ):
            cursor = _skip_definition(text, command, cursor)
            continue
        if command == "verb":
            if cursor < len(text) and text[cursor] == "*":
                cursor += 1
            if cursor < len(text):
                delimiter_character = text[cursor]
                ending = text.find(delimiter_character, cursor + 1)
                cursor = len(text) if ending < 0 else ending + 1
            continue
        conditional_arguments = _GROUP_CONDITIONAL_ARGUMENTS.get(command)
        if conditional_arguments is not None:
            cursor = _skip_group_conditional(
                text,
                cursor,
                arguments=conditional_arguments,
            )
            continue
        if command.startswith("if"):
            cursor = _skip_conditional(text, cursor)
            continue
        if command in {"begin", "end"}:
            group = _mandatory_group(text, cursor)
            if group is None:
                continue
            environment, cursor = group
            environment = environment.strip()
            if command == "begin" and environment == "document" and main_document:
                active = True
                continue
            if command == "end" and environment == "document" and main_document:
                active = False
                continue
            if command == "begin" and active and environment in _VERBATIM_ENVIRONMENTS:
                terminator = f"\\end{{{environment}}}"
                ending = text.find(terminator, cursor)
                cursor = len(text) if ending < 0 else ending + len(terminator)
            continue
        if not active or command not in revision_commands:
            continue

        projection_calls += 1
        if projection_calls > _MAX_FEATURE_OCCURRENCES:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "LaTeX source exceeds the revision feature projection limit",
                details={"max_projection_calls": _MAX_FEATURE_OCCURRENCES},
            )
        parse_cursor = command_end
        optional = _group_span(text, parse_cursor, opening="[", closing="]")
        if optional is not None:
            exclusions.append(optional)
            excluded_starts[optional[0]] = optional[1]
            parse_cursor = optional[1]
        mandatory: list[tuple[int, int]] = []
        for _ in range(_REVISION_COMMAND_ARITY[command]):
            revision_group = _group_span(text, parse_cursor, opening="{", closing="}")
            if revision_group is None:
                line, column = _line_column(text, command_start)
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "revision macro cannot be projected for source feature inventory",
                    details={
                        "command": f"\\{command}",
                        "line": line,
                        "column": column,
                    },
                )
            mandatory.append(revision_group)
            parse_cursor = revision_group[1]
        if revision_view == "clean":
            hidden: tuple[int, int] | None = None
            if command in {"deleted", "delete"}:
                hidden = mandatory[0]
            elif command == "replaced":
                hidden = mandatory[1]
            if hidden is not None:
                exclusions.append(hidden)
                excluded_starts[hidden[0]] = hidden[1]

    merged: list[tuple[int, int]] = []
    for start, end in sorted(exclusions):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _mask_projection_ranges(
    text: str,
    ranges: tuple[tuple[int, int], ...],
) -> str:
    if not ranges:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in ranges:
        parts.append(text[cursor:start])
        parts.append(
            "".join(character if character in "\r\n" else " " for character in text[start:end])
        )
        cursor = end
    parts.append(text[cursor:])
    projected = "".join(parts)
    if len(projected) != len(text):
        raise AssertionError("revision feature projection changed source character offsets")
    return projected


def _static_keys(value: str) -> tuple[str, ...] | None:
    keys = tuple(item.strip() for item in value.split(",") if item.strip())
    if not keys:
        return None
    for key in keys:
        if len(key) > _MAX_STATIC_KEY_LENGTH or any(
            character.isspace() or character in "\\{}%#$" for character in key
        ):
            return None
    return keys


def _line_column(text: str, index: int) -> tuple[int, int]:
    prefix = text[:index]
    return prefix.count("\n") + 1, len(prefix.rsplit("\n", 1)[-1]) + 1


def _location(
    data: bytes,
    text: str,
    source_file: SourceFile,
    start: int,
    end: int,
) -> FeatureSourceLocation:
    start_byte = len(text[:start].encode("utf-8"))
    end_byte = len(text[:end].encode("utf-8"))
    start_line, start_column = _line_column(text, start)
    end_line, end_column = _line_column(text, end)
    return FeatureSourceLocation(
        path=source_file.path,
        start_byte=start_byte,
        end_byte=end_byte,
        slice_sha256=sha256_bytes(data[start_byte:end_byte]),
        newline=source_file.newline or "none",
        start_line=start_line,
        end_line=end_line,
        start_column=start_column,
        end_column=end_column,
    )


def _remember(
    mutable: _MutableInventory,
    feature: FeatureKind,
    data: bytes,
    text: str,
    source_file: SourceFile,
    start: int,
    end: int,
) -> None:
    if feature not in mutable.first_locations:
        mutable.first_locations[feature] = _location(data, text, source_file, start, end)
    if mutable.occurrences > _MAX_FEATURE_OCCURRENCES:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "LaTeX source exceeds the feature inventory occurrence limit",
            details={"max_feature_occurrences": _MAX_FEATURE_OCCURRENCES},
        )


def _scan_file(
    data: bytes,
    source_file: SourceFile,
    mutable: _MutableInventory,
    *,
    main_document: bool,
    revision_view: RevisionFeatureView,
    revision_aliases: tuple[RevisionAlias, ...],
) -> None:
    try:
        source_text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "included LaTeX source dependency must be valid UTF-8",
            details={"path": source_file.path},
        ) from exc
    text = source_text
    if revision_view != "source":
        text = _mask_projection_ranges(
            source_text,
            _revision_projection_exclusions(
                source_text,
                main_document=main_document,
                revision_view=revision_view,
                revision_aliases=revision_aliases,
            ),
        )
    active = not main_document
    environments: list[str] = []
    delimiter: str | None = None
    cursor = 0
    while cursor < len(text):
        character = text[cursor]
        if character == "%" and not _is_escaped(text, cursor):
            cursor = _skip_comment(text, cursor)
            continue
        if (
            character == "$"
            and not _is_escaped(text, cursor)
            and active
            and not any(environment in _MATH_ENVIRONMENTS for environment in environments)
        ):
            doubled = cursor + 1 < len(text) and text[cursor + 1] == "$"
            marker = "$$" if doubled else "$"
            end = cursor + len(marker)
            if delimiter is None:
                delimiter = marker
                if doubled:
                    mutable.display_math_instances += 1
                else:
                    mutable.inline_math_instances += 1
                mutable.math_objects += 1
                _remember(mutable, "math", data, source_text, source_file, cursor, end)
            elif delimiter == marker:
                delimiter = None
            cursor = end
            continue
        if character != "\\":
            cursor += 1
            continue
        start = cursor
        command, cursor = _command(text, cursor)
        if command == "endinput":
            break
        if (
            command
            in _NEW_COMMAND_DEFINITIONS
            | _XPARSE_COMMAND_DEFINITIONS
            | _ENVIRONMENT_DEFINITIONS
            | _PRIMITIVE_DEFINITIONS
        ):
            cursor = _skip_definition(text, command, cursor)
            continue
        if command == "verb":
            if cursor < len(text) and text[cursor] == "*":
                cursor += 1
            if cursor < len(text):
                delimiter_character = text[cursor]
                ending = text.find(delimiter_character, cursor + 1)
                cursor = len(text) if ending < 0 else ending + 1
            continue
        conditional_arguments = _GROUP_CONDITIONAL_ARGUMENTS.get(command)
        if conditional_arguments is not None:
            mutable.skipped_dynamic_regions += 1
            cursor = _skip_group_conditional(
                text,
                cursor,
                arguments=conditional_arguments,
            )
            continue
        if command.startswith("if"):
            mutable.skipped_dynamic_regions += 1
            cursor = _skip_conditional(text, cursor)
            continue
        if command == "begin" or command == "end":
            group = _mandatory_group(text, cursor)
            if group is None:
                continue
            environment, cursor = group
            environment = environment.strip()
            if command == "begin" and environment == "document" and main_document:
                active = True
                continue
            if command == "end" and environment == "document" and main_document:
                active = False
                environments.clear()
                delimiter = None
                continue
            if not active:
                continue
            if command == "begin" and environment in _VERBATIM_ENVIRONMENTS:
                terminator = f"\\end{{{environment}}}"
                ending = text.find(terminator, cursor)
                cursor = len(text) if ending < 0 else ending + len(terminator)
                continue
            if command == "begin":
                environments.append(environment)
                if environment in _MATH_ENVIRONMENTS:
                    mutable.display_math_instances += 1
                    mutable.math_objects += 1
                    _remember(mutable, "math", data, source_text, source_file, start, cursor)
                if environment in _EQUIVALENT_TABLE_ENVIRONMENTS:
                    mutable.table_instances += 1
                    _remember(mutable, "tables", data, source_text, source_file, start, cursor)
                elif environment in _NON_EQUIVALENT_TABLE_ENVIRONMENTS:
                    mutable.non_equivalent_table_instances += 1
                    _remember(mutable, "tables", data, source_text, source_file, start, cursor)
            elif environments:
                if environments[-1] == environment:
                    environments.pop()
                elif environment in environments:
                    del environments[environments.index(environment) :]
            continue
        if not active:
            continue
        if command in {"(", "["}:
            if delimiter is None:
                delimiter = command
                if command == "[":
                    mutable.display_math_instances += 1
                else:
                    mutable.inline_math_instances += 1
                mutable.math_objects += 1
                _remember(mutable, "math", data, source_text, source_file, start, cursor)
            continue
        if (
            command
            in {
                ")",
                "]",
            }
            and delimiter == ({")": "(", "]": "["}[command])
        ):
            delimiter = None
            continue
        if command == "\\" and environments and environments[-1] in _MULTI_ROW_MATH_ENVIRONMENTS:
            mutable.math_objects += 1
            _remember(mutable, "math", data, source_text, source_file, start, cursor)
            continue
        if command == "label":
            group = _mandatory_group(text, cursor)
            if group is None:
                mutable.dynamic_label_instances += 1
            else:
                value, cursor = group
                keys = _static_keys(value)
                if keys is None or len(keys) != 1:
                    mutable.dynamic_label_instances += 1
                else:
                    mutable.static_labels.append(keys[0])
            _remember(mutable, "labels", data, source_text, source_file, start, cursor)
            continue
        if command in _FIELD_REFERENCE_COMMANDS:
            count = 0
            if command in _RANGE_REFERENCE_COMMANDS:
                for _ in range(2):
                    group = _mandatory_group(text, cursor)
                    if group is None:
                        break
                    value, cursor = group
                    keys = _static_keys(value)
                    if keys is not None:
                        count += len(keys)
            else:
                group = _mandatory_group(text, _optional_groups(text, cursor))
                if group is not None:
                    value, cursor = group
                    keys = _static_keys(value)
                    if keys is not None:
                        count = len(keys)
            if count:
                mutable.reference_instances += count
                mutable.reference_warning_constructs.add(f"\\{command}")
                mutable.reference_warning_constructs.add("\\ref")
            else:
                mutable.non_equivalent_reference_instances += 1
            _remember(mutable, "references", data, source_text, source_file, start, cursor)
            continue
        if command in _NON_EQUIVALENT_REFERENCE_COMMANDS:
            mutable.non_equivalent_reference_instances += 1
            _remember(mutable, "references", data, source_text, source_file, start, cursor)
            continue
        if command in _CITATION_COMMANDS:
            group = _mandatory_group(text, _optional_groups(text, cursor))
            if group is not None:
                value, cursor = group
                keys = _static_keys(value)
                if keys is not None:
                    mutable.citation_instances += len(keys)
                    _remember(mutable, "citations", data, source_text, source_file, start, cursor)


def scan_source_features(
    source_root: Path,
    discovery: ProjectDiscovery,
    *,
    image_instances: int,
    revision_view: RevisionFeatureView = "source",
    revision_aliases: tuple[RevisionAlias, ...] = (),
) -> SourceFeatureInventory:
    """Build a bounded conservative inventory from one sealed discovery tree."""

    if image_instances < 0:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "image instance count cannot be negative")
    if revision_view not in {"source", "clean", "display"}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid revision feature projection")
    if revision_aliases not in {(), ("add",), ("delete",), ("add", "delete")}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid revision feature aliases")
    if revision_view == "source" and revision_aliases:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "source revision feature view cannot enable aliases",
        )
    if discovery.external_references:
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "blocked discovery cannot be inventoried")
    mutable = _MutableInventory()
    source_paths = {discovery.main_document}
    source_paths.update(
        edge.target for edge in discovery.dependency_edges if edge.kind in {"input", "include"}
    )
    tex_files = 0
    for source_file in sorted(discovery.files, key=lambda item: item.path):
        if source_file.role != "tex" and source_file.path not in source_paths:
            continue
        tex_files += 1
        path = resolve_within(source_root, source_file.path)
        data = read_stable_bytes(path, max_bytes=max(source_file.size_bytes, 1))
        if len(data) != source_file.size_bytes or sha256_bytes(data) != source_file.sha256:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "LaTeX source changed while building the feature inventory",
            )
        _scan_file(
            data,
            source_file,
            mutable,
            main_document=source_file.path == discovery.main_document,
            revision_view=revision_view,
            revision_aliases=revision_aliases,
        )
    return SourceFeatureInventory(
        source_tree_sha256=discovery.source_tree_sha256,
        tex_files=tex_files,
        inline_math_instances=mutable.inline_math_instances,
        display_math_instances=mutable.display_math_instances,
        math_objects=mutable.math_objects,
        table_instances=mutable.table_instances,
        non_equivalent_table_instances=mutable.non_equivalent_table_instances,
        reference_instances=mutable.reference_instances,
        non_equivalent_reference_instances=mutable.non_equivalent_reference_instances,
        label_instances=len(mutable.static_labels),
        dynamic_label_instances=mutable.dynamic_label_instances,
        citation_instances=mutable.citation_instances,
        image_instances=image_instances,
        skipped_dynamic_regions=mutable.skipped_dynamic_regions,
        static_labels=tuple(mutable.static_labels),
        reference_warning_constructs=tuple(sorted(mutable.reference_warning_constructs)),
        first_locations=tuple(sorted(mutable.first_locations.items())),
    )


def _native_counter(result: BackendResult, name: str) -> int | None:
    value = result.native_report.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _is_locked_tex2word(result: BackendResult) -> bool:
    capabilities = result.capabilities
    return (
        capabilities.backend_id == "tex2word-public-api"
        and capabilities.tool_name == "tex2word"
        and capabilities.tool_version == SUPPORTED_TEX2WORD_VERSION
        and capabilities.interface_version == TEX2WORD_INTERFACE_VERSION
    )


def _tex2word_bookmark(label: str) -> str:
    cleaned = "".join(
        character if character.isascii() and (character.isalnum() or character == "_") else "_"
        for character in label
    )
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "ref_" + cleaned
    return cleaned[:40]


def independently_observed_label_count(
    inventory: SourceFeatureInventory,
    inspection: DocxInspection,
    *,
    backend_id: str,
    tool_name: str,
    tool_version: str | None,
    interface_version: str | None,
) -> int | None:
    """Count static label bookmarks using only sealed capabilities and final DOCX names."""

    if not (
        backend_id == "tex2word-public-api"
        and tool_name == "tex2word"
        and tool_version == SUPPORTED_TEX2WORD_VERSION
        and interface_version == TEX2WORD_INTERFACE_VERSION
    ):
        return None
    expected_names = tuple(_tex2word_bookmark(label) for label in inventory.static_labels)
    name_counts = Counter(expected_names)
    unique_names = {name for name, count in name_counts.items() if count == 1}
    present_names = set(inspection.bookmark_names)
    return sum(1 for name in unique_names if name in present_names)


def _finding(
    inventory: SourceFeatureInventory,
    feature: FeatureKind,
    *,
    code: ErrorCode,
    message: str,
    recoverable: bool,
    evidence: dict[str, object],
    remediation: str,
) -> ExportFinding:
    location = inventory.location_for(feature)
    return ExportFinding(
        code=code,
        severity="warning" if recoverable else "error",
        phase="inspect",
        message=message,
        recoverable=recoverable,
        fingerprint=sha256_canonical(
            {
                "inventory_profile": INVENTORY_PROFILE_NAME,
                "inventory_version": INVENTORY_PROFILE_VERSION,
                "feature": feature,
                **evidence,
            }
        ),
        source_location=None if location is None else location.as_contract(),
        remediation=remediation,
    )


def reconcile_source_features(
    inventory: SourceFeatureInventory,
    inspection: DocxInspection,
    backend_result: BackendResult,
) -> FeatureReconciliation:
    """Compare conservative source counts with output structure and fallbacks."""

    findings: list[ExportFinding] = []
    results: list[ExportFeatureResult] = []

    image_status: Literal["preserved", "failed"] = "preserved"
    image_ids: tuple[str, ...] = ()
    if inspection.image_instances < inventory.image_instances:
        finding = _finding(
            inventory,
            "images",
            code=ErrorCode.EXPORT_SILENT_LOSS,
            message="one or more LaTeX image instances are absent from the review DOCX",
            recoverable=False,
            evidence={
                "source_image_instances": inventory.image_instances,
                "output_image_instances": inspection.image_instances,
            },
            remediation="resolve the reported image conversion before reviewer handoff",
        )
        findings.append(finding)
        image_status = "failed"
        image_ids = (finding.diagnostic_id,)
    results.append(
        ExportFeatureResult(
            "images",
            image_status,
            inventory.image_instances,
            inspection.image_instances,
            image_ids,
        )
    )

    table_status: Literal["preserved", "degraded", "failed"] = "preserved"
    table_ids: list[str] = []
    if inspection.tables < inventory.table_instances:
        finding = _finding(
            inventory,
            "tables",
            code=ErrorCode.EXPORT_SILENT_LOSS,
            message="one or more statically supported LaTeX tables are absent from the review DOCX",
            recoverable=False,
            evidence={
                "source_table_instances": inventory.table_instances,
                "output_tables": inspection.tables,
            },
            remediation="resolve table conversion before reviewer handoff",
        )
        findings.append(finding)
        table_ids.append(finding.diagnostic_id)
        table_status = "failed"
    elif inventory.non_equivalent_table_instances:
        finding = _finding(
            inventory,
            "tables",
            code=ErrorCode.EXPORT_DEGRADED,
            message=(
                "one or more LaTeX table environments have no count-equivalent Word representation"
            ),
            recoverable=True,
            evidence={
                "non_equivalent_table_instances": inventory.non_equivalent_table_instances,
            },
            remediation="inspect the affected table environments manually",
        )
        findings.append(finding)
        table_ids.append(finding.diagnostic_id)
        table_status = "degraded"
    results.append(
        ExportFeatureResult(
            "tables",
            table_status,
            inventory.table_instances,
            inspection.tables,
            tuple(table_ids),
        )
    )

    math_status: Literal["preserved", "degraded", "failed"] = "preserved"
    math_ids: list[str] = []
    math_omml = _native_counter(backend_result, "math_omml")
    math_image = _native_counter(backend_result, "math_image")
    math_raw = _native_counter(backend_result, "math_raw")
    locked_backend = _is_locked_tex2word(backend_result)
    counters_complete = inventory.math_objects == 0 or (
        not locked_backend or all(value is not None for value in (math_omml, math_image, math_raw))
    )
    if not locked_backend or inventory.math_objects == 0:
        math_omml = inspection.omml_objects
        math_image = 0
        math_raw = 0
    if not counters_complete or inspection.omml_objects < inventory.math_objects:
        finding = _finding(
            inventory,
            "math",
            code=ErrorCode.EXPORT_SILENT_LOSS,
            message="the final review DOCX lacks count-bound OMML evidence for source math objects",
            recoverable=False,
            evidence={
                "source_math_objects": inventory.math_objects,
                "output_omml_objects": inspection.omml_objects,
                "backend_math_omml": math_omml,
                "backend_math_image": math_image,
                "backend_math_raw": math_raw,
            },
            remediation=(
                "resolve conversion to native OMML or add independently bound fallback evidence"
            ),
        )
        findings.append(finding)
        math_ids.append(finding.diagnostic_id)
        math_status = "failed"
    elif math_omml is not None and inspection.omml_objects < math_omml:
        finding = _finding(
            inventory,
            "math",
            code=ErrorCode.EXPORT_SILENT_LOSS,
            message="the review DOCX contains fewer OMML objects than the backend reported",
            recoverable=False,
            evidence={
                "backend_math_omml": math_omml,
                "output_omml_objects": inspection.omml_objects,
            },
            remediation="reject the incomplete DOCX and rerun conversion",
        )
        findings.append(finding)
        math_ids.append(finding.diagnostic_id)
        math_status = "failed"
    elif (math_image or 0) + (math_raw or 0) > 0:
        finding = _finding(
            inventory,
            "math",
            code=ErrorCode.EXPORT_DEGRADED,
            message="one or more equations use an explicit non-OMML fallback in the review DOCX",
            recoverable=True,
            evidence={"math_image": math_image or 0, "math_raw": math_raw or 0},
            remediation="inspect the visible equation fallbacks before review",
        )
        findings.append(finding)
        math_ids.append(finding.diagnostic_id)
        math_status = "degraded"
    results.append(
        ExportFeatureResult(
            "math",
            math_status,
            inventory.math_objects,
            inspection.omml_objects,
            tuple(math_ids),
        )
    )

    output_references = inspection.ref_fields + inspection.pageref_fields
    reference_status: Literal["preserved", "degraded", "failed"] = "preserved"
    reference_ids: list[str] = []
    if output_references < inventory.reference_instances:
        missing_references = inventory.reference_instances - output_references
        finding = _finding(
            inventory,
            "references",
            code=ErrorCode.EXPORT_SILENT_LOSS,
            message=(
                "one or more statically recognized LaTeX references are absent from the review DOCX"
            ),
            recoverable=False,
            evidence={
                "source_reference_instances": inventory.reference_instances,
                "output_reference_fields": output_references,
                "missing_reference_instances": missing_references,
                # Warning construct names are deliberately not accepted as
                # per-instance output evidence. They do not prove that a
                # visible fallback exists in the final DOCX.
                "backend_warning_constructs_are_evidence": False,
            },
            remediation="resolve reference conversion before reviewer handoff",
        )
        findings.append(finding)
        reference_ids.append(finding.diagnostic_id)
        reference_status = "failed"
    elif inventory.non_equivalent_reference_instances:
        finding = _finding(
            inventory,
            "references",
            code=ErrorCode.EXPORT_DEGRADED,
            message=(
                "one or more LaTeX reference constructs have no count-equivalent "
                "Word field representation"
            ),
            recoverable=True,
            evidence={
                "non_equivalent_reference_instances": (
                    inventory.non_equivalent_reference_instances
                ),
            },
            remediation="inspect the affected visible reference text manually",
        )
        findings.append(finding)
        reference_ids.append(finding.diagnostic_id)
        reference_status = "degraded"
    results.append(
        ExportFeatureResult(
            "references",
            reference_status,
            inventory.reference_instances,
            output_references,
            tuple(reference_ids),
        )
    )

    label_status: Literal["preserved", "degraded", "unsupported", "failed"]
    label_ids: list[str] = []
    matched_labels: int | None
    if _is_locked_tex2word(backend_result):
        expected_names = tuple(_tex2word_bookmark(label) for label in inventory.static_labels)
        name_counts = Counter(expected_names)
        unique_names = {name for name, count in name_counts.items() if count == 1}
        present_names = set(inspection.bookmark_names)
        matched_labels = sum(1 for name in unique_names if name in present_names)
        if not unique_names.issubset(present_names):
            finding = _finding(
                inventory,
                "labels",
                code=ErrorCode.EXPORT_DEGRADED,
                message=(
                    "one or more uniquely representable LaTeX label bookmarks are "
                    "not available as native Word bookmarks"
                ),
                recoverable=True,
                evidence={
                    "expected_unique_label_bookmarks": len(unique_names),
                    "matched_unique_label_bookmarks": matched_labels,
                },
                remediation=(
                    "review unresolved Word reference placeholders manually; "
                    "the original LaTeX labels remain unchanged"
                ),
            )
            findings.append(finding)
            label_ids.append(finding.diagnostic_id)
            label_status = "degraded"
        elif len(unique_names) != len(expected_names) or inventory.dynamic_label_instances:
            finding = _finding(
                inventory,
                "labels",
                code=ErrorCode.EXPORT_DEGRADED,
                message=(
                    "one or more LaTeX labels cannot be reconciled to a unique static Word bookmark"
                ),
                recoverable=True,
                evidence={
                    "source_label_instances": inventory.label_instances,
                    "unique_expected_bookmarks": len(unique_names),
                    "dynamic_label_instances": inventory.dynamic_label_instances,
                },
                remediation="inspect colliding or dynamic labels manually",
            )
            findings.append(finding)
            label_ids.append(finding.diagnostic_id)
            label_status = "degraded"
        else:
            label_status = "preserved"
    else:
        matched_labels = None
        if inventory.label_instances or inventory.dynamic_label_instances:
            finding = _finding(
                inventory,
                "labels",
                code=ErrorCode.EXPORT_SILENT_LOSS,
                message=("the selected backend has no contract-bound static label representation"),
                recoverable=False,
                evidence={
                    "source_label_instances": inventory.label_instances,
                    "dynamic_label_instances": inventory.dynamic_label_instances,
                },
                remediation="inspect label destinations manually in the review DOCX",
            )
            findings.append(finding)
            label_ids.append(finding.diagnostic_id)
            label_status = "unsupported"
        else:
            label_status = "preserved"
    results.append(
        ExportFeatureResult(
            "labels",
            label_status,
            inventory.label_instances,
            matched_labels,
            tuple(label_ids),
        )
    )

    citation_ids: tuple[str, ...] = ()
    if inventory.citation_instances:
        finding = _finding(
            inventory,
            "citations",
            code=ErrorCode.EXPORT_DEGRADED,
            message="citation rendering is not count-equivalent to a native Word field contract",
            recoverable=True,
            evidence={"source_citation_instances": inventory.citation_instances},
            remediation="inspect rendered citations and bibliography entries manually",
        )
        findings.append(finding)
        citation_ids = (finding.diagnostic_id,)
    results.append(
        ExportFeatureResult(
            "citations",
            "unsupported" if inventory.citation_instances else "preserved",
            inventory.citation_instances,
            None,
            citation_ids,
        )
    )
    if inventory.skipped_dynamic_regions:
        dynamic_finding = ExportFinding(
            code=ErrorCode.EXPORT_DEGRADED,
            severity="warning",
            phase="inspect",
            message=(
                "one or more dynamic TeX conditional regions were excluded from the static "
                "feature inventory"
            ),
            recoverable=True,
            fingerprint=sha256_canonical(
                {
                    "inventory_profile": INVENTORY_PROFILE_NAME,
                    "inventory_version": INVENTORY_PROFILE_VERSION,
                    "feature": "dynamic_regions",
                    "skipped_dynamic_regions": inventory.skipped_dynamic_regions,
                }
            ),
            remediation="inspect the skipped dynamic conditional regions manually",
        )
        findings.append(dynamic_finding)
        results.append(
            ExportFeatureResult(
                "dynamic_regions",
                "degraded",
                inventory.skipped_dynamic_regions,
                None,
                (dynamic_finding.diagnostic_id,),
            )
        )
    return FeatureReconciliation(tuple(results), tuple(findings))


__all__ = [
    "FeatureReconciliation",
    "FeatureSourceLocation",
    "INVENTORY_PROFILE_NAME",
    "INVENTORY_PROFILE_VERSION",
    "RevisionAlias",
    "RevisionFeatureView",
    "SourceFeatureInventory",
    "independently_observed_label_count",
    "reconcile_source_features",
    "scan_source_features",
]
