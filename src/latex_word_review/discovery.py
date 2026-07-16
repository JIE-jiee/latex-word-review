"""Conservative, bounded discovery for a LaTeX project snapshot."""

from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from latex_word_review.canonical import sha256_canonical
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import FileDigest, TreeEntry, digest_bytes, read_stable_bytes
from latex_word_review.paths import resolve_within, validate_relative_path

_INPUT_RE = re.compile(r"\\(input|include)\s*\{([^{}]+)\}")
_BIB_RE = re.compile(r"\\(bibliography|addbibresource)(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}")
_GRAPHIC_RE = re.compile(r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^{}]+\}\s*)+)\}")
_INNER_BRACE_RE = re.compile(r"\{([^{}]+)\}")
_ENGINE_RE = re.compile(r"^\s*%\s*!TeX\s+program\s*=\s*([^\s]+)\s*$", re.IGNORECASE)
_DOCUMENT_CLASS_RE = re.compile(r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{")
_BEGIN_DOCUMENT_RE = re.compile(r"\\begin\s*\{document\}")

_GRAPHIC_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg")
_MEDIA_TYPES = {
    ".bib": "text/x-bibtex",
    ".eps": "application/postscript",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".pdf": "application/pdf",
    ".png": "image/png",
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


def _read_source_file(
    path: Path,
    relative_path: str,
    limits: DiscoveryLimits,
) -> tuple[str, SourceFile]:
    data = read_stable_bytes(path, max_bytes=limits.max_file_bytes)
    digest = digest_bytes(data)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "v0.1 LaTeX source files must be valid UTF-8",
        ) from exc
    source_file = SourceFile(
        path=relative_path,
        role="tex",
        media_type="text/x-tex",
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
    try:
        candidates = _candidate_paths(
            raw_reference,
            kind=kind,
            graphic_directories=graphic_directories,
        )
    except ContractError:
        state.external.add(
            ExternalReference(_safe_reference_label(raw_reference), kind, "path_rejected")
        )
        return None

    matches: list[str] = []
    for candidate in candidates:
        try:
            resolved = resolve_within(state.root, candidate, must_exist=False)
        except ContractError:
            state.external.add(
                ExternalReference(_safe_reference_label(candidate), kind, "link_escape")
            )
            return None
        if resolved.is_file():
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    reason = "missing" if not matches else "ambiguous"
    state.external.add(ExternalReference(_safe_reference_label(raw_reference), kind, reason))
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
            cleaned = raw.strip().rstrip("/")
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
    state = _DiscoveryState(resolved_root, limits, {}, set(), set(), set())
    pending: deque[str] = deque([selected_main])
    parsed_tex: set[str] = set()
    global_graphic_directories: tuple[str, ...] = ()

    while pending:
        relative_path = pending.popleft()
        if relative_path in parsed_tex:
            continue
        resolved_path = resolve_within(resolved_root, relative_path)
        text, source_file = _read_source_file(resolved_path, relative_path, limits)
        state.add_file(source_file)
        parsed_tex.add(relative_path)

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
        references.extend(("graphic", match.group(1)) for match in _GRAPHIC_RE.finditer(visible))
        if len(references) > limits.max_dependencies_per_file:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "source file exceeds the dependency reference limit",
            )

        for kind, reference in references:
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
            if PurePosixPath(target).suffix.casefold() == ".tex":
                pending.append(target)
            else:
                state.add_file(_read_binary_file(target_path, target, limits))

    files = tuple(sorted(state.files.values(), key=lambda item: item.path))
    tree_hash = sha256_canonical([file.as_tree_entry().as_dict() for file in files])
    profile_hash = sha256_canonical(
        {"name": "latex-project-discovery", "version": "1", "limits": asdict(limits)}
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
