"""Bounded no-clobber directory publication for the supported Windows runtime."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

_WINDOWS_TRANSIENT_WINERRORS: Final[frozenset[int]] = frozenset({5, 32, 33})
_WINDOWS_TRANSIENT_ERRNOS: Final[frozenset[int]] = frozenset({errno.EACCES, errno.EPERM})
_WINDOWS_RETRY_DELAYS: Final[tuple[float, ...]] = (0.02, 0.04, 0.08, 0.16, 0.32)


def _path_exists_without_following(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _is_link_or_junction(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_probe is not None and junction_probe())


def _require_stage_directory(stage: Path) -> None:
    if not _path_exists_without_following(stage):
        raise FileNotFoundError(errno.ENOENT, "publication stage disappeared", os.fspath(stage))
    if _is_link_or_junction(stage) or not stage.is_dir():
        raise NotADirectoryError(
            errno.ENOTDIR,
            "publication stage must be a real directory",
            os.fspath(stage),
        )


def _destination_conflict(destination: Path, cause: OSError | None = None) -> None:
    error = FileExistsError(
        errno.EEXIST,
        "publication destination already exists",
        os.fspath(destination),
    )
    if cause is None:
        raise error
    raise error from cause


def _is_transient_windows_rename_error(error: OSError) -> bool:
    if os.name != "nt":
        return False
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        return winerror in _WINDOWS_TRANSIENT_WINERRORS
    return error.errno in _WINDOWS_TRANSIENT_ERRNOS


def publish_new_directory(
    stage: Path,
    destination: Path,
    *,
    _retry_delays: Sequence[float] = _WINDOWS_RETRY_DELAYS,
    _sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Atomically publish one owned directory without replacing any destination.

    Windows filesystem filters can briefly retain handles to newly written files
    and make an otherwise valid directory rename fail with access or sharing
    errors.  Only those transient Windows errors are retried, with a bounded
    delay budget.  A destination race or a missing/unsafe stage always fails
    closed and is never retried or removed here.
    """

    if any(delay < 0 for delay in _retry_delays):
        raise ValueError("publication retry delays must be non-negative")
    _require_stage_directory(stage)
    if _path_exists_without_following(destination):
        _destination_conflict(destination)

    attempt = 0
    while True:
        try:
            stage.rename(destination)
            return
        except FileExistsError:
            raise
        except OSError as error:
            if not _is_transient_windows_rename_error(error) or attempt >= len(_retry_delays):
                raise
            if _path_exists_without_following(destination):
                _destination_conflict(destination, error)
            _require_stage_directory(stage)
            delay = _retry_delays[attempt]
            attempt += 1
            _sleep(delay)


__all__ = ["publish_new_directory"]
