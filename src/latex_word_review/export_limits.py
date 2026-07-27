"""Shared timeout contract for LaTeX-to-Word conversion processes."""

from __future__ import annotations

from typing import Final

from latex_word_review.errors import ContractError, ErrorCode

DEFAULT_EXPORT_TIMEOUT_SECONDS: Final = 300.0
MAX_EXPORT_TIMEOUT_SECONDS: Final = 600.0


def validate_export_timeout(timeout_s: float) -> float:
    """Return one valid per-conversion budget or fail closed."""

    if isinstance(timeout_s, bool) or not 0 < timeout_s <= MAX_EXPORT_TIMEOUT_SECONDS:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            f"export conversion timeout must be in (0, {MAX_EXPORT_TIMEOUT_SECONDS:g}]",
        )
    return float(timeout_s)


__all__ = [
    "DEFAULT_EXPORT_TIMEOUT_SECONDS",
    "MAX_EXPORT_TIMEOUT_SECONDS",
    "validate_export_timeout",
]
