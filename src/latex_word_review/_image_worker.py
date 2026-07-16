"""Private contained worker for native PDFium rendering.

The parent process owns path validation, source hashing, timeout enforcement,
Windows Job Object containment, cache publication, and output verification.
This module imports PDFium only after the contained process has started.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from latex_word_review.canonical import canonical_json
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes, read_stable_bytes
from latex_word_review.image_materializer import (
    _WORKER_PNG_NAME,
    _WORKER_REPORT_FORMAT,
    _WORKER_REPORT_NAME,
    _WORKER_REQUEST_FORMAT,
    _WORKER_REQUEST_NAME,
    _WORKER_SOURCE_NAME,
    _image_request_from_dict,
    _load_pdf_runtime,
    _read_canonical_object,
    _render_pdf,
    _renderer_identity_from_dict,
    _write_exclusive,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    return parser


def _fixed_path(value: str, expected: str) -> Path:
    if value != expected:
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "image worker path is not fixed")
    return Path(expected)


def _run(source_path: Path, request_path: Path, output_path: Path, report_path: Path) -> int:
    document = _read_canonical_object(
        request_path,
        max_bytes=128 * 1024,
        label="image worker request",
    )
    if set(document) != {"format", "request", "request_sha256", "renderer"}:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "image worker request shape is invalid")
    if document.get("format") != _WORKER_REQUEST_FORMAT:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "image worker request format is unsupported")
    request = _image_request_from_dict(document.get("request"))
    if document.get("request_sha256") != request.request_sha256:
        raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "image worker request hash differs")
    expected_renderer = _renderer_identity_from_dict(document.get("renderer"))
    source_bytes = read_stable_bytes(
        source_path,
        max_bytes=request.quality.max_source_bytes,
    )
    source_digest = digest_bytes(source_bytes)
    if (
        source_digest.sha256 != request.source_sha256
        or source_digest.size_bytes != request.source_size_bytes
    ):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "image worker source hash differs")

    runtime = _load_pdf_runtime()
    if runtime.identity != expected_renderer:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "image worker renderer identity differs",
        )
    png_bytes, width_px, height_px, pixel_sha256 = _render_pdf(
        source_bytes,
        request,
        runtime,
    )
    png_digest = digest_bytes(png_bytes)
    report = {
        "format": _WORKER_REPORT_FORMAT,
        "request_sha256": request.request_sha256,
        "renderer": runtime.identity.as_dict(),
        "output": {
            "path": _WORKER_PNG_NAME,
            "media_type": "image/png",
            "mode": "RGB",
            "size_bytes": png_digest.size_bytes,
            "sha256": png_digest.sha256,
            "pixel_sha256": pixel_sha256,
            "width_px": width_px,
            "height_px": height_px,
            "dpi": request.quality.dpi,
        },
    }
    _write_exclusive(output_path, png_bytes)
    _write_exclusive(report_path, canonical_json(report) + b"\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return _run(
        _fixed_path(args.source, _WORKER_SOURCE_NAME),
        _fixed_path(args.request, _WORKER_REQUEST_NAME),
        _fixed_path(args.output, _WORKER_PNG_NAME),
        _fixed_path(args.report, _WORKER_REPORT_NAME),
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, ImportError, OSError, RuntimeError, UnicodeError, ValueError):
        # Native failures, malformed input, and local paths never cross the
        # worker boundary.  The parent reports a stable fail-closed error.
        sys.stderr.write("image render worker failed\n")
        raise SystemExit(22) from None
