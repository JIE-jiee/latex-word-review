"""Contracts for static graphicx scanning and deterministic PDF previews."""

from __future__ import annotations

import importlib
import importlib.metadata
import io
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

import latex_word_review.image_materializer as materializer_module
from latex_word_review.canonical import canonical_json
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.image_materializer import (
    REVIEW_QUALITY,
    ImageOperation,
    ImageRequest,
    IncludeGraphics,
    QualityProfile,
    RendererIdentity,
    SealedImageCacheVerifier,
    image_cache_key,
    materialize_image,
    plan_image_request,
    resolve_graphic,
    scan_graphics_source,
    scan_graphics_text,
    verify_image_cache_entry,
)


def _minimal_pdf(
    colors: tuple[tuple[float, float, float], ...],
    *,
    width: int = 72,
    height: int = 72,
) -> bytes:
    """Build a public, deterministic PDF without another PDF dependency."""

    page_numbers = [3 + index * 2 for index in range(len(colors))]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Count {len(colors)} /Kids "
            f"[{' '.join(f'{number} 0 R' for number in page_numbers)}] >>"
        ).encode("ascii"),
    ]
    for index, (red, green, blue) in enumerate(colors):
        page_number = page_numbers[index]
        content_number = page_number + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                f"/Resources << >> /Contents {content_number} 0 R >>"
            ).encode("ascii")
        )
        content = (f"{red:.3f} {green:.3f} {blue:.3f} rg\n0 0 {width} {height} re f\n").encode(
            "ascii"
        )
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"endstream"
        )

    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(value)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def _write_source(root: Path, latex: str, *, name: str = "main.tex") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(latex, encoding="utf-8", newline="\n")


def _scan_one(root: Path, latex: str) -> IncludeGraphics:
    _write_source(root, latex)
    scan = scan_graphics_source(root, "main.tex")
    assert len(scan.includes) == 1
    return scan.includes[0]


def _require_pdf_runtime() -> None:
    if os.name != "nt":
        pytest.skip("Windows-only PDF renderer")
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL.Image")


def _assert_error(code: ErrorCode, operation: Callable[[], object]) -> ContractError:
    with pytest.raises(ContractError) as raised:
        operation()
    assert raised.value.code is code
    return raised.value


def _plan_pdf(
    root: Path,
    *,
    options: str = "",
    quality: QualityProfile = REVIEW_QUALITY,
    pdf_name: str = "multi.pdf",
) -> ImageRequest:
    option_text = f"[{options}]" if options else ""
    occurrence = _scan_one(
        root,
        "\\graphicspath{{figures/}}\n"
        "\\DeclareGraphicsExtensions{.pdf}\n"
        f"\\includegraphics{option_text}{{{Path(pdf_name).stem}}}\n",
    )
    plan = plan_image_request(root, occurrence, quality=quality)
    assert plan.diagnostics == ()
    assert plan.request is not None
    return plan.request


def test_scan_preserves_directive_state_comments_and_option_order() -> None:
    text = r"""
% \includegraphics{ignored}
\graphicspath{{figures/}{assets/}}
\DeclareGraphicsExtensions{.pdf,.svg}
\includegraphics [page=2,trim=1bp 2bp 3bp 4bp,clip,angle=90]{plot}
"""
    scan = scan_graphics_text(text, source_path="main.tex")
    assert [directive.kind for directive in scan.directives] == [
        "graphicspath",
        "DeclareGraphicsExtensions",
    ]
    assert scan.directives[0].values == ("figures/", "assets/")
    assert scan.directives[1].values == (".pdf", ".svg")
    assert len(scan.includes) == 1
    occurrence = scan.includes[0]
    assert occurrence.graphic_paths == ("figures/", "assets/")
    assert occurrence.extensions == (".pdf", ".svg")
    assert [(option.key, option.value, option.ordinal) for option in occurrence.options] == [
        ("page", "2", 0),
        ("trim", "1bp 2bp 3bp 4bp", 1),
        ("clip", None, 2),
        ("angle", "90", 3),
    ]
    assert occurrence.line == 5
    assert scan.diagnostics == ()


def test_scan_is_bounded_and_malformed_commands_are_manual() -> None:
    with pytest.raises(ContractError) as raised:
        scan_graphics_text(
            "x" * 11,
            source_path="main.tex",
            quality=replace(REVIEW_QUALITY, max_tex_bytes=10),
        )
    assert raised.value.code is ErrorCode.SCHEMA_INVALID

    scan = scan_graphics_text(
        "\\graphicspath{figures/}\n\\includegraphics[page=2{plot}\n",
        source_path="main.tex",
    )
    assert {item.code for item in scan.diagnostics} == {
        "IMAGE_PARSE_MALFORMED",
    }


def test_plan_normalizes_supported_page_trim_clip_and_angle(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "figures").mkdir(parents=True)
    (root / "figures/plot.pdf").write_bytes(_minimal_pdf(((1, 0, 0), (0, 0, 1))))
    occurrence = _scan_one(
        root,
        "\\graphicspath{{figures/}}\n"
        "\\DeclareGraphicsExtensions{.pdf,.svg}\n"
        "\\includegraphics[page=2,trim=1bp 2bp 3bp 4bp,clip,angle=-90,"
        "width=.72\\linewidth,scale=.8,keepaspectratio]{plot}\n",
    )
    plan = plan_image_request(root, occurrence)
    assert plan.diagnostics == ()
    assert plan.request is not None
    assert plan.request.source_path == "figures/plot.pdf"
    assert plan.request.page == 2
    assert plan.request.angle_millidegrees == 270_000
    assert plan.request.trim_micro_bp == (1_000_000, 2_000_000, 3_000_000, 4_000_000)
    assert [operation.kind for operation in plan.request.operations] == [
        "page",
        "trim",
        "clip",
        "angle",
    ]
    assert plan.blocking_diagnostics == ()
    assert [option.key for option in plan.passthrough_options] == [
        "width",
        "scale",
        "keepaspectratio",
    ]
    assert plan.passthrough_option_text == "width=.72\\linewidth,scale=.8,keepaspectratio"


@pytest.mark.parametrize(
    ("latex", "diagnostic"),
    [
        (r"\includegraphics[trim=1 2 3 4]{plot.pdf}", "IMAGE_TRIM_REQUIRES_CLIP"),
        (
            r"\includegraphics[width=\dimexpr.5\linewidth\relax]{plot.pdf}",
            "IMAGE_DYNAMIC_INPUT",
        ),
        (r"\includegraphics{figures/\jobname.pdf}", "IMAGE_DYNAMIC_INPUT"),
        (r"\includegraphics{./figures/\jobname.pdf}", "IMAGE_DYNAMIC_INPUT"),
        (
            r"\includegraphics[angle=90,trim=1 2 3 4,clip]{plot.pdf}",
            "IMAGE_UNSUPPORTED_OPTION",
        ),
        (
            r"\includegraphics[width=.72\linewidth,angle=90]{plot.pdf}",
            "IMAGE_UNSUPPORTED_OPTION",
        ),
    ],
)
def test_dynamic_and_layout_dependent_semantics_stay_manual(
    tmp_path: Path,
    latex: str,
    diagnostic: str,
) -> None:
    root = tmp_path / "source"
    occurrence = _scan_one(root, latex)
    plan = plan_image_request(root, occurrence)
    assert plan.request is None
    assert diagnostic in {item.code for item in plan.diagnostics}


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("/absolute/plot.pdf", ErrorCode.PATH_ABSOLUTE),
        ("//server/share/plot.pdf", ErrorCode.PATH_ABSOLUTE),
        ("C:/private/plot.pdf", ErrorCode.PATH_ABSOLUTE),
        ("../plot.pdf", ErrorCode.PATH_TRAVERSAL),
        ("./../plot.pdf", ErrorCode.PATH_TRAVERSAL),
        (".//plot.pdf", ErrorCode.PATH_ABSOLUTE),
        ("figures/plot.pdf:secret", ErrorCode.PATH_TRAVERSAL),
    ],
)
def test_image_lookup_rejects_unsafe_windows_paths(
    tmp_path: Path,
    target: str,
    code: ErrorCode,
) -> None:
    root = tmp_path / "source"
    occurrence = _scan_one(root, f"\\includegraphics{{{target}}}")
    with pytest.raises(ContractError) as raised:
        plan_image_request(root, occurrence)
    assert raised.value.code is code


def test_resolver_handles_explicit_and_extensionless_and_rejects_ambiguity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    (root / "figures").mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "direct.pdf").write_bytes(b"explicit")
    (root / "figures/plot.pdf").write_bytes(b"first")
    _write_source(root, "placeholder")

    explicit = scan_graphics_text("\\includegraphics{direct.pdf}", source_path="main.tex").includes[
        0
    ]
    assert resolve_graphic(root, explicit).source_path == "direct.pdf"

    extensionless = scan_graphics_text(
        "\\graphicspath{{figures/}}\n"
        "\\DeclareGraphicsExtensions{.pdf,.svg}\n"
        "\\includegraphics{plot}",
        source_path="main.tex",
    ).includes[0]
    assert resolve_graphic(root, extensionless).source_path == "figures/plot.pdf"

    (root / "assets/plot.pdf").write_bytes(b"second")
    ambiguous = scan_graphics_text(
        "\\graphicspath{{figures/}{assets/}}\n"
        "\\DeclareGraphicsExtensions{.pdf}\n"
        "\\includegraphics{plot}",
        source_path="main.tex",
    ).includes[0]
    with pytest.raises(ContractError) as raised:
        resolve_graphic(root, ambiguous)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert "ambiguous" in str(raised.value)


def test_resolver_accepts_exact_current_directory_prefixes_in_graphicspath(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    (root / "Figure").mkdir(parents=True)
    (root / "Figure/plot.pdf").write_bytes(b"figure")
    occurrence = scan_graphics_text(
        "\\graphicspath{{././Figure/}}\n\\includegraphics{plot.pdf}",
        source_path="main.tex",
    ).includes[0]
    direct = scan_graphics_text(
        "\\includegraphics{././Figure/plot.pdf}",
        source_path="main.tex",
    ).includes[0]

    assert resolve_graphic(root, occurrence).source_path == "Figure/plot.pdf"
    assert resolve_graphic(root, direct).source_path == "Figure/plot.pdf"
    assert materializer_module._normalize_graphics_directory("./Figure/") == "Figure"


def test_resolver_searches_extensions_after_an_unknown_dotted_suffix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "3.RCJB-0_hc.pdf").write_bytes(b"pdf")
    occurrence = scan_graphics_text(
        "\\DeclareGraphicsExtensions{.pdf}\n\\includegraphics{3.RCJB-0_hc}",
        source_path="main.tex",
    ).includes[0]

    assert resolve_graphic(root, occurrence).source_path == "3.RCJB-0_hc.pdf"

    (root / "3.RCJB-0_hc").write_bytes(b"exact")
    error = _assert_error(ErrorCode.SCHEMA_INVALID, lambda: resolve_graphic(root, occurrence))
    assert "ambiguous" in str(error)


def test_known_suffix_remains_exact_and_unknown_suffix_needs_static_extensions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "known.pdf.png").write_bytes(b"not the explicit target")
    known = scan_graphics_text("\\includegraphics{known.pdf}", source_path="main.tex").includes[0]
    _assert_error(ErrorCode.BACKEND_FAILED, lambda: resolve_graphic(root, known))

    (root / "known.pdf").write_bytes(b"exact")
    assert resolve_graphic(root, replace(known, extensions_dynamic=True)).source_path == "known.pdf"

    unknown = replace(known, target="3.RCJB-0_hc", extensions_dynamic=True)
    _assert_error(ErrorCode.PATH_TRAVERSAL, lambda: resolve_graphic(root, unknown))
    plan = plan_image_request(root, unknown)
    assert plan.request is None
    assert {item.code for item in plan.diagnostics} == {"IMAGE_DYNAMIC_INPUT"}


@pytest.mark.parametrize(
    ("directory", "code"),
    [
        ("../Figure/", ErrorCode.PATH_TRAVERSAL),
        ("./../Figure/", ErrorCode.PATH_TRAVERSAL),
        ("/Figure/", ErrorCode.PATH_ABSOLUTE),
        (".//Figure/", ErrorCode.PATH_ABSOLUTE),
        (r"Figure\nested/", ErrorCode.PATH_TRAVERSAL),
        (r"./\jobname/", ErrorCode.PATH_TRAVERSAL),
    ],
)
def test_graphicspath_prefix_normalization_remains_fail_closed(
    directory: str,
    code: ErrorCode,
) -> None:
    _assert_error(code, lambda: materializer_module._normalize_graphics_directory(directory))


def test_resolver_rejects_symlink_escape_when_windows_allows_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "source"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "plot.pdf").write_bytes(b"outside")
    _write_source(root, "placeholder")
    try:
        (root / "figures").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows policy does not allow this process to create a symlink")
    occurrence = scan_graphics_text(
        "\\graphicspath{{figures/}}\n\\includegraphics{plot.pdf}",
        source_path="main.tex",
    ).includes[0]
    with pytest.raises(ContractError) as raised:
        resolve_graphic(root, occurrence)
    assert raised.value.code is ErrorCode.PATH_LINK_ESCAPE


def test_request_quality_and_cache_key_are_bounded_and_deterministic() -> None:
    with pytest.raises(ContractError):
        QualityProfile(dpi=601)
    with pytest.raises(ContractError):
        QualityProfile(min_render_dpi=71)
    with pytest.raises(ContractError):
        QualityProfile(render_timeout_s=0)
    with pytest.raises(ContractError):
        QualityProfile(render_timeout_s=61)
    request = ImageRequest(
        source_path="figure.pdf",
        source_format="pdf",
        source_sha256="sha256:" + "a" * 64,
        source_size_bytes=10,
        page=1,
        angle_millidegrees=0,
        trim_micro_bp=(0, 0, 0, 0),
        clip=False,
        operations=(),
    )
    renderer = RendererIdentity(
        name="pypdfium2-pillow",
        pypdfium2_version="5.12.0",
        pdfium_version="152.0.7947.0",
        pdfium_binary_sha256="sha256:" + "b" * 64,
        pillow_version="12.3.0",
    )
    assert materializer_module._MATERIALIZER_POLICY_VERSION == "pdf-png-v3"
    assert materializer_module._CACHE_FORMAT == "latex-word-review-image-cache-v2"
    assert materializer_module._WORKER_REQUEST_FORMAT.endswith("-v2")
    assert materializer_module._WORKER_REPORT_FORMAT.endswith("-v2")
    assert image_cache_key(request, renderer).sha256 == image_cache_key(request, renderer).sha256
    with pytest.raises(ContractError):
        replace(request, page=2)
    with pytest.raises(ContractError):
        replace(
            request,
            trim_micro_bp=(1, 0, 0, 0),
            clip=False,
            operations=(ImageOperation("trim", (1, 0, 0, 0)),),
        )


@pytest.mark.parametrize(
    ("returncode", "timed_out", "output_truncated", "message"),
    [
        (-9, True, False, "timed out"),
        (22, False, False, "failed in containment"),
        (0, False, False, "without complete artifacts"),
        (0, False, True, "failed in containment"),
    ],
)
def test_pdf_worker_failures_are_bounded_fail_closed_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    timed_out: bool,
    output_truncated: bool,
    message: str,
) -> None:
    request = ImageRequest(
        source_path="figure.pdf",
        source_format="pdf",
        source_sha256="sha256:" + "a" * 64,
        source_size_bytes=3,
        page=1,
        angle_millidegrees=0,
        trim_micro_bp=(0, 0, 0, 0),
        clip=False,
        operations=(),
        quality=replace(REVIEW_QUALITY, render_timeout_s=1),
    )
    renderer = RendererIdentity(
        name="pypdfium2-pillow",
        pypdfium2_version="5.12.0",
        pdfium_version="152.0.7947.0",
        pdfium_binary_sha256="sha256:" + "b" * 64,
        pillow_version="12.3.0",
    )
    captured: dict[str, object] = {}

    def fake_run_command(
        executable: str | Path,
        arguments: tuple[str, ...],
        **kwargs: Any,
    ) -> SimpleNamespace:
        captured.update(
            executable=os.fspath(executable),
            arguments=arguments,
            **kwargs,
        )
        return SimpleNamespace(
            returncode=returncode,
            timed_out=timed_out,
            output_truncated=output_truncated,
        )

    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(materializer_module, "run_command", fake_run_command)

    with pytest.raises(ContractError) as raised:
        materializer_module._run_pdf_worker(
            b"pdf",
            request,
            renderer,
            object(),
            cache_root,
        )

    assert raised.value.code is ErrorCode.BACKEND_FAILED
    assert message in str(raised.value)
    assert captured["timeout_s"] == 1.0
    assert captured["max_output_bytes"] == 64 * 1024
    assert cast("tuple[str, ...]", captured["arguments"])[0:2] == (
        "-m",
        "latex_word_review._image_worker",
    )
    assert not list(cache_root.iterdir())


@pytest.mark.parametrize(("extension", "source_format"), [("svg", "svg"), ("eps", "eps")])
def test_svg_and_eps_are_explicitly_manual_without_external_programs(
    tmp_path: Path,
    extension: str,
    source_format: Literal["svg", "eps"],
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    path = root / f"figure.{extension}"
    path.write_bytes(b"static fixture")
    occurrence = _scan_one(root, f"\\includegraphics{{figure.{extension}}}")
    plan = plan_image_request(root, occurrence)
    assert plan.request is None
    assert [item.code for item in plan.diagnostics] == ["IMAGE_UNSUPPORTED_FORMAT"]

    request = ImageRequest(
        source_path=f"figure.{extension}",
        source_format=source_format,
        source_sha256="sha256:" + "a" * 64,
        source_size_bytes=1,
        page=1,
        angle_millidegrees=0,
        trim_micro_bp=(0, 0, 0, 0),
        clip=False,
        operations=(),
    )
    cache = tmp_path / "cache"
    with pytest.raises(ContractError) as raised:
        materialize_image(request, source_root=root, cache_root=cache)
    assert raised.value.code is ErrorCode.BACKEND_CAPABILITY_MISSING
    assert not cache.exists()


def test_pdf_page_selection_rotation_trim_and_determinism(tmp_path: Path) -> None:
    _require_pdf_runtime()
    image_module = importlib.import_module("PIL.Image")
    root = tmp_path / "source"
    (root / "figures").mkdir(parents=True)
    (root / "figures/multi.pdf").write_bytes(
        _minimal_pdf(((1, 0, 0), (0, 0, 1)), width=72, height=36)
    )

    first_request = _plan_pdf(root, options="page=1")
    second_request = _plan_pdf(root, options="page=2")
    transformed_request = _plan_pdf(
        root,
        options="page=1,trim=6bp 0 6bp 0,clip,angle=90",
    )
    first_cache = tmp_path / "cache-one"
    second_cache = tmp_path / "cache-two"
    first = materialize_image(first_request, source_root=root, cache_root=first_cache)
    second = materialize_image(second_request, source_root=root, cache_root=first_cache)
    transformed = materialize_image(
        transformed_request,
        source_root=root,
        cache_root=first_cache,
    )
    repeated = materialize_image(first_request, source_root=root, cache_root=first_cache)
    independent = materialize_image(first_request, source_root=root, cache_root=second_cache)

    assert (first.width_px, first.height_px) == (200, 100)
    assert (transformed.width_px, transformed.height_px) == (100, 166)
    assert first.pixel_sha256 != second.pixel_sha256
    assert repeated.reused is True
    assert repeated.png_sha256 == first.png_sha256
    assert independent.png_sha256 == first.png_sha256
    assert independent.pixel_sha256 == first.pixel_sha256
    assert (second_cache / independent.png_path).read_bytes() == (
        first_cache / first.png_path
    ).read_bytes()

    with image_module.open(first_cache / first.png_path) as image:
        assert image.convert("RGB").getpixel((100, 50))[0] > 240
    with image_module.open(first_cache / second.png_path) as image:
        assert image.convert("RGB").getpixel((100, 50))[2] > 240


def test_large_pdf_uses_highest_bounded_dpi_and_audits_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_pdf_runtime()
    root = tmp_path / "source"
    (root / "figures").mkdir(parents=True)
    (root / "figures/large.pdf").write_bytes(
        _minimal_pdf(((0.2, 0.4, 0.6),), width=1800, height=1000)
    )
    request = _plan_pdf(
        root,
        pdf_name="large.pdf",
        quality=replace(REVIEW_QUALITY, max_render_pixels=3_500_000),
    )
    runtime = materializer_module._load_pdf_runtime()

    def render_in_contained_worker_shape(
        source_bytes: bytes,
        selected_request: ImageRequest,
        renderer: RendererIdentity,
        image_module: Any,
        cache_root: Path,
    ) -> tuple[bytes, int, int, str, int]:
        assert renderer == runtime.identity
        assert image_module is runtime.image_module
        assert cache_root.is_dir()
        return materializer_module._render_pdf(source_bytes, selected_request, runtime)

    monkeypatch.setattr(materializer_module, "_run_pdf_worker", render_in_contained_worker_shape)
    cache = tmp_path / "cache"
    first = materialize_image(request, source_root=root, cache_root=cache)
    original_decode = materializer_module._decode_png
    original_encode = materializer_module._encode_canonical_png
    verification_calls = {"decode": 0, "encode": 0}

    def tracked_decode(*args: Any, **kwargs: Any) -> Any:
        verification_calls["decode"] += 1
        return original_decode(*args, **kwargs)

    def tracked_encode(*args: Any, **kwargs: Any) -> bytes:
        verification_calls["encode"] += 1
        return original_encode(*args, **kwargs)

    monkeypatch.setattr(materializer_module, "_decode_png", tracked_decode)
    monkeypatch.setattr(materializer_module, "_encode_canonical_png", tracked_encode)
    repeated = materialize_image(request, source_root=root, cache_root=cache)
    entry = (cache / first.png_path).parent
    verified = verify_image_cache_entry(
        entry,
        request=request.as_cache_input(),
        renderer=runtime.identity.as_dict(),
    )
    strong_calls = dict(verification_calls)
    sealed_verifier = SealedImageCacheVerifier(runtime.identity.as_dict())
    sealed = sealed_verifier.verify(
        entry,
        request=request.as_cache_input(),
        renderer=runtime.identity.as_dict(),
    )
    manifest = json.loads((entry / "manifest.json").read_bytes())

    assert request.quality.dpi == 200
    assert request.quality.min_render_dpi == 96
    assert first.dpi == repeated.dpi == verified.dpi == 100
    assert sealed == replace(verified, reused=True)
    assert strong_calls["decode"] >= 2
    assert strong_calls["encode"] >= 2
    assert verification_calls == strong_calls
    assert (first.width_px, first.height_px) == (2500, 1389)
    assert first.width_px * first.height_px <= request.quality.max_render_pixels
    assert repeated.reused is True
    assert manifest["request"]["quality"]["dpi"] == 200
    assert manifest["output"]["dpi"] == 100
    with runtime.image_module.open(cache / first.png_path) as image:
        horizontal_dpi, vertical_dpi = image.info["dpi"]
        assert horizontal_dpi == pytest.approx(100, abs=0.05)
        assert vertical_dpi == pytest.approx(100, abs=0.05)

    changed_renderer = runtime.identity.as_dict()
    changed_renderer["pdfium_version"] = "0.0.0.0"
    _assert_error(
        ErrorCode.HASH_INTEGRITY_MISMATCH,
        lambda: SealedImageCacheVerifier(changed_renderer),
    )
    png_path = entry / "preview.png"
    original_png = png_path.read_bytes()
    png_path.write_bytes(original_png[:-1] + bytes([original_png[-1] ^ 1]))
    _assert_error(
        ErrorCode.HASH_INTEGRITY_MISMATCH,
        lambda: sealed_verifier.verify(
            entry,
            request=request.as_cache_input(),
            renderer=runtime.identity.as_dict(),
        ),
    )


def test_adaptive_dpi_never_upscales_and_fails_below_its_floor() -> None:
    assert materializer_module._select_effective_dpi(
        1500,
        1500,
        (0, 0, 0, 0),
        0,
        REVIEW_QUALITY,
    ) == (166, 3459, 3459)

    low_request = replace(REVIEW_QUALITY, dpi=72, min_render_dpi=96)
    assert materializer_module._select_effective_dpi(
        72,
        72,
        (0, 0, 0, 0),
        0,
        low_request,
    ) == (72, 72, 72)

    dimension_limited = replace(
        REVIEW_QUALITY,
        max_dimension_px=1000,
        max_render_pixels=100_000_000,
    )
    assert materializer_module._select_effective_dpi(
        720,
        360,
        (0, 0, 0, 0),
        0,
        dimension_limited,
    ) == (100, 1000, 500)

    too_large = replace(REVIEW_QUALITY, dpi=200, min_render_dpi=96)
    error = _assert_error(
        ErrorCode.BACKEND_FAILED,
        lambda: materializer_module._select_effective_dpi(
            3000,
            3000,
            (0, 0, 0, 0),
            0,
            too_large,
        ),
    )
    assert "minimum permitted DPI" in str(error)


def test_graphics_in_included_files_resolve_from_backend_project_root(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "sections").mkdir(parents=True)
    (root / "figures").mkdir()
    (root / "figures/plot.pdf").write_bytes(_minimal_pdf(((1, 0, 0),)))
    (root / "sections/chapter.tex").write_text(
        "\\includegraphics{figures/plot.pdf}\n",
        encoding="utf-8",
        newline="\n",
    )

    occurrence = scan_graphics_source(root, "sections/chapter.tex").includes[0]

    assert resolve_graphic(root, occurrence).source_path == "figures/plot.pdf"


def test_pdf_page_corruption_encryption_limits_and_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_pdf_runtime()
    root = tmp_path / "source"
    (root / "figures").mkdir(parents=True)
    pdf_path = root / "figures/multi.pdf"
    pdf_path.write_bytes(_minimal_pdf(((1, 0, 0), (0, 0, 1))))

    out_of_range = _plan_pdf(root, options="page=3")
    with pytest.raises(ContractError) as raised:
        materialize_image(out_of_range, source_root=root, cache_root=tmp_path / "page-cache")
    assert raised.value.code is ErrorCode.BACKEND_FAILED

    limited = _plan_pdf(
        root,
        quality=replace(REVIEW_QUALITY, max_render_pixels=9_000),
    )
    with pytest.raises(ContractError) as raised:
        materialize_image(limited, source_root=root, cache_root=tmp_path / "limit-cache")
    assert raised.value.code is ErrorCode.BACKEND_FAILED

    encrypted = _plan_pdf(root)
    raw_module = importlib.import_module("pypdfium2.raw")
    monkeypatch.setattr(raw_module, "FPDF_GetSecurityHandlerRevision", lambda document: 4)
    runtime = materializer_module._load_pdf_runtime()
    with pytest.raises(ContractError) as raised:
        materializer_module._render_pdf(pdf_path.read_bytes(), encrypted, runtime)
    assert raised.value.code is ErrorCode.BACKEND_FAILED
    assert "encrypted" in str(raised.value)
    monkeypatch.undo()

    drifted = _plan_pdf(root)
    pdf_path.write_bytes(pdf_path.read_bytes() + b"\n% drift")
    with pytest.raises(ContractError) as raised:
        materialize_image(drifted, source_root=root, cache_root=tmp_path / "drift-cache")
    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH
    assert not (tmp_path / "drift-cache").exists()

    pdf_path.write_bytes(b"not a PDF")
    corrupt = _plan_pdf(root)
    with pytest.raises(ContractError) as raised:
        materialize_image(corrupt, source_root=root, cache_root=tmp_path / "corrupt-cache")
    assert raised.value.code is ErrorCode.BACKEND_FAILED


@pytest.mark.parametrize("member", ["preview.png", "manifest.json"])
def test_cache_tampering_is_detected_without_overwrite(tmp_path: Path, member: str) -> None:
    _require_pdf_runtime()
    root = tmp_path / "source"
    (root / "figures").mkdir(parents=True)
    (root / "figures/multi.pdf").write_bytes(_minimal_pdf(((1, 0, 0),)))
    request = _plan_pdf(root)
    cache = tmp_path / "cache"
    result = materialize_image(request, source_root=root, cache_root=cache)
    entry = (cache / result.png_path).parent
    path = entry / member
    if member == "preview.png":
        tampered = path.read_bytes() + b"tamper"
    else:
        manifest = json.loads(path.read_bytes())
        manifest["output"]["sha256"] = "sha256:" + "0" * 64
        tampered = canonical_json(manifest) + b"\n"
    path.write_bytes(tampered)

    with pytest.raises(ContractError) as raised:
        materialize_image(request, source_root=root, cache_root=cache)
    assert raised.value.code is ErrorCode.HASH_INTEGRITY_MISMATCH
    assert path.read_bytes() == tampered


def test_cache_must_be_disjoint_from_source(tmp_path: Path) -> None:
    _require_pdf_runtime()
    root = tmp_path / "source"
    (root / "figures").mkdir(parents=True)
    (root / "figures/multi.pdf").write_bytes(_minimal_pdf(((1, 0, 0),)))
    request = _plan_pdf(root)
    cache = root / "cache"
    with pytest.raises(ContractError) as raised:
        materialize_image(request, source_root=root, cache_root=cache)
    assert raised.value.code is ErrorCode.PATH_TRAVERSAL
    assert not cache.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Bad Name"},
        {"dpi": 71},
        {"dpi": 200.0},
        {"min_render_dpi": 71},
        {"min_render_dpi": True},
        {"max_tex_bytes": 0},
        {"max_graphics_commands": 0},
        {"max_source_bytes": 0},
        {"max_document_pages": 0},
        {"max_page_points": 0},
        {"max_dimension_px": 0},
        {"max_render_pixels": 0},
        {"max_png_bytes": 0},
    ],
)
def test_quality_profile_rejects_every_unbounded_dimension(changes: dict[str, object]) -> None:
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: replace(REVIEW_QUALITY, **cast("Any", changes)),
    )


def test_value_objects_reject_inconsistent_data_and_serialize() -> None:
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: materializer_module.GraphicOption("bad key", None, 0),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: materializer_module.GraphicOption("width", "x" * 2049, 0),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: materializer_module.GraphicOption("width", "1cm", 128),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: materializer_module.GraphicOption("width", "1cm", 0, ""),
    )
    diagnostic = materializer_module.GraphicsDiagnostic(
        "IMAGE_DYNAMIC_INPUT", "manual", "main.tex", 1, 2
    )
    assert diagnostic.as_dict()["disposition"] == "manual"
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: materializer_module.GraphicsDiagnostic(
            "IMAGE_DYNAMIC_INPUT", "manual", "main.tex", 0, 1
        ),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: materializer_module.ResolvedGraphic("x.pdf", "pdf", -1, "bad"),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: ImageOperation("page", (1, 2)),
    )

    request = ImageRequest(
        source_path="figure.pdf",
        source_format="pdf",
        source_sha256="sha256:" + "a" * 64,
        source_size_bytes=10,
        page=1,
        angle_millidegrees=0,
        trim_micro_bp=(0, 0, 0, 0),
        clip=False,
        operations=(),
    )
    bad_requests: list[Callable[[], object]] = [
        lambda: replace(request, source_format=cast("Any", "svg")),
        lambda: replace(request, source_sha256="bad"),
        lambda: replace(request, source_size_bytes=-1),
        lambda: replace(request, page=0),
        lambda: replace(request, angle_millidegrees=360_000),
        lambda: replace(request, trim_micro_bp=(-1, 0, 0, 0)),
        lambda: replace(
            request,
            operations=(ImageOperation("page", (1,)), ImageOperation("page", (1,))),
        ),
        lambda: replace(
            request,
            operations=(ImageOperation("angle", (0,)), ImageOperation("trim", (0, 0, 0, 0))),
        ),
        lambda: replace(request, operations=(ImageOperation("page", (2,)),)),
        lambda: replace(request, operations=(ImageOperation("angle", (90_000,)),)),
    ]
    for operation in bad_requests:
        _assert_error(ErrorCode.SCHEMA_INVALID, operation)

    renderer = RendererIdentity(
        name="pypdfium2-pillow",
        pypdfium2_version="5.12.0",
        pdfium_version="152.0.7947.0",
        pdfium_binary_sha256="sha256:" + "b" * 64,
        pillow_version="12.3.0",
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: replace(renderer, pypdfium2_version=""),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: replace(renderer, pdfium_binary_sha256="bad"),
    )
    key = image_cache_key(request, renderer)
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: replace(key, policy_version="v2"))


def test_parser_failure_paths_and_balanced_helpers(tmp_path: Path) -> None:
    assert materializer_module._read_balanced("x", 0, "{", "}") is None
    assert materializer_module._read_balanced("[{x,y}]", 0, "[", "]") == ("{x,y}", 7)
    assert materializer_module._split_top_level("{x,y},z", ",") == ["{x,y}", "z"]
    assert materializer_module._split_top_level("}", ",") is None
    assert materializer_module._split_top_level("[", ",") is None
    assert materializer_module._split_option("a=b=c") is None
    assert materializer_module._split_option("bad key=x") is None
    assert materializer_module._parse_options("a,,b") is None
    assert materializer_module._parse_graphicspath("") is None
    assert materializer_module._parse_graphicspath("{}") is None
    assert materializer_module._parse_extensions("pdf") is None

    malformed = [
        "\\includegraphics[page=2{plot.pdf}",
        "\\includegraphics",
        "\\graphicspath{figures/}",
        "\\DeclareGraphicsExtensions{pdf}",
        "\\includegraphics[a=b=c]{plot.pdf}",
        "\\includegraphics{}",
    ]
    for text in malformed:
        scan = scan_graphics_text(text, source_path="main.tex")
        assert "IMAGE_PARSE_MALFORMED" in {item.code for item in scan.diagnostics}
    assert scan_graphics_text("\\% escaped", source_path="main.tex").includes == ()
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: scan_graphics_text("\x00", source_path="main.tex"),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: scan_graphics_text("\ud800", source_path="main.tex"),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: scan_graphics_text(
            "汉" * 4,
            source_path="main.tex",
            quality=replace(REVIEW_QUALITY, max_tex_bytes=5),
        ),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: scan_graphics_text(
            "\\includegraphics{x.pdf}\\includegraphics{y.pdf}",
            source_path="main.tex",
            quality=replace(REVIEW_QUALITY, max_graphics_commands=1),
        ),
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_bytes(b"\xff")
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: scan_graphics_source(source, "main.tex"))


def test_option_policy_covers_safe_passthrough_and_blocking_variants() -> None:
    safe = scan_graphics_text(
        "\\includegraphics[width=3cm,height=\\textheight,scale=.5,"
        "keepaspectratio=false,width=.72\\linewidth]{figure.pdf}",
        source_path="main.tex",
    ).includes[0]
    *_, safe_diagnostics, passthrough = materializer_module._supported_option_plan(
        safe, REVIEW_QUALITY
    )
    assert safe_diagnostics == ()
    assert [option.key for option in passthrough] == [
        "width",
        "height",
        "scale",
        "keepaspectratio",
        "width",
    ]

    cases = [
        "*{figure.pdf}",
        "[page=1,page=2]{figure.pdf}",
        "[width]{figure.pdf}",
        "[scale=0]{figure.pdf}",
        "[keepaspectratio=maybe]{figure.pdf}",
        "[draft]{figure.pdf}",
        "[page=\\foo]{figure.pdf}",
        "[page=0]{figure.pdf}",
        "[angle=90.1234]{figure.pdf}",
        "[trim=1 2 3]{figure.pdf}",
        "[trim=-1 2 3 4,clip]{figure.pdf}",
        "[clip=maybe]{figure.pdf}",
        "[clip]{figure.pdf}",
        "[trim=1 2 3 4,clip=false]{figure.pdf}",
    ]
    for suffix in cases:
        occurrence = scan_graphics_text(
            f"\\includegraphics{suffix}", source_path="main.tex"
        ).includes[0]
        *_, diagnostics, _ = materializer_module._supported_option_plan(occurrence, REVIEW_QUALITY)
        assert diagnostics


def test_resolver_rejects_dynamic_missing_unknown_and_directory_candidates(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _write_source(root, "placeholder")
    base = scan_graphics_text("\\includegraphics{figure.pdf}", source_path="main.tex").includes[0]
    _assert_error(
        ErrorCode.PATH_TRAVERSAL,
        lambda: resolve_graphic(root, replace(base, graphic_paths_dynamic=True)),
    )
    extensionless = replace(base, target="figure", extensions_dynamic=True)
    _assert_error(ErrorCode.PATH_TRAVERSAL, lambda: resolve_graphic(root, extensionless))
    _assert_error(
        ErrorCode.BACKEND_FAILED,
        lambda: resolve_graphic(root, replace(base, target="figure.gif")),
    )
    (root / "figure.gif").write_bytes(b"unsupported exact match")
    _assert_error(
        ErrorCode.BACKEND_CAPABILITY_MISSING,
        lambda: resolve_graphic(root, replace(base, target="figure.gif")),
    )
    (root / "figure.gif").unlink()
    _assert_error(
        ErrorCode.BACKEND_CAPABILITY_MISSING,
        lambda: resolve_graphic(root, replace(base, target="figure", extensions=(".gif",))),
    )
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: resolve_graphic(root, replace(base, target="figure", extensions=())),
    )
    _assert_error(ErrorCode.BACKEND_FAILED, lambda: resolve_graphic(root, base))
    (root / "figure.pdf").mkdir()
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: resolve_graphic(root, base))
    _assert_error(
        ErrorCode.SCHEMA_INVALID,
        lambda: resolve_graphic(tmp_path / "missing", base),
    )
    root_file = tmp_path / "root-file"
    root_file.write_bytes(b"x")
    _assert_error(ErrorCode.SCHEMA_INVALID, lambda: resolve_graphic(root_file, base))
    _assert_error(
        ErrorCode.PATH_TRAVERSAL,
        lambda: materializer_module._normalize_graphics_directory("\\macro/"),
    )
    assert materializer_module._normalize_graphics_directory("./") == ""
    _assert_error(
        ErrorCode.PATH_ABSOLUTE,
        lambda: materializer_module._normalize_graphics_directory("/"),
    )


def test_pdf_runtime_loader_fail_closed_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_pdf_runtime()
    original_import = importlib.import_module
    original_version = importlib.metadata.version
    original_digest = cast("Any", vars(materializer_module)["digest_file"])

    monkeypatch.setattr(os, "name", "posix")
    _assert_error(ErrorCode.TOOL_VERSION_UNSUPPORTED, materializer_module._load_pdf_runtime)
    monkeypatch.undo()

    def missing(name: str) -> Any:
        if name == "pypdfium2":
            raise ImportError("missing")
        return original_import(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    _assert_error(ErrorCode.TOOL_MISSING, materializer_module._load_pdf_runtime)
    monkeypatch.undo()

    def old_version(name: str) -> str:
        return "4.0.0" if name == "pypdfium2" else original_version(name)

    monkeypatch.setattr(importlib.metadata, "version", old_version)
    _assert_error(ErrorCode.TOOL_VERSION_UNSUPPORTED, materializer_module._load_pdf_runtime)
    monkeypatch.undo()

    def incomplete(name: str) -> Any:
        if name == "pypdfium2_raw":
            return SimpleNamespace(__file__=None)
        return original_import(name)

    monkeypatch.setattr(importlib, "import_module", incomplete)
    _assert_error(ErrorCode.TOOL_VERSION_UNSUPPORTED, materializer_module._load_pdf_runtime)
    monkeypatch.undo()

    def failed_digest(path: Path, *, max_bytes: int) -> Any:
        if path.name == "pdfium.dll":
            raise ContractError(ErrorCode.SCHEMA_INVALID, "unavailable")
        return original_digest(path, max_bytes=max_bytes)

    monkeypatch.setattr(materializer_module, "digest_file", failed_digest)
    _assert_error(ErrorCode.TOOL_VERSION_UNSUPPORTED, materializer_module._load_pdf_runtime)


def test_pixel_rotation_png_and_decode_limits() -> None:
    _require_pdf_runtime()
    image_api = importlib.import_module("PIL.Image")
    image = image_api.new("RGB", (10, 5), (10, 20, 30))
    try:
        assert materializer_module._rotated_bounds(10, 5, 180_000) == (10, 5)
        assert materializer_module._rotated_bounds(10, 5, 270_000) == (5, 10)
        assert materializer_module._rotated_bounds(10, 5, 37_000)[0] > 10
        for angle in (180_000, 270_000, 37_000):
            rotated = materializer_module._rotate_image(image, angle, image_api)
            try:
                assert rotated.size == materializer_module._rotated_bounds(10, 5, angle)
            finally:
                rotated.close()
        png = materializer_module._encode_canonical_png(
            image,
            REVIEW_QUALITY,
            REVIEW_QUALITY.dpi,
        )
    finally:
        image.close()
    _assert_error(
        ErrorCode.BACKEND_FAILED,
        lambda: materializer_module._check_pixel_bounds(0, 1, REVIEW_QUALITY),
    )
    _assert_error(
        ErrorCode.BACKEND_FAILED,
        lambda: materializer_module._check_pixel_bounds(
            11, 1, replace(REVIEW_QUALITY, max_dimension_px=10)
        ),
    )
    _assert_error(
        ErrorCode.BACKEND_FAILED,
        lambda: materializer_module._check_pixel_bounds(
            10, 10, replace(REVIEW_QUALITY, max_render_pixels=99)
        ),
    )
    small_image = image_api.new("RGB", (2, 2), (1, 2, 3))
    try:
        _assert_error(
            ErrorCode.BACKEND_FAILED,
            lambda: materializer_module._encode_canonical_png(
                small_image,
                replace(REVIEW_QUALITY, max_png_bytes=1),
                REVIEW_QUALITY.dpi,
            ),
        )
    finally:
        small_image.close()
    _assert_error(
        ErrorCode.HASH_INTEGRITY_MISMATCH,
        lambda: materializer_module._decode_png(
            b"",
            REVIEW_QUALITY,
            image_api,
            REVIEW_QUALITY.dpi,
        ),
    )
    _assert_error(
        ErrorCode.HASH_INTEGRITY_MISMATCH,
        lambda: materializer_module._decode_png(
            b"not-png",
            REVIEW_QUALITY,
            image_api,
            REVIEW_QUALITY.dpi,
        ),
    )
    jpeg_buffer = io.BytesIO()
    jpeg = image_api.new("RGB", (2, 2), (1, 2, 3))
    try:
        jpeg.save(jpeg_buffer, format="JPEG")
    finally:
        jpeg.close()
    _assert_error(
        ErrorCode.HASH_INTEGRITY_MISMATCH,
        lambda: materializer_module._decode_png(
            jpeg_buffer.getvalue(),
            REVIEW_QUALITY,
            image_api,
            REVIEW_QUALITY.dpi,
        ),
    )
    decoded = materializer_module._decode_png(
        png,
        REVIEW_QUALITY,
        image_api,
        REVIEW_QUALITY.dpi,
    )
    decoded.close()


def test_pdf_zero_pages_page_limit_trim_limit_and_arbitrary_rotation(tmp_path: Path) -> None:
    _require_pdf_runtime()
    for name, data in (
        ("empty", _minimal_pdf(())),
        ("two", _minimal_pdf(((1, 0, 0), (0, 1, 0)))),
        ("one", _minimal_pdf(((1, 0, 0),))),
    ):
        root = tmp_path / name
        (root / "figures").mkdir(parents=True)
        (root / "figures/multi.pdf").write_bytes(data)

    empty_request = _plan_pdf(tmp_path / "empty")
    _assert_error(
        ErrorCode.BACKEND_FAILED,
        lambda: materialize_image(
            empty_request, source_root=tmp_path / "empty", cache_root=tmp_path / "empty-cache"
        ),
    )
    page_limited = _plan_pdf(
        tmp_path / "two",
        quality=replace(REVIEW_QUALITY, max_document_pages=1),
    )
    _assert_error(
        ErrorCode.BACKEND_FAILED,
        lambda: materialize_image(
            page_limited, source_root=tmp_path / "two", cache_root=tmp_path / "pages-cache"
        ),
    )
    trim_request = _plan_pdf(
        tmp_path / "one",
        options="trim=36bp 0 36bp 0,clip",
    )
    _assert_error(
        ErrorCode.BACKEND_FAILED,
        lambda: materialize_image(
            trim_request, source_root=tmp_path / "one", cache_root=tmp_path / "trim-cache"
        ),
    )
    rotated = _plan_pdf(tmp_path / "one", options="angle=37")
    result = materialize_image(
        rotated,
        source_root=tmp_path / "one",
        cache_root=tmp_path / "rotate-cache",
    )
    assert result.width_px > 200 and result.height_px > 200
    assert result.as_dict()["reused"] is False


def test_cache_manifest_shape_and_atomic_publication_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_pdf_runtime()
    root = tmp_path / "source"
    (root / "figures").mkdir(parents=True)
    (root / "figures/multi.pdf").write_bytes(_minimal_pdf(((1, 0, 0),)))
    request = _plan_pdf(root)
    reference_cache = tmp_path / "reference"
    reference = materialize_image(request, source_root=root, cache_root=reference_cache)
    reference_entry = (reference_cache / reference.png_path).parent

    race_cache = tmp_path / "race"
    concrete_path = type(race_cache)
    original_rename = concrete_path.rename

    def racing_rename(self: Path, target: Path) -> Path:
        if self.name.startswith(".") and ".stage-" in self.name:
            shutil.copytree(reference_entry, target)
            raise FileExistsError("competitor won")
        return original_rename(self, target)

    monkeypatch.setattr(concrete_path, "rename", racing_rename)
    raced = materialize_image(request, source_root=root, cache_root=race_cache)
    assert raced.reused is True
    assert not list(race_cache.glob(".stage-*"))
    monkeypatch.undo()

    broken_cache = tmp_path / "broken"

    def failed_rename(self: Path, target: Path) -> Path:
        if self.name.startswith(".") and ".stage-" in self.name:
            raise PermissionError("denied")
        return original_rename(self, target)

    monkeypatch.setattr(concrete_path, "rename", failed_rename)
    _assert_error(
        ErrorCode.INTERNAL_INVARIANT,
        lambda: materialize_image(request, source_root=root, cache_root=broken_cache),
    )
    assert not list(broken_cache.glob(".stage-*"))


def test_one_redundant_graphic_group_resolves_and_preserves_source_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    (root / "figures").mkdir(parents=True)
    figure_name = "Review \N{EN DASH} plot.pdf"
    (root / "figures" / figure_name).write_bytes(_minimal_pdf(((0.1, 0.2, 0.3),)))
    command = r"\includegraphics[width=.5\textwidth]{{" + figure_name + "}}"
    latex = "\\graphicspath{{figures/}}\n" + command + "\n"

    occurrence = _scan_one(root, latex)
    resolved = resolve_graphic(root, occurrence)
    plan = plan_image_request(root, occurrence)

    assert occurrence.target == f"{{{figure_name}}}"
    assert occurrence.source_text == command
    assert latex[occurrence.start_char : occurrence.end_char] == command
    assert resolved.source_path == f"figures/{figure_name}"
    assert plan.diagnostics == ()
    assert plan.request is not None
    assert plan.request.source_path == f"figures/{figure_name}"
    assert plan.passthrough_option_text == r"width=.5\textwidth"


@pytest.mark.parametrize(
    "command",
    [
        r"\includegraphics{{{plot.pdf}}}",
        r"\includegraphics{{plot.pdf}suffix}",
        r"\includegraphics{prefix{plot.pdf}}",
        r"\includegraphics{\jobname.pdf}",
    ],
)
def test_nested_partial_and_macro_graphic_targets_remain_manual(
    tmp_path: Path,
    command: str,
) -> None:
    root = tmp_path / "source"
    occurrence = _scan_one(root, command)

    plan = plan_image_request(root, occurrence)

    assert plan.request is None
    assert {item.code for item in plan.diagnostics} == {"IMAGE_DYNAMIC_INPUT"}
    with pytest.raises(ContractError) as raised:
        resolve_graphic(root, occurrence)
    assert raised.value.code is ErrorCode.PATH_TRAVERSAL


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("/absolute/plot.pdf", ErrorCode.PATH_ABSOLUTE),
        (r"\\server\share\plot.pdf", ErrorCode.PATH_ABSOLUTE),
        ("C:/private/plot.pdf", ErrorCode.PATH_ABSOLUTE),
        ("../plot.pdf", ErrorCode.PATH_TRAVERSAL),
    ],
)
def test_redundant_group_does_not_relax_unsafe_graphic_paths(
    tmp_path: Path,
    target: str,
    code: ErrorCode,
) -> None:
    root = tmp_path / "source"
    occurrence = _scan_one(root, "\\includegraphics{{" + target + "}}")

    with pytest.raises(ContractError) as raised:
        plan_image_request(root, occurrence)

    assert raised.value.code is code
