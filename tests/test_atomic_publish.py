"""Windows no-clobber directory publication and transient-lock recovery tests."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from latex_word_review.atomic_publish import publish_new_directory
from latex_word_review.ingest import archive_returned_docx
from latex_word_review.snapshot import snapshot_project

RUN_ID = "run_019b0000-0000-7000-8000-000000000001"
EXPORTED_HASH = "sha256:" + "a" * 64


def _windows_error(winerror: int, path: Path) -> OSError:
    error = PermissionError(errno.EACCES, "transient Windows filesystem lock", os.fspath(path))
    error.winerror = winerror
    return error


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_publish_retries_only_transient_windows_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
) -> None:
    stage = tmp_path / "stage"
    destination = tmp_path / "published"
    stage.mkdir()
    (stage / "evidence.txt").write_text("sealed", encoding="utf-8")
    concrete_path = type(stage)
    original_rename = concrete_path.rename
    calls = 0
    delays: list[float] = []

    def flaky_rename(self: Path, target: Path) -> Path:
        nonlocal calls
        if self == stage:
            calls += 1
            if calls <= 2:
                raise _windows_error(winerror, self)
        return original_rename(self, target)

    monkeypatch.setattr(concrete_path, "rename", flaky_rename)
    publish_new_directory(stage, destination, _sleep=delays.append)

    assert calls == 3
    assert delays == [0.02, 0.04]
    assert not stage.exists()
    assert (destination / "evidence.txt").read_text(encoding="utf-8") == "sealed"


def test_publish_exhausts_bounded_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    destination = tmp_path / "published"
    stage.mkdir()
    concrete_path = type(stage)
    calls = 0
    delays: list[float] = []

    def always_busy(self: Path, target: Path) -> Path:
        del target
        nonlocal calls
        if self == stage:
            calls += 1
        raise _windows_error(5, self)

    monkeypatch.setattr(concrete_path, "rename", always_busy)
    with pytest.raises(PermissionError) as raised:
        publish_new_directory(stage, destination, _sleep=delays.append)

    assert getattr(raised.value, "winerror", None) == 5
    assert calls == 6
    assert delays == [0.02, 0.04, 0.08, 0.16, 0.32]
    assert stage.is_dir()
    assert not destination.exists()


def test_publish_never_replaces_destination_that_appears_during_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    destination = tmp_path / "published"
    stage.mkdir()
    concrete_path = type(stage)
    calls = 0
    delays: list[float] = []

    def competitor_wins(self: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        target.mkdir()
        (target / "competitor.txt").write_text("keep", encoding="utf-8")
        raise _windows_error(5, self)

    monkeypatch.setattr(concrete_path, "rename", competitor_wins)
    with pytest.raises(FileExistsError):
        publish_new_directory(stage, destination, _sleep=delays.append)

    assert calls == 1
    assert delays == []
    assert stage.is_dir()
    assert (destination / "competitor.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "error",
    [
        FileExistsError(errno.EEXIST, "exists"),
        OSError(errno.EXDEV, "cross-device"),
        OSError(errno.ENOENT, "missing path"),
    ],
)
def test_publish_does_not_retry_deterministic_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    stage = tmp_path / "stage"
    destination = tmp_path / "published"
    stage.mkdir()
    concrete_path = type(stage)
    calls = 0
    delays: list[float] = []

    def fail(self: Path, target: Path) -> Path:
        del self, target
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(concrete_path, "rename", fail)
    with pytest.raises(type(error)):
        publish_new_directory(stage, destination, _sleep=delays.append)

    assert calls == 1
    assert delays == []
    assert stage.is_dir()
    assert not destination.exists()


def test_publish_stops_if_stage_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    destination = tmp_path / "published"
    stage.mkdir()
    concrete_path = type(stage)
    delays: list[float] = []

    def remove_stage(self: Path, target: Path) -> Path:
        del target
        self.rmdir()
        raise _windows_error(5, self)

    monkeypatch.setattr(concrete_path, "rename", remove_stage)
    with pytest.raises(FileNotFoundError):
        publish_new_directory(stage, destination, _sleep=delays.append)

    assert delays == []
    assert not stage.exists()
    assert not destination.exists()


def test_snapshot_recovers_from_one_transient_publish_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}ok\\end{document}\n",
        encoding="utf-8",
    )
    destination = tmp_path / "snapshot"
    concrete_path = type(destination)
    original_rename = concrete_path.rename
    injected = False

    def flaky_rename(self: Path, target: Path) -> Path:
        nonlocal injected
        if not injected and self.name.startswith(".snapshot.tmp-") and target == destination:
            injected = True
            raise _windows_error(5, self)
        return original_rename(self, target)

    monkeypatch.setattr(concrete_path, "rename", flaky_rename)
    result = snapshot_project(source, destination, main_document="main.tex")

    assert injected
    assert result.file_count == 1
    assert (source / "main.tex").read_text(encoding="utf-8").endswith("\\end{document}\n")
    assert not tuple(tmp_path.glob(".snapshot.tmp-*"))


def test_returned_archive_recovers_from_one_transient_publish_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    returned = tmp_path / "returned.docx"
    returned.write_bytes(b"synthetic immutable returned Word bytes")
    destination = tmp_path / "archive"
    concrete_path = type(destination)
    original_rename = concrete_path.rename
    injected = False

    def flaky_rename(self: Path, target: Path) -> Path:
        nonlocal injected
        if not injected and self.name.startswith(".archive.returned-") and target == destination:
            injected = True
            raise _windows_error(5, self)
        return original_rename(self, target)

    monkeypatch.setattr(concrete_path, "rename", flaky_rename)
    result = archive_returned_docx(
        returned,
        destination,
        run_id=RUN_ID,
        exported_docx_sha256=EXPORTED_HASH,
        confidentiality="public_fixture",
    )

    assert injected
    assert result.docx_path.read_bytes() == returned.read_bytes()
    assert not tuple(tmp_path.glob(".archive.returned-*"))
