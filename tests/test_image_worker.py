"""Direct contracts for the contained PDF worker and its trust boundary."""

from __future__ import annotations

import copy
import importlib
import io
import json
import runpy
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import latex_word_review._image_worker as worker
import latex_word_review.image_materializer as materializer
from latex_word_review.canonical import canonical_json
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes
from latex_word_review.image_materializer import (
    REVIEW_QUALITY,
    ImageOperation,
    ImageRequest,
    RendererIdentity,
    image_cache_key,
)


def _renderer(*, pdfium_version: str = "152.0.7947.0") -> RendererIdentity:
    return RendererIdentity(
        name="pypdfium2-pillow",
        pypdfium2_version="5.12.0",
        pdfium_version=pdfium_version,
        pdfium_binary_sha256="sha256:" + "b" * 64,
        pillow_version="12.3.0",
    )


def _request(source_bytes: bytes, *, page: int = 1) -> ImageRequest:
    source_digest = digest_bytes(source_bytes)
    operations = () if page == 1 else (ImageOperation("page", (page,)),)
    return ImageRequest(
        source_path="source.pdf",
        source_format="pdf",
        source_sha256=source_digest.sha256,
        source_size_bytes=source_digest.size_bytes,
        page=page,
        angle_millidegrees=0,
        trim_micro_bp=(0, 0, 0, 0),
        clip=False,
        operations=operations,
    )


def _canonical_png() -> tuple[Any, bytes, int, int, str]:
    image_module = importlib.import_module("PIL.Image")
    image = image_module.new("RGB", (3, 2), (10, 20, 30))
    try:
        png_bytes = materializer._encode_canonical_png(image, REVIEW_QUALITY)
        pixel_sha256 = materializer._pixel_sha256(image)
    finally:
        image.close()
    return image_module, png_bytes, 3, 2, pixel_sha256


def _one_page_pdf() -> bytes:
    """Build a public deterministic 72-point RGB page without another dependency."""

    content = b"0.125 0.500 0.875 rg\n0 0 72 72 re f\n"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Resources << >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"endstream",
    )
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, value in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(value)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(b"xref\n0 5\n0000000000 65535 f \n")
    for offset in offsets:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n").encode("ascii")
    )
    return bytes(payload)


def _write_worker_request(
    root: Path,
    source_bytes: bytes,
    request: ImageRequest,
    renderer: RendererIdentity,
    *,
    document: dict[str, object] | None = None,
) -> None:
    (root / materializer._WORKER_SOURCE_NAME).write_bytes(source_bytes)
    payload = document or materializer._worker_request_document(request, renderer)
    (root / materializer._WORKER_REQUEST_NAME).write_bytes(canonical_json(payload) + b"\n")


def _write_worker_output(
    root: Path,
    request: ImageRequest,
    renderer: RendererIdentity,
    png_bytes: bytes,
    width_px: int,
    height_px: int,
    pixel_sha256: str,
) -> dict[str, object]:
    png_digest = digest_bytes(png_bytes)
    report: dict[str, object] = {
        "format": materializer._WORKER_REPORT_FORMAT,
        "request_sha256": request.request_sha256,
        "renderer": renderer.as_dict(),
        "output": {
            "path": materializer._WORKER_PNG_NAME,
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
    (root / materializer._WORKER_PNG_NAME).write_bytes(png_bytes)
    (root / materializer._WORKER_REPORT_NAME).write_bytes(canonical_json(report) + b"\n")
    return report


def _assert_error(code: ErrorCode, operation: Any) -> ContractError:
    with pytest.raises(ContractError) as raised:
        operation()
    assert raised.value.code is code
    return raised.value


def test_worker_main_writes_bound_report_and_parent_reverifies_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bytes = b"synthetic-pdf-evidence"
    request = _request(source_bytes)
    renderer = _renderer()
    image_module, png_bytes, width_px, height_px, pixel_sha256 = _canonical_png()
    _write_worker_request(tmp_path, source_bytes, request, renderer)
    runtime = SimpleNamespace(identity=renderer)
    monkeypatch.setattr(worker, "_load_pdf_runtime", lambda: runtime)
    monkeypatch.setattr(
        worker,
        "_render_pdf",
        lambda source, parsed, loaded: (
            png_bytes,
            width_px,
            height_px,
            pixel_sha256,
        ),
    )
    monkeypatch.chdir(tmp_path)

    assert (
        worker.main(
            [
                "--source",
                materializer._WORKER_SOURCE_NAME,
                "--request",
                materializer._WORKER_REQUEST_NAME,
                "--output",
                materializer._WORKER_PNG_NAME,
                "--report",
                materializer._WORKER_REPORT_NAME,
            ]
        )
        == 0
    )
    verified = materializer._verify_worker_output(
        tmp_path,
        request,
        renderer,
        image_module,
    )
    assert verified == (png_bytes, width_px, height_px, pixel_sha256)
    report = json.loads((tmp_path / materializer._WORKER_REPORT_NAME).read_bytes())
    assert report["request_sha256"] == request.request_sha256

    _assert_error(
        ErrorCode.PATH_TRAVERSAL,
        lambda: worker._fixed_path("../source.pdf", materializer._WORKER_SOURCE_NAME),
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("shape", ErrorCode.SCHEMA_INVALID),
        ("format", ErrorCode.SCHEMA_INVALID),
        ("request_hash", ErrorCode.HASH_INTEGRITY_MISMATCH),
        ("source_hash", ErrorCode.HASH_SOURCE_MISMATCH),
        ("renderer", ErrorCode.TOOL_VERSION_UNSUPPORTED),
    ],
)
def test_worker_rejects_each_untrusted_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: ErrorCode,
) -> None:
    source_bytes = b"bound-source"
    request = _request(source_bytes)
    renderer = _renderer()
    document = materializer._worker_request_document(request, renderer)
    if case == "shape":
        document["unexpected"] = True
    elif case == "format":
        document["format"] = "unsupported"
    elif case == "request_hash":
        document["request_sha256"] = "sha256:" + "0" * 64
    elif case == "source_hash":
        source_bytes = b"changed-after-request"

    _write_worker_request(tmp_path, source_bytes, request, renderer, document=document)
    runtime_identity = _renderer(pdfium_version="different") if case == "renderer" else renderer
    monkeypatch.setattr(
        worker,
        "_load_pdf_runtime",
        lambda: SimpleNamespace(identity=runtime_identity),
    )
    monkeypatch.setattr(worker, "_render_pdf", lambda *args: (b"png", 1, 1, "sha256:" + "c" * 64))

    _assert_error(
        expected,
        lambda: worker._run(
            tmp_path / materializer._WORKER_SOURCE_NAME,
            tmp_path / materializer._WORKER_REQUEST_NAME,
            tmp_path / materializer._WORKER_PNG_NAME,
            tmp_path / materializer._WORKER_REPORT_NAME,
        ),
    )
    assert not (tmp_path / materializer._WORKER_PNG_NAME).exists()
    assert not (tmp_path / materializer._WORKER_REPORT_NAME).exists()


def test_worker_serializers_reject_noncanonical_shapes(tmp_path: Path) -> None:
    source_bytes = b"source"
    request = _request(source_bytes)
    renderer = _renderer()
    request_payload = request.as_cache_input()

    invalid_quality_values: list[object] = [None, {"name": "review"}]
    wrong_name = copy.deepcopy(request.quality.as_dict())
    wrong_name["name"] = 1
    invalid_quality_values.append(wrong_name)
    wrong_integer = copy.deepcopy(request.quality.as_dict())
    wrong_integer["dpi"] = True
    invalid_quality_values.append(wrong_integer)
    for value in invalid_quality_values:
        _assert_error(
            ErrorCode.SCHEMA_INVALID,
            lambda value=value: materializer._quality_from_dict(value),
        )

    invalid_requests: list[object] = [None, {"source_path": "source.pdf"}]
    bad_source = copy.deepcopy(request_payload)
    bad_source["source_format"] = "gif"
    invalid_requests.append(bad_source)
    bad_trim = copy.deepcopy(request_payload)
    bad_trim["trim_micro_bp"] = [0]
    invalid_requests.append(bad_trim)
    bad_operations = copy.deepcopy(request_payload)
    bad_operations["operations"] = {}
    invalid_requests.append(bad_operations)
    bad_operation_shape = copy.deepcopy(request_payload)
    bad_operation_shape["operations"] = [{"kind": "page"}]
    invalid_requests.append(bad_operation_shape)
    bad_operation_value = copy.deepcopy(request_payload)
    bad_operation_value["operations"] = [{"kind": "unknown", "values": [1]}]
    invalid_requests.append(bad_operation_value)
    bad_clip = copy.deepcopy(request_payload)
    bad_clip["clip"] = 1
    invalid_requests.append(bad_clip)
    bad_integer = copy.deepcopy(request_payload)
    bad_integer["source_size_bytes"] = True
    invalid_requests.append(bad_integer)
    for value in invalid_requests:
        _assert_error(
            ErrorCode.SCHEMA_INVALID,
            lambda value=value: materializer._image_request_from_dict(value),
        )

    invalid_renderers: list[object] = [None, {"name": "pypdfium2-pillow"}]
    wrong_renderer_name = renderer.as_dict()
    wrong_renderer_name["name"] = "other"
    invalid_renderers.append(wrong_renderer_name)
    wrong_renderer_value: dict[str, object] = {}
    wrong_renderer_value.update(renderer.as_dict())
    wrong_renderer_value["pillow_version"] = 12
    invalid_renderers.append(wrong_renderer_value)
    for value in invalid_renderers:
        _assert_error(
            ErrorCode.SCHEMA_INVALID,
            lambda value=value: materializer._renderer_identity_from_dict(value),
        )

    invalid_documents = [
        b"not-json\n",
        b'{"a":1,"a":2}\n',
        canonical_json([1]) + b"\n",
        b'{ "a": 1 }\n',
    ]
    for index, data in enumerate(invalid_documents):
        path = tmp_path / f"invalid-{index}.json"
        path.write_bytes(data)
        _assert_error(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            lambda path=path: materializer._read_canonical_object(
                path,
                max_bytes=1024,
                label="test object",
            ),
        )

    existing = tmp_path / "existing.bin"
    existing.write_bytes(b"first")
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT,
        lambda: materializer._write_exclusive(existing, b"second"),
    )
    assert existing.read_bytes() == b"first"


@pytest.mark.parametrize(
    "case",
    ["shape", "binding", "output_shape", "output_values", "digest", "pixels"],
)
def test_parent_rejects_tampered_worker_artifacts(tmp_path: Path, case: str) -> None:
    source_bytes = b"source"
    request = _request(source_bytes)
    renderer = _renderer()
    image_module, png_bytes, width_px, height_px, pixel_sha256 = _canonical_png()
    report = _write_worker_output(
        tmp_path,
        request,
        renderer,
        png_bytes,
        width_px,
        height_px,
        pixel_sha256,
    )
    output = report["output"]
    assert isinstance(output, dict)
    if case == "shape":
        report["extra"] = True
    elif case == "binding":
        report["request_sha256"] = "sha256:" + "0" * 64
    elif case == "output_shape":
        output.pop("dpi")
    elif case == "output_values":
        output["mode"] = "RGBA"
    elif case == "digest":
        output["size_bytes"] = len(png_bytes) + 1
    elif case == "pixels":
        output["width_px"] = width_px + 1
    (tmp_path / materializer._WORKER_REPORT_NAME).write_bytes(canonical_json(report) + b"\n")

    _assert_error(
        ErrorCode.HASH_INTEGRITY_MISMATCH,
        lambda: materializer._verify_worker_output(
            tmp_path,
            request,
            renderer,
            image_module,
        ),
    )


@pytest.mark.parametrize(
    "case",
    [
        "not_directory",
        "member_set",
        "manifest_shape",
        "binding",
        "output_shape",
        "output_values",
        "pixels",
        "noncanonical_png",
    ],
)
def test_cache_verifier_rejects_structural_and_pixel_tampering(
    tmp_path: Path,
    case: str,
) -> None:
    request = _request(b"source")
    renderer = _renderer()
    key = image_cache_key(request, renderer)
    image_module, png_bytes, width_px, height_px, pixel_sha256 = _canonical_png()
    entry = tmp_path / key.sha256.removeprefix("sha256:")
    if case == "not_directory":
        entry.write_bytes(b"not-a-directory")
    else:
        entry.mkdir()
        if case == "noncanonical_png":
            image = image_module.new("RGB", (width_px, height_px), (10, 20, 30))
            try:
                output = io.BytesIO()
                image.save(
                    output,
                    format="PNG",
                    compress_level=0,
                    optimize=False,
                    dpi=(REVIEW_QUALITY.dpi, REVIEW_QUALITY.dpi),
                )
                png_bytes = output.getvalue()
            finally:
                image.close()
        manifest = materializer._cache_manifest(
            request,
            key,
            png_bytes=png_bytes,
            pixel_sha256=pixel_sha256,
            width_px=width_px,
            height_px=height_px,
        )
        manifest_output = manifest["output"]
        assert isinstance(manifest_output, dict)
        if case == "manifest_shape":
            manifest["extra"] = True
        elif case == "binding":
            manifest["request_sha256"] = "sha256:" + "0" * 64
        elif case == "output_shape":
            manifest_output.pop("dpi")
        elif case == "output_values":
            manifest_output["path"] = "other.png"
        elif case == "pixels":
            manifest_output["height_px"] = height_px + 1
        (entry / materializer._CACHE_PNG_NAME).write_bytes(png_bytes)
        (entry / materializer._CACHE_MANIFEST_NAME).write_bytes(canonical_json(manifest) + b"\n")
        if case == "member_set":
            (entry / "unexpected").write_bytes(b"x")

    _assert_error(
        ErrorCode.HASH_INTEGRITY_MISMATCH,
        lambda: materializer._verify_cache_entry(
            entry,
            request,
            key,
            renderer,
            image_module,
            reused=True,
        ),
    )


def test_render_pdf_classifies_open_security_page_and_runtime_failures() -> None:
    request = _request(b"source")
    renderer = _renderer()
    image_module = importlib.import_module("PIL.Image")

    class PdfOpenError(Exception):
        def __init__(self, err_code: int | None = None) -> None:
            super().__init__("open failed")
            self.err_code = err_code

    for error, expected_message in (
        (PdfOpenError(), "corrupt"),
        (PdfOpenError(7), "encrypted"),
    ):
        runtime = materializer._PdfRuntime(
            SimpleNamespace(
                PdfDocument=lambda *args, error=error, **kwargs: (_ for _ in ()).throw(error)
            ),
            SimpleNamespace(FPDF_ERR_PASSWORD=7),
            image_module,
            renderer,
        )
        raised = _assert_error(
            ErrorCode.BACKEND_FAILED,
            lambda runtime=runtime: materializer._render_pdf(b"bad", request, runtime),
        )
        assert expected_message in str(raised)

    class FakeDocument:
        def __init__(self) -> None:
            self.raw = object()
            self.closed = False

        def __len__(self) -> int:
            return 1

        def close(self) -> None:
            self.closed = True

    for case in ("encrypted", "page", "runtime"):
        document = FakeDocument()

        def security_revision(raw: object, *, case: str = case) -> int:
            if case == "runtime":
                raise RuntimeError("native failure")
            return 4 if case == "encrypted" else -1

        runtime = materializer._PdfRuntime(
            SimpleNamespace(PdfDocument=lambda *args, document=document, **kwargs: document),
            SimpleNamespace(
                FPDF_ERR_PASSWORD=7,
                FPDF_GetSecurityHandlerRevision=security_revision,
            ),
            image_module,
            renderer,
        )
        selected_request = (
            replace(request, page=2, operations=(ImageOperation("page", (2,)),))
            if case == "page"
            else request
        )
        raised = _assert_error(
            ErrorCode.BACKEND_FAILED,
            lambda runtime=runtime, selected_request=selected_request: materializer._render_pdf(
                b"pdf",
                selected_request,
                runtime,
            ),
        )
        if case == "runtime":
            assert "PDF rendering failed safely" in str(raised)
        assert document.closed is True

    image = image_module.new("RGB", (3, 2), (1, 2, 3))
    try:
        assert materializer._rotate_image(image, 0, image_module) is image
        rotated = materializer._rotate_image(image, 90_000, image_module)
        try:
            assert rotated.size == (2, 3)
        finally:
            rotated.close()
    finally:
        image.close()


def test_render_pdf_directly_covers_native_success_and_resource_cleanup() -> None:
    source_bytes = _one_page_pdf()
    request = _request(source_bytes)
    runtime = materializer._load_pdf_runtime()

    png_bytes, width_px, height_px, pixel_sha256 = materializer._render_pdf(
        source_bytes,
        request,
        runtime,
    )

    assert (width_px, height_px) == (200, 200)
    assert digest_bytes(png_bytes).size_bytes <= request.quality.max_png_bytes
    decoded = materializer._decode_png(png_bytes, request.quality, runtime.image_module)
    try:
        assert decoded.size == (width_px, height_px)
        assert materializer._pixel_sha256(decoded) == pixel_sha256
    finally:
        decoded.close()


def test_worker_module_entrypoint_collapses_failures_to_stable_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "latex_word_review._image_worker",
            "--source",
            "not-the-fixed-source.pdf",
            "--request",
            materializer._WORKER_REQUEST_NAME,
            "--output",
            materializer._WORKER_PNG_NAME,
            "--report",
            materializer._WORKER_REPORT_NAME,
        ],
    )
    with (
        pytest.warns(RuntimeWarning, match="found in sys.modules"),
        pytest.raises(SystemExit) as raised,
    ):
        runpy.run_module("latex_word_review._image_worker", run_name="__main__")
    assert raised.value.code == 22


def test_scanner_reports_commands_outside_the_rewrite_bound() -> None:
    scan = materializer.scan_graphics_text(
        "\\includegraphics" + " " * (17 * 1024) + "{figure.pdf}",
        source_path="main.tex",
    )
    assert scan.includes == ()
    assert [diagnostic.code for diagnostic in scan.diagnostics] == ["IMAGE_PARSE_MALFORMED"]

    _assert_error(
        ErrorCode.PATH_ABSOLUTE,
        lambda: materializer._normalize_graphics_directory(""),
    )
