"""Restricted internal-worker commands for source and frozen runtimes.

PyInstaller replaces :data:`sys.executable` with the bundled application. A
frozen application therefore cannot use ``sys.executable -m ...`` to start a
Python worker. This module preserves development behaviour while switching
frozen builds to one hidden, allow-listed self-spawn protocol.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from latex_word_review.errors import ContractError, ErrorCode

InternalWorker = Literal["windows-gated-launcher", "tex2word", "image-render"]
INTERNAL_WORKER_FLAG: Final[str] = "--latex-word-review-internal-worker"

_DEVELOPMENT_MODULES: Final[dict[InternalWorker, str]] = {
    "tex2word": "latex_word_review.backends._tex2word_worker",
    "image-render": "latex_word_review._image_worker",
}
_FIXED_FLAGS: Final[dict[InternalWorker, tuple[str, ...]]] = {
    "tex2word": ("--source", "--output", "--report"),
    "image-render": ("--source", "--request", "--output", "--report"),
}
_WINDOWS_GATED_LAUNCHER: Final[str] = """\
import subprocess
import sys

if sys.stdin.buffer.read(1) != b"1":
    raise SystemExit(125)
try:
    child = subprocess.Popen(
        sys.argv[1:],
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
    )
except OSError:
    sys.stderr.buffer.write(b"contained launcher could not start command\\n")
    raise SystemExit(126) from None
raise SystemExit(child.wait())
"""


@dataclass(frozen=True, slots=True)
class InternalWorkerCommand:
    """One executable plus an immutable, shell-free argument vector."""

    executable: str
    arguments: tuple[str, ...]


def is_frozen_application() -> bool:
    """Return whether Python is running from a freezer-provided executable."""

    return bool(getattr(sys, "frozen", False))


def validate_internal_worker_arguments(
    worker: InternalWorker | str,
    arguments: Sequence[str],
) -> tuple[str, ...]:
    """Validate a worker name and its fixed positional flag layout."""

    if worker not in {"windows-gated-launcher", "tex2word", "image-render"}:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "internal worker is not allow-listed")
    normalized = tuple(arguments)
    if any(not isinstance(item, str) or "\x00" in item for item in normalized):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "internal worker argv is invalid")
    if worker == "windows-gated-launcher":
        if not normalized or not normalized[0]:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "contained launcher requires an executable",
            )
        return normalized

    typed_worker = worker
    assert typed_worker in _FIXED_FLAGS
    expected_flags = _FIXED_FLAGS[typed_worker]
    if len(normalized) != len(expected_flags) * 2 or normalized[::2] != expected_flags:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "internal worker argv does not match its fixed layout",
        )
    return normalized


def internal_worker_command(
    worker: InternalWorker,
    arguments: Sequence[str],
) -> InternalWorkerCommand:
    """Build a bounded worker invocation for CPython or a frozen application."""

    normalized = validate_internal_worker_arguments(worker, arguments)
    if is_frozen_application():
        return InternalWorkerCommand(
            executable=sys.executable,
            arguments=(INTERNAL_WORKER_FLAG, worker, *normalized),
        )
    if worker == "windows-gated-launcher":
        return InternalWorkerCommand(
            executable=sys.executable,
            arguments=("-I", "-S", "-c", _WINDOWS_GATED_LAUNCHER, *normalized),
        )
    return InternalWorkerCommand(
        executable=sys.executable,
        arguments=("-m", _DEVELOPMENT_MODULES[worker], *normalized),
    )


__all__ = [
    "INTERNAL_WORKER_FLAG",
    "InternalWorker",
    "InternalWorkerCommand",
    "internal_worker_command",
    "is_frozen_application",
    "validate_internal_worker_arguments",
]
