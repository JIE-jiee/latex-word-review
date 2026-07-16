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
from pathlib import Path
from typing import Protocol, cast


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
    ) -> _ConversionResult: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    return parser


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


def _run(source_path: Path, output_path: Path, report_path: Path) -> int:
    source = source_path.read_text(encoding="utf-8")
    module = importlib.import_module("tex2word")
    convert_source = cast(_ConvertSource, module.convert_source)
    result = convert_source(
        source,
        base_dir=".",
        embed_manifest=False,
        citation_mode="static",
        frontend="pure",
    )
    entries = tuple(result.report.entries)
    severities = tuple(_severity_value(entry) for entry in entries)
    errors = tuple(
        entry for entry, value in zip(entries, severities, strict=True) if value == "error"
    )
    warnings = tuple(
        entry for entry, value in zip(entries, severities, strict=True) if value == "warning"
    )
    report = {
        "constructs": _bounded_constructs(entries),
        "entry_count": len(entries),
        "error_count": len(errors),
        "math_image": int(result.report.math_image),
        "math_omml": int(result.report.math_omml),
        "math_raw": int(result.report.math_raw),
        "warning_count": len(warnings),
        "warning_constructs": _bounded_constructs(warnings),
    }
    report_bytes = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
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
    return _run(Path(args.source), Path(args.output), Path(args.report))


if __name__ == "__main__":  # pragma: no branch - module process entry point
    try:
        raise SystemExit(main())
    except (ImportError, OSError, RuntimeError, UnicodeError, ValueError):
        # Do not serialize tracebacks or local paths across the worker boundary.
        sys.stderr.write("tex2word worker failed\n")
        raise SystemExit(22) from None
