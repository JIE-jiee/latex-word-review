"""Shared pytest safety fixtures."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

import latex_word_review.word_fields as word_fields_module
from latex_word_review.docx_reader import read_docx_package
from latex_word_review.inspection import inspect_docx


def fake_invoke_word(
    source: Path,
    working: Path,
    destination: Path,
    report_path: Path,
    pid_state: Path,
    *,
    expected_fields: int,
    expected_unresolved: int,
) -> tuple[word_fields_module._WordReport, int]:
    """Return deterministic field evidence without crossing the Word COM boundary."""

    del working, report_path, pid_state
    shutil.copyfile(source, destination)
    package = read_docx_package(destination)
    inspection = inspect_docx(destination)
    return (
        word_fields_module._WordReport(
            word_version="16.0-pytest-double",
            field_count=expected_fields,
            updated_fields=expected_fields - expected_unresolved,
            unresolved_fields=expected_unresolved,
            revision_count=0,
            bookmark_count=inspection.bookmarks,
            source_sha256=package.file_sha256.removeprefix("sha256:"),
            output_sha256=package.file_sha256.removeprefix("sha256:"),
        ),
        0,
    )


@pytest.fixture(autouse=True)
def _isolate_word_field_automation(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Keep ordinary unit/integration tests independent of Microsoft Word.

    The focused ``test_word_fields.py`` module installs its own bounded runner
    doubles and therefore remains outside this shared fixture.  All other tests
    still exercise the production field inventory, protected-content checks,
    field sealing, and no-clobber publication; only the external COM boundary
    is replaced with a deterministic byte-preserving result.
    """

    if request.node.path.name == "test_word_fields.py":
        yield
        return

    monkeypatch.setattr(word_fields_module, "_invoke_word", fake_invoke_word)
    yield
