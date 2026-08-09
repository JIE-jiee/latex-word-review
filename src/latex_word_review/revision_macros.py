"""Bounded inventory and deterministic tex2word views for revision macros.

The scanner is deliberately not a TeX engine.  It recognizes a small,
versioned surface for ``changes``-style inline revision commands while
skipping inert or dynamically selected source regions.  The injection helper
does not touch disk; it appends definitions that the locked tex2word release collects before
expanding the complete source.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, NoReturn

from latex_word_review.canonical import sha256_bytes
from latex_word_review.discovery import ProjectDiscovery, SourceFile, discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import read_stable_bytes
from latex_word_review.paths import resolve_within

RevisionMacroMode = Literal["source", "clean", "display"]

REVISION_MACRO_PROFILE_NAME: Final = "bounded-static-revision-macros"
REVISION_MACRO_PROFILE_VERSION: Final = 3
REVISION_MACRO_PROFILE_ID: Final = (
    f"{REVISION_MACRO_PROFILE_NAME}-v{REVISION_MACRO_PROFILE_VERSION}"
)

_MAX_REVISION_MACRO_OCCURRENCES: Final = 10_000
_MAX_REVISION_MACRO_NESTING: Final = 32
_MAX_OPTION_CHARACTERS: Final = 8 * 1024
_MAX_REVISION_ARGUMENT_NESTING: Final = 32
_MAX_ALIAS_AUDIT_NESTING: Final = 32
_MAX_ALIAS_AUDIT_COMMAND_NODES: Final = 100_000
_MAX_REVISION_CONTEXT_CHARACTERS: Final = 32
_MAX_REVISION_CONTEXT_SOURCE_CHARACTERS: Final = 4 * 1024

_SIMPLE_INLINE_FORMATTING: Final = frozenset(
    {
        "emph",
        "textbf",
        "textit",
        "textmd",
        "textnormal",
        "textsc",
        "textsf",
        "textsl",
        "textsubscript",
        "textsuperscript",
        "texttt",
        "textrm",
        "textup",
        "underline",
    }
)
_LITERAL_TEXT_ESCAPES: Final = frozenset({" ", "#", "$", "%", "&", "_", "{", "}"})
_STRUCTURAL_TEXT_CHARACTERS: Final = frozenset({"#", "$", "&", "^", "_"})

_CANONICAL_ARITY: Final = {
    "added": 1,
    "deleted": 1,
    "replaced": 2,
}
_ALIAS_ARITY: Final = {
    "add": 1,
    "delete": 1,
}
_REVISION_ARITY: Final = _CANONICAL_ARITY | _ALIAS_ARITY

_VERBATIM_ENVIRONMENTS: Final = frozenset(
    {"Verbatim", "lstlisting", "minted", "verbatim", "verbatim*"}
)
_NEW_COMMAND_DEFINITIONS: Final = frozenset(
    {
        "DeclareRobustCommand",
        "newcommand",
        "newrobustcmd",
        "providecommand",
        "providerobustcmd",
        "renewcommand",
        "renewrobustcmd",
    }
)
_XPARSE_COMMAND_DEFINITIONS: Final = frozenset(
    {
        "DeclareDocumentCommand",
        "DeclareExpandableDocumentCommand",
        "NewDocumentCommand",
        "NewExpandableDocumentCommand",
        "ProvideDocumentCommand",
        "ProvideExpandableDocumentCommand",
        "RenewDocumentCommand",
        "RenewExpandableDocumentCommand",
    }
)
_XPARSE_ENVIRONMENT_DEFINITIONS: Final = frozenset(
    {
        "DeclareDocumentEnvironment",
        "NewDocumentEnvironment",
        "ProvideDocumentEnvironment",
        "RenewDocumentEnvironment",
    }
)
_ENVIRONMENT_DEFINITIONS: Final = frozenset({"newenvironment", "newtheorem", "renewenvironment"})
_PRIMITIVE_DEFINITIONS: Final = frozenset({"def", "edef", "gdef", "xdef"})
_ALIAS_ASSIGNMENT_DEFINITIONS: Final = frozenset({"futurelet", "let"})
_PACKAGE_LOAD_COMMANDS: Final = frozenset(
    {"RequirePackage", "RequirePackageWithOptions", "usepackage"}
)
_GROUP_CONDITIONAL_ARGUMENTS: Final = {
    "IfFileExists": 3,
    "InputIfFileExists": 3,
    "ifthenelse": 3,
}
_DYNAMIC_NAMED_PRIMITIVE_DEFINITIONS: Final = frozenset({"csdef", "csedef", "csgdef", "csxdef"})
_DYNAMIC_NAMED_DEFINITION_ARGUMENTS: Final = {
    "csappto": 2,
    "csdimdef": 2,
    "csdimgdef": 2,
    "cseappto": 2,
    "csepreto": 2,
    "csgappto": 2,
    "csgluedef": 2,
    "csgluegdef": 2,
    "csgpreto": 2,
    "csgundef": 1,
    "cslet": 2,
    "csletcs": 2,
    "csmudef": 2,
    "csmugdef": 2,
    "csnumdef": 2,
    "csnumgdef": 2,
    "cspreto": 2,
    "csundef": 1,
    "csxappto": 2,
    "csxpreto": 2,
}
_PATCH_DEFINITION_ARGUMENTS: Final = {
    "appto": 2,
    "apptocmd": 4,
    "eappto": 2,
    "epreto": 2,
    "gappto": 2,
    "gpreto": 2,
    "gundef": 1,
    "letcs": 2,
    "patchcmd": 5,
    "preto": 2,
    "pretocmd": 4,
    "robustify": 1,
    "undef": 1,
    "xappto": 2,
    "xapptocmd": 4,
    "xpatchcmd": 5,
    "xpreto": 2,
    "xpretocmd": 4,
}

_INJECTION_MARKER_PREFIX: Final = "% latex-word-review:revision-macros:"

_CLEAN_DEFINITIONS: Final = (
    r"\renewcommand{\added}[2][]{#2}",
    r"\renewcommand{\deleted}[2][]{}",
    r"\renewcommand{\replaced}[3][]{#2}",
    r"\renewcommand{\add}[2][]{#2}",
    r"\renewcommand{\delete}[2][]{}",
)
_DISPLAY_DEFINITIONS: Final = (
    r"\renewcommand{\added}[2][]{\textcolor{blue}{#2}}",
    r"\renewcommand{\deleted}[2][]{\textcolor{blue}{\sout{#2}}}",
    # The product contract intentionally presents old text first.
    r"\renewcommand{\replaced}[3][]{\textcolor{blue}{\sout{#3}}\textcolor{blue}{#2}}",
    r"\renewcommand{\add}[2][]{\textcolor{blue}{#2}}",
    r"\renewcommand{\delete}[2][]{\textcolor{blue}{\sout{#2}}}",
)


@dataclass(frozen=True, slots=True)
class CanonicalRevisionMacroCounts:
    """Counts of the three canonical ``changes`` commands."""

    added: int = 0
    deleted: int = 0
    replaced: int = 0

    @property
    def total(self) -> int:
        return self.added + self.deleted + self.replaced

    def as_dict(self) -> dict[str, int]:
        return {
            "added": self.added,
            "deleted": self.deleted,
            "replaced": self.replaced,
        }


@dataclass(frozen=True, slots=True)
class AliasRevisionMacroCounts:
    """Counts of the explicitly supported short aliases."""

    add: int = 0
    delete: int = 0

    @property
    def total(self) -> int:
        return self.add + self.delete

    def as_dict(self) -> dict[str, int]:
        return {"add": self.add, "delete": self.delete}


@dataclass(frozen=True, slots=True)
class RevisionDisplaySegment:
    """One normalized visible text segment with a single strike state."""

    text: str
    strike: bool

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("revision display segments must not be empty")


@dataclass(frozen=True, slots=True)
class RevisionDisplayExpectation:
    """Source-bound visible projection for one top-level revision macro tree."""

    source_path: str
    source_character_offset: int
    source_call_sha256: str
    segments: tuple[RevisionDisplaySegment, ...]
    macro_instances: int
    left_context: str = ""
    right_context: str = ""

    def __post_init__(self) -> None:
        if self.source_character_offset < 0 or self.macro_instances <= 0:
            raise ValueError("revision display expectation metadata is invalid")
        if (
            not self.source_call_sha256.startswith("sha256:")
            or len(self.source_call_sha256) != 71
            or any(
                character not in "0123456789abcdef"
                for character in self.source_call_sha256.removeprefix("sha256:")
            )
        ):
            raise ValueError("revision display expectation digest is invalid")
        for name, context in (
            ("left_context", self.left_context),
            ("right_context", self.right_context),
        ):
            if (
                len(context) > _MAX_REVISION_CONTEXT_CHARACTERS
                or "~" in context
                or "  " in context
                or any(character.isspace() and character != " " for character in context)
            ):
                raise ValueError(f"revision display expectation {name} is not normalized")

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)

    @property
    def strike_mask(self) -> tuple[bool, ...]:
        return tuple(segment.strike for segment in self.segments for _character in segment.text)

    @property
    def strike_text_characters(self) -> int:
        return sum(len(segment.text) for segment in self.segments if segment.strike)


@dataclass(frozen=True, slots=True)
class RevisionDisplayDegradation:
    """One source-bound revision call that requires best-effort Word display."""

    source_path: str
    source_character_offset: int
    source_character_end: int
    source_call_sha256: str
    line: int
    column: int
    end_line: int
    end_column: int
    command: str
    reason: Literal["command", "empty", "paragraph", "structured"]
    token: str

    def __post_init__(self) -> None:
        digest = self.source_call_sha256.removeprefix("sha256:")
        if (
            self.source_character_offset < 0
            or self.source_character_end <= self.source_character_offset
            or self.line <= 0
            or self.column <= 0
            or self.end_line <= 0
            or self.end_column <= 0
            or self.command not in _REVISION_ARITY
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("revision display degradation metadata is invalid")


class _RevisionProjectionUnsupported(Exception):
    """Internal signal: conversion may continue, but exact display proof cannot."""

    def __init__(
        self,
        source_character_offset: int,
        reason: Literal["command", "empty", "paragraph", "structured"],
        token: str,
    ) -> None:
        super().__init__(reason, token)
        self.source_character_offset = source_character_offset
        self.reason = reason
        self.token = token


class _ProjectionBuilder:
    """Build the same normalized text/strike stream inspected in Word."""

    def __init__(self) -> None:
        self._characters: list[tuple[str, bool]] = []

    def append(self, value: str, *, strike: bool) -> None:
        if len(value) != 1:
            raise AssertionError("projected values must be single characters")
        canonical = " " if value.isspace() or value == "~" else value
        if canonical == " " and self._characters and self._characters[-1][0] == " ":
            previous_strike = self._characters[-1][1]
            self._characters[-1] = (
                " ",
                previous_strike if previous_strike == strike else False,
            )
            return
        self._characters.append((canonical, strike))

    def extend(
        self,
        segments: tuple[RevisionDisplaySegment, ...],
        *,
        force_strike: bool = False,
    ) -> None:
        for segment in segments:
            for character in segment.text:
                self.append(
                    character,
                    strike=True if force_strike else segment.strike,
                )

    def finish(self) -> tuple[RevisionDisplaySegment, ...]:
        if not self._characters:
            return ()
        output: list[RevisionDisplaySegment] = []
        current_strike = self._characters[0][1]
        text: list[str] = []
        for character, strike in self._characters:
            if strike != current_strike:
                output.append(RevisionDisplaySegment("".join(text), current_strike))
                text = []
                current_strike = strike
            text.append(character)
        output.append(RevisionDisplaySegment("".join(text), current_strike))
        return tuple(output)


@dataclass(frozen=True, slots=True)
class RevisionMacroInventory:
    """Immutable, source-tree-bound inventory of active revision commands."""

    source_tree_sha256: str
    tex_files: int
    canonical_counts: CanonicalRevisionMacroCounts
    alias_counts: AliasRevisionMacroCounts
    skipped_dynamic_regions: int
    display_expectations: tuple[RevisionDisplayExpectation, ...] = ()
    display_degradations: tuple[RevisionDisplayDegradation, ...] = ()
    profile_name: str = REVISION_MACRO_PROFILE_NAME
    profile_version: int = REVISION_MACRO_PROFILE_VERSION

    @property
    def total(self) -> int:
        return self.canonical_counts.total + self.alias_counts.total

    @property
    def expected_blue_text_characters(self) -> int:
        return sum(len(expectation.text) for expectation in self.display_expectations)

    @property
    def expected_strike_text_characters(self) -> int:
        return sum(expectation.strike_text_characters for expectation in self.display_expectations)

    @property
    def expected_display_expectations(self) -> int:
        return sum(bool(expectation.text) for expectation in self.display_expectations)

    @property
    def exact_display_macro_instances(self) -> int:
        return sum(expectation.macro_instances for expectation in self.display_expectations)

    @property
    def degraded_display_macro_instances(self) -> int:
        return self.total - self.exact_display_macro_instances

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": {
                "name": self.profile_name,
                "version": self.profile_version,
            },
            "source_tree_sha256": self.source_tree_sha256,
            "tex_files": self.tex_files,
            "canonical_counts": self.canonical_counts.as_dict(),
            "alias_counts": self.alias_counts.as_dict(),
            "total": self.total,
            "skipped_dynamic_regions": self.skipped_dynamic_regions,
            "expected_display_expectations": self.expected_display_expectations,
            "expected_blue_text_characters": self.expected_blue_text_characters,
            "expected_strike_text_characters": self.expected_strike_text_characters,
            "exact_display_macro_instances": self.exact_display_macro_instances,
            "degraded_display_macro_instances": self.degraded_display_macro_instances,
            "display_degradation_calls": len(self.display_degradations),
        }

    def as_metrics(self) -> dict[str, int]:
        return {
            "revision_macro_inventory_version": self.profile_version,
            "revision_macro_tex_files": self.tex_files,
            "revision_macros_total": self.total,
            "revision_macros_added": self.canonical_counts.added,
            "revision_macros_deleted": self.canonical_counts.deleted,
            "revision_macros_replaced": self.canonical_counts.replaced,
            "revision_macros_alias_add": self.alias_counts.add,
            "revision_macros_alias_delete": self.alias_counts.delete,
            "revision_macro_skipped_dynamic_regions": self.skipped_dynamic_regions,
            "revision_display_expected_expectations": self.expected_display_expectations,
            "revision_display_expected_blue_text_characters": self.expected_blue_text_characters,
            "revision_display_expected_strike_text_characters": (
                self.expected_strike_text_characters
            ),
            "revision_display_exact_macro_instances": self.exact_display_macro_instances,
            "revision_display_degraded_macro_instances": self.degraded_display_macro_instances,
            "revision_display_degradation_calls": len(self.display_degradations),
        }


@dataclass(frozen=True, slots=True)
class _Group:
    body_start: int
    body_end: int
    end: int


@dataclass(frozen=True, slots=True)
class _AliasCall:
    alias: str
    has_optional_argument: bool
    path: str
    character_offset: int
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class _AliasSource:
    alias: str
    source_kind: str
    allows_optional_argument: bool
    path: str
    character_offset: int
    line: int
    column: int
    definition_command: str
    proven: bool

    def location_details(self) -> dict[str, object]:
        return {
            "path": self.path,
            "character_offset": self.character_offset,
            "line": self.line,
            "column": self.column,
        }


@dataclass(frozen=True, slots=True)
class _PackageSearchOverride:
    path: str
    character_offset: int
    line: int
    column: int
    definition_command: str
    source_kind: str


@dataclass(slots=True)
class _MutableCounts:
    added: int = 0
    deleted: int = 0
    replaced: int = 0
    add: int = 0
    delete: int = 0
    skipped_dynamic_regions: int = 0
    alias_audit_command_nodes: int = 0
    alias_calls: list[_AliasCall] = field(default_factory=list)
    alias_sources: list[_AliasSource] = field(default_factory=list)
    canonical_calls: list[_AliasCall] = field(default_factory=list)
    canonical_sources: list[_AliasSource] = field(default_factory=list)
    local_changes_package_paths: list[str] = field(default_factory=list)
    local_trackchanges_package_paths: list[str] = field(default_factory=list)
    package_search_overrides: list[_PackageSearchOverride] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.added + self.deleted + self.replaced + self.add + self.delete

    def record(self, command: str) -> None:
        if command == "added":
            self.added += 1
        elif command == "deleted":
            self.deleted += 1
        elif command == "replaced":
            self.replaced += 1
        elif command == "add":
            self.add += 1
        elif command == "delete":
            self.delete += 1
        else:  # pragma: no cover - private caller has a closed command table
            raise AssertionError(f"unknown revision command: {command}")


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


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


def _definition_command(text: str, index: int, *, xparse: bool) -> tuple[str, int]:
    """Read a definition target, including LaTeX3 name characters for xparse."""

    if not xparse:
        return _command(text, index)
    cursor = index + 1
    if cursor >= len(text):
        return "", cursor
    if text[cursor].isascii() and (text[cursor].isalpha() or text[cursor] in "@_:"):
        end = cursor + 1
        while (
            end < len(text) and text[end].isascii() and (text[end].isalpha() or text[end] in "@_:")
        ):
            end += 1
        return text[cursor:end], end
    return text[cursor], cursor + 1


class _FileScanner:
    def __init__(self, text: str, path: str, counts: _MutableCounts) -> None:
        self._text = text
        self._path = path
        self._counts = counts
        line_starts = [0]
        line_starts.extend(index + 1 for index, character in enumerate(text) if character == "\n")
        self._line_starts = tuple(line_starts)
        self._alias_audit_dynamic_depth = 0
        self._alias_audit_group_depth = 0
        self._display_expectations: list[RevisionDisplayExpectation] = []
        self._display_degradations: list[RevisionDisplayDegradation] = []

    def scan(self, *, main_document: bool) -> None:
        self._scan_range(
            0,
            len(self._text),
            active=not main_document,
            document_gated=main_document,
            nesting=0,
        )

    def _error(self, message: str, index: int, **details: object) -> NoReturn:
        line, column = self._line_column(index)
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            message,
            details={
                "path": self._path,
                "character_offset": index,
                "line": line,
                "column": column,
                **details,
            },
        )

    def _skip_comment(self, index: int, limit: int) -> int:
        line_feed = self._text.find("\n", index, limit)
        carriage_return = self._text.find("\r", index, limit)
        endings = tuple(ending for ending in (line_feed, carriage_return) if ending >= 0)
        if not endings:
            return limit
        ending = min(endings)
        if self._text[ending] == "\r" and ending + 1 < limit and self._text[ending + 1] == "\n":
            return ending + 2
        return ending + 1

    def _skip_space_and_comments(self, index: int, limit: int) -> int:
        cursor = index
        while cursor < limit:
            if self._text[cursor].isspace():
                cursor += 1
                continue
            if self._text[cursor] == "%" and not _is_escaped(self._text, cursor):
                cursor = self._skip_comment(cursor, limit)
                continue
            break
        return cursor

    def _line_column(self, index: int) -> tuple[int, int]:
        line_index = bisect_right(self._line_starts, index) - 1
        return line_index + 1, index - self._line_starts[line_index] + 1

    def _record_package_search_override(
        self,
        *,
        index: int,
        definition_command: str,
        source_kind: str,
    ) -> None:
        line, column = self._line_column(index)
        if self._alias_audit_dynamic_depth or self._alias_audit_group_depth:
            context = "dynamic" if self._alias_audit_dynamic_depth else "grouped"
            source_kind = f"{context}_{source_kind}"
        self._counts.package_search_overrides.append(
            _PackageSearchOverride(
                path=self._path,
                character_offset=index,
                line=line,
                column=column,
                definition_command=definition_command,
                source_kind=source_kind,
            )
        )

    def _record_alias_source(
        self,
        *,
        alias: str,
        source_kind: str,
        allows_optional_argument: bool,
        index: int,
        definition_command: str,
        proven: bool,
    ) -> None:
        line, column = self._line_column(index)
        if self._alias_audit_dynamic_depth or self._alias_audit_group_depth:
            context = "dynamic" if self._alias_audit_dynamic_depth else "grouped"
            source_kind = f"{context}_{source_kind}"
            proven = False
        self._counts.alias_sources.append(
            _AliasSource(
                alias=alias,
                source_kind=source_kind,
                allows_optional_argument=allows_optional_argument,
                path=self._path,
                character_offset=index,
                line=line,
                column=column,
                definition_command=definition_command,
                proven=proven,
            )
        )

    def _record_canonical_source(
        self,
        *,
        command_name: str,
        source_kind: str,
        index: int,
        definition_command: str,
        proven: bool,
    ) -> None:
        line, column = self._line_column(index)
        if self._alias_audit_dynamic_depth or self._alias_audit_group_depth:
            context = "dynamic" if self._alias_audit_dynamic_depth else "grouped"
            source_kind = f"{context}_{source_kind}"
            proven = False
        self._counts.canonical_sources.append(
            _AliasSource(
                alias=command_name,
                source_kind=source_kind,
                allows_optional_argument=proven,
                path=self._path,
                character_offset=index,
                line=line,
                column=column,
                definition_command=definition_command,
                proven=proven,
            )
        )

    def _record_canonical_definition(
        self,
        *,
        command_name: str,
        index: int,
        definition_command: str,
    ) -> None:
        self._record_canonical_source(
            command_name=command_name,
            source_kind="canonical_definition",
            index=index,
            definition_command=definition_command,
            proven=False,
        )

    def _definition_target_info(
        self,
        index: int,
        limit: int,
        *,
        command: str,
    ) -> tuple[str | None, int, int]:
        cursor = self._skip_space_and_comments(index, limit)
        target_start = cursor
        if cursor < limit and self._text[cursor] == "{":
            group = self._balanced(
                cursor,
                limit,
                opening="{",
                label=f"\\{command} target",
            )
            inner = self._skip_space_and_comments(group.body_start, group.body_end)
            if inner < group.body_end and self._text[inner] == "\\":
                target, target_end = _definition_command(
                    self._text,
                    inner,
                    xparse=command in _XPARSE_COMMAND_DEFINITIONS,
                )
                if self._skip_space_and_comments(target_end, group.body_end) == group.body_end:
                    return target, target_start, group.end
            return None, target_start, group.end
        if cursor < limit and self._text[cursor] == "\\":
            target, target_end = _definition_command(
                self._text,
                cursor,
                xparse=command in _XPARSE_COMMAND_DEFINITIONS,
            )
            return target, target_start, target_end
        return None, target_start, min(cursor + 1, limit)

    def _compact_range(self, start: int, limit: int) -> str:
        compact: list[str] = []
        cursor = start
        while cursor < limit:
            character = self._text[cursor]
            if character == "%" and not _is_escaped(self._text, cursor):
                cursor = self._skip_comment(cursor, limit)
                continue
            if character.isspace():
                cursor += 1
                continue
            compact.append(character)
            cursor += 1
        return "".join(compact)

    @staticmethod
    def _xparse_optional_then_mandatory(specification: str) -> bool:
        if not specification.startswith("O{"):
            return False
        depth = 1
        cursor = 2
        while cursor < len(specification):
            character = specification[cursor]
            if character == "\\":
                cursor += 2
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return specification[cursor + 1 :] == "m"
            cursor += 1
        return False

    def _audit_newcommand_alias(
        self,
        command: str,
        target: str,
        target_start: int,
        cursor: int,
        limit: int,
    ) -> int:
        option_groups: list[_Group] = []
        cursor = self._skip_space_and_comments(cursor, limit)
        for _ in range(2):
            if cursor >= limit or self._text[cursor] != "[":
                break
            group = self._balanced(
                cursor,
                limit,
                opening="[",
                label=f"\\{command}",
            )
            option_groups.append(group)
            cursor = self._skip_space_and_comments(group.end, limit)
        if cursor < limit and self._text[cursor] == "[":
            self._error(f"\\{command} has too many optional groups", cursor)
        body = self._mandatory_group(cursor, limit, label=f"\\{command} body")
        body_text = self._compact_range(body.body_start, body.body_end)
        canonical = "added" if target == "add" else "deleted"
        one_argument = (
            len(option_groups) == 1
            and self._compact_range(
                option_groups[0].body_start,
                option_groups[0].body_end,
            )
            == "1"
            and body_text == f"\\{canonical}{{#1}}"
        )
        optional_argument = (
            len(option_groups) == 2
            and self._compact_range(
                option_groups[0].body_start,
                option_groups[0].body_end,
            )
            == "2"
            and body_text == f"\\{canonical}[#1]{{#2}}"
        )
        self._record_alias_source(
            alias=target,
            source_kind="local_wrapper"
            if one_argument or optional_argument
            else "local_definition",
            allows_optional_argument=optional_argument,
            index=target_start,
            definition_command=command,
            proven=one_argument or optional_argument,
        )
        return body.end

    def _audit_xparse_alias(
        self,
        command: str,
        target: str,
        target_start: int,
        cursor: int,
        limit: int,
    ) -> int:
        specification = self._mandatory_group(
            cursor,
            limit,
            label=f"\\{command} argument specification",
        )
        body = self._mandatory_group(
            specification.end,
            limit,
            label=f"\\{command} body",
        )
        spec_text = self._compact_range(specification.body_start, specification.body_end)
        body_text = self._compact_range(body.body_start, body.body_end)
        canonical = "added" if target == "add" else "deleted"
        one_argument = spec_text == "m" and body_text == f"\\{canonical}{{#1}}"
        optional_argument = (
            self._xparse_optional_then_mandatory(spec_text)
            and body_text == f"\\{canonical}[#1]{{#2}}"
        )
        self._record_alias_source(
            alias=target,
            source_kind="local_wrapper"
            if one_argument or optional_argument
            else "local_definition",
            allows_optional_argument=optional_argument,
            index=target_start,
            definition_command=command,
            proven=one_argument or optional_argument,
        )
        return body.end

    def _audit_primitive_alias(
        self,
        command: str,
        target: str,
        target_start: int,
        cursor: int,
        limit: int,
    ) -> int:
        body_start = cursor
        while body_start < limit and self._text[body_start] != "{":
            if self._text[body_start] == "%" and not _is_escaped(self._text, body_start):
                body_start = self._skip_comment(body_start, limit)
            else:
                body_start += 1
        body = self._balanced(
            body_start,
            limit,
            opening="{",
            label=f"\\{command} body",
        )
        parameters = self._compact_range(cursor, body_start)
        body_text = self._compact_range(body.body_start, body.body_end)
        canonical = "added" if target == "add" else "deleted"
        proven = (
            command in {"def", "gdef"}
            and parameters == "#1"
            and body_text == f"\\{canonical}{{#1}}"
        )
        self._record_alias_source(
            alias=target,
            source_kind="local_wrapper" if proven else "local_definition",
            allows_optional_argument=False,
            index=target_start,
            definition_command=command,
            proven=proven,
        )
        return body.end

    def _audit_assignment_alias(
        self,
        command: str,
        target: str,
        target_start: int,
        cursor: int,
        limit: int,
    ) -> int:
        cursor = self._skip_space_and_comments(cursor, limit)
        if cursor < limit and self._text[cursor] == "=":
            cursor = self._skip_space_and_comments(cursor + 1, limit)
        source = None
        if cursor < limit and self._text[cursor] == "\\":
            source, cursor = _command(self._text, cursor)
        elif cursor < limit:
            cursor += 1
        canonical = "added" if target == "add" else "deleted"
        proven = command == "let" and source == canonical
        self._record_alias_source(
            alias=target,
            source_kind="local_wrapper" if proven else "local_definition",
            allows_optional_argument=proven,
            index=target_start,
            definition_command=command,
            proven=proven,
        )
        return cursor

    def _skip_assignment_source(self, index: int, limit: int) -> int:
        cursor = self._skip_space_and_comments(index, limit)
        if cursor < limit and self._text[cursor] == "=":
            cursor = self._skip_space_and_comments(cursor + 1, limit)
        if cursor < limit and self._text[cursor] == "\\":
            _, cursor = _command(self._text, cursor)
        elif cursor < limit:
            cursor += 1
        return cursor

    def _audit_package_load(self, command: str, index: int, limit: int) -> int:
        cursor = self._skip_space_and_comments(index, limit)
        if cursor < limit and self._text[cursor] == "[":
            cursor = self._balanced(
                cursor,
                limit,
                opening="[",
                label=f"\\{command} options",
            ).end
        packages = self._mandatory_group(
            cursor,
            limit,
            label=f"\\{command} package list",
        )
        package_names = {
            item.strip() for item in self._text[packages.body_start : packages.body_end].split(",")
        }
        if "trackchanges" in package_names:
            self._record_alias_source(
                alias="add",
                source_kind="trackchanges_package",
                allows_optional_argument=True,
                index=packages.body_start,
                definition_command=command,
                proven=True,
            )
        if "changes" in package_names:
            for command_name in _CANONICAL_ARITY:
                self._record_canonical_source(
                    command_name=command_name,
                    source_kind="changes_package",
                    index=packages.body_start,
                    definition_command=command,
                    proven=True,
                )
        return packages.end

    def _consume_alias_audit_command_node(self, index: int, *, dynamic_depth: int) -> None:
        if dynamic_depth > _MAX_ALIAS_AUDIT_NESTING:
            self._error(
                "revision source audit nesting exceeds the safety limit",
                index,
                max_nesting=_MAX_ALIAS_AUDIT_NESTING,
            )
        self._counts.alias_audit_command_nodes += 1
        if self._counts.alias_audit_command_nodes > _MAX_ALIAS_AUDIT_COMMAND_NODES:
            self._error(
                "revision source audit command count exceeds the safety limit",
                index,
                max_command_nodes=_MAX_ALIAS_AUDIT_COMMAND_NODES,
            )

    @staticmethod
    def _literal_named_control_sequence(value: str) -> tuple[str | None, bool]:
        if value and all(
            character.isascii() and (character.isalnum() or character in {"@", ":", "_", "-"})
            for character in value
        ):
            return value, True
        return None, False

    @staticmethod
    def _literal_command_control_sequence(value: str) -> tuple[str | None, bool]:
        if not value.startswith("\\"):
            return None, False
        target, end = _command(value, 0)
        if end != len(value):
            return None, False
        return target, True

    def _record_computed_canonical_target(
        self,
        *,
        target: str | None,
        target_is_static: bool,
        target_expression: str,
        index: int,
        definition_command: str,
        source_kind: str,
    ) -> None:
        command_names: tuple[str, ...]
        if target_is_static:
            if target not in _CANONICAL_ARITY:
                return
            command_names = (target,)
        else:
            literal_prefix = target_expression.split("\\", 1)[0]
            command_names = tuple(
                name for name in _CANONICAL_ARITY if name.startswith(literal_prefix)
            )
            if not command_names:
                return
        for command_name in command_names:
            self._record_canonical_source(
                command_name=command_name,
                source_kind=source_kind,
                index=index,
                definition_command=definition_command,
                proven=False,
            )

    def _argument_token(self, index: int, limit: int, *, label: str) -> _Group:
        cursor = self._skip_space_and_comments(index, limit)
        if cursor >= limit:
            self._error(f"{label} is missing", cursor)
        if self._text[cursor] == "{":
            return self._balanced(cursor, limit, opening="{", label=label)
        if self._text[cursor] == "\\":
            _, end = _command(self._text, cursor)
            return _Group(cursor, end, end)
        return _Group(cursor, cursor + 1, cursor + 1)

    def _audit_named_definition(
        self,
        command: str,
        index: int,
        limit: int,
        *,
        arguments: int,
    ) -> int:
        target_group = self._argument_token(index, limit, label=f"\\{command} target name")
        target_expression = self._compact_range(target_group.body_start, target_group.body_end)
        target, target_is_static = self._literal_named_control_sequence(target_expression)
        if target_is_static and target == "input@path":
            self._record_package_search_override(
                index=target_group.body_start,
                definition_command=command,
                source_kind="computed_definition",
            )
        self._record_computed_canonical_target(
            target=target,
            target_is_static=target_is_static,
            target_expression=target_expression,
            index=target_group.body_start,
            definition_command=command,
            source_kind="computed_definition",
        )
        cursor = target_group.end
        for number in range(2, arguments + 1):
            cursor = self._argument_token(
                cursor,
                limit,
                label=f"\\{command} argument {number}",
            ).end
        return cursor

    def _audit_named_primitive_definition(
        self,
        command: str,
        index: int,
        limit: int,
    ) -> int:
        target_group = self._argument_token(index, limit, label=f"\\{command} target name")
        target_expression = self._compact_range(target_group.body_start, target_group.body_end)
        target, target_is_static = self._literal_named_control_sequence(target_expression)
        if target_is_static and target == "input@path":
            self._record_package_search_override(
                index=target_group.body_start,
                definition_command=command,
                source_kind="computed_definition",
            )
        self._record_computed_canonical_target(
            target=target,
            target_is_static=target_is_static,
            target_expression=target_expression,
            index=target_group.body_start,
            definition_command=command,
            source_kind="computed_definition",
        )
        cursor = target_group.end
        while cursor < limit and self._text[cursor] != "{":
            if self._text[cursor] == "%" and not _is_escaped(self._text, cursor):
                cursor = self._skip_comment(cursor, limit)
            elif self._text[cursor] == "\\":
                _, cursor = _command(self._text, cursor)
            else:
                cursor += 1
        return self._balanced(
            cursor,
            limit,
            opening="{",
            label=f"\\{command} body",
        ).end

    def _audit_patch_definition(
        self,
        command: str,
        index: int,
        limit: int,
        *,
        arguments: int,
    ) -> int:
        cursor = self._skip_space_and_comments(index, limit)
        target_index = cursor
        target_expression = ""
        if cursor < limit and self._text[cursor] == "{":
            target_group = self._balanced(
                cursor,
                limit,
                opening="{",
                label=f"\\{command} target",
            )
            target_expression = self._compact_range(target_group.body_start, target_group.body_end)
            target, target_is_static = self._literal_command_control_sequence(target_expression)
            cursor = target_group.end
        elif cursor < limit and self._text[cursor] == "\\":
            target, cursor = _command(self._text, cursor)
            target_expression = f"\\{target}"
            target_is_static = True
        else:
            target = None
            target_is_static = False
            cursor = min(cursor + 1, limit)
        if target_is_static and target == "input@path":
            self._record_package_search_override(
                index=target_index,
                definition_command=command,
                source_kind="patch_definition",
            )
        self._record_computed_canonical_target(
            target=target,
            target_is_static=target_is_static,
            target_expression=target_expression,
            index=target_index,
            definition_command=command,
            source_kind="patch_definition",
        )
        for number in range(2, arguments + 1):
            cursor = self._argument_token(
                cursor,
                limit,
                label=f"\\{command} argument {number}",
            ).end
        return cursor

    def _audit_expandafter_definition(self, index: int, limit: int) -> int | None:
        cursor = self._skip_space_and_comments(index, limit)
        while cursor < limit and self._text[cursor] == "\\":
            command, command_end = _command(self._text, cursor)
            if command == "expandafter":
                cursor = self._skip_space_and_comments(command_end, limit)
                continue
            if command not in _PRIMITIVE_DEFINITIONS | _ALIAS_ASSIGNMENT_DEFINITIONS:
                return None
            target_start = self._skip_space_and_comments(command_end, limit)
            if target_start >= limit or self._text[target_start] != "\\":
                return None
            target_command, name_start = _command(self._text, target_start)
            if target_command != "csname":
                return None
            name_cursor = name_start
            has_dynamic_name = False
            while name_cursor < limit:
                if self._text[name_cursor] == "%" and not _is_escaped(self._text, name_cursor):
                    name_cursor = self._skip_comment(name_cursor, limit)
                    continue
                if self._text[name_cursor] != "\\":
                    name_cursor += 1
                    continue
                name_command, name_end = _command(self._text, name_cursor)
                if name_command == "endcsname":
                    target, target_is_static = self._literal_named_control_sequence(
                        self._compact_range(name_start, name_cursor)
                    )
                    target_is_literal = target_is_static and not has_dynamic_name
                    if target_is_literal and target == "input@path":
                        self._record_package_search_override(
                            index=name_start,
                            definition_command=f"expandafter/{command}",
                            source_kind="computed_definition",
                        )
                    target_expression = self._compact_range(name_start, name_cursor)
                    self._record_computed_canonical_target(
                        target=target,
                        target_is_static=target_is_literal,
                        target_expression=target_expression,
                        index=name_start,
                        definition_command=f"expandafter/{command}",
                        source_kind="computed_definition",
                    )
                    if command in _ALIAS_ASSIGNMENT_DEFINITIONS:
                        return self._skip_assignment_source(name_end, limit)
                    body_start = name_end
                    while body_start < limit and self._text[body_start] != "{":
                        if self._text[body_start] == "%" and not _is_escaped(
                            self._text, body_start
                        ):
                            body_start = self._skip_comment(body_start, limit)
                        else:
                            body_start += 1
                    return self._balanced(
                        body_start,
                        limit,
                        opening="{",
                        label=f"\\{command} body",
                    ).end
                has_dynamic_name = True
                name_cursor = name_end
            self._error("computed control-sequence definition is unbalanced", target_start)
        return None

    def _audit_alias_sources_range(
        self,
        start: int,
        limit: int,
        *,
        dynamic_depth: int,
    ) -> None:
        if dynamic_depth > _MAX_ALIAS_AUDIT_NESTING:
            self._error(
                "revision source audit nesting exceeds the safety limit",
                start,
                max_nesting=_MAX_ALIAS_AUDIT_NESTING,
            )
        cursor = start
        group_depth = 0
        self._alias_audit_dynamic_depth = dynamic_depth
        self._alias_audit_group_depth = group_depth
        while cursor < limit:
            character = self._text[cursor]
            if character == "%" and not _is_escaped(self._text, cursor):
                cursor = self._skip_comment(cursor, limit)
                continue
            if character != "\\":
                if character == "{":
                    group_depth += 1
                    self._alias_audit_group_depth = group_depth
                elif character == "}" and group_depth:
                    group_depth -= 1
                    self._alias_audit_group_depth = group_depth
                cursor += 1
                continue
            command_start = cursor
            command, cursor = _command(self._text, cursor)
            self._consume_alias_audit_command_node(
                command_start,
                dynamic_depth=dynamic_depth,
            )
            if command == "endinput":
                return
            if command == "input@path":
                self._record_package_search_override(
                    index=command_start,
                    definition_command=command,
                    source_kind="direct_reference",
                )
                continue
            if command == "verb":
                cursor = self._skip_verb(cursor, limit)
                continue
            if command == "begin":
                group = self._mandatory_group(
                    cursor,
                    limit,
                    label="\\begin environment name",
                )
                environment = self._text[group.body_start : group.body_end].strip()
                cursor = group.end
                if environment in _VERBATIM_ENVIRONMENTS:
                    cursor = self._skip_verbatim_environment(environment, cursor, limit)
                continue
            conditional_arguments = _GROUP_CONDITIONAL_ARGUMENTS.get(command)
            if conditional_arguments is not None:
                for number in range(1, conditional_arguments + 1):
                    group = self._mandatory_group(
                        cursor,
                        limit,
                        label=f"dynamic conditional argument {number}",
                    )
                    self._audit_alias_sources_range(
                        group.body_start,
                        group.body_end,
                        dynamic_depth=dynamic_depth + 1,
                    )
                    cursor = group.end
                self._alias_audit_dynamic_depth = dynamic_depth
                self._alias_audit_group_depth = group_depth
                continue
            if command.startswith("if"):
                dynamic_depth += 1
                if dynamic_depth > _MAX_ALIAS_AUDIT_NESTING:
                    self._error(
                        "revision source audit nesting exceeds the safety limit",
                        command_start,
                        max_nesting=_MAX_ALIAS_AUDIT_NESTING,
                    )
                self._alias_audit_dynamic_depth = dynamic_depth
                continue
            if command == "fi":
                if dynamic_depth:
                    dynamic_depth -= 1
                self._alias_audit_dynamic_depth = dynamic_depth
                continue
            if command in {"begingroup", "bgroup"}:
                group_depth += 1
                self._alias_audit_group_depth = group_depth
                continue
            if command in {"egroup", "endgroup"}:
                if group_depth:
                    group_depth -= 1
                self._alias_audit_group_depth = group_depth
                continue
            if command == "expandafter":
                definition_end = self._audit_expandafter_definition(cursor, limit)
                if definition_end is not None:
                    cursor = definition_end
                    continue
            if command in _DYNAMIC_NAMED_PRIMITIVE_DEFINITIONS:
                cursor = self._audit_named_primitive_definition(
                    command,
                    cursor,
                    limit,
                )
                continue
            dynamic_arguments = _DYNAMIC_NAMED_DEFINITION_ARGUMENTS.get(command)
            if dynamic_arguments is not None:
                cursor = self._audit_named_definition(
                    command,
                    cursor,
                    limit,
                    arguments=dynamic_arguments,
                )
                continue
            patch_arguments = _PATCH_DEFINITION_ARGUMENTS.get(command)
            if patch_arguments is not None:
                cursor = self._audit_patch_definition(
                    command,
                    cursor,
                    limit,
                    arguments=patch_arguments,
                )
                continue
            if command in _PACKAGE_LOAD_COMMANDS:
                cursor = self._audit_package_load(command, cursor, limit)
                continue
            if command in _NEW_COMMAND_DEFINITIONS | _XPARSE_COMMAND_DEFINITIONS:
                definition_cursor = cursor
                target_cursor = self._skip_space_and_comments(cursor, limit)
                if target_cursor < limit and self._text[target_cursor] == "*":
                    target_cursor = self._skip_space_and_comments(target_cursor + 1, limit)
                target, target_start, target_end = self._definition_target_info(
                    target_cursor,
                    limit,
                    command=command,
                )
                if target == "input@path":
                    self._record_package_search_override(
                        index=target_start,
                        definition_command=command,
                        source_kind="definition",
                    )
                    cursor = self._skip_definition(command, definition_cursor, limit)
                    continue
                if target in _CANONICAL_ARITY:
                    self._record_canonical_definition(
                        command_name=target,
                        index=target_start,
                        definition_command=command,
                    )
                    cursor = self._skip_definition(command, definition_cursor, limit)
                    continue
                if target not in _ALIAS_ARITY:
                    cursor = self._skip_definition(command, definition_cursor, limit)
                    continue
                if command in _NEW_COMMAND_DEFINITIONS:
                    cursor = self._audit_newcommand_alias(
                        command,
                        target,
                        target_start,
                        target_end,
                        limit,
                    )
                else:
                    cursor = self._audit_xparse_alias(
                        command,
                        target,
                        target_start,
                        target_end,
                        limit,
                    )
                continue
            if command in _PRIMITIVE_DEFINITIONS:
                definition_cursor = cursor
                target, target_start, target_end = self._definition_target_info(
                    cursor,
                    limit,
                    command=command,
                )
                if target == "input@path":
                    self._record_package_search_override(
                        index=target_start,
                        definition_command=command,
                        source_kind="definition",
                    )
                    cursor = self._skip_definition(command, definition_cursor, limit)
                elif target in _CANONICAL_ARITY:
                    self._record_canonical_definition(
                        command_name=target,
                        index=target_start,
                        definition_command=command,
                    )
                    cursor = self._skip_definition(command, definition_cursor, limit)
                elif target in _ALIAS_ARITY:
                    cursor = self._audit_primitive_alias(
                        command,
                        target,
                        target_start,
                        target_end,
                        limit,
                    )
                else:
                    cursor = self._skip_definition(command, definition_cursor, limit)
                continue
            if command in _ALIAS_ASSIGNMENT_DEFINITIONS:
                target, target_start, target_end = self._definition_target_info(
                    cursor,
                    limit,
                    command=command,
                )
                if target == "input@path":
                    self._record_package_search_override(
                        index=target_start,
                        definition_command=command,
                        source_kind="definition",
                    )
                    cursor = self._skip_assignment_source(target_end, limit)
                elif target in _CANONICAL_ARITY:
                    self._record_canonical_definition(
                        command_name=target,
                        index=target_start,
                        definition_command=command,
                    )
                    cursor = self._skip_assignment_source(target_end, limit)
                elif target in _ALIAS_ARITY:
                    cursor = self._audit_assignment_alias(
                        command,
                        target,
                        target_start,
                        target_end,
                        limit,
                    )
                else:
                    cursor = self._skip_assignment_source(target_end, limit)
                continue
            if command in _XPARSE_ENVIRONMENT_DEFINITIONS | _ENVIRONMENT_DEFINITIONS:
                cursor = self._skip_definition(command, cursor, limit)
                continue
            if cursor <= command_start:
                self._error("revision alias source audit did not advance", command_start)

    def audit_alias_sources(self) -> None:
        self._audit_alias_sources_range(0, len(self._text), dynamic_depth=0)
        self._alias_audit_dynamic_depth = 0
        self._alias_audit_group_depth = 0

    def _balanced(
        self,
        index: int,
        limit: int,
        *,
        opening: Literal["[", "{"],
        label: str,
    ) -> _Group:
        closing = "]" if opening == "[" else "}"
        if index >= limit or self._text[index] != opening:
            self._error(f"{label} is missing", index, expected=opening)
        cursor = index + 1
        depth = 1
        brace_depth = 0
        while cursor < limit:
            character = self._text[cursor]
            if character == "%" and not _is_escaped(self._text, cursor):
                cursor = self._skip_comment(cursor, limit)
                continue
            if character == "\\":
                cursor = min(cursor + 2, limit)
                continue
            if opening == "{":
                if character == opening:
                    depth += 1
                elif character == closing:
                    depth -= 1
                    if depth == 0:
                        return _Group(index + 1, cursor, cursor + 1)
            else:
                if character == "{":
                    brace_depth += 1
                elif character == "}" and brace_depth:
                    brace_depth -= 1
                elif brace_depth == 0:
                    if character == opening:
                        depth += 1
                    elif character == closing:
                        depth -= 1
                        if depth == 0:
                            return _Group(index + 1, cursor, cursor + 1)
            cursor += 1
        self._error(
            f"{label} is unbalanced",
            index,
            opening=opening,
            closing=closing,
        )

    def _mandatory_group(self, index: int, limit: int, *, label: str) -> _Group:
        cursor = self._skip_space_and_comments(index, limit)
        return self._balanced(cursor, limit, opening="{", label=label)

    def _validate_revision_argument(
        self,
        group: _Group,
        *,
        revision_command: str,
        argument_number: int,
    ) -> None:
        self._validate_revision_argument_range(
            group.body_start,
            group.body_end,
            revision_command=revision_command,
            argument_number=argument_number,
            nesting=0,
        )

    def _validate_revision_argument_range(
        self,
        start: int,
        limit: int,
        *,
        revision_command: str,
        argument_number: int,
        nesting: int,
    ) -> None:
        if nesting > _MAX_REVISION_ARGUMENT_NESTING:
            self._error(
                "revision macro argument nesting exceeds the safety limit",
                start,
                command=revision_command,
                argument=argument_number,
                max_nesting=_MAX_REVISION_ARGUMENT_NESTING,
            )
        cursor = start
        pending_line_break = False
        while cursor < limit:
            character = self._text[cursor]
            if character == "%" and not _is_escaped(self._text, cursor):
                cursor = self._skip_comment(cursor, limit)
                continue
            if character in "\r\n":
                if pending_line_break:
                    raise _RevisionProjectionUnsupported(cursor, "paragraph", "paragraph-break")
                pending_line_break = True
                cursor += (
                    2
                    if character == "\r" and cursor + 1 < limit and self._text[cursor + 1] == "\n"
                    else 1
                )
                continue
            if character.isspace():
                cursor += 1
                continue
            pending_line_break = False
            if character == "\\":
                command_start = cursor
                inline_command, cursor = _command(self._text, cursor)
                if inline_command in _REVISION_ARITY or inline_command in _LITERAL_TEXT_ESCAPES:
                    continue
                if inline_command in _SIMPLE_INLINE_FORMATTING:
                    content = self._mandatory_group(
                        cursor,
                        limit,
                        label=f"\\{inline_command} inline text",
                    )
                    self._validate_revision_argument_range(
                        content.body_start,
                        content.body_end,
                        revision_command=revision_command,
                        argument_number=argument_number,
                        nesting=nesting + 1,
                    )
                    cursor = content.end
                    continue
                raise _RevisionProjectionUnsupported(
                    command_start,
                    "command",
                    f"\\{inline_command}" if inline_command else "trailing-backslash",
                )
            if character == "{":
                content = self._balanced(
                    cursor,
                    limit,
                    opening="{",
                    label=f"\\{revision_command} argument {argument_number} group",
                )
                self._validate_revision_argument_range(
                    content.body_start,
                    content.body_end,
                    revision_command=revision_command,
                    argument_number=argument_number,
                    nesting=nesting + 1,
                )
                cursor = content.end
                continue
            if character == "}":
                self._error(
                    "revision macro argument contains an unmatched closing brace",
                    cursor,
                    command=revision_command,
                    argument=argument_number,
                )
            if character in _STRUCTURAL_TEXT_CHARACTERS:
                raise _RevisionProjectionUnsupported(cursor, "structured", character)
            cursor += 1

    @staticmethod
    def _normalize_revision_context(
        raw: str,
        *,
        side: Literal["left", "right"],
    ) -> str:
        builder = _ProjectionBuilder()
        for character in raw:
            builder.append(character, strike=False)
        value = "".join(segment.text for segment in builder.finish())
        value = value.lstrip(" ") if side == "left" else value.rstrip(" ")
        if not value:
            return ""
        if side == "left":
            return value[-_MAX_REVISION_CONTEXT_CHARACTERS:]
        return value[:_MAX_REVISION_CONTEXT_CHARACTERS]

    def _left_revision_context(self, index: int) -> str:
        cursor = index
        raw_reversed: list[str] = []
        source_characters = 0
        whitespace_start: int | None = None
        line_breaks = 0
        while cursor > 0 and source_characters < _MAX_REVISION_CONTEXT_SOURCE_CHARACTERS:
            character = self._text[cursor - 1]
            consumed = 1
            if character == "\n" and cursor >= 2 and self._text[cursor - 2] == "\r":
                cursor -= 1
                consumed = 2
            cursor -= 1
            source_characters += consumed
            if character in "\r\n":
                if whitespace_start is None:
                    whitespace_start = len(raw_reversed)
                line_breaks += 1
                if line_breaks >= 2:
                    raw_reversed = raw_reversed[:whitespace_start]
                    break
                raw_reversed.append("\n")
                continue
            if character.isspace() or character == "~":
                if whitespace_start is None:
                    whitespace_start = len(raw_reversed)
                raw_reversed.append(character)
                continue
            line_breaks = 0
            whitespace_start = None
            if character == "%":
                newline_index = next(
                    (position for position, value in enumerate(raw_reversed) if value in "\r\n"),
                    len(raw_reversed),
                )
                raw_reversed = raw_reversed[:newline_index]
                break
            if character == "\\":
                # Walking backwards cannot prove which following token an unknown
                # control sequence consumes, so none of that candidate is trusted.
                raw_reversed.clear()
                break
            if character in "{}#$&^_":
                break
            raw_reversed.append(character)
        return self._normalize_revision_context(
            "".join(reversed(raw_reversed)),
            side="left",
        )

    def _right_revision_context(self, index: int) -> str:
        cursor = index
        raw: list[str] = []
        source_characters = 0
        whitespace_start: int | None = None
        line_breaks = 0
        while (
            cursor < len(self._text) and source_characters < _MAX_REVISION_CONTEXT_SOURCE_CHARACTERS
        ):
            character = self._text[cursor]
            consumed = 1
            if (
                character == "\r"
                and cursor + 1 < len(self._text)
                and self._text[cursor + 1] == "\n"
            ):
                consumed = 2
            cursor += consumed
            source_characters += consumed
            if character in "\r\n":
                if whitespace_start is None:
                    whitespace_start = len(raw)
                line_breaks += 1
                if line_breaks >= 2:
                    raw = raw[:whitespace_start]
                    break
                raw.append("\n")
                continue
            if character.isspace() or character == "~":
                if whitespace_start is None:
                    whitespace_start = len(raw)
                raw.append(character)
                continue
            line_breaks = 0
            whitespace_start = None
            if character in "\\%{}#$&^_":
                break
            raw.append(character)
        return self._normalize_revision_context("".join(raw), side="right")

    def _revision_context(self, start: int, end: int) -> tuple[str, str]:
        return self._left_revision_context(start), self._right_revision_context(end)

    @property
    def display_expectations(self) -> tuple[RevisionDisplayExpectation, ...]:
        return tuple(self._display_expectations)

    @property
    def display_degradations(self) -> tuple[RevisionDisplayDegradation, ...]:
        return tuple(self._display_degradations)

    def _project_text_range(
        self,
        start: int,
        limit: int,
    ) -> tuple[tuple[RevisionDisplaySegment, ...], int]:
        builder = _ProjectionBuilder()
        macro_instances = 0
        cursor = start
        while cursor < limit:
            character = self._text[cursor]
            if character == "%" and not _is_escaped(self._text, cursor):
                cursor = self._skip_comment(cursor, limit)
                continue
            if character == "\\":
                command_start = cursor
                command, cursor = _command(self._text, cursor)
                if command in _REVISION_ARITY:
                    segments, cursor, nested_instances = self._project_revision_macro(
                        command,
                        cursor,
                        limit,
                    )
                    builder.extend(segments)
                    macro_instances += nested_instances
                    continue
                if command in _LITERAL_TEXT_ESCAPES:
                    builder.append(command, strike=False)
                    continue
                if command in _SIMPLE_INLINE_FORMATTING:
                    content = self._mandatory_group(
                        cursor,
                        limit,
                        label=f"\\{command} inline text",
                    )
                    segments, nested_instances = self._project_text_range(
                        content.body_start,
                        content.body_end,
                    )
                    builder.extend(segments)
                    macro_instances += nested_instances
                    cursor = content.end
                    continue
                self._error(
                    "validated revision projection contains an unsupported command",
                    command_start,
                    unsafe_command=f"\\{command}" if command else "trailing backslash",
                )
            if character == "{":
                content = self._balanced(
                    cursor,
                    limit,
                    opening="{",
                    label="revision projection group",
                )
                segments, nested_instances = self._project_text_range(
                    content.body_start,
                    content.body_end,
                )
                builder.extend(segments)
                macro_instances += nested_instances
                cursor = content.end
                continue
            if character == "}" or character in _STRUCTURAL_TEXT_CHARACTERS:
                self._error(
                    "validated revision projection contains unsupported structured content",
                    cursor,
                    unsafe_token=character,
                )
            builder.append(character, strike=False)
            cursor += 1
        return builder.finish(), macro_instances

    def _project_revision_macro(
        self,
        command: str,
        index: int,
        limit: int,
    ) -> tuple[tuple[RevisionDisplaySegment, ...], int, int]:
        cursor = self._skip_space_and_comments(index, limit)
        if cursor < limit and self._text[cursor] == "[":
            option = self._balanced(
                cursor,
                limit,
                opening="[",
                label=f"\\{command} optional argument",
            )
            cursor = self._skip_space_and_comments(option.end, limit)

        arguments: list[_Group] = []
        for number in range(1, _REVISION_ARITY[command] + 1):
            group = self._mandatory_group(
                cursor,
                limit,
                label=f"\\{command} argument {number}",
            )
            arguments.append(group)
            cursor = group.end

        projected: list[tuple[tuple[RevisionDisplaySegment, ...], int]] = []
        for group in arguments:
            projected.append(self._project_text_range(group.body_start, group.body_end))

        builder = _ProjectionBuilder()
        if command == "replaced":
            builder.extend(projected[1][0], force_strike=True)
            builder.extend(projected[0][0])
        else:
            builder.extend(
                projected[0][0],
                force_strike=command in {"delete", "deleted"},
            )
        macro_instances = 1 + sum(item[1] for item in projected)
        return builder.finish(), cursor, macro_instances

    def _definition_target(self, index: int, limit: int, *, command: str) -> int:
        cursor = self._skip_space_and_comments(index, limit)
        if cursor < limit and self._text[cursor] == "{":
            return self._balanced(
                cursor,
                limit,
                opening="{",
                label=f"\\{command} target",
            ).end
        if cursor < limit and self._text[cursor] == "\\":
            _, end = _definition_command(
                self._text,
                cursor,
                xparse=command in _XPARSE_COMMAND_DEFINITIONS,
            )
            return end
        self._error(f"\\{command} target is missing", cursor)

    def _optional_groups(self, index: int, limit: int, *, maximum: int, label: str) -> int:
        cursor = self._skip_space_and_comments(index, limit)
        for _ in range(maximum):
            if cursor >= limit or self._text[cursor] != "[":
                return cursor
            cursor = self._balanced(
                cursor,
                limit,
                opening="[",
                label=label,
            ).end
            cursor = self._skip_space_and_comments(cursor, limit)
        if cursor < limit and self._text[cursor] == "[":
            self._error(f"{label} has too many optional groups", cursor)
        return cursor

    def _skip_definition(self, command: str, index: int, limit: int) -> int:
        cursor = self._skip_space_and_comments(index, limit)
        if cursor < limit and self._text[cursor] == "*":
            cursor = self._skip_space_and_comments(cursor + 1, limit)
        if command in _PRIMITIVE_DEFINITIONS:
            while cursor < limit and self._text[cursor] != "{":
                if self._text[cursor] == "%" and not _is_escaped(self._text, cursor):
                    cursor = self._skip_comment(cursor, limit)
                elif self._text[cursor] == "\\":
                    cursor = min(cursor + 2, limit)
                else:
                    cursor += 1
            return self._balanced(
                cursor,
                limit,
                opening="{",
                label=f"\\{command} body",
            ).end
        if command in _NEW_COMMAND_DEFINITIONS:
            cursor = self._definition_target(cursor, limit, command=command)
            cursor = self._optional_groups(
                cursor,
                limit,
                maximum=2,
                label=f"\\{command}",
            )
            return self._mandatory_group(
                cursor,
                limit,
                label=f"\\{command} body",
            ).end
        if command in _XPARSE_COMMAND_DEFINITIONS:
            cursor = self._definition_target(cursor, limit, command=command)
            for group_name in ("argument specification", "body"):
                cursor = self._mandatory_group(
                    cursor,
                    limit,
                    label=f"\\{command} {group_name}",
                ).end
            return cursor
        if command in _XPARSE_ENVIRONMENT_DEFINITIONS:
            cursor = self._mandatory_group(
                cursor,
                limit,
                label=f"\\{command} environment name",
            ).end
            for group_name in ("argument specification", "begin body", "end body"):
                cursor = self._mandatory_group(
                    cursor,
                    limit,
                    label=f"\\{command} {group_name}",
                ).end
            return cursor
        cursor = self._mandatory_group(
            cursor,
            limit,
            label=f"\\{command} environment name",
        ).end
        if command in {"newenvironment", "renewenvironment"}:
            cursor = self._optional_groups(
                cursor,
                limit,
                maximum=2,
                label=f"\\{command}",
            )
            for group_name in ("begin body", "end body"):
                cursor = self._mandatory_group(
                    cursor,
                    limit,
                    label=f"\\{command} {group_name}",
                ).end
            return cursor
        cursor = self._optional_groups(
            cursor,
            limit,
            maximum=1,
            label=f"\\{command}",
        )
        cursor = self._mandatory_group(
            cursor,
            limit,
            label=f"\\{command} title",
        ).end
        return self._optional_groups(
            cursor,
            limit,
            maximum=1,
            label=f"\\{command}",
        )

    def _skip_verb(self, index: int, limit: int) -> int:
        cursor = index
        if cursor < limit and self._text[cursor] == "*":
            cursor += 1
        if cursor >= limit or self._text[cursor] in "\r\n":
            self._error("\\verb delimiter is missing", cursor)
        delimiter = self._text[cursor]
        ending = self._text.find(delimiter, cursor + 1, limit)
        newline = self._text.find("\n", cursor + 1, limit)
        if ending < 0 or (newline >= 0 and newline < ending):
            self._error("\\verb body is unbalanced", cursor)
        return ending + 1

    def _skip_verbatim_environment(self, environment: str, index: int, limit: int) -> int:
        terminator = f"\\end{{{environment}}}"
        ending = self._text.find(terminator, index, limit)
        if ending < 0:
            self._error(
                "verbatim-like environment is unbalanced",
                index,
                environment=environment,
            )
        return ending + len(terminator)

    def _skip_group_conditional(self, index: int, limit: int, *, arguments: int) -> int:
        cursor = index
        for number in range(1, arguments + 1):
            cursor = self._mandatory_group(
                cursor,
                limit,
                label=f"dynamic conditional argument {number}",
            ).end
        return cursor

    def _skip_conditional(self, index: int, limit: int) -> int:
        depth = 1
        cursor = index
        while cursor < limit:
            character = self._text[cursor]
            if character == "%" and not _is_escaped(self._text, cursor):
                cursor = self._skip_comment(cursor, limit)
                continue
            if character != "\\":
                cursor += 1
                continue
            command, end = _command(self._text, cursor)
            if command == "verb":
                cursor = self._skip_verb(end, limit)
                continue
            if command in _GROUP_CONDITIONAL_ARGUMENTS:
                cursor = self._skip_group_conditional(
                    end,
                    limit,
                    arguments=_GROUP_CONDITIONAL_ARGUMENTS[command],
                )
                continue
            if command.startswith("if"):
                depth += 1
            elif command == "fi":
                depth -= 1
                if depth == 0:
                    return end
            cursor = end
        self._error("dynamic TeX conditional is unbalanced", index)

    def _parse_revision_macro(
        self,
        command: str,
        index: int,
        limit: int,
        *,
        nesting: int,
        command_start: int,
    ) -> int:
        if nesting >= _MAX_REVISION_MACRO_NESTING:
            self._error(
                "revision macro nesting exceeds the safety limit",
                index,
                max_nesting=_MAX_REVISION_MACRO_NESTING,
            )
        cursor = index
        if cursor < limit and self._text[cursor] == "*":
            self._error("revision macros do not support a starred form", cursor, command=command)
        has_optional_argument = False
        cursor = self._skip_space_and_comments(cursor, limit)
        if cursor < limit and self._text[cursor] == "[":
            has_optional_argument = True
            option = self._balanced(
                cursor,
                limit,
                opening="[",
                label=f"\\{command} optional argument",
            )
            if option.body_end - option.body_start > _MAX_OPTION_CHARACTERS:
                self._error(
                    "revision macro option exceeds the safety limit",
                    cursor,
                    command=command,
                    max_option_characters=_MAX_OPTION_CHARACTERS,
                )
            cursor = self._skip_space_and_comments(option.end, limit)
            if cursor < limit and self._text[cursor] == "[":
                self._error(
                    "revision macro has more than one optional argument",
                    cursor,
                    command=command,
                )

        arguments: list[_Group] = []
        for number in range(1, _REVISION_ARITY[command] + 1):
            group = self._mandatory_group(
                cursor,
                limit,
                label=f"\\{command} argument {number}",
            )
            arguments.append(group)
            cursor = group.end

        projection_issue: _RevisionProjectionUnsupported | None = None
        for number, group in enumerate(arguments, start=1):
            try:
                self._validate_revision_argument(
                    group,
                    revision_command=command,
                    argument_number=number,
                )
            except _RevisionProjectionUnsupported as exc:
                if projection_issue is None:
                    projection_issue = exc

        after = self._skip_space_and_comments(cursor, limit)
        if after < limit and self._text[after] in "[{":
            self._error(
                "revision macro has the wrong number of arguments",
                after,
                command=command,
                expected_mandatory_arguments=_REVISION_ARITY[command],
            )

        if nesting == 0:
            call_text = self._text[command_start:cursor]
            call_sha256 = sha256_bytes(call_text.encode("utf-8"))
            if projection_issue is None:
                segments, projected_end, macro_instances = self._project_revision_macro(
                    command,
                    index,
                    limit,
                )
                if projected_end != cursor:
                    self._error(
                        "revision display projection did not consume the parsed source call",
                        command_start,
                        command=command,
                    )
                if segments:
                    left_context, right_context = self._revision_context(command_start, cursor)
                    self._display_expectations.append(
                        RevisionDisplayExpectation(
                            source_path=self._path,
                            source_character_offset=command_start,
                            source_call_sha256=call_sha256,
                            segments=segments,
                            macro_instances=macro_instances,
                            left_context=left_context,
                            right_context=right_context,
                        )
                    )
                else:
                    projection_issue = _RevisionProjectionUnsupported(
                        command_start,
                        "empty",
                        "empty-visible-projection",
                    )
            if projection_issue is not None:
                issue_line, issue_column = self._line_column(command_start)
                end_line, end_column = self._line_column(cursor)
                self._display_degradations.append(
                    RevisionDisplayDegradation(
                        source_path=self._path,
                        source_character_offset=command_start,
                        source_character_end=cursor,
                        source_call_sha256=call_sha256,
                        line=issue_line,
                        column=issue_column,
                        end_line=end_line,
                        end_column=end_column,
                        command=command,
                        reason=projection_issue.reason,
                        token=projection_issue.token,
                    )
                )

        line, column = self._line_column(command_start)
        call = _AliasCall(
            alias=command,
            has_optional_argument=has_optional_argument,
            path=self._path,
            character_offset=command_start,
            line=line,
            column=column,
        )
        if command in _ALIAS_ARITY:
            self._counts.alias_calls.append(call)
        else:
            self._counts.canonical_calls.append(call)
        self._counts.record(command)
        if self._counts.total > _MAX_REVISION_MACRO_OCCURRENCES:
            self._error(
                "revision macro occurrence count exceeds the safety limit",
                index,
                max_occurrences=_MAX_REVISION_MACRO_OCCURRENCES,
            )
        for group in arguments:
            self._scan_range(
                group.body_start,
                group.body_end,
                active=True,
                document_gated=False,
                nesting=nesting + 1,
            )
        return after

    def _scan_range(
        self,
        start: int,
        end: int,
        *,
        active: bool,
        document_gated: bool,
        nesting: int,
    ) -> bool:
        cursor = start
        while cursor < end:
            character = self._text[cursor]
            if character == "%" and not _is_escaped(self._text, cursor):
                cursor = self._skip_comment(cursor, end)
                continue
            if character != "\\":
                cursor += 1
                continue
            command_start = cursor
            command, cursor = _command(self._text, cursor)
            if command == "endinput":
                return active
            if command in _ALIAS_ASSIGNMENT_DEFINITIONS:
                _, _, cursor = self._definition_target_info(
                    cursor,
                    end,
                    command=command,
                )
                cursor = self._skip_space_and_comments(cursor, end)
                if cursor < end and self._text[cursor] == "=":
                    cursor = self._skip_space_and_comments(cursor + 1, end)
                if cursor < end and self._text[cursor] == "\\":
                    _, cursor = _command(self._text, cursor)
                elif cursor < end:
                    cursor += 1
                continue
            if command in (
                _NEW_COMMAND_DEFINITIONS
                | _XPARSE_COMMAND_DEFINITIONS
                | _XPARSE_ENVIRONMENT_DEFINITIONS
                | _ENVIRONMENT_DEFINITIONS
                | _PRIMITIVE_DEFINITIONS
            ):
                cursor = self._skip_definition(command, cursor, end)
                continue
            if command == "verb":
                cursor = self._skip_verb(cursor, end)
                continue
            conditional_arguments = _GROUP_CONDITIONAL_ARGUMENTS.get(command)
            if conditional_arguments is not None:
                self._counts.skipped_dynamic_regions += 1
                cursor = self._skip_group_conditional(
                    cursor,
                    end,
                    arguments=conditional_arguments,
                )
                continue
            if command.startswith("if"):
                self._counts.skipped_dynamic_regions += 1
                cursor = self._skip_conditional(cursor, end)
                continue
            if command in {"begin", "end"}:
                group = self._mandatory_group(
                    cursor,
                    end,
                    label=f"\\{command} environment name",
                )
                environment = self._text[group.body_start : group.body_end].strip()
                cursor = group.end
                if command == "begin" and environment in _VERBATIM_ENVIRONMENTS:
                    cursor = self._skip_verbatim_environment(environment, cursor, end)
                    continue
                if document_gated and environment == "document":
                    active = command == "begin"
                continue
            if active and command in _REVISION_ARITY:
                cursor = self._parse_revision_macro(
                    command,
                    cursor,
                    end,
                    nesting=nesting,
                    command_start=command_start,
                )
                continue
            if cursor <= command_start:
                self._error("revision macro scanner did not advance", command_start)
        return active


def _require_discovery_binding(source_root: Path, discovery: ProjectDiscovery) -> None:
    current = discover_project(source_root, main_document=discovery.main_document)
    if current != discovery:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source discovery binding changed before revision macro inventory",
        )


def _read_bound_source(source_root: Path, source_file: SourceFile) -> str:
    path = resolve_within(source_root, source_file.path)
    data = read_stable_bytes(path, max_bytes=max(1, source_file.size_bytes))
    if len(data) != source_file.size_bytes or sha256_bytes(data) != source_file.sha256:
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "LaTeX source changed while scanning revision macros",
            details={"path": source_file.path},
        )
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "revision macro source must be valid UTF-8",
            details={"path": source_file.path},
        ) from exc


def _raise_alias_source_error(
    message: str,
    *,
    alias: str,
    location: _AliasCall | _AliasSource,
    **details: object,
) -> NoReturn:
    raise ContractError(
        ErrorCode.SCHEMA_INVALID,
        message,
        details={
            "path": location.path,
            "character_offset": location.character_offset,
            "line": location.line,
            "column": location.column,
            "alias": f"\\{alias}",
            **details,
        },
    )


def _validate_alias_sources(counts: _MutableCounts) -> None:
    for alias in ("add", "delete"):
        calls = [call for call in counts.alias_calls if call.alias == alias]
        if not calls:
            continue
        sources = [source for source in counts.alias_sources if source.alias == alias]
        package_sources = [
            source
            for source in sources
            if source.proven and source.source_kind == "trackchanges_package"
        ]
        if alias == "add" and package_sources and counts.local_trackchanges_package_paths:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "static trackchanges package declaration is shadowed by a "
                "project-local trackchanges.sty",
                details={
                    "path": counts.local_trackchanges_package_paths[0],
                    "character_offset": 0,
                    "line": 1,
                    "column": 1,
                    "alias": r"\add",
                    "local_package_paths": sorted(set(counts.local_trackchanges_package_paths)),
                },
            )
        if alias == "add" and package_sources and counts.package_search_overrides:
            override = counts.package_search_overrides[0]
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "static trackchanges declaration is unsafe after a package search path override",
                details={
                    "path": override.path,
                    "character_offset": override.character_offset,
                    "line": override.line,
                    "column": override.column,
                    "alias": r"\add",
                    "definition_command": override.definition_command,
                    "source_kind": override.source_kind,
                    "search_control_sequence": r"\input@path",
                },
            )
        conflicts = [source for source in sources if not source.proven]
        if conflicts:
            conflict = conflicts[0]
            _raise_alias_source_error(
                "revision alias conflicts with a source-defined command",
                alias=alias,
                location=conflict,
                definition_command=conflict.definition_command,
                source_kind=conflict.source_kind,
                source_count=len(sources),
            )
        proven_sources = [source for source in sources if source.proven]
        if not proven_sources:
            _raise_alias_source_error(
                "revision alias has no accepted static semantic source",
                alias=alias,
                location=calls[0],
                accepted_source=(
                    "a static trackchanges declaration or a direct \\added wrapper"
                    if alias == "add"
                    else "a direct \\deleted wrapper"
                ),
            )
        if len(proven_sources) != 1:
            _raise_alias_source_error(
                "revision alias has multiple semantic sources",
                alias=alias,
                location=proven_sources[0],
                source_count=len(proven_sources),
                source_kinds=[source.source_kind for source in proven_sources],
            )
        source = proven_sources[0]
        optional_calls = [call for call in calls if call.has_optional_argument]
        if optional_calls and not source.allows_optional_argument:
            _raise_alias_source_error(
                "revision alias optional argument is not supported by its accepted static source",
                alias=alias,
                location=optional_calls[0],
                definition_command=source.definition_command,
                source_kind=source.source_kind,
            )


def _validate_canonical_sources(counts: _MutableCounts) -> None:
    wrapper_aliases = {"added": "add", "deleted": "delete"}
    for command_name in _CANONICAL_ARITY:
        requirements: list[_AliasCall | _AliasSource] = [
            call for call in counts.canonical_calls if call.alias == command_name
        ]
        alias = wrapper_aliases.get(command_name)
        if alias and any(call.alias == alias for call in counts.alias_calls):
            requirements.extend(
                source
                for source in counts.alias_sources
                if source.alias == alias and source.proven and source.source_kind == "local_wrapper"
            )
        if not requirements:
            continue
        if counts.local_changes_package_paths:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "static changes package declaration is shadowed by a project-local changes.sty",
                details={
                    "path": counts.local_changes_package_paths[0],
                    "character_offset": 0,
                    "line": 1,
                    "column": 1,
                    "command": f"\\{command_name}",
                    "local_package_paths": sorted(set(counts.local_changes_package_paths)),
                },
            )
        sources = [source for source in counts.canonical_sources if source.alias == command_name]
        static_package_sources = [
            source
            for source in sources
            if source.proven and source.source_kind == "changes_package"
        ]
        if static_package_sources and counts.package_search_overrides:
            override = counts.package_search_overrides[0]
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "static changes declaration is unsafe after a package search path override",
                details={
                    "path": override.path,
                    "character_offset": override.character_offset,
                    "line": override.line,
                    "column": override.column,
                    "command": f"\\{command_name}",
                    "definition_command": override.definition_command,
                    "source_kind": override.source_kind,
                    "search_control_sequence": r"\input@path",
                },
            )
        conflicts = [source for source in sources if not source.proven]
        if conflicts:
            conflict = conflicts[0]
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "canonical revision command conflicts with a source-defined command",
                details={
                    "path": conflict.path,
                    "character_offset": conflict.character_offset,
                    "line": conflict.line,
                    "column": conflict.column,
                    "command": f"\\{command_name}",
                    "definition_command": conflict.definition_command,
                    "source_kind": conflict.source_kind,
                    "source_count": len(sources),
                },
            )
        if len(static_package_sources) == 1:
            continue
        if len(static_package_sources) > 1:
            source = static_package_sources[0]
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "canonical revision command has multiple static changes package declarations",
                details={
                    "path": source.path,
                    "character_offset": source.character_offset,
                    "line": source.line,
                    "column": source.column,
                    "command": f"\\{command_name}",
                    "source_count": len(static_package_sources),
                    "source_paths": [item.path for item in static_package_sources],
                },
            )
        location = requirements[0]
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "canonical revision command has no accepted static changes package declaration",
            details={
                "path": location.path,
                "character_offset": location.character_offset,
                "line": location.line,
                "column": location.column,
                "command": f"\\{command_name}",
                "accepted_source": "a static \\usepackage{changes} or \\RequirePackage{changes}",
            },
        )


def scan_revision_macros(
    source_root: Path,
    discovery: ProjectDiscovery,
) -> RevisionMacroInventory:
    """Inventory active, statically recognizable revision macros.

    The caller supplies a previously sealed discovery.  Every selected file is
    hash-checked before decoding, and the complete discovery is checked both
    before and after the bounded scan.
    """

    if discovery.external_references:
        raise ContractError(
            ErrorCode.PATH_TRAVERSAL,
            "blocked discovery cannot be scanned for revision macros",
        )
    _require_discovery_binding(source_root, discovery)
    counts = _MutableCounts()
    tex_files = 0
    display_expectations: list[RevisionDisplayExpectation] = []
    display_degradations: list[RevisionDisplayDegradation] = []
    for source_file in sorted(discovery.files, key=lambda item: item.path):
        if source_file.role not in {"class", "style", "tex"}:
            continue
        if source_file.role == "style":
            package_name = source_file.path.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            if package_name == "changes.sty":
                counts.local_changes_package_paths.append(source_file.path)
            elif package_name == "trackchanges.sty":
                counts.local_trackchanges_package_paths.append(source_file.path)
        text = _read_bound_source(source_root, source_file)
        scanner = _FileScanner(text, source_file.path, counts)
        scanner.audit_alias_sources()
        if source_file.role == "tex":
            tex_files += 1
            scanner.scan(main_document=source_file.path == discovery.main_document)
            display_expectations.extend(scanner.display_expectations)
            display_degradations.extend(scanner.display_degradations)
    _require_discovery_binding(source_root, discovery)
    _validate_alias_sources(counts)
    _validate_canonical_sources(counts)
    covered_instances = sum(expectation.macro_instances for expectation in display_expectations)
    degraded_instances = counts.total - covered_instances
    if (
        degraded_instances < 0
        or (degraded_instances > 0 and not display_degradations)
        or (degraded_instances == 0 and display_degradations)
    ):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "revision display evidence does not cover the source inventory",
        )
    return RevisionMacroInventory(
        source_tree_sha256=discovery.source_tree_sha256,
        tex_files=tex_files,
        canonical_counts=CanonicalRevisionMacroCounts(
            added=counts.added,
            deleted=counts.deleted,
            replaced=counts.replaced,
        ),
        alias_counts=AliasRevisionMacroCounts(
            add=counts.add,
            delete=counts.delete,
        ),
        skipped_dynamic_regions=counts.skipped_dynamic_regions,
        display_expectations=tuple(display_expectations),
        display_degradations=tuple(display_degradations),
    )


def _injection_block(
    mode: Literal["clean", "display"],
    *,
    aliases: tuple[Literal["add", "delete"], ...],
) -> str:
    available = _CLEAN_DEFINITIONS if mode == "clean" else _DISPLAY_DEFINITIONS
    alias_definitions = {"add": available[3], "delete": available[4]}
    definitions = (*available[:3], *(alias_definitions[alias] for alias in aliases))
    alias_selector = ",".join(aliases) or "none"
    return "\n".join(
        (
            (
                f"{_INJECTION_MARKER_PREFIX}begin "
                f"profile={REVISION_MACRO_PROFILE_ID} mode={mode} aliases={alias_selector}"
            ),
            *definitions,
            f"{_INJECTION_MARKER_PREFIX}end profile={REVISION_MACRO_PROFILE_ID}",
            "",
        )
    )


def inject_revision_macros(
    source: str,
    mode: RevisionMacroMode,
    *,
    aliases: tuple[Literal["add", "delete"], ...] = (),
) -> str:
    """Return source unchanged, or append one deterministic tex2word view.

    ``clean`` keeps only the current text.  ``display`` uses tex2word's
    ``\\textcolor{blue}`` (``0000FF``) and ``\\sout`` support, with replacement
    old text immediately before new text.  No file is read or written.
    """

    if aliases not in {(), ("add",), ("delete",), ("add", "delete")}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid revision macro aliases")
    if mode == "source":
        if aliases:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "source revision view cannot redefine aliases",
            )
        return source
    if mode not in {"clean", "display"}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid revision macro view mode")
    block = _injection_block(mode, aliases=aliases)
    if source.endswith(block):
        return source
    if _INJECTION_MARKER_PREFIX in source:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "source already contains a reserved revision macro injection marker",
        )
    separator = "" if not source or source.endswith(("\n", "\r")) else "\n"
    return f"{source}{separator}{block}"


__all__ = [
    "AliasRevisionMacroCounts",
    "CanonicalRevisionMacroCounts",
    "REVISION_MACRO_PROFILE_ID",
    "REVISION_MACRO_PROFILE_NAME",
    "REVISION_MACRO_PROFILE_VERSION",
    "RevisionDisplayExpectation",
    "RevisionDisplayDegradation",
    "RevisionDisplaySegment",
    "RevisionMacroInventory",
    "RevisionMacroMode",
    "inject_revision_macros",
    "scan_revision_macros",
]
