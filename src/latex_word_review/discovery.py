"""Conservative, bounded discovery for a LaTeX project snapshot."""

from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from latex_word_review.canonical import sha256_canonical
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.graphic_targets import (
    is_dynamic_graphic_target_error,
    normalize_static_graphic_target,
)
from latex_word_review.hashing import FileDigest, TreeEntry, digest_bytes, read_stable_bytes
from latex_word_review.paths import resolve_within, validate_relative_path

_INPUT_RE = re.compile(r"\\(input|include)\s*\{([^{}]+)\}")
_BIB_RE = re.compile(r"\\(bibliography|addbibresource)(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}")
_GRAPHIC_RE = re.compile(r"\\includegraphics\*?(?:\s*\[[^\]]*\])?\s*\{((?:\{[^{}]+\})|[^{}]+)\}")
_GRAPHIC_COMMAND_RE = re.compile(r"\\includegraphics\b")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^{}]+\}\s*)+)\}")
_INNER_BRACE_RE = re.compile(r"\{([^{}]+)\}")
_ENGINE_RE = re.compile(r"^\s*%\s*!TeX\s+program\s*=\s*([^\s]+)\s*$", re.IGNORECASE)
_DOCUMENT_CLASS_RE = re.compile(r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{")
_BEGIN_DOCUMENT_RE = re.compile(r"\\begin\s*\{document\}")

_GRAPHIC_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg")
_RUNTIME_TEXT_EXTENSIONS = frozenset(
    {
        ".bbx",
        ".bst",
        ".cbx",
        ".cfg",
        ".clo",
        ".cls",
        ".def",
        ".fd",
        ".lbx",
        ".ltx",
        ".sty",
        ".tex",
    }
)
_STYLE_EXTENSIONS = _RUNTIME_TEXT_EXTENSIONS - {".cls", ".tex"}
_DYNAMIC_REFERENCE_TOKENS = ("\\", "#", "$", "~", "{", "}")
_RUNTIME_COMMANDS: dict[str, tuple[str, str | None, bool, bool]] = {
    # command: (dependency kind, required extension, comma-separated, optional probe)
    "documentclass": ("class", ".cls", False, False),
    "LoadClass": ("class", ".cls", False, False),
    "LoadClassWithOptions": ("class", ".cls", False, False),
    "usepackage": ("package", ".sty", True, False),
    "RequirePackage": ("package", ".sty", True, False),
    "RequirePackageWithOptions": ("package", ".sty", True, False),
    "bibliographystyle": ("bibliography", ".bst", False, False),
    "IfFileExists": ("other", None, False, True),
    "InputIfFileExists": ("other", None, False, True),
}
_MEDIA_TYPES = {
    ".bbx": "text/x-tex",
    ".bib": "text/x-bibtex",
    ".bst": "text/x-bibtex-style",
    ".cbx": "text/x-tex",
    ".cfg": "text/x-tex",
    ".clo": "text/x-tex",
    ".cls": "text/x-tex",
    ".def": "text/x-tex",
    ".eps": "application/postscript",
    ".fd": "text/x-tex",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".lbx": "text/x-tex",
    ".ltx": "text/x-tex",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".sty": "text/x-tex",
    ".svg": "image/svg+xml",
    ".tex": "text/x-tex",
}


@dataclass(frozen=True, slots=True)
class DiscoveryLimits:
    max_files: int = 2048
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_dependencies_per_file: int = 256

    def __post_init__(self) -> None:
        if (
            min(
                self.max_files,
                self.max_file_bytes,
                self.max_total_bytes,
                self.max_dependencies_per_file,
            )
            <= 0
        ):
            raise ContractError(ErrorCode.SCHEMA_INVALID, "discovery limits must be positive")


DEFAULT_DISCOVERY_LIMITS = DiscoveryLimits()


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    role: str
    media_type: str
    size_bytes: int
    sha256: str
    encoding: str | None
    newline: str | None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "path": self.path,
            "role": self.role,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "encoding": self.encoding,
            "newline": self.newline,
        }

    def as_tree_entry(self) -> TreeEntry:
        return TreeEntry(self.path, self.role, self.size_bytes, self.sha256)


@dataclass(frozen=True, slots=True, order=True)
class DependencyEdge:
    source: str
    target: str
    kind: str

    def as_dict(self) -> dict[str, str]:
        return {"from": self.source, "to": self.target, "kind": self.kind}


@dataclass(frozen=True, slots=True, order=True)
class ExternalReference:
    reference: str
    kind: str
    reason: str
    status: str = "rejected"

    def as_dict(self) -> dict[str, str]:
        return {
            "reference": self.reference,
            "kind": self.kind,
            "reason": self.reason,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True, order=True)
class EngineHint:
    engine: str
    source: str
    value: str

    def as_dict(self) -> dict[str, str]:
        return {"engine": self.engine, "source": self.source, "value": self.value}


@dataclass(frozen=True, slots=True)
class _RuntimeReference:
    kind: str
    reference: str
    extension: str | None
    optional: bool


@dataclass(frozen=True, slots=True)
class ProjectDiscovery:
    main_document: str
    files: tuple[SourceFile, ...]
    dependency_edges: tuple[DependencyEdge, ...]
    external_references: tuple[ExternalReference, ...]
    engine_hints: tuple[EngineHint, ...]
    source_tree_sha256: str
    profile_sha256: str

    @property
    def blocked(self) -> bool:
        return bool(self.external_references)


@dataclass(slots=True)
class _DiscoveryState:
    root: Path
    limits: DiscoveryLimits
    files: dict[str, SourceFile]
    edges: set[DependencyEdge]
    external: set[ExternalReference]
    engine_hints: set[EngineHint]
    directory_entries: dict[str, tuple[Path, ...]]
    total_bytes: int = 0

    def add_file(self, source_file: SourceFile) -> None:
        if source_file.path in self.files:
            return
        if len(self.files) >= self.limits.max_files:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "project exceeds the file count limit")
        if self.total_bytes + source_file.size_bytes > self.limits.max_total_bytes:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "project exceeds the byte limit")
        self.files[source_file.path] = source_file
        self.total_bytes += source_file.size_bytes


def _strip_comments(text: str) -> str:
    stripped: list[str] = []
    for line in text.splitlines(keepends=True):
        comment_at: int | None = None
        for index, character in enumerate(line):
            if character != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                comment_at = index
                break
        stripped.append(line if comment_at is None else line[:comment_at] + "\n")
    return "".join(stripped)


def _newline_style(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    without_crlf = data.replace(b"\r\n", b"")
    lf = without_crlf.count(b"\n")
    cr = without_crlf.count(b"\r")
    if crlf and not lf and not cr:
        return "crlf"
    if lf and not crlf and not cr:
        return "lf"
    if crlf or lf or cr:
        return "mixed"
    return "none"


def _role_for_path(path: str) -> str:
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix == ".tex":
        return "tex"
    if suffix == ".cls":
        return "class"
    if suffix in _STYLE_EXTENSIONS:
        return "style"
    if suffix == ".bib":
        return "bib"
    if suffix in _GRAPHIC_EXTENSIONS:
        return "image"
    return "other"


def _safe_reference_label(reference: str) -> str:
    try:
        return validate_relative_path(reference)
    except ContractError:
        fingerprint = hashlib.sha256(reference.encode("utf-8", errors="surrogatepass")).hexdigest()
        return f"unsafe-path:{fingerprint[:16]}"


def _strip_current_directory_prefix(reference: str) -> str:
    """Remove only exact, repeated TeX ``./`` prefixes before validation."""

    while reference.startswith("./"):
        reference = reference[2:]
    return reference


def _is_dynamic_reference(reference: str) -> bool:
    return any(token in reference for token in _DYNAMIC_REFERENCE_TOKENS)


def _graphic_references(
    text: str,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], int]:
    """Extract static graphics and surface every unhandled command as rejected."""

    matches = tuple(_GRAPHIC_RE.finditer(text))
    matched_starts = {match.start() for match in matches}
    commands = tuple(_GRAPHIC_COMMAND_RE.finditer(text))
    references: list[str] = []
    rejected: list[tuple[str, str]] = []
    for match in matches:
        raw_reference = match.group(1)
        try:
            references.append(normalize_static_graphic_target(raw_reference))
        except ContractError as error:
            reason = (
                "dynamic_or_unsafe" if is_dynamic_graphic_target_error(error) else "path_rejected"
            )
            rejected.append((raw_reference, reason))
    for command in commands:
        if command.start() not in matched_starts:
            rejected.append((command.group(0), "dynamic_or_unsafe"))
    return tuple(references), tuple(rejected), len(commands)


def _is_link_or_junction(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_probe is not None and junction_probe())


def _directory_entries(state: _DiscoveryState, directory: Path, relative: str) -> tuple[Path, ...]:
    cached = state.directory_entries.get(relative)
    if cached is not None:
        return cached
    try:
        entries: list[Path] = []
        for entry in directory.iterdir():
            if len(entries) >= state.limits.max_files:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "project directory enumeration exceeds the file count limit",
                )
            entries.append(entry)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "project directory cannot be enumerated safely",
        ) from exc
    result = tuple(sorted(entries, key=lambda item: (item.name.casefold(), item.name)))
    state.directory_entries[relative] = result
    return result


def _casefold_existing_file(state: _DiscoveryState, relative_path: str) -> str | None:
    """Resolve one local file using deterministic Windows case semantics."""

    normalized = validate_relative_path(relative_path)
    parts = normalized.split("/")
    current = state.root
    actual_parts: list[str] = []
    for index, part in enumerate(parts):
        parent_relative = "/".join(actual_parts)
        matches = [
            entry
            for entry in _directory_entries(state, current, parent_relative)
            if entry.name.casefold() == part.casefold()
        ]
        if len(matches) > 1:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "project path is ambiguous under Windows case-insensitive semantics",
            )
        if not matches:
            return None
        match = matches[0]
        try:
            if _is_link_or_junction(match):
                raise ContractError(
                    ErrorCode.PATH_LINK_ESCAPE,
                    "project dependency crosses a link or junction",
                )
            is_last = index == len(parts) - 1
            if (is_last and not match.is_file()) or (not is_last and not match.is_dir()):
                return None
        except ContractError:
            raise
        except OSError as exc:
            raise ContractError(
                ErrorCode.PATH_LINK_ESCAPE,
                "project dependency type cannot be inspected safely",
            ) from exc
        actual_parts.append(match.name)
        current = match
    actual = validate_relative_path("/".join(actual_parts))
    resolve_within(state.root, actual)
    return actual


def _read_source_file(
    path: Path,
    relative_path: str,
    limits: DiscoveryLimits,
) -> tuple[str, SourceFile]:
    suffix = PurePosixPath(relative_path).suffix.casefold()
    if suffix not in _RUNTIME_TEXT_EXTENSIONS:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "unsupported LaTeX runtime text file")
    data = read_stable_bytes(path, max_bytes=limits.max_file_bytes)
    digest = digest_bytes(data)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "LaTeX source and local runtime text files must be valid UTF-8",
        ) from exc
    source_file = SourceFile(
        path=relative_path,
        role=_role_for_path(relative_path),
        media_type=_MEDIA_TYPES[suffix],
        size_bytes=digest.size_bytes,
        sha256=digest.sha256,
        encoding="utf-8",
        newline=_newline_style(data),
    )
    return text, source_file


def _read_binary_file(path: Path, relative_path: str, limits: DiscoveryLimits) -> SourceFile:
    data = read_stable_bytes(path, max_bytes=limits.max_file_bytes)
    digest: FileDigest = digest_bytes(data)
    suffix = PurePosixPath(relative_path).suffix.casefold()
    if suffix == ".bib":
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "v0.1 bibliography files must be valid UTF-8",
            ) from exc
    return SourceFile(
        path=relative_path,
        role=_role_for_path(relative_path),
        media_type=_MEDIA_TYPES.get(suffix, "application/octet-stream"),
        size_bytes=digest.size_bytes,
        sha256=digest.sha256,
        encoding="utf-8" if suffix == ".bib" else None,
        newline=_newline_style(data) if suffix == ".bib" else None,
    )


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _balanced_group(
    text: str,
    index: int,
    *,
    opening: str,
    closing: str,
) -> tuple[str, int]:
    if index >= len(text) or text[index] != opening:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "runtime command argument is missing")
    depth = 1
    cursor = index + 1
    while cursor < len(text):
        character = text[cursor]
        if character == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return text[index + 1 : cursor], cursor + 1
        cursor += 1
    raise ContractError(ErrorCode.SCHEMA_INVALID, "runtime command argument is unbalanced")


def _runtime_references(
    text: str,
) -> tuple[tuple[_RuntimeReference, ...], tuple[ExternalReference, ...], int]:
    references: list[_RuntimeReference] = []
    rejected: list[ExternalReference] = []
    reference_count = 0
    cursor = 0
    while cursor < len(text):
        command_start = text.find("\\", cursor)
        if command_start < 0:
            break
        preceding_backslashes = 0
        preceding_at = command_start - 1
        while preceding_at >= 0 and text[preceding_at] == "\\":
            preceding_backslashes += 1
            preceding_at -= 1
        if preceding_backslashes % 2 == 1:
            cursor = command_start + 1
            continue
        name_end = command_start + 1
        while name_end < len(text) and (
            text[name_end].isascii() and (text[name_end].isalpha() or text[name_end] == "@")
        ):
            name_end += 1
        command = text[command_start + 1 : name_end]
        specification = _RUNTIME_COMMANDS.get(command)
        cursor = max(name_end, command_start + 1)
        if specification is None:
            continue
        kind, extension, comma_separated, optional_probe = specification
        argument_at = _skip_whitespace(text, name_end)
        if argument_at < len(text) and text[argument_at] == "[":
            _, argument_at = _balanced_group(
                text,
                argument_at,
                opening="[",
                closing="]",
            )
            argument_at = _skip_whitespace(text, argument_at)
        if argument_at >= len(text) or text[argument_at] != "{":
            continue
        argument, cursor = _balanced_group(
            text,
            argument_at,
            opening="{",
            closing="}",
        )
        values = argument.split(",") if comma_separated else [argument]
        for value in values:
            reference_count += 1
            reference = value.strip()
            if not reference:
                if not optional_probe:
                    rejected.append(
                        ExternalReference(
                            _safe_reference_label(reference),
                            kind,
                            "dynamic_or_unsafe",
                        )
                    )
                continue
            if _is_dynamic_reference(reference):
                # Optional probes commonly use TeX macros to query system
                # package data.  They are outside the static local closure;
                # required runtime loads remain fail-closed.
                if not optional_probe:
                    rejected.append(
                        ExternalReference(
                            _safe_reference_label(reference),
                            kind,
                            "dynamic_or_unsafe",
                        )
                    )
                continue
            references.append(_RuntimeReference(kind, reference, extension, optional_probe))
    return tuple(references), tuple(rejected), reference_count


def _candidate_paths(
    reference: str,
    *,
    kind: str,
    graphic_directories: tuple[str, ...],
) -> tuple[str, ...]:
    suffixes: tuple[str, ...]
    if kind in {"input", "include"}:
        suffixes = ("",) if PurePosixPath(reference).suffix else (".tex",)
    elif kind == "bibliography":
        suffixes = ("",) if PurePosixPath(reference).suffix else (".bib",)
    else:
        graphic_suffix = PurePosixPath(reference).suffix.casefold()
        if graphic_suffix in _GRAPHIC_EXTENSIONS:
            suffixes = ("",)
        elif graphic_suffix:
            # A dot in a graphic basename is not necessarily a file extension
            # (for example, ``response.v2`` may resolve to
            # ``response.v2.pdf``).  Preserve the exact-name probe for formats
            # outside the built-in list, then apply TeX-style extension search.
            suffixes = ("", *_GRAPHIC_EXTENSIONS)
        else:
            suffixes = _GRAPHIC_EXTENSIONS

    bases: tuple[str, ...] = ("",)
    if kind == "graphic":
        bases = ("", *graphic_directories)
    candidates: list[str] = []
    for search_base in bases:
        for suffix in suffixes:
            referenced_path = f"{reference}{suffix}"
            if search_base:
                candidates.append(validate_relative_path(f"{search_base}/{referenced_path}"))
            else:
                # TeX resolves ordinary \input/\includegraphics paths from the
                # main process working directory, which our adapters pin to the
                # project source root.  It does not implicitly switch cwd when
                # processing an included file.
                candidates.append(validate_relative_path(referenced_path))
    return tuple(dict.fromkeys(candidates))


def _resolve_dependency(
    state: _DiscoveryState,
    *,
    source_path: str,
    reference: str,
    kind: str,
    graphic_directories: tuple[str, ...],
) -> str | None:
    raw_reference = reference.strip()
    if not raw_reference or any(token in raw_reference for token in ("\\", "#", "$", "~")):
        state.external.add(
            ExternalReference(_safe_reference_label(raw_reference), kind, "dynamic_or_unsafe")
        )
        return None
    normalized_reference = _strip_current_directory_prefix(raw_reference)
    if not normalized_reference:
        state.external.add(
            ExternalReference(_safe_reference_label(raw_reference), kind, "path_rejected")
        )
        return None
    try:
        candidates = _candidate_paths(
            normalized_reference,
            kind=kind,
            graphic_directories=graphic_directories,
        )
    except ContractError:
        state.external.add(
            ExternalReference(_safe_reference_label(normalized_reference), kind, "path_rejected")
        )
        return None

    matches: list[str] = []
    for candidate in candidates:
        try:
            actual = _casefold_existing_file(state, candidate)
        except ContractError as exc:
            if exc.code is not ErrorCode.PATH_LINK_ESCAPE:
                raise
            state.external.add(
                ExternalReference(_safe_reference_label(candidate), kind, "link_escape")
            )
            return None
        if actual is not None:
            matches.append(actual)
    if len(matches) == 1:
        return matches[0]
    reason = "missing" if not matches else "ambiguous"
    state.external.add(ExternalReference(_safe_reference_label(normalized_reference), kind, reason))
    return None


def _resolve_runtime_dependency(
    state: _DiscoveryState,
    *,
    reference: _RuntimeReference,
) -> str | None:
    raw_reference = reference.reference.strip()
    if not raw_reference or _is_dynamic_reference(raw_reference):
        state.external.add(
            ExternalReference(
                _safe_reference_label(raw_reference),
                reference.kind,
                "dynamic_or_unsafe",
            )
        )
        return None
    normalized_reference = _strip_current_directory_prefix(raw_reference)
    if not normalized_reference:
        state.external.add(
            ExternalReference(
                _safe_reference_label(raw_reference),
                reference.kind,
                "path_rejected",
            )
        )
        return None
    try:
        candidate = validate_relative_path(normalized_reference)
        if reference.extension is not None and not PurePosixPath(candidate).suffix:
            candidate = validate_relative_path(f"{candidate}{reference.extension}")
    except ContractError:
        state.external.add(
            ExternalReference(
                _safe_reference_label(normalized_reference),
                reference.kind,
                "path_rejected",
            )
        )
        return None
    try:
        target = _casefold_existing_file(state, candidate)
    except ContractError as exc:
        if exc.code is not ErrorCode.PATH_LINK_ESCAPE:
            raise
        state.external.add(
            ExternalReference(_safe_reference_label(candidate), reference.kind, "link_escape")
        )
        return None
    if target is not None:
        return target
    suffix = PurePosixPath(normalized_reference).suffix.casefold()
    explicitly_local = "/" in raw_reference or suffix in {".bst", ".cls", ".sty"}
    if not reference.optional and explicitly_local:
        state.external.add(
            ExternalReference(
                _safe_reference_label(normalized_reference),
                reference.kind,
                "missing",
            )
        )
    return None


def _select_main(root: Path, explicit: str | None, limits: DiscoveryLimits) -> str:
    if explicit is not None:
        normalized = validate_relative_path(explicit)
        resolved = resolve_within(root, normalized)
        if not resolved.is_file() or PurePosixPath(normalized).suffix.casefold() != ".tex":
            raise ContractError(ErrorCode.SCHEMA_INVALID, "main document must be a TeX file")
        return normalized

    conventional = root / "main.tex"
    if conventional.is_file():
        resolve_within(root, "main.tex")
        return "main.tex"

    candidates: list[str] = []
    try:
        top_level: list[Path] = []
        for item in root.iterdir():
            if len(top_level) >= limits.max_files:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "project root enumeration exceeds the file count limit",
                )
            top_level.append(item)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "project root cannot be enumerated") from exc
    for item in sorted(top_level, key=lambda path: path.name):
        if item.suffix.casefold() != ".tex" or not item.is_file():
            continue
        relative = validate_relative_path(item.name)
        resolved = resolve_within(root, relative)
        text, _ = _read_source_file(resolved, relative, limits)
        visible = _strip_comments(text)
        if _DOCUMENT_CLASS_RE.search(visible) and _BEGIN_DOCUMENT_RE.search(visible):
            candidates.append(relative)
        if len(candidates) > 1:
            break
    if len(candidates) != 1:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "main document is missing or ambiguous; select it explicitly",
        )
    return candidates[0]


def _graphic_directories(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    directories: list[str] = []
    rejected: list[str] = []
    for outer in _GRAPHICSPATH_RE.findall(text):
        for raw in _INNER_BRACE_RE.findall(outer):
            cleaned = _strip_current_directory_prefix(raw.strip()).rstrip("/")
            if not cleaned:
                continue
            try:
                directories.append(validate_relative_path(cleaned))
            except ContractError:
                rejected.append(_safe_reference_label(cleaned))
    return tuple(dict.fromkeys(directories)), tuple(dict.fromkeys(rejected))


def discover_project(
    root: Path,
    *,
    main_document: str | None = None,
    limits: DiscoveryLimits = DEFAULT_DISCOVERY_LIMITS,
) -> ProjectDiscovery:
    """Discover only explicit local dependencies reachable from the main TeX file."""

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "project root is unavailable") from exc
    if not resolved_root.is_dir():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "project root must be a directory")

    selected_main = _select_main(resolved_root, main_document, limits)
    state = _DiscoveryState(resolved_root, limits, {}, set(), set(), set(), {})
    pending: deque[str] = deque([selected_main])
    parsed_runtime_text: set[str] = set()
    global_graphic_directories: tuple[str, ...] = ()

    while pending:
        relative_path = pending.popleft()
        if relative_path in parsed_runtime_text:
            continue
        resolved_path = resolve_within(resolved_root, relative_path)
        text, source_file = _read_source_file(resolved_path, relative_path, limits)
        state.add_file(source_file)
        parsed_runtime_text.add(relative_path)

        if source_file.role == "tex":
            for line in text.splitlines()[:20]:
                engine_match = _ENGINE_RE.match(line)
                if engine_match:
                    value = engine_match.group(1)
                    state.engine_hints.add(EngineHint(value.casefold(), relative_path, value))
        visible = _strip_comments(text)
        local_graphic_directories, rejected_graphic_directories = _graphic_directories(visible)
        state.external.update(
            ExternalReference(reference, "graphicspath", "path_rejected")
            for reference in rejected_graphic_directories
        )
        global_graphic_directories = tuple(
            dict.fromkeys((*global_graphic_directories, *local_graphic_directories))
        )

        references: list[tuple[str, str]] = []
        references.extend((match.group(1), match.group(2)) for match in _INPUT_RE.finditer(visible))
        for match in _BIB_RE.finditer(visible):
            references.extend(("bibliography", item.strip()) for item in match.group(2).split(","))
        non_graphic_reference_count = len(references)
        (
            graphic_references,
            rejected_graphic_references,
            graphic_reference_count,
        ) = _graphic_references(visible)
        references.extend(("graphic", reference) for reference in graphic_references)
        if source_file.role == "tex":
            state.external.update(
                ExternalReference(_safe_reference_label(reference), "graphic", reason)
                for reference, reason in rejected_graphic_references
            )
        (
            runtime_references,
            rejected_runtime_references,
            runtime_reference_count,
        ) = _runtime_references(visible)
        state.external.update(rejected_runtime_references)
        dependency_count = (
            non_graphic_reference_count + graphic_reference_count + runtime_reference_count
        )
        if dependency_count > limits.max_dependencies_per_file:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "source file exceeds the dependency reference limit",
            )

        for runtime_reference in runtime_references:
            target = _resolve_runtime_dependency(state, reference=runtime_reference)
            if target is None:
                continue
            state.edges.add(DependencyEdge(relative_path, target, runtime_reference.kind))
            if target in state.files:
                continue
            target_path = resolve_within(resolved_root, target)
            if PurePosixPath(target).suffix.casefold() in _RUNTIME_TEXT_EXTENSIONS:
                pending.append(target)
            else:
                state.add_file(_read_binary_file(target_path, target, limits))

        for kind, reference in references:
            if (
                source_file.role != "tex"
                and kind in {"input", "include", "graphic"}
                and _is_dynamic_reference(reference)
            ):
                # Class and style files commonly contain macro definitions such
                # as ``\input{#2}``.  These are not executable static edges;
                # the compile verifier remains responsible for the expanded
                # runtime behavior.  Dynamic dependencies in user TeX sources
                # remain blocking.
                continue
            target = _resolve_dependency(
                state,
                source_path=relative_path,
                reference=reference,
                kind=kind,
                graphic_directories=global_graphic_directories,
            )
            if target is None:
                continue
            state.edges.add(DependencyEdge(relative_path, target, kind))
            if target in state.files:
                continue
            target_path = resolve_within(resolved_root, target)
            if PurePosixPath(target).suffix.casefold() in _RUNTIME_TEXT_EXTENSIONS:
                pending.append(target)
            else:
                state.add_file(_read_binary_file(target_path, target, limits))

    files = tuple(sorted(state.files.values(), key=lambda item: item.path))
    tree_hash = sha256_canonical([file.as_tree_entry().as_dict() for file in files])
    profile_hash = sha256_canonical(
        {
            "name": "latex-project-discovery",
            "version": "1",
            "runtime_dependency_closure": "static-local-v1",
            "limits": asdict(limits),
        }
    )
    return ProjectDiscovery(
        main_document=selected_main,
        files=files,
        dependency_edges=tuple(sorted(state.edges)),
        external_references=tuple(sorted(state.external)),
        engine_hints=tuple(sorted(state.engine_hints)),
        source_tree_sha256=tree_hash,
        profile_sha256=profile_hash,
    )


__all__ = [
    "DependencyEdge",
    "DEFAULT_DISCOVERY_LIMITS",
    "DiscoveryLimits",
    "EngineHint",
    "ExternalReference",
    "ProjectDiscovery",
    "SourceFile",
    "discover_project",
]
