r"""Strict normalization for literal ``includegraphics`` targets.

LaTeX authors sometimes write ``\includegraphics{{figure.pdf}}`` to add one
redundant grouping layer around a filename.  The outer argument braces are
syntax; this module accepts exactly one additional, complete pair while
continuing to reject macro expansion, nested/local grouping, and unsafe
filesystem spellings.
"""

from __future__ import annotations

import re

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.paths import validate_relative_path

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_DYNAMIC_TARGET_CHARACTERS = frozenset("\\#${}~^")
_DYNAMIC_REASON = "dynamic_graphic_target"


def normalize_static_graphic_target(value: str) -> str:
    """Return one canonical, source-root-relative literal image target.

    The caller passes the contents of the ordinary outer argument braces.  A
    single extra pair that encloses the complete value is removed.  Any brace
    left before or after that operation is evidence of macro/group semantics,
    so it remains manual instead of being guessed.
    """

    if not isinstance(value, str):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "image target must be text")
    target = value.strip()
    if not target:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "image target is empty")

    has_opening_group = target.startswith("{")
    has_closing_group = target.endswith("}")
    if has_opening_group or has_closing_group:
        if not (has_opening_group and has_closing_group):
            raise _dynamic_target_error()
        inner = target[1:-1].strip()
        if not inner or "{" in inner or "}" in inner:
            raise _dynamic_target_error()
        target = inner

    while target.startswith("./"):
        target = target[2:]
    if target.startswith(("/", "\\\\")) or _WINDOWS_DRIVE_RE.match(target):
        raise ContractError(ErrorCode.PATH_ABSOLUTE, "absolute image paths are not allowed")
    if ":" in target:
        raise ContractError(
            ErrorCode.PATH_TRAVERSAL,
            "image paths must not contain ADS syntax",
        )
    if any(character in _DYNAMIC_TARGET_CHARACTERS for character in target):
        raise _dynamic_target_error()
    return validate_relative_path(target)


def is_dynamic_graphic_target_error(error: ContractError) -> bool:
    """Return whether a normalization failure requires TeX/manual handling."""

    details = error.violation.details
    return bool(details is not None and details.get("reason") == _DYNAMIC_REASON)


def _dynamic_target_error() -> ContractError:
    return ContractError(
        ErrorCode.PATH_TRAVERSAL,
        "dynamic image target is not resolvable",
        details={"reason": _DYNAMIC_REASON},
    )


__all__ = [
    "is_dynamic_graphic_target_error",
    "normalize_static_graphic_target",
]
