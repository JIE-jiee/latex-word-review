"""Private tex2word worker executed in a bounded child process.

This module is intentionally not a public API.  The parent adapter owns all
path validation, source hashing, artifact inspection, and publication.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Protocol, cast

from latex_word_review.paths import windows_extended_path
from latex_word_review.review_reference import (
    PROFILE_ID,
    REFERENCE_DOCX_SHA256,
    materialized_review_reference,
)
from latex_word_review.revision_macros import inject_revision_macros


class _Severity(Protocol):
    value: str


class _ReportEntry(Protocol):
    severity: _Severity | str
    construct: str


class _ConversionReport(Protocol):
    entries: list[_ReportEntry]
    math_omml: int
    math_image: int
    math_raw: int


class _ConversionResult(Protocol):
    report: _ConversionReport
    docx: bytes


class _ConvertSource(Protocol):
    def __call__(
        self,
        source: str,
        base_dir: str = ".",
        *,
        embed_manifest: bool = True,
        citation_mode: str = "static",
        frontend: str = "pure",
        reference_doc: str | None = None,
    ) -> _ConversionResult: ...


class _ImageWriter(Protocol):
    base_dir: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument(
        "--revision-view",
        choices=("source", "clean", "display"),
        default="source",
    )
    parser.add_argument(
        "--revision-aliases",
        choices=("none", "add", "delete", "add,delete"),
        default="none",
    )
    return parser


def _parse_revision_aliases(
    raw: str,
) -> tuple[Literal["add", "delete"], ...]:
    if raw == "none":
        return ()
    if raw == "add":
        return ("add",)
    if raw == "delete":
        return ("delete",)
    if raw == "add,delete":
        return ("add", "delete")
    raise ValueError("invalid revision alias selector")


def _write_exclusive(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _bounded_constructs(entries: tuple[_ReportEntry, ...]) -> list[str]:
    constructs: set[str] = set()
    for entry in entries:
        value = str(entry.construct).replace("\x00", "�")[:128]
        constructs.add(value)
        if len(constructs) >= 256:
            break
    return sorted(constructs)


def _severity_value(entry: _ReportEntry) -> str:
    """Normalize both report shapes emitted by tex2word 1.0.5."""

    severity = entry.severity
    value = severity if isinstance(severity, str) else severity.value
    if not isinstance(value, str):
        raise ValueError("tex2word report severity must be a string")
    return value


@contextmanager
def _windows_image_path_compat(module: object) -> Iterator[None]:
    """Temporarily normalize locked tex2word image paths for one conversion."""
    if os.name != "nt":
        yield
        return
    if getattr(module, "__version__", None) != "1.0.5":
        raise RuntimeError("tex2word image-path compatibility version mismatch")
    document_module = importlib.import_module("tex2word.backend.document")
    writer = getattr(document_module, "DocumentWriter", None)
    candidate = getattr(writer, "_resolve_image_path", None)
    if not isinstance(writer, type) or not callable(candidate):
        raise RuntimeError("tex2word image-path compatibility target is unavailable")
    original = cast("Callable[[object, str], str | None]", candidate)

    def normalized_resolver(instance: object, relative: str) -> str | None:
        image_writer = cast(_ImageWriter, instance)
        normalized = os.path.normpath(relative)
        if os.path.isabs(normalized):
            absolute = os.fspath(windows_extended_path(Path(normalized)))
            return original(instance, absolute)
        original_base_dir = image_writer.base_dir
        image_writer.base_dir = os.fspath(windows_extended_path(Path(original_base_dir)))
        try:
            return original(instance, normalized)
        finally:
            image_writer.base_dir = original_base_dir

    type.__setattr__(writer, "_resolve_image_path", normalized_resolver)
    try:
        yield
    finally:
        type.__setattr__(writer, "_resolve_image_path", original)


def _run(
    source_path: Path,
    output_path: Path,
    report_path: Path,
    revision_view: Literal["source", "clean", "display"] = "source",
    revision_aliases: tuple[Literal["add", "delete"], ...] = (),
) -> int:
    source = inject_revision_macros(
        source_path.read_text(encoding="utf-8"),
        mode=revision_view,
        aliases=revision_aliases,
    )
    module = importlib.import_module("tex2word")
    convert_source = cast(_ConvertSource, module.convert_source)
    with (
        materialized_review_reference(output_path.parent) as reference_path,
        _windows_image_path_compat(module),
    ):
        result = convert_source(
            source,
            base_dir=".",
            embed_manifest=False,
            citation_mode="static",
            frontend="pure",
            reference_doc=os.fspath(reference_path),
        )
    entries = tuple(result.report.entries)
    severities = tuple(_severity_value(entry) for entry in entries)
    errors = tuple(
        entry for entry, value in zip(entries, severities, strict=True) if value == "error"
    )
    warnings = tuple(
        entry for entry, value in zip(entries, severities, strict=True) if value == "warning"
    )
    reference_events = tuple(
        value
        for entry, value in zip(entries, severities, strict=True)
        if entry.construct == "reference-doc"
    )
    reference_loaded = "info" in reference_events and not any(
        value in {"warning", "error"} for value in reference_events
    )
    report = {
        "constructs": _bounded_constructs(entries),
        "entry_count": len(entries),
        "error_count": len(errors),
        "math_image": int(result.report.math_image),
        "math_omml": int(result.report.math_omml),
        "math_raw": int(result.report.math_raw),
        "reference_loaded": reference_loaded,
        "reference_profile": PROFILE_ID,
        "reference_sha256": REFERENCE_DOCX_SHA256,
        "revision_view": revision_view,
        "warning_count": len(warnings),
        "revision_aliases": list(revision_aliases),
        "warning_constructs": _bounded_constructs(warnings),
    }
    report_bytes = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not reference_loaded:
        _write_exclusive(report_path, report_bytes)
        return 23
    if errors:
        _write_exclusive(report_path, report_bytes)
        return 20
    if not isinstance(result.docx, bytes) or not result.docx:
        return 21
    _write_exclusive(output_path, result.docx)
    _write_exclusive(report_path, report_bytes)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return _run(
        Path(args.source),
        Path(args.output),
        Path(args.report),
        cast("Literal['source', 'clean', 'display']", args.revision_view),
        _parse_revision_aliases(args.revision_aliases),
    )


if __name__ == "__main__":  # pragma: no branch - module process entry point
    try:
        raise SystemExit(main())
    except (ImportError, OSError, RuntimeError, UnicodeError, ValueError):
        # Do not serialize tracebacks or local paths across the worker boundary.
        sys.stderr.write("tex2word worker failed\n")
        raise SystemExit(22) from None
