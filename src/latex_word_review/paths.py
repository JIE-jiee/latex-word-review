"""Portable relative-path validation and containment checks.

Filesystem roots are process-local inputs.  Values returned for manifests and
diagnostics are always portable, forward-slash-separated relative paths.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from latex_word_review.errors import ContractError, ErrorCode

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def validate_relative_path(value: str) -> str:
    """Validate and return one canonical portable relative path.

    No normalization is attempted.  Ambiguous spellings are rejected so a
    validated string has identical meaning on Windows and POSIX systems.
    """

    if not isinstance(value, str) or not value:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "relative path must be non-empty")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "relative path contains a control character")
    if (
        value.startswith(("/", "\\"))
        or _WINDOWS_DRIVE_RE.match(value)
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
    ):
        raise ContractError(ErrorCode.PATH_ABSOLUTE, "absolute paths are not allowed")
    if "\\" in value:
        raise ContractError(
            ErrorCode.PATH_TRAVERSAL,
            "backslashes are not allowed in portable relative paths",
        )

    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ContractError(
            ErrorCode.PATH_TRAVERSAL,
            "relative path contains an empty or traversal component",
        )
    for part in parts:
        if any(character in part for character in '<>:"|?*') or part.endswith((" ", ".")):
            raise ContractError(
                ErrorCode.PATH_TRAVERSAL,
                "relative path is not portable across supported platforms",
            )
        basename = part.split(".", 1)[0].casefold()
        if basename in _WINDOWS_RESERVED:
            raise ContractError(
                ErrorCode.PATH_TRAVERSAL,
                "relative path uses a reserved platform name",
            )
    return "/".join(parts)


def resolve_within(
    root: Path,
    relative_path: str,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve a validated path and reject symlink or junction escapes."""

    normalized = validate_relative_path(relative_path)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "filesystem root is unavailable") from exc
    if not resolved_root.is_dir():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "filesystem root must be a directory")

    candidate = resolved_root.joinpath(*normalized.split("/"))
    try:
        resolved_candidate = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ContractError(
            ErrorCode.PATH_LINK_ESCAPE,
            "path cannot be resolved without crossing an unsafe link",
        ) from exc
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ContractError(
            ErrorCode.PATH_LINK_ESCAPE,
            "symlink or junction resolves outside the allowed root",
        )
    return resolved_candidate


def relative_path_from(root: Path, path: Path) -> str:
    """Return a portable relative path after proving containment."""

    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "filesystem path is unavailable") from exc
    if not resolved_path.is_relative_to(resolved_root):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "path is outside the allowed root")
    value = resolved_path.relative_to(resolved_root).as_posix()
    return validate_relative_path(value)


def ensure_disjoint_roots(source_root: Path, destination: Path) -> tuple[Path, Path]:
    """Reject destinations that contain, equal, or sit inside the source tree."""

    current = destination.absolute()
    while True:
        try:
            junction_probe = getattr(current, "is_junction", None)
            is_junction = bool(junction_probe is not None and junction_probe())
            if current.is_symlink() or is_junction:
                raise ContractError(
                    ErrorCode.PATH_LINK_ESCAPE,
                    "snapshot destination crosses a symlink or junction",
                )
        except OSError as exc:
            raise ContractError(
                ErrorCode.PATH_LINK_ESCAPE,
                "snapshot destination link status is unavailable",
            ) from exc
        if current == current.parent:
            break
        current = current.parent

    try:
        source = source_root.resolve(strict=True)
        target = destination.resolve(strict=False)
    except OSError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "source or destination is unavailable",
        ) from exc
    if not source.is_dir():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "source root must be a directory")
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise ContractError(
            ErrorCode.PATH_TRAVERSAL,
            "source and destination trees must be disjoint",
        )
    return source, target


def path_identity(path: Path) -> tuple[int, int]:
    """Return a device/inode identity without following a final symlink."""

    status = os.stat(path, follow_symlinks=False)
    return status.st_dev, status.st_ino


__all__ = [
    "ensure_disjoint_roots",
    "path_identity",
    "relative_path_from",
    "resolve_within",
    "validate_relative_path",
]
