"""Shared closed vocabularies used across the product surfaces.

Keep values that cross the library, CLI, and local web application here.  The
individual modules may re-export the type aliases for compatibility, but they
must not maintain independent copies of these vocabularies.
"""

from __future__ import annotations

from typing import Final, Literal

Confidentiality = Literal["public_fixture", "local_private", "derived_private"]
CONFIDENTIALITY_VALUES: Final[tuple[Confidentiality, ...]] = (
    "public_fixture",
    "local_private",
    "derived_private",
)
CONFIDENTIALITY_VALUE_SET: Final[frozenset[Confidentiality]] = frozenset(CONFIDENTIALITY_VALUES)

Decision = Literal[
    "pending",
    "accepted",
    "accepted_with_edit",
    "rejected",
    "manual",
    "conflict",
]
DECISION_VALUES: Final[tuple[Decision, ...]] = (
    "pending",
    "accepted",
    "accepted_with_edit",
    "rejected",
    "manual",
    "conflict",
)
DECISION_VALUE_SET: Final[frozenset[Decision]] = frozenset(DECISION_VALUES)
ACTION_DECISION_VALUE_SET: Final[frozenset[Decision]] = frozenset(
    value for value in DECISION_VALUES if value != "pending"
)

__all__ = [
    "ACTION_DECISION_VALUE_SET",
    "CONFIDENTIALITY_VALUES",
    "CONFIDENTIALITY_VALUE_SET",
    "DECISION_VALUES",
    "DECISION_VALUE_SET",
    "Confidentiality",
    "Decision",
]
