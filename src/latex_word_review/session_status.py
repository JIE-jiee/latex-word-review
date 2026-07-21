"""JSON-ready product status values for one local review session.

The product UI consumes this deliberately small projection instead of domain
objects.  It contains only paths relative to the run root; authorization and
integrity remain in the sealed objects reconstructed by :mod:`application`.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

SessionPhase = Literal[
    "ready_to_export",
    "waiting_for_return",
    "approval_required",
    "approval_in_progress",
    "approval_ready_to_finalize",
    "ready_to_plan",
    "plan_blocked",
    "awaiting_apply_confirmation",
    "applied",
    "verified",
    "partially_completed",
    "ready_to_bundle",
    "completed",
]


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """A stable, UI-facing projection rebuilt from sealed run evidence."""

    run_id: str
    main_document: str
    phase: SessionPhase
    step: Literal[1, 2, 3, 4]
    next_action: str
    artifacts: Mapping[str, str]
    approval: Mapping[str, Any] | None = None
    plan: Mapping[str, Any] | None = None
    blockers: tuple[Mapping[str, Any], ...] = ()
    apply_confirmation: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return an isolated JSON-ready mapping for HTTP/GUI consumers."""

        return {
            "run_id": self.run_id,
            "main_document": self.main_document,
            "phase": self.phase,
            "step": self.step,
            "next_action": self.next_action,
            # Reading status is never the source-modifying gate.  The only
            # authorized path is ApplicationSession.apply_confirmed(), which
            # re-reads and compares the current PatchPlan payload hash.
            "can_apply": False,
            "artifacts": dict(self.artifacts),
            "approval": copy.deepcopy(dict(self.approval)) if self.approval else None,
            "plan": copy.deepcopy(dict(self.plan)) if self.plan else None,
            "blockers": [copy.deepcopy(dict(item)) for item in self.blockers],
            "apply_confirmation": (
                copy.deepcopy(dict(self.apply_confirmation))
                if self.apply_confirmation is not None
                else None
            ),
        }


__all__ = ["SessionPhase", "SessionStatus"]
