"""Deterministic, fail-closed materialization of static LaTeX graphics.

The module is intentionally independent from the export adapters.  It scans a
small, static subset of ``graphicx``, resolves files below an immutable source
root, and materializes PDF pages into content-addressed canonical PNG cache
entries.  It never expands TeX macros and never invokes an external program.

Only the Windows ``pdf-figures`` extra is used for rendering.  SVG and EPS are
recognized during resolution but are deliberately routed to a manual
diagnostic until separately sandboxed renderers are integrated.
"""

from __future__ import annotations

import bisect
import importlib
import importlib.metadata
import io
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

from latex_word_review.atomic_publish import publish_new_directory
from latex_word_review.canonical import canonical_json, sha256_bytes, sha256_canonical
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.frozen_runtime import internal_worker_command
from latex_word_review.graphic_targets import (
    is_dynamic_graphic_target_error,
    normalize_static_graphic_target,
)
from latex_word_review.hashing import digest_bytes, digest_file, read_stable_bytes
from latex_word_review.paths import ensure_disjoint_roots, resolve_within, validate_relative_path
from latex_word_review.runtime import minimal_environment, run_command

ImageFormat = Literal["pdf", "png", "jpg", "jpeg", "svg", "eps", "ps"]
DirectiveKind = Literal["graphicspath", "DeclareGraphicsExtensions"]
OperationKind = Literal["page", "trim", "clip", "angle"]
DiagnosticCode = Literal[
    "IMAGE_PARSE_MALFORMED",
    "IMAGE_DYNAMIC_INPUT",
    "IMAGE_TRIM_REQUIRES_CLIP",
    "IMAGE_UNSUPPORTED_OPTION",
    "IMAGE_UNSUPPORTED_FORMAT",
]

DEFAULT_GRAPHICS_EXTENSIONS: Final[tuple[str, ...]] = (
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".eps",
    ".ps",
)
_KNOWN_FORMATS: Final[dict[str, ImageFormat]] = {
    ".pdf": "pdf",
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpeg",
    ".svg": "svg",
    ".eps": "eps",
    ".ps": "ps",
}
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_OPTION_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9@_-]{0,63}$")
_SIMPLE_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d{0,3})?|\.\d{1,3})$")
_SIMPLE_LENGTH_RE = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d{0,3})?|\.\d{1,3}))"
    r"(?P<unit>bp|pt|in|cm|mm)?$",
    re.IGNORECASE,
)
_SAFE_RELATIVE_LENGTH_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d{0,3})?|\.\d{1,3}))?"
    r"\\(?:linewidth|textwidth|columnwidth|paperwidth|textheight|paperheight)$",
    re.IGNORECASE,
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MATERIALIZER_POLICY_VERSION: Final[str] = "pdf-png-v3"
_CACHE_FORMAT: Final[str] = "latex-word-review-image-cache-v2"
_WORKER_REQUEST_FORMAT: Final[str] = "latex-word-review-image-worker-request-v2"
_WORKER_REPORT_FORMAT: Final[str] = "latex-word-review-image-worker-report-v2"
_WORKER_SOURCE_NAME: Final[str] = "source.pdf"
_WORKER_REQUEST_NAME: Final[str] = "request.json"
_WORKER_PNG_NAME: Final[str] = "rendered.png"
_WORKER_REPORT_NAME: Final[str] = "report.json"
_CACHE_PNG_NAME: Final[str] = "preview.png"
_CACHE_MANIFEST_NAME: Final[str] = "manifest.json"
_MICRO_BP: Final[int] = 1_000_000


def _contract(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(ErrorCode.SCHEMA_INVALID, message)


@dataclass(frozen=True, slots=True)
class QualityProfile:
    """Bounded render and scan policy included in every cache key.

    ``dpi`` is the requested upper bound.  Rendering may select a lower integer
    DPI to stay within the pixel limits, but never below ``min_render_dpi``
    unless the requested upper bound itself is lower.
    """

    name: str = "review"
    dpi: int = 200
    min_render_dpi: int = 96
    max_tex_bytes: int = 8 * 1024 * 1024
    max_graphics_commands: int = 4096
    max_source_bytes: int = 128 * 1024 * 1024
    max_document_pages: int = 2048
    max_page_points: int = 14_400
    max_dimension_px: int = 6000
    max_render_pixels: int = 12_000_000
    max_png_bytes: int = 32 * 1024 * 1024
    render_timeout_s: int = 30

    def __post_init__(self) -> None:
        _contract(bool(_PROFILE_NAME_RE.fullmatch(self.name)), "quality profile name is invalid")
        _contract(
            type(self.dpi) is int and 72 <= self.dpi <= 600,
            "quality profile DPI must be an integer in [72, 600]",
        )
        _contract(
            type(self.min_render_dpi) is int and 72 <= self.min_render_dpi <= 600,
            "minimum render DPI must be an integer in [72, 600]",
        )
        _contract(0 < self.max_tex_bytes <= 16 * 1024 * 1024, "TeX scan limit is invalid")
        _contract(
            0 < self.max_graphics_commands <= 10_000,
            "graphics command limit is invalid",
        )
        _contract(
            0 < self.max_source_bytes <= 512 * 1024 * 1024,
            "image source limit is invalid",
        )
        _contract(0 < self.max_document_pages <= 10_000, "PDF page limit is invalid")
        _contract(0 < self.max_page_points <= 72_000, "PDF page-size limit is invalid")
        _contract(0 < self.max_dimension_px <= 20_000, "image dimension limit is invalid")
        _contract(0 < self.max_render_pixels <= 100_000_000, "pixel limit is invalid")
        _contract(0 < self.max_png_bytes <= 128 * 1024 * 1024, "PNG limit is invalid")
        _contract(0 < self.render_timeout_s <= 60, "render timeout must be in (0, 60]")

    def as_dict(self) -> dict[str, str | int]:
        return {
            "name": self.name,
            "dpi": self.dpi,
            "min_render_dpi": self.min_render_dpi,
            "max_tex_bytes": self.max_tex_bytes,
            "max_graphics_commands": self.max_graphics_commands,
            "max_source_bytes": self.max_source_bytes,
            "max_document_pages": self.max_document_pages,
            "max_page_points": self.max_page_points,
            "max_dimension_px": self.max_dimension_px,
            "max_render_pixels": self.max_render_pixels,
            "max_png_bytes": self.max_png_bytes,
            "render_timeout_s": self.render_timeout_s,
        }


REVIEW_QUALITY: Final[QualityProfile] = QualityProfile()


@dataclass(frozen=True, slots=True)
class GraphicOption:
    """One ``graphicx`` option in exact source order."""

    key: str
    value: str | None
    ordinal: int
    raw: str | None = None

    def __post_init__(self) -> None:
        _contract(bool(_OPTION_KEY_RE.fullmatch(self.key)), "graphicx option key is invalid")
        _contract(0 <= self.ordinal < 128, "graphicx option ordinal is invalid")
        if self.value is not None:
            _contract(len(self.value) <= 2048, "graphicx option value is too long")
        if self.raw is not None:
            _contract(0 < len(self.raw) <= 4096, "raw graphicx option is invalid")

    def as_dict(self) -> dict[str, str | int | None]:
        return {"key": self.key, "value": self.value, "ordinal": self.ordinal}

    @property
    def source_text(self) -> str:
        if self.raw is not None:
            return self.raw
        return self.key if self.value is None else f"{self.key}={self.value}"


@dataclass(frozen=True, slots=True)
class GraphicsDirective:
    kind: DirectiveKind
    values: tuple[str, ...]
    dynamic: bool
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class GraphicsDiagnostic:
    """A path-safe diagnostic that requires manual handling."""

    code: DiagnosticCode
    message: str
    source_path: str
    line: int
    column: int
    disposition: Literal["manual"] = "manual"

    def __post_init__(self) -> None:
        validate_relative_path(self.source_path)
        _contract(self.line > 0 and self.column > 0, "diagnostic location is invalid")

    def as_dict(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "message": self.message,
            "source_path": self.source_path,
            "line": self.line,
            "column": self.column,
            "disposition": self.disposition,
        }


@dataclass(frozen=True, slots=True)
class IncludeGraphics:
    """A static lexical occurrence plus directive state at that location."""

    source_path: str
    target: str
    options: tuple[GraphicOption, ...]
    graphic_paths: tuple[str, ...]
    extensions: tuple[str, ...]
    graphic_paths_dynamic: bool
    extensions_dynamic: bool
    starred: bool
    line: int
    column: int
    start_char: int
    end_char: int
    source_text: str

    def __post_init__(self) -> None:
        validate_relative_path(self.source_path)
        _contract(0 < len(self.target) <= 2048, "includegraphics target is invalid")
        _contract(len(self.options) <= 64, "includegraphics has too many options")
        _contract(len(self.graphic_paths) <= 64, "graphicspath has too many entries")
        _contract(len(self.extensions) <= 64, "graphics extension list is too large")
        _contract(self.line > 0 and self.column > 0, "includegraphics location is invalid")
        _contract(
            0 <= self.start_char < self.end_char,
            "includegraphics character span is invalid",
        )
        _contract(
            0 < len(self.source_text) <= 16 * 1024,
            "includegraphics source command is outside the rewrite bound",
        )
        _contract(
            self.source_text.startswith("\\includegraphics"),
            "includegraphics source command is invalid",
        )


@dataclass(frozen=True, slots=True)
class GraphicsScan:
    source_path: str
    directives: tuple[GraphicsDirective, ...]
    includes: tuple[IncludeGraphics, ...]
    diagnostics: tuple[GraphicsDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class ResolvedGraphic:
    source_path: str
    source_format: ImageFormat
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        validate_relative_path(self.source_path)
        _contract(self.size_bytes >= 0, "resolved image size is invalid")
        _contract(bool(_HASH_RE.fullmatch(self.sha256)), "resolved image hash is invalid")


@dataclass(frozen=True, slots=True)
class ImageOperation:
    """A normalized operation that retains the original graphicx order."""

    kind: OperationKind
    values: tuple[int, ...]

    def __post_init__(self) -> None:
        expected = {"page": 1, "trim": 4, "clip": 1, "angle": 1}[self.kind]
        _contract(len(self.values) == expected, "image operation arity is invalid")

    def as_dict(self) -> dict[str, str | list[int]]:
        return {"kind": self.kind, "values": list(self.values)}


@dataclass(frozen=True, slots=True)
class ImageRequest:
    """A fully resolved and bounded request suitable for cache hashing."""

    source_path: str
    source_format: ImageFormat
    source_sha256: str
    source_size_bytes: int
    page: int
    angle_millidegrees: int
    trim_micro_bp: tuple[int, int, int, int]
    clip: bool
    operations: tuple[ImageOperation, ...]
    quality: QualityProfile = REVIEW_QUALITY

    def __post_init__(self) -> None:
        validate_relative_path(self.source_path)
        expected_format = _KNOWN_FORMATS.get(PurePosixPath(self.source_path).suffix.casefold())
        _contract(expected_format == self.source_format, "image request format does not match path")
        _contract(bool(_HASH_RE.fullmatch(self.source_sha256)), "image request hash is invalid")
        _contract(
            0 <= self.source_size_bytes <= self.quality.max_source_bytes, "image is too large"
        )
        _contract(1 <= self.page <= 10_000, "image page must be in [1, 10000]")
        _contract(
            0 <= self.angle_millidegrees < 360_000,
            "image angle must be normalized to [0, 360)",
        )
        _contract(len(self.trim_micro_bp) == 4, "image trim must contain four values")
        _contract(
            all(
                0 <= value <= self.quality.max_page_points * _MICRO_BP
                for value in self.trim_micro_bp
            ),
            "image trim is outside the bounded page range",
        )
        _contract(len(self.operations) <= 8, "image request has too many operations")
        kinds = [operation.kind for operation in self.operations]
        _contract(len(kinds) == len(set(kinds)), "image request repeats an operation")
        non_page = tuple(kind for kind in kinds if kind != "page")
        _contract(
            non_page in {(), ("angle",), ("trim", "clip"), ("trim", "clip", "angle")},
            "image request operation order is unsupported",
        )
        values_by_kind = {operation.kind: operation.values for operation in self.operations}
        _contract(values_by_kind.get("page", (1,))[0] == self.page, "page operation mismatch")
        _contract(
            values_by_kind.get("angle", (0,))[0] == self.angle_millidegrees,
            "angle operation mismatch",
        )
        _contract(
            values_by_kind.get("trim", (0, 0, 0, 0)) == self.trim_micro_bp,
            "trim operation mismatch",
        )
        _contract(
            bool(values_by_kind.get("clip", (0,))[0]) is self.clip,
            "clip operation mismatch",
        )
        _contract(
            self.clip is ("trim" in values_by_kind),
            "static trim and clip must be present together",
        )

    def as_cache_input(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "source_format": self.source_format,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "page": self.page,
            "angle_millidegrees": self.angle_millidegrees,
            "trim_micro_bp": list(self.trim_micro_bp),
            "clip": self.clip,
            "operations": [operation.as_dict() for operation in self.operations],
            "quality": self.quality.as_dict(),
        }

    @property
    def request_sha256(self) -> str:
        return sha256_canonical(self.as_cache_input())


@dataclass(frozen=True, slots=True)
class ImagePlan:
    request: ImageRequest | None
    diagnostics: tuple[GraphicsDiagnostic, ...]
    passthrough_options: tuple[GraphicOption, ...] = ()

    @property
    def blocking_diagnostics(self) -> tuple[GraphicsDiagnostic, ...]:
        return self.diagnostics

    @property
    def passthrough_option_text(self) -> str:
        return ",".join(option.source_text for option in self.passthrough_options)


@dataclass(frozen=True, slots=True)
class RendererIdentity:
    name: Literal["pypdfium2-pillow"]
    pypdfium2_version: str
    pdfium_version: str
    pdfium_binary_sha256: str
    pillow_version: str

    def __post_init__(self) -> None:
        for version in (self.pypdfium2_version, self.pdfium_version, self.pillow_version):
            _contract(0 < len(version) <= 64, "renderer version is invalid")
        _contract(
            bool(_HASH_RE.fullmatch(self.pdfium_binary_sha256)),
            "PDFium binary hash is invalid",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "pypdfium2_version": self.pypdfium2_version,
            "pdfium_version": self.pdfium_version,
            "pdfium_binary_sha256": self.pdfium_binary_sha256,
            "pillow_version": self.pillow_version,
        }


@dataclass(frozen=True, slots=True)
class ImageCacheKey:
    request_sha256: str
    renderer: RendererIdentity
    policy_version: str = _MATERIALIZER_POLICY_VERSION

    def __post_init__(self) -> None:
        _contract(bool(_HASH_RE.fullmatch(self.request_sha256)), "request hash is invalid")
        _contract(
            self.policy_version == _MATERIALIZER_POLICY_VERSION, "cache policy is unsupported"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "request_sha256": self.request_sha256,
            "renderer": self.renderer.as_dict(),
            "policy_version": self.policy_version,
        }

    @property
    def sha256(self) -> str:
        return sha256_canonical(self.as_dict())


@dataclass(frozen=True, slots=True)
class MaterializedImage:
    """Verified cache result; ``dpi`` is the effective rendered DPI."""

    cache_key_sha256: str
    request_sha256: str
    png_path: str
    manifest_path: str
    png_sha256: str
    pixel_sha256: str
    width_px: int
    height_px: int
    dpi: int
    renderer: RendererIdentity
    reused: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "cache_key_sha256": self.cache_key_sha256,
            "request_sha256": self.request_sha256,
            "png_path": self.png_path,
            "manifest_path": self.manifest_path,
            "png_sha256": self.png_sha256,
            "pixel_sha256": self.pixel_sha256,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "dpi": self.dpi,
            "renderer": self.renderer.as_dict(),
            "reused": self.reused,
        }


@dataclass(frozen=True, slots=True)
class _PdfRuntime:
    pdfium: Any
    pdfium_raw: Any
    image_module: Any
    identity: RendererIdentity


def _mask_comments(text: str) -> str:
    characters = list(text)
    in_comment = False
    for index, character in enumerate(characters):
        if in_comment:
            if character in "\r\n":
                in_comment = False
            else:
                characters[index] = " "
            continue
        if character != "%":
            continue
        slash_count = 0
        probe = index - 1
        while probe >= 0 and characters[probe] == "\\":
            slash_count += 1
            probe -= 1
        if slash_count % 2 == 0:
            characters[index] = " "
            in_comment = True
    return "".join(characters)


def _skip_space(text: str, offset: int) -> int:
    while offset < len(text) and text[offset].isspace():
        offset += 1
    return offset


def _read_balanced(
    text: str,
    offset: int,
    opening: str,
    closing: str,
) -> tuple[str, int] | None:
    if offset >= len(text) or text[offset] != opening:
        return None
    depth = 1
    brace_depth = 0
    index = offset + 1
    while index < len(text):
        character = text[index]
        escaped = index > 0 and text[index - 1] == "\\"
        if opening == "[":
            if character == "{" and not escaped:
                brace_depth += 1
            elif character == "}" and not escaped and brace_depth:
                brace_depth -= 1
            elif not brace_depth and character == opening and not escaped:
                depth += 1
            elif not brace_depth and character == closing and not escaped:
                depth -= 1
        else:
            if character == opening and not escaped:
                depth += 1
            elif character == closing and not escaped:
                depth -= 1
        if depth == 0:
            return text[offset + 1 : index], index + 1
        index += 1
    return None


def _split_top_level(value: str, delimiter: str) -> list[str] | None:
    pieces: list[str] = []
    start = 0
    brace_depth = 0
    bracket_depth = 0
    for index, character in enumerate(value):
        escaped = index > 0 and value[index - 1] == "\\"
        if escaped:
            continue
        if character == "{":
            brace_depth += 1
        elif character == "}":
            brace_depth -= 1
            if brace_depth < 0:
                return None
        elif character == "[":
            bracket_depth += 1
        elif character == "]":
            bracket_depth -= 1
            if bracket_depth < 0:
                return None
        elif character == delimiter and not brace_depth and not bracket_depth:
            pieces.append(value[start:index].strip())
            start = index + 1
    if brace_depth or bracket_depth:
        return None
    pieces.append(value[start:].strip())
    return pieces


def _split_option(raw: str) -> tuple[str, str | None] | None:
    pieces = _split_top_level(raw, "=")
    if pieces is None or not pieces or len(pieces) > 2:
        return None
    key = pieces[0].strip()
    value = None if len(pieces) == 1 else pieces[1].strip()
    if not key or not _OPTION_KEY_RE.fullmatch(key):
        return None
    return key, value


def _parse_options(raw: str) -> tuple[GraphicOption, ...] | None:
    if not raw.strip():
        return ()
    pieces = _split_top_level(raw, ",")
    if pieces is None or any(not piece for piece in pieces) or len(pieces) > 64:
        return None
    result: list[GraphicOption] = []
    for ordinal, piece in enumerate(pieces):
        parsed = _split_option(piece)
        if parsed is None:
            return None
        result.append(GraphicOption(parsed[0], parsed[1], ordinal, piece))
    return tuple(result)


def _parse_graphicspath(raw: str) -> tuple[str, ...] | None:
    values: list[str] = []
    offset = 0
    while True:
        offset = _skip_space(raw, offset)
        if offset >= len(raw):
            break
        parsed = _read_balanced(raw, offset, "{", "}")
        if parsed is None:
            return None
        value, offset = parsed
        value = value.strip()
        if not value or len(value) > 1024 or len(values) >= 64:
            return None
        values.append(value)
    return tuple(values) if values else None


def _parse_extensions(raw: str) -> tuple[str, ...] | None:
    pieces = _split_top_level(raw, ",")
    if pieces is None or not pieces or len(pieces) > 64:
        return None
    values: list[str] = []
    for piece in pieces:
        value = piece.strip()
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", value):
            return None
        values.append(value.casefold())
    return tuple(values)


def _contains_dynamic(value: str) -> bool:
    return any(character in value for character in "\\#${}~^")


def _location(newlines: list[int], offset: int) -> tuple[int, int]:
    line_index = bisect.bisect_right(newlines, offset)
    previous_newline = -1 if line_index == 0 else newlines[line_index - 1]
    return line_index + 1, offset - previous_newline


def _manual_diagnostic(
    code: DiagnosticCode,
    message: str,
    occurrence: IncludeGraphics,
) -> GraphicsDiagnostic:
    return GraphicsDiagnostic(
        code=code,
        message=message,
        source_path=occurrence.source_path,
        line=occurrence.line,
        column=occurrence.column,
    )


def scan_graphics_text(
    text: str,
    *,
    source_path: str,
    quality: QualityProfile = REVIEW_QUALITY,
) -> GraphicsScan:
    """Lexically scan a bounded UTF-8 LaTeX source without macro expansion."""

    normalized_source = validate_relative_path(source_path)
    _contract("\x00" not in text, "LaTeX source contains NUL")
    if len(text) > quality.max_tex_bytes:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "LaTeX source exceeds the scan limit")
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "LaTeX source is not valid Unicode") from exc
    if encoded_size > quality.max_tex_bytes:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "LaTeX source exceeds the scan limit")

    masked = _mask_comments(text)
    newlines = [index for index, character in enumerate(masked) if character == "\n"]
    directives: list[GraphicsDirective] = []
    includes: list[IncludeGraphics] = []
    diagnostics: list[GraphicsDiagnostic] = []
    graphic_paths: tuple[str, ...] = ()
    extensions = DEFAULT_GRAPHICS_EXTENSIONS
    graphic_paths_dynamic = False
    extensions_dynamic = False
    command_count = 0
    index = 0

    while index < len(masked):
        if masked[index] != "\\":
            index += 1
            continue
        command_start = index
        index += 1
        name_start = index
        while index < len(masked) and (masked[index].isalpha() or masked[index] == "@"):
            index += 1
        command = masked[name_start:index]
        if command not in {"graphicspath", "DeclareGraphicsExtensions", "includegraphics"}:
            if not command and index < len(masked):
                index += 1
            continue
        command_count += 1
        if command_count > quality.max_graphics_commands:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "too many graphicx commands")
        starred = False
        if command == "includegraphics" and index < len(masked) and masked[index] == "*":
            starred = True
            index += 1
        index = _skip_space(masked, index)
        line, column = _location(newlines, command_start)

        option_text: str | None = None
        if command == "includegraphics" and index < len(masked) and masked[index] == "[":
            parsed_options = _read_balanced(masked, index, "[", "]")
            if parsed_options is None:
                diagnostics.append(
                    GraphicsDiagnostic(
                        "IMAGE_PARSE_MALFORMED",
                        "includegraphics has an unterminated option list",
                        normalized_source,
                        line,
                        column,
                    )
                )
                break
            option_text, index = parsed_options
            index = _skip_space(masked, index)

        parsed_group = _read_balanced(masked, index, "{", "}")
        if parsed_group is None:
            diagnostics.append(
                GraphicsDiagnostic(
                    "IMAGE_PARSE_MALFORMED",
                    f"{command} requires one static braced argument",
                    normalized_source,
                    line,
                    column,
                )
            )
            continue
        argument, index = parsed_group

        if command == "graphicspath":
            values = _parse_graphicspath(argument)
            dynamic = values is None or any(_contains_dynamic(value) for value in values)
            graphic_paths = () if values is None else values
            graphic_paths_dynamic = dynamic
            directives.append(
                GraphicsDirective("graphicspath", graphic_paths, dynamic, line, column)
            )
            if values is None:
                diagnostics.append(
                    GraphicsDiagnostic(
                        "IMAGE_PARSE_MALFORMED",
                        "graphicspath is not a static list of braced directories",
                        normalized_source,
                        line,
                        column,
                    )
                )
            continue

        if command == "DeclareGraphicsExtensions":
            values = _parse_extensions(argument)
            dynamic = values is None or any(_contains_dynamic(value) for value in values)
            extensions = DEFAULT_GRAPHICS_EXTENSIONS if values is None else values
            extensions_dynamic = dynamic
            directives.append(
                GraphicsDirective(
                    "DeclareGraphicsExtensions",
                    extensions,
                    dynamic,
                    line,
                    column,
                )
            )
            if values is None:
                diagnostics.append(
                    GraphicsDiagnostic(
                        "IMAGE_PARSE_MALFORMED",
                        "DeclareGraphicsExtensions is not a static extension list",
                        normalized_source,
                        line,
                        column,
                    )
                )
            continue

        options = _parse_options(option_text or "")
        if options is None:
            diagnostics.append(
                GraphicsDiagnostic(
                    "IMAGE_PARSE_MALFORMED",
                    "includegraphics options are not a bounded static key/value list",
                    normalized_source,
                    line,
                    column,
                )
            )
            continue
        target = argument.strip()
        if not target:
            diagnostics.append(
                GraphicsDiagnostic(
                    "IMAGE_PARSE_MALFORMED",
                    "includegraphics target is empty",
                    normalized_source,
                    line,
                    column,
                )
            )
            continue
        source_command = text[command_start:index]
        if len(source_command) > 16 * 1024:
            diagnostics.append(
                GraphicsDiagnostic(
                    "IMAGE_PARSE_MALFORMED",
                    "includegraphics command exceeds the safe rewrite bound",
                    normalized_source,
                    line,
                    column,
                )
            )
            continue
        includes.append(
            IncludeGraphics(
                source_path=normalized_source,
                target=target,
                options=options,
                graphic_paths=graphic_paths,
                extensions=extensions,
                graphic_paths_dynamic=graphic_paths_dynamic,
                extensions_dynamic=extensions_dynamic,
                starred=starred,
                line=line,
                column=column,
                start_char=command_start,
                end_char=index,
                source_text=source_command,
            )
        )

    return GraphicsScan(
        source_path=normalized_source,
        directives=tuple(directives),
        includes=tuple(includes),
        diagnostics=tuple(diagnostics),
    )


def scan_graphics_source(
    source_root: Path,
    source_path: str,
    *,
    quality: QualityProfile = REVIEW_QUALITY,
) -> GraphicsScan:
    """Read and scan one source-root-relative UTF-8 LaTeX file."""

    normalized = validate_relative_path(source_path)
    path = resolve_within(source_root, normalized)
    data = read_stable_bytes(path, max_bytes=quality.max_tex_bytes)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "LaTeX source must be UTF-8") from exc
    return scan_graphics_text(text, source_path=normalized, quality=quality)


def _reject_definitely_unsafe_path(value: str) -> None:
    stripped = value.strip()
    if stripped.startswith(("/", "\\\\")) or _WINDOWS_DRIVE_RE.match(stripped):
        raise ContractError(ErrorCode.PATH_ABSOLUTE, "absolute image paths are not allowed")
    if ":" in stripped:
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "image paths must not contain ADS syntax")
    if any(part == ".." for part in stripped.rstrip("/").split("/")):
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "image paths must not traverse parents")


def _strip_exact_current_directory_prefix(value: str) -> str:
    """Remove only exact, consecutive TeX ``./`` prefixes."""

    while value.startswith("./"):
        value = value[2:]
    return value


def _normalize_graphics_directory(value: str) -> str:
    _reject_definitely_unsafe_path(value)
    stripped = value.strip()
    if _contains_dynamic(stripped):
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "dynamic graphicspath is not resolvable")
    had_current_directory_prefix = stripped.startswith("./")
    stripped = _strip_exact_current_directory_prefix(stripped)
    if stripped == "." or (had_current_directory_prefix and not stripped):
        return ""
    if not stripped:
        raise ContractError(ErrorCode.PATH_ABSOLUTE, "root graphicspath is not allowed")
    _reject_definitely_unsafe_path(stripped)
    normalized = stripped.rstrip("/")
    if not normalized:
        raise ContractError(ErrorCode.PATH_ABSOLUTE, "root graphicspath is not allowed")
    return validate_relative_path(normalized)


def _join_portable(*parts: str) -> str:
    nonempty = [part for part in parts if part and part != "."]
    return validate_relative_path("/".join(nonempty))


def _candidate_names(occurrence: IncludeGraphics) -> tuple[str, ...]:
    normalized_target = normalize_static_graphic_target(occurrence.target)
    suffix = PurePosixPath(normalized_target).suffix.casefold()
    if suffix in _KNOWN_FORMATS:
        return (normalized_target,)

    extensions: list[str] = []
    for extension in occurrence.extensions:
        normalized_extension = extension.strip().casefold()
        if normalized_extension not in _KNOWN_FORMATS:
            raise ContractError(
                ErrorCode.BACKEND_CAPABILITY_MISSING,
                "DeclareGraphicsExtensions contains an unsupported format",
            )
        if normalized_extension not in extensions:
            extensions.append(normalized_extension)
    if not extensions:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "graphics extension list is empty")
    candidates = [normalized_target] if suffix else []
    candidates.extend(f"{normalized_target}{extension}" for extension in extensions)
    return tuple(candidates)


def _target_has_known_suffix(target: str) -> bool:
    normalized = normalize_static_graphic_target(target)
    suffix = PurePosixPath(normalized).suffix.casefold()
    return suffix in _KNOWN_FORMATS


def resolve_graphic(
    source_root: Path,
    occurrence: IncludeGraphics,
    *,
    quality: QualityProfile = REVIEW_QUALITY,
) -> ResolvedGraphic:
    """Resolve one static reference inside ``source_root`` without guessing.

    All direct and ``graphicspath`` candidates are considered.  More than one
    existing regular file is an ambiguity even when extension order differs.
    """

    if occurrence.graphic_paths_dynamic:
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "dynamic graphicspath is not resolvable")
    target_has_known_suffix = _target_has_known_suffix(occurrence.target)
    if occurrence.extensions_dynamic and not target_has_known_suffix:
        raise ContractError(ErrorCode.PATH_TRAVERSAL, "dynamic extension list is not resolvable")

    try:
        root = source_root.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "source root is unavailable") from exc
    if not root.is_dir():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "source root must be a directory")

    # Both production adapters pin their working directory to the project
    # root.  TeX therefore resolves ordinary graphics and graphicspath entries
    # from that root even when the command appears in an included sub-file.
    # Mirroring that rule is essential: resolving relative to the sub-file
    # would materialize a different asset than the backend sees.
    directories = [""]
    for raw_directory in occurrence.graphic_paths:
        directory = _normalize_graphics_directory(raw_directory)
        if directory not in directories:
            directories.append(directory)

    candidate_names = _candidate_names(occurrence)
    matches: dict[str, tuple[str, Path]] = {}
    for directory in directories:
        for candidate_name in candidate_names:
            relative_candidate = _join_portable(directory, candidate_name)
            resolved_candidate = resolve_within(
                root,
                relative_candidate,
                must_exist=False,
            )
            try:
                exists = resolved_candidate.exists()
                is_file = resolved_candidate.is_file() if exists else False
            except OSError as exc:
                raise ContractError(
                    ErrorCode.PATH_LINK_ESCAPE,
                    "image candidate cannot be inspected safely",
                ) from exc
            if not exists:
                continue
            if not is_file:
                raise ContractError(ErrorCode.SCHEMA_INVALID, "image candidate is not a file")
            try:
                physical_relative = resolved_candidate.relative_to(root).as_posix()
            except ValueError as exc:  # pragma: no cover - resolve_within invariant
                raise ContractError(
                    ErrorCode.PATH_LINK_ESCAPE, "image escaped source root"
                ) from exc
            normalized_physical = validate_relative_path(physical_relative)
            matches.setdefault(
                normalized_physical.casefold(), (normalized_physical, resolved_candidate)
            )

    if not matches:
        raise ContractError(ErrorCode.BACKEND_FAILED, "image reference did not resolve to a file")
    if len(matches) != 1:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "image reference is ambiguous",
            details={"candidates": sorted(path for path, _ in matches.values())},
        )

    relative_path, image_path = next(iter(matches.values()))
    extension = PurePosixPath(relative_path).suffix.casefold()
    source_format = _KNOWN_FORMATS.get(extension)
    if source_format is None:  # pragma: no cover - candidate construction invariant
        raise ContractError(ErrorCode.BACKEND_CAPABILITY_MISSING, "image format is unsupported")
    digest = digest_file(image_path, max_bytes=quality.max_source_bytes)
    return ResolvedGraphic(
        source_path=relative_path,
        source_format=source_format,
        size_bytes=digest.size_bytes,
        sha256=digest.sha256,
    )


def _parse_angle(value: str | None) -> int | None:
    if value is None or not _SIMPLE_NUMBER_RE.fullmatch(value.strip()):
        return None
    try:
        millidegrees = int(Decimal(value.strip()) * 1000)
    except (InvalidOperation, ValueError):
        return None
    if not -360_000 <= millidegrees <= 360_000:
        return None
    return millidegrees % 360_000


def _length_to_micro_bp(value: str) -> int | None:
    matched = _SIMPLE_LENGTH_RE.fullmatch(value.strip())
    if matched is None:
        return None
    try:
        number = Decimal(matched.group("number"))
        if number < 0:
            return None
        unit = (matched.group("unit") or "bp").casefold()
        factor = {
            "bp": Decimal(1),
            "pt": Decimal(7200) / Decimal(7227),
            "in": Decimal(72),
            "cm": Decimal(3600) / Decimal(127),
            "mm": Decimal(360) / Decimal(127),
        }[unit]
        micro_bp = (number * factor * _MICRO_BP).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        return int(micro_bp)
    except (InvalidOperation, OverflowError, ValueError):
        return None


def _parse_trim(value: str | None, quality: QualityProfile) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    pieces = value.split()
    if len(pieces) != 4:
        return None
    parsed = tuple(_length_to_micro_bp(piece) for piece in pieces)
    if any(item is None for item in parsed):
        return None
    values = tuple(int(item) for item in parsed if item is not None)
    if any(item > quality.max_page_points * _MICRO_BP for item in values):
        return None
    return values  # type: ignore[return-value]


def _parse_clip(value: str | None) -> bool | None:
    if value is None or not value.strip() or value.strip().casefold() == "true":
        return True
    if value.strip().casefold() == "false":
        return False
    return None


def _safe_layout_option(option: GraphicOption, quality: QualityProfile) -> bool:
    key = option.key.casefold()
    value = option.value
    if key in {"width", "height", "totalheight"}:
        if value is None:
            return False
        stripped = value.strip()
        if _SAFE_RELATIVE_LENGTH_RE.fullmatch(stripped):
            coefficient = stripped.split("\\", 1)[0]
            return not coefficient or Decimal(coefficient) > 0
        absolute = _length_to_micro_bp(stripped)
        return absolute is not None and 0 < absolute <= quality.max_page_points * _MICRO_BP
    if key == "scale":
        if value is None or not _SIMPLE_NUMBER_RE.fullmatch(value.strip()):
            return False
        scale = Decimal(value.strip())
        return 0 < scale <= 100
    if key == "keepaspectratio":
        return _parse_clip(value) is not None
    return False


def _supported_option_plan(
    occurrence: IncludeGraphics,
    quality: QualityProfile,
) -> tuple[
    int,
    int,
    tuple[int, int, int, int],
    bool,
    tuple[ImageOperation, ...],
    tuple[GraphicsDiagnostic, ...],
    tuple[GraphicOption, ...],
]:
    diagnostics: list[GraphicsDiagnostic] = []
    if occurrence.starred:
        diagnostics.append(
            _manual_diagnostic(
                "IMAGE_UNSUPPORTED_OPTION",
                "starred includegraphics clipping is not yet materialized",
                occurrence,
            )
        )

    seen_renderer: set[str] = set()
    normalized: dict[str, int | bool | tuple[int, int, int, int]] = {}
    operations: list[ImageOperation] = []
    passthrough: list[GraphicOption] = []
    non_page_keys: list[str] = []
    layout_seen = False
    for option in occurrence.options:
        key = option.key.casefold()
        if key in {"width", "height", "totalheight", "scale", "keepaspectratio"}:
            if _safe_layout_option(option, quality):
                passthrough.append(option)
                layout_seen = True
            else:
                diagnostic_code: DiagnosticCode = (
                    "IMAGE_DYNAMIC_INPUT"
                    if option.value is not None and _contains_dynamic(option.value)
                    else "IMAGE_UNSUPPORTED_OPTION"
                )
                diagnostics.append(
                    _manual_diagnostic(
                        diagnostic_code,
                        f"layout option '{option.key}' is not a bounded safe passthrough value",
                        occurrence,
                    )
                )
            continue
        if key in seen_renderer:
            diagnostics.append(
                _manual_diagnostic(
                    "IMAGE_UNSUPPORTED_OPTION",
                    f"repeated graphicx option '{option.key}' is not normalized automatically",
                    occurrence,
                )
            )
            continue
        seen_renderer.add(key)
        if key not in {"page", "angle", "trim", "clip"}:
            diagnostics.append(
                _manual_diagnostic(
                    "IMAGE_UNSUPPORTED_OPTION",
                    f"graphicx option '{option.key}' is outside the static materializer subset",
                    occurrence,
                )
            )
            continue
        if key != "page" and layout_seen:
            diagnostics.append(
                _manual_diagnostic(
                    "IMAGE_UNSUPPORTED_OPTION",
                    "pixel operations after layout options cannot be safely baked",
                    occurrence,
                )
            )
        if option.value is not None and _contains_dynamic(option.value):
            diagnostics.append(
                _manual_diagnostic(
                    "IMAGE_DYNAMIC_INPUT",
                    f"graphicx option '{option.key}' requires TeX expansion",
                    occurrence,
                )
            )
            continue

        if key == "page":
            raw_page = "" if option.value is None else option.value.strip()
            page = int(raw_page) if raw_page.isdecimal() else 0
            if not 1 <= page <= quality.max_document_pages:
                diagnostics.append(
                    _manual_diagnostic(
                        "IMAGE_UNSUPPORTED_OPTION",
                        "page must be a bounded positive integer",
                        occurrence,
                    )
                )
                continue
            normalized[key] = page
            operations.append(ImageOperation("page", (page,)))
            continue

        non_page_keys.append(key)
        if key == "angle":
            angle = _parse_angle(option.value)
            if angle is None:
                diagnostics.append(
                    _manual_diagnostic(
                        "IMAGE_DYNAMIC_INPUT",
                        "angle must be a simple decimal number with at most three decimals",
                        occurrence,
                    )
                )
                continue
            normalized[key] = angle
            operations.append(ImageOperation("angle", (angle,)))
        elif key == "trim":
            trim = _parse_trim(option.value, quality)
            if trim is None:
                diagnostics.append(
                    _manual_diagnostic(
                        "IMAGE_DYNAMIC_INPUT",
                        "trim must contain four non-negative simple lengths",
                        occurrence,
                    )
                )
                continue
            normalized[key] = trim
            operations.append(ImageOperation("trim", trim))
        else:
            clip = _parse_clip(option.value)
            if clip is None:
                diagnostics.append(
                    _manual_diagnostic(
                        "IMAGE_DYNAMIC_INPUT",
                        "clip must be a flag or the literal true/false",
                        occurrence,
                    )
                )
                continue
            normalized[key] = clip
            operations.append(ImageOperation("clip", (int(clip),)))

    allowed_sequences = {(), ("angle",), ("trim", "clip"), ("trim", "clip", "angle")}
    if tuple(non_page_keys) not in allowed_sequences:
        diagnostics.append(
            _manual_diagnostic(
                "IMAGE_UNSUPPORTED_OPTION",
                "supported geometric order is trim, clip, then optional angle",
                occurrence,
            )
        )
    trim_value = normalized.get("trim")
    clip_value = normalized.get("clip", False)
    if isinstance(trim_value, tuple) and clip_value is not True:
        diagnostics.append(
            _manual_diagnostic(
                "IMAGE_TRIM_REQUIRES_CLIP",
                "trim without effective clip can overprint and remains manual",
                occurrence,
            )
        )
    if clip_value is True and not isinstance(trim_value, tuple):
        diagnostics.append(
            _manual_diagnostic(
                "IMAGE_UNSUPPORTED_OPTION",
                "clip without a static trim box remains manual",
                occurrence,
            )
        )

    raw_page_value = normalized.get("page", 1)
    raw_angle_value = normalized.get("angle", 0)
    page_value = raw_page_value if isinstance(raw_page_value, int) else 1
    angle_value = raw_angle_value if isinstance(raw_angle_value, int) else 0
    trim = trim_value if isinstance(trim_value, tuple) else (0, 0, 0, 0)
    return (
        page_value,
        angle_value,
        trim,
        bool(clip_value),
        tuple(operations),
        tuple(diagnostics),
        tuple(passthrough),
    )


def plan_image_request(
    source_root: Path,
    occurrence: IncludeGraphics,
    *,
    quality: QualityProfile = REVIEW_QUALITY,
) -> ImagePlan:
    """Resolve a static PDF request or return explicit manual diagnostics."""

    try:
        normalized_target: str | None = normalize_static_graphic_target(occurrence.target)
    except ContractError as error:
        if not is_dynamic_graphic_target_error(error):
            raise
        normalized_target = None
    for directory in occurrence.graphic_paths:
        _reject_definitely_unsafe_path(directory)
    target_has_known_suffix = (
        False if normalized_target is None else _target_has_known_suffix(normalized_target)
    )
    if (
        normalized_target is None
        or occurrence.graphic_paths_dynamic
        or (occurrence.extensions_dynamic and not target_has_known_suffix)
    ):
        return ImagePlan(
            None,
            (
                _manual_diagnostic(
                    "IMAGE_DYNAMIC_INPUT",
                    "image lookup depends on TeX expansion and remains manual",
                    occurrence,
                ),
            ),
        )

    page, angle, trim, clip, operations, diagnostics, passthrough = _supported_option_plan(
        occurrence,
        quality,
    )
    if diagnostics:
        return ImagePlan(None, diagnostics, passthrough)
    resolved = resolve_graphic(source_root, occurrence, quality=quality)
    if resolved.source_format != "pdf":
        return ImagePlan(
            None,
            (
                _manual_diagnostic(
                    "IMAGE_UNSUPPORTED_FORMAT",
                    "only PDF has an in-process renderer; SVG/EPS and raster passthrough "
                    "remain manual",
                    occurrence,
                ),
            ),
            passthrough,
        )
    return ImagePlan(
        ImageRequest(
            source_path=resolved.source_path,
            source_format=resolved.source_format,
            source_sha256=resolved.sha256,
            source_size_bytes=resolved.size_bytes,
            page=page,
            angle_millidegrees=angle,
            trim_micro_bp=trim,
            clip=clip,
            operations=operations,
            quality=quality,
        ),
        (),
        passthrough,
    )


def image_cache_key(request: ImageRequest, renderer: RendererIdentity) -> ImageCacheKey:
    return ImageCacheKey(request_sha256=request.request_sha256, renderer=renderer)


def _distribution_member(
    distribution: importlib.metadata.Distribution,
    member_name: str,
) -> Path:
    normalized_name = member_name.casefold()
    matches = [
        item
        for item in distribution.files or ()
        if str(item).replace("\\", "/").casefold() == normalized_name
    ]
    if len(matches) != 1:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDF renderer distribution is incomplete",
        )
    path = Path(str(distribution.locate_file(matches[0])))
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDF renderer distribution member is unavailable",
        ) from exc
    if not resolved.is_file():
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDF renderer distribution member is not a file",
        )
    return resolved


def _pdfium_version_from_distribution(distribution: importlib.metadata.Distribution) -> str:
    version_path = _distribution_member(distribution, "pypdfium2_raw/version.json")
    data = read_stable_bytes(version_path, max_bytes=64 * 1024)
    try:
        parsed = json.loads(data, object_pairs_hook=_reject_duplicate_keys)
    except ContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDFium version metadata is invalid",
        ) from exc
    if not isinstance(parsed, dict):
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDFium version metadata is not an object",
        )
    values = [parsed.get(name) for name in ("major", "minor", "build", "patch")]
    if any(type(value) is not int or value < 0 for value in values):
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDFium version metadata is incomplete",
        )
    return ".".join(str(value) for value in values)


def _load_renderer_identity() -> RendererIdentity:
    """Identify the renderer without importing PDFium into this process."""

    if os.name != "nt":
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "the image materializer supports Windows only",
        )
    try:
        pdfium_distribution = importlib.metadata.distribution("pypdfium2")
        pillow_version = importlib.metadata.version("Pillow")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ContractError(
            ErrorCode.TOOL_MISSING,
            "PDF materialization requires the pdf-figures optional dependency",
        ) from exc
    pypdfium2_version = pdfium_distribution.version
    if pypdfium2_version.split(".", 1)[0] != "5" or pillow_version.split(".", 1)[0] != "12":
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDF materialization requires pypdfium2 5.x and Pillow 12.x",
        )
    pdfium_binary = _distribution_member(pdfium_distribution, "pypdfium2_raw/pdfium.dll")
    try:
        binary_digest = digest_file(pdfium_binary, max_bytes=128 * 1024 * 1024)
    except ContractError as exc:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDFium binary cannot be identified",
        ) from exc
    return RendererIdentity(
        name="pypdfium2-pillow",
        pypdfium2_version=pypdfium2_version,
        pdfium_version=_pdfium_version_from_distribution(pdfium_distribution),
        pdfium_binary_sha256=binary_digest.sha256,
        pillow_version=pillow_version,
    )


def _load_pillow_image_module(renderer: RendererIdentity) -> Any:
    try:
        image_module = importlib.import_module("PIL.Image")
        version = importlib.metadata.version("Pillow")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise ContractError(
            ErrorCode.TOOL_MISSING,
            "PDF materialization requires Pillow",
        ) from exc
    if version != renderer.pillow_version:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "Pillow changed after renderer identification",
        )
    return image_module


def _load_pdf_runtime() -> _PdfRuntime:
    if os.name != "nt":
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "the image materializer supports Windows only",
        )
    try:
        pdfium = importlib.import_module("pypdfium2")
        pdfium_raw = importlib.import_module("pypdfium2.raw")
        pdfium_version_module = importlib.import_module("pypdfium2.version")
        raw_package = importlib.import_module("pypdfium2_raw")
        image_module = importlib.import_module("PIL.Image")
        pypdfium2_version = importlib.metadata.version("pypdfium2")
        pillow_version = importlib.metadata.version("Pillow")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise ContractError(
            ErrorCode.TOOL_MISSING,
            "PDF materialization requires the pdf-figures optional dependency",
        ) from exc
    if pypdfium2_version.split(".", 1)[0] != "5" or pillow_version.split(".", 1)[0] != "12":
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDF materialization requires pypdfium2 5.x and Pillow 12.x",
        )
    raw_file = getattr(raw_package, "__file__", None)
    if not isinstance(raw_file, str):
        raise ContractError(ErrorCode.TOOL_VERSION_UNSUPPORTED, "PDFium package is incomplete")
    pdfium_binary = Path(raw_file).with_name("pdfium.dll")
    try:
        binary_digest = digest_file(pdfium_binary, max_bytes=128 * 1024 * 1024)
    except ContractError as exc:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "PDFium binary cannot be identified",
        ) from exc
    pdfium_info = getattr(pdfium_version_module, "PDFIUM_INFO", None)
    pdfium_version = str(getattr(pdfium_info, "version", pdfium_info))
    identity = RendererIdentity(
        name="pypdfium2-pillow",
        pypdfium2_version=pypdfium2_version,
        pdfium_version=pdfium_version,
        pdfium_binary_sha256=binary_digest.sha256,
        pillow_version=pillow_version,
    )
    return _PdfRuntime(pdfium, pdfium_raw, image_module, identity)


def _check_pixel_bounds(width: int, height: int, quality: QualityProfile) -> None:
    if width < 1 or height < 1:
        raise ContractError(ErrorCode.BACKEND_FAILED, "rendered image has an empty canvas")
    if width > quality.max_dimension_px or height > quality.max_dimension_px:
        raise ContractError(ErrorCode.BACKEND_FAILED, "rendered image exceeds dimension limits")
    if width * height > quality.max_render_pixels:
        raise ContractError(ErrorCode.BACKEND_FAILED, "rendered image exceeds the pixel limit")


def _pixel_bounds_satisfied(width: int, height: int, quality: QualityProfile) -> bool:
    return (
        1 <= width <= quality.max_dimension_px
        and 1 <= height <= quality.max_dimension_px
        and width * height <= quality.max_render_pixels
    )


def _effective_dpi_is_valid(effective_dpi: object, quality: QualityProfile) -> bool:
    minimum = min(quality.dpi, quality.min_render_dpi)
    return type(effective_dpi) is int and minimum <= effective_dpi <= quality.dpi


def _rotated_bounds(width: int, height: int, angle_millidegrees: int) -> tuple[int, int]:
    if angle_millidegrees in {0, 180_000}:
        return width, height
    if angle_millidegrees in {90_000, 270_000}:
        return height, width
    # Match Pillow 12's centered ``rotate(..., expand=True)`` canvas
    # calculation exactly so the selector chooses the highest DPI whose real
    # output fits, rather than a lower conservative approximation.
    radians = -math.radians(angle_millidegrees / 1000)
    cosine = round(math.cos(radians), 15)
    sine = round(math.sin(radians), 15)
    center_x = width / 2
    center_y = height / 2
    translate_x = cosine * -center_x + sine * -center_y + center_x
    translate_y = -sine * -center_x + cosine * -center_y + center_y
    transformed = tuple(
        (
            cosine * x + sine * y + translate_x,
            -sine * x + cosine * y + translate_y,
        )
        for x, y in ((0, 0), (width, 0), (width, height), (0, height))
    )
    horizontal = tuple(point[0] for point in transformed)
    vertical = tuple(point[1] for point in transformed)
    return (
        math.ceil(max(horizontal)) - math.floor(min(horizontal)),
        math.ceil(max(vertical)) - math.floor(min(vertical)),
    )


def _dimensions_at_dpi(
    page_width: float,
    page_height: float,
    crop: tuple[float, float, float, float],
    angle_millidegrees: int,
    dpi: int,
) -> tuple[int, int, int, int]:
    scale = dpi / 72
    left, bottom, right, top = crop
    width_px = math.ceil(page_width * scale) - math.ceil(left * scale) - math.ceil(right * scale)
    height_px = math.ceil(page_height * scale) - math.ceil(bottom * scale) - math.ceil(top * scale)
    rotated_width, rotated_height = _rotated_bounds(
        width_px,
        height_px,
        angle_millidegrees,
    )
    return width_px, height_px, rotated_width, rotated_height


def _select_effective_dpi(
    page_width: float,
    page_height: float,
    crop: tuple[float, float, float, float],
    angle_millidegrees: int,
    quality: QualityProfile,
) -> tuple[int, int, int]:
    """Choose the highest bounded integer DPI without exceeding the request."""

    minimum = min(quality.dpi, quality.min_render_dpi)
    for effective_dpi in range(quality.dpi, minimum - 1, -1):
        width_px, height_px, rotated_width, rotated_height = _dimensions_at_dpi(
            page_width,
            page_height,
            crop,
            angle_millidegrees,
            effective_dpi,
        )
        if _pixel_bounds_satisfied(
            width_px,
            height_px,
            quality,
        ) and _pixel_bounds_satisfied(rotated_width, rotated_height, quality):
            return effective_dpi, width_px, height_px
    raise ContractError(
        ErrorCode.BACKEND_FAILED,
        "PDF page cannot fit render limits at the minimum permitted DPI",
    )


def _rotate_image(image: Any, angle_millidegrees: int, image_module: Any) -> Any:
    if angle_millidegrees == 0:
        return image
    transpose = image_module.Transpose
    if angle_millidegrees == 90_000:
        return image.transpose(transpose.ROTATE_90)
    if angle_millidegrees == 180_000:
        return image.transpose(transpose.ROTATE_180)
    if angle_millidegrees == 270_000:
        return image.transpose(transpose.ROTATE_270)
    return image.rotate(
        angle_millidegrees / 1000,
        resample=image_module.Resampling.BICUBIC,
        expand=True,
        fillcolor=(255, 255, 255),
    )


def _pixel_sha256(image: Any) -> str:
    width, height = image.size
    header = f"RGB\x00{width}x{height}\x00".encode("ascii")
    return sha256_bytes(header + image.tobytes())


def _encode_canonical_png(
    image: Any,
    quality: QualityProfile,
    effective_dpi: int,
) -> bytes:
    _contract(
        _effective_dpi_is_valid(effective_dpi, quality),
        "effective render DPI is outside the quality profile",
    )
    output = io.BytesIO()
    image.save(
        output,
        format="PNG",
        compress_level=9,
        optimize=False,
        dpi=(effective_dpi, effective_dpi),
    )
    data = output.getvalue()
    if len(data) > quality.max_png_bytes:
        raise ContractError(ErrorCode.BACKEND_FAILED, "canonical PNG exceeds the output limit")
    return data


def _decode_png(
    data: bytes,
    quality: QualityProfile,
    image_module: Any,
    effective_dpi: int,
) -> Any:
    if not data or len(data) > quality.max_png_bytes:
        raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "cached PNG size is invalid")
    if not _effective_dpi_is_valid(effective_dpi, quality):
        raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "PNG DPI is outside the request")
    try:
        with image_module.open(io.BytesIO(data)) as opened:
            if opened.format != "PNG":
                raise ContractError(
                    ErrorCode.HASH_INTEGRITY_MISMATCH,
                    "cache entry is not a PNG",
                )
            metadata_dpi = opened.info.get("dpi")
            if (
                not isinstance(metadata_dpi, tuple)
                or len(metadata_dpi) != 2
                or any(
                    not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or abs(float(value) - effective_dpi) > 0.05
                    for value in metadata_dpi
                )
            ):
                raise ContractError(
                    ErrorCode.HASH_INTEGRITY_MISMATCH,
                    "PNG resolution metadata does not match the effective DPI",
                )
            width, height = opened.size
            _check_pixel_bounds(int(width), int(height), quality)
            opened.load()
            image = opened.convert("RGB").copy()
    except ContractError:
        raise
    except (OSError, ValueError, MemoryError) as exc:
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "cached PNG cannot be decoded safely",
        ) from exc
    return image


def _render_pdf(
    source_bytes: bytes, request: ImageRequest, runtime: _PdfRuntime
) -> tuple[bytes, int, int, str, int]:
    try:
        document = runtime.pdfium.PdfDocument(source_bytes, password=None)
    except Exception as exc:
        password_code = getattr(runtime.pdfium_raw, "FPDF_ERR_PASSWORD", object())
        if getattr(exc, "err_code", None) == password_code:
            message = "encrypted PDFs are not accepted"
        else:
            message = "PDF is corrupt or cannot be opened"
        raise ContractError(ErrorCode.BACKEND_FAILED, message) from exc

    page = None
    bitmap = None
    source_image = None
    final_image = None
    try:
        security_revision = int(runtime.pdfium_raw.FPDF_GetSecurityHandlerRevision(document.raw))
        if security_revision >= 0:
            raise ContractError(ErrorCode.BACKEND_FAILED, "encrypted PDFs are not accepted")
        page_count = int(len(document))
        if page_count < 1:
            raise ContractError(ErrorCode.BACKEND_FAILED, "PDF has no pages")
        if page_count > request.quality.max_document_pages:
            raise ContractError(ErrorCode.BACKEND_FAILED, "PDF exceeds the page-count limit")
        if request.page > page_count:
            raise ContractError(ErrorCode.BACKEND_FAILED, "requested PDF page is out of range")
        page_index = request.page - 1
        page_width, page_height = document.get_page_size(page_index)
        if (
            not math.isfinite(page_width)
            or not math.isfinite(page_height)
            or page_width <= 0
            or page_height <= 0
            or page_width > request.quality.max_page_points
            or page_height > request.quality.max_page_points
        ):
            raise ContractError(ErrorCode.BACKEND_FAILED, "PDF page geometry is invalid")

        crop = cast(
            tuple[float, float, float, float],
            tuple(value / _MICRO_BP for value in request.trim_micro_bp),
        )
        left, bottom, right, top = crop
        if left + right >= page_width or bottom + top >= page_height:
            raise ContractError(ErrorCode.BACKEND_FAILED, "trim removes the entire PDF page")
        effective_dpi, width_px, height_px = _select_effective_dpi(
            page_width,
            page_height,
            crop,
            request.angle_millidegrees,
            request.quality,
        )
        scale = effective_dpi / 72
        _check_pixel_bounds(width_px, height_px, request.quality)
        rotated_width, rotated_height = _rotated_bounds(
            width_px,
            height_px,
            request.angle_millidegrees,
        )
        _check_pixel_bounds(rotated_width, rotated_height, request.quality)

        page = document[page_index]
        bitmap = page.render(
            scale=scale,
            rotation=0,
            crop=crop,
            may_draw_forms=False,
            fill_color=(255, 255, 255, 255),
            draw_annots=False,
            rev_byteorder=True,
            maybe_alpha=False,
        )
        source_image = bitmap.to_pil().convert("RGB").copy()
        if source_image.size != (width_px, height_px):
            raise ContractError(ErrorCode.BACKEND_FAILED, "PDFium returned unexpected dimensions")
        final_image = _rotate_image(
            source_image,
            request.angle_millidegrees,
            runtime.image_module,
        )
        if final_image is not source_image:
            source_image.close()
            source_image = None
        final_image = final_image.convert("RGB")
        final_width, final_height = (int(value) for value in final_image.size)
        _check_pixel_bounds(final_width, final_height, request.quality)
        pixel_sha256 = _pixel_sha256(final_image)
        png_bytes = _encode_canonical_png(final_image, request.quality, effective_dpi)
        decoded = _decode_png(
            png_bytes,
            request.quality,
            runtime.image_module,
            effective_dpi,
        )
        try:
            if decoded.size != final_image.size or _pixel_sha256(decoded) != pixel_sha256:
                raise ContractError(
                    ErrorCode.BACKEND_FAILED,
                    "canonical PNG verification failed",
                )
        finally:
            decoded.close()
        return png_bytes, final_width, final_height, pixel_sha256, effective_dpi
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError(ErrorCode.BACKEND_FAILED, "PDF rendering failed safely") from exc
    finally:
        if final_image is not None:
            final_image.close()
        if source_image is not None:
            source_image.close()
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        document.close()


def _is_link_or_junction(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction_probe is not None and junction_probe())


def _prepare_cache_root(source_root: Path, cache_root: Path) -> Path:
    _, target = ensure_disjoint_roots(source_root, cache_root)
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "cache parent is unavailable") from exc
    if not parent.is_dir() or _is_link_or_junction(parent):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "cache parent must be a real directory")
    if target.exists() or target.is_symlink():
        if _is_link_or_junction(target):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "cache root must not be a link")
        if not target.is_dir():
            raise ContractError(ErrorCode.SCHEMA_INVALID, "cache root must be a directory")
    else:
        try:
            target.mkdir(mode=0o700)
        except FileExistsError:
            if _is_link_or_junction(target) or not target.is_dir():
                raise ContractError(
                    ErrorCode.PATH_LINK_ESCAPE, "cache root creation raced"
                ) from None
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT, "cache root cannot be created"
            ) from exc
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:  # pragma: no cover - mkdir/exists invariant
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "cache root disappeared") from exc
    if resolved.parent != parent:
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "cache root escaped its parent")
    return resolved


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "cache manifest repeats a key")
        result[key] = value
    return result


def _read_cache_manifest(path: Path) -> dict[str, Any]:
    data = read_stable_bytes(path, max_bytes=128 * 1024)
    try:
        parsed = json.loads(data, object_pairs_hook=_reject_duplicate_keys)
    except ContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "cache manifest is invalid JSON",
        ) from exc
    if not isinstance(parsed, dict) or data != canonical_json(parsed) + b"\n":
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "cache manifest is not canonical",
        )
    return parsed


def _read_canonical_object(path: Path, *, max_bytes: int, label: str) -> dict[str, Any]:
    data = read_stable_bytes(path, max_bytes=max_bytes)
    try:
        parsed = json.loads(data, object_pairs_hook=_reject_duplicate_keys)
    except ContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            f"{label} is invalid JSON",
        ) from exc
    if not isinstance(parsed, dict) or data != canonical_json(parsed) + b"\n":
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            f"{label} is not a canonical object",
        )
    return parsed


def _strict_int(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} must be an integer")
    return value


def _quality_from_dict(value: object) -> QualityProfile:
    expected = {
        "name",
        "dpi",
        "min_render_dpi",
        "max_tex_bytes",
        "max_graphics_commands",
        "max_source_bytes",
        "max_document_pages",
        "max_page_points",
        "max_dimension_px",
        "max_render_pixels",
        "max_png_bytes",
        "render_timeout_s",
    }
    if isinstance(value, dict) and set(value) == expected - {"min_render_dpi"}:
        raise ContractError(
            ErrorCode.TOOL_VERSION_UNSUPPORTED,
            "legacy image cache requests are unsupported; regenerate the image overlay",
        )
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "worker quality profile is invalid")
    name = value["name"]
    if not isinstance(name, str):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "worker quality name is invalid")
    return QualityProfile(
        name=name,
        dpi=_strict_int(value["dpi"], label="worker DPI"),
        min_render_dpi=_strict_int(
            value["min_render_dpi"],
            label="worker minimum render DPI",
        ),
        max_tex_bytes=_strict_int(value["max_tex_bytes"], label="worker TeX limit"),
        max_graphics_commands=_strict_int(
            value["max_graphics_commands"],
            label="worker command limit",
        ),
        max_source_bytes=_strict_int(
            value["max_source_bytes"],
            label="worker source limit",
        ),
        max_document_pages=_strict_int(
            value["max_document_pages"],
            label="worker page limit",
        ),
        max_page_points=_strict_int(
            value["max_page_points"],
            label="worker geometry limit",
        ),
        max_dimension_px=_strict_int(
            value["max_dimension_px"],
            label="worker dimension limit",
        ),
        max_render_pixels=_strict_int(
            value["max_render_pixels"],
            label="worker pixel limit",
        ),
        max_png_bytes=_strict_int(value["max_png_bytes"], label="worker PNG limit"),
        render_timeout_s=_strict_int(
            value["render_timeout_s"],
            label="worker timeout",
        ),
    )


def _image_request_from_dict(value: object) -> ImageRequest:
    expected = {
        "source_path",
        "source_format",
        "source_sha256",
        "source_size_bytes",
        "page",
        "angle_millidegrees",
        "trim_micro_bp",
        "clip",
        "operations",
        "quality",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "worker image request is invalid")
    source_path = value["source_path"]
    source_format = value["source_format"]
    source_sha256 = value["source_sha256"]
    if (
        not isinstance(source_path, str)
        or not isinstance(source_format, str)
        or source_format not in set(_KNOWN_FORMATS.values())
        or not isinstance(source_sha256, str)
    ):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "worker image source binding is invalid")
    raw_trim = value["trim_micro_bp"]
    if not isinstance(raw_trim, list) or len(raw_trim) != 4:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "worker trim box is invalid")
    trim = tuple(_strict_int(item, label="worker trim value") for item in raw_trim)
    raw_operations = value["operations"]
    if not isinstance(raw_operations, list) or len(raw_operations) > 8:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "worker operations are invalid")
    operations: list[ImageOperation] = []
    for raw_operation in raw_operations:
        if not isinstance(raw_operation, dict) or set(raw_operation) != {"kind", "values"}:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "worker operation is invalid")
        kind = raw_operation["kind"]
        raw_values = raw_operation["values"]
        if (
            not isinstance(kind, str)
            or kind not in {"page", "trim", "clip", "angle"}
            or not isinstance(raw_values, list)
        ):
            raise ContractError(ErrorCode.SCHEMA_INVALID, "worker operation values are invalid")
        operations.append(
            ImageOperation(
                cast(OperationKind, kind),
                tuple(_strict_int(item, label="worker operation value") for item in raw_values),
            )
        )
    clip = value["clip"]
    if type(clip) is not bool:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "worker clip flag is invalid")
    return ImageRequest(
        source_path=source_path,
        source_format=source_format,
        source_sha256=source_sha256,
        source_size_bytes=_strict_int(value["source_size_bytes"], label="worker source size"),
        page=_strict_int(value["page"], label="worker page"),
        angle_millidegrees=_strict_int(
            value["angle_millidegrees"],
            label="worker angle",
        ),
        trim_micro_bp=cast(tuple[int, int, int, int], trim),
        clip=clip,
        operations=tuple(operations),
        quality=_quality_from_dict(value["quality"]),
    )


def _worker_request_document(
    request: ImageRequest,
    renderer: RendererIdentity,
) -> dict[str, object]:
    return {
        "format": _WORKER_REQUEST_FORMAT,
        "request": request.as_cache_input(),
        "request_sha256": request.request_sha256,
        "renderer": renderer.as_dict(),
    }


def _renderer_identity_from_dict(value: object) -> RendererIdentity:
    expected = {
        "name",
        "pypdfium2_version",
        "pdfium_version",
        "pdfium_binary_sha256",
        "pillow_version",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "worker renderer identity is invalid")
    if value["name"] != "pypdfium2-pillow" or any(
        not isinstance(value[field], str) for field in expected - {"name"}
    ):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "worker renderer values are invalid")
    return RendererIdentity(
        name="pypdfium2-pillow",
        pypdfium2_version=cast(str, value["pypdfium2_version"]),
        pdfium_version=cast(str, value["pdfium_version"]),
        pdfium_binary_sha256=cast(str, value["pdfium_binary_sha256"]),
        pillow_version=cast(str, value["pillow_version"]),
    )


def _write_exclusive(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "cache stage write failed") from exc


def _remove_owned_stage(stage: Path, cache_root: Path, prefix: str) -> None:
    if not stage.exists():
        return
    try:
        resolved_root = cache_root.resolve(strict=True)
        resolved_stage = stage.resolve(strict=False)
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT, "cache stage cannot be inspected"
        ) from exc
    if (
        resolved_stage.parent != resolved_root
        or not stage.name.startswith(prefix)
        or _is_link_or_junction(stage)
    ):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT, "refusing to remove an unowned cache stage"
        )
    try:
        if any(_is_link_or_junction(child) for child in stage.rglob("*")):
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "cache stage contains a link")
        shutil.rmtree(stage)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "cache stage cleanup failed") from exc


def _verify_worker_output(
    worker_root: Path,
    request: ImageRequest,
    renderer: RendererIdentity,
    image_module: Any,
) -> tuple[bytes, int, int, str, int]:
    report_path = resolve_within(worker_root, _WORKER_REPORT_NAME)
    png_path = resolve_within(worker_root, _WORKER_PNG_NAME)
    report = _read_canonical_object(
        report_path,
        max_bytes=128 * 1024,
        label="image worker report",
    )
    if set(report) != {"format", "request_sha256", "renderer", "output"}:
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "image worker report shape is invalid",
        )
    if (
        report.get("format") != _WORKER_REPORT_FORMAT
        or report.get("request_sha256") != request.request_sha256
        or report.get("renderer") != renderer.as_dict()
    ):
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "image worker report bindings do not match",
        )
    output = report.get("output")
    if not isinstance(output, dict) or set(output) != {
        "path",
        "media_type",
        "mode",
        "size_bytes",
        "sha256",
        "pixel_sha256",
        "width_px",
        "height_px",
        "dpi",
    }:
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "image worker output record is invalid",
        )
    if (
        output.get("path") != _WORKER_PNG_NAME
        or output.get("media_type") != "image/png"
        or output.get("mode") != "RGB"
        or not _effective_dpi_is_valid(output.get("dpi"), request.quality)
        or type(output.get("size_bytes")) is not int
        or type(output.get("width_px")) is not int
        or type(output.get("height_px")) is not int
        or not isinstance(output.get("sha256"), str)
        or not isinstance(output.get("pixel_sha256"), str)
        or not _HASH_RE.fullmatch(cast(str, output.get("sha256")))
        or not _HASH_RE.fullmatch(cast(str, output.get("pixel_sha256")))
    ):
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "image worker output values are invalid",
        )
    effective_dpi = cast(int, output["dpi"])
    png_bytes = read_stable_bytes(png_path, max_bytes=request.quality.max_png_bytes)
    png_digest = digest_bytes(png_bytes)
    if png_digest.size_bytes != output["size_bytes"] or png_digest.sha256 != output["sha256"]:
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "image worker PNG digest does not match",
        )
    decoded = _decode_png(
        png_bytes,
        request.quality,
        image_module,
        effective_dpi,
    )
    try:
        width_px, height_px = (int(value) for value in decoded.size)
        pixel_sha256 = _pixel_sha256(decoded)
        if (
            width_px != output["width_px"]
            or height_px != output["height_px"]
            or pixel_sha256 != output["pixel_sha256"]
            or _encode_canonical_png(decoded, request.quality, effective_dpi) != png_bytes
        ):
            raise ContractError(
                ErrorCode.HASH_INTEGRITY_MISMATCH,
                "image worker PNG verification failed",
            )
    finally:
        decoded.close()
    return png_bytes, width_px, height_px, pixel_sha256, effective_dpi


def _run_pdf_worker(
    source_bytes: bytes,
    request: ImageRequest,
    renderer: RendererIdentity,
    image_module: Any,
    cache_root: Path,
) -> tuple[bytes, int, int, str, int]:
    prefix = ".pdf-render-worker-"
    try:
        worker_root = Path(tempfile.mkdtemp(prefix=prefix, dir=cache_root))
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "PDF render worker directory cannot be created",
        ) from exc
    try:
        _write_exclusive(worker_root / _WORKER_SOURCE_NAME, source_bytes)
        request_bytes = canonical_json(_worker_request_document(request, renderer)) + b"\n"
        _write_exclusive(worker_root / _WORKER_REQUEST_NAME, request_bytes)
        worker = internal_worker_command(
            "image-render",
            (
                "--source",
                _WORKER_SOURCE_NAME,
                "--request",
                _WORKER_REQUEST_NAME,
                "--output",
                _WORKER_PNG_NAME,
                "--report",
                _WORKER_REPORT_NAME,
            ),
        )
        result = run_command(
            worker.executable,
            worker.arguments,
            cwd=worker_root,
            timeout_s=float(request.quality.render_timeout_s),
            max_output_bytes=64 * 1024,
            environment=minimal_environment(temp_root=worker_root),
        )
        if result.timed_out:
            raise ContractError(ErrorCode.BACKEND_FAILED, "PDF render worker timed out")
        if result.output_truncated or result.returncode != 0:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "PDF render worker failed in containment",
            )
        if (
            not (worker_root / _WORKER_PNG_NAME).is_file()
            or not (worker_root / _WORKER_REPORT_NAME).is_file()
        ):
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "PDF render worker returned success without complete artifacts",
            )
        return _verify_worker_output(
            worker_root,
            request,
            renderer,
            image_module,
        )
    finally:
        _remove_owned_stage(worker_root, cache_root, prefix)


def _cache_manifest(
    request: ImageRequest,
    key: ImageCacheKey,
    *,
    png_bytes: bytes,
    pixel_sha256: str,
    width_px: int,
    height_px: int,
    effective_dpi: int,
) -> dict[str, object]:
    _contract(
        _effective_dpi_is_valid(effective_dpi, request.quality),
        "effective render DPI is outside the cache request",
    )
    return {
        "format": _CACHE_FORMAT,
        "cache_key": key.as_dict(),
        "cache_key_sha256": key.sha256,
        "request": request.as_cache_input(),
        "request_sha256": request.request_sha256,
        "output": {
            "path": _CACHE_PNG_NAME,
            "media_type": "image/png",
            "mode": "RGB",
            "size_bytes": len(png_bytes),
            "sha256": digest_bytes(png_bytes).sha256,
            "pixel_sha256": pixel_sha256,
            "width_px": width_px,
            "height_px": height_px,
            "dpi": effective_dpi,
        },
    }


def _verify_cache_entry(
    entry: Path,
    request: ImageRequest,
    key: ImageCacheKey,
    renderer: RendererIdentity,
    image_module: Any | None,
    *,
    reused: bool,
    verify_pixels: bool = True,
) -> MaterializedImage:
    try:
        if _is_link_or_junction(entry) or not entry.is_dir():
            raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "cache entry is not a directory")
        members = {child.name for child in entry.iterdir()}
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH, "cache entry is unavailable"
        ) from exc
    if members != {_CACHE_MANIFEST_NAME, _CACHE_PNG_NAME}:
        raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "cache entry file set is invalid")
    for child_name in members:
        child = entry / child_name
        if _is_link_or_junction(child):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "cache entry contains a link")

    manifest_path = resolve_within(entry, _CACHE_MANIFEST_NAME)
    png_path = resolve_within(entry, _CACHE_PNG_NAME)
    manifest = _read_cache_manifest(manifest_path)
    expected_keys = {
        "format",
        "cache_key",
        "cache_key_sha256",
        "request",
        "request_sha256",
        "output",
    }
    if set(manifest) != expected_keys:
        raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "cache manifest shape is invalid")
    expected_bindings = {
        "format": _CACHE_FORMAT,
        "cache_key": key.as_dict(),
        "cache_key_sha256": key.sha256,
        "request": request.as_cache_input(),
        "request_sha256": request.request_sha256,
    }
    if any(manifest.get(field) != value for field, value in expected_bindings.items()):
        raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "cache bindings do not match")
    output = manifest.get("output")
    if not isinstance(output, dict) or set(output) != {
        "path",
        "media_type",
        "mode",
        "size_bytes",
        "sha256",
        "pixel_sha256",
        "width_px",
        "height_px",
        "dpi",
    }:
        raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "cache output record is invalid")
    if (
        output.get("path") != _CACHE_PNG_NAME
        or output.get("media_type") != "image/png"
        or output.get("mode") != "RGB"
        or not _effective_dpi_is_valid(output.get("dpi"), request.quality)
        or type(output.get("size_bytes")) is not int
        or type(output.get("width_px")) is not int
        or type(output.get("height_px")) is not int
        or not isinstance(output.get("sha256"), str)
        or not isinstance(output.get("pixel_sha256"), str)
        or not _HASH_RE.fullmatch(str(output.get("sha256")))
        or not _HASH_RE.fullmatch(str(output.get("pixel_sha256")))
    ):
        raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "cache output values are invalid")
    width_px = cast(int, output["width_px"])
    height_px = cast(int, output["height_px"])
    size_bytes = cast(int, output["size_bytes"])
    if (
        size_bytes <= 0
        or size_bytes > request.quality.max_png_bytes
        or width_px <= 0
        or height_px <= 0
        or width_px > request.quality.max_dimension_px
        or height_px > request.quality.max_dimension_px
        or width_px * height_px > request.quality.max_render_pixels
    ):
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "cache output dimensions are invalid",
        )
    effective_dpi = cast(int, output["dpi"])
    png_bytes = read_stable_bytes(png_path, max_bytes=request.quality.max_png_bytes)
    png_digest = digest_bytes(png_bytes)
    if png_digest.size_bytes != size_bytes or png_digest.sha256 != output["sha256"]:
        raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "cached PNG bytes were modified")
    pixel_sha256 = cast(str, output["pixel_sha256"])
    if verify_pixels:
        if image_module is None:  # pragma: no cover - internal call invariant
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "strong cache verification requires the image runtime",
            )
        decoded = _decode_png(
            png_bytes,
            request.quality,
            image_module,
            effective_dpi,
        )
        try:
            decoded_width, decoded_height = (int(value) for value in decoded.size)
            decoded_pixel_sha256 = _pixel_sha256(decoded)
            if (
                decoded_width != width_px
                or decoded_height != height_px
                or decoded_pixel_sha256 != pixel_sha256
            ):
                raise ContractError(
                    ErrorCode.HASH_INTEGRITY_MISMATCH,
                    "cached pixels were modified",
                )
            if _encode_canonical_png(decoded, request.quality, effective_dpi) != png_bytes:
                raise ContractError(
                    ErrorCode.HASH_INTEGRITY_MISMATCH,
                    "cached PNG is not canonical",
                )
        finally:
            decoded.close()

    entry_name = key.sha256.removeprefix("sha256:")
    return MaterializedImage(
        cache_key_sha256=key.sha256,
        request_sha256=request.request_sha256,
        png_path=f"{entry_name}/{_CACHE_PNG_NAME}",
        manifest_path=f"{entry_name}/{_CACHE_MANIFEST_NAME}",
        png_sha256=png_digest.sha256,
        pixel_sha256=pixel_sha256,
        width_px=width_px,
        height_px=height_px,
        dpi=effective_dpi,
        renderer=renderer,
        reused=reused,
    )


class SealedImageCacheVerifier:
    """Fast verifier for cache entries already bound into sealed export evidence.

    Cache creation and reuse always use :func:`verify_image_cache_entry`'s
    strong pixel/canonical-PNG verification. Once an overlay manifest has
    bound those verified pixel properties and the exact PNG SHA-256, workflow
    reloads only need to re-check the installed renderer, the complete cache
    manifest/request/key bindings, the exact directory contents, and the PNG
    byte size/SHA-256. Matching bytes necessarily retain the pixel evidence
    established at publication, so decoding and re-encoding on every status
    read adds no integrity signal.

    One instance validates the installed renderer once for one workflow
    inspection. It does not cache file evidence: every ``verify`` call re-reads
    the manifest and PNG bytes, so later mutations remain detectable.
    """

    __slots__ = ("_renderer",)

    def __init__(self, renderer: Mapping[str, Any]) -> None:
        declared_renderer = _renderer_identity_from_dict(dict(renderer))
        current_renderer = _load_renderer_identity()
        if declared_renderer != current_renderer:
            raise ContractError(
                ErrorCode.HASH_INTEGRITY_MISMATCH,
                "cached renderer identity differs from the installed runtime",
            )
        self._renderer = current_renderer

    def verify(
        self,
        entry: Path,
        *,
        request: Mapping[str, Any],
        renderer: Mapping[str, Any],
    ) -> MaterializedImage:
        """Verify one sealed entry without decoding or re-encoding its PNG."""

        parsed_request = _image_request_from_dict(dict(request))
        declared_renderer = _renderer_identity_from_dict(dict(renderer))
        if declared_renderer != self._renderer:
            raise ContractError(
                ErrorCode.HASH_INTEGRITY_MISMATCH,
                "cached renderer identity differs within sealed evidence",
            )
        key = image_cache_key(parsed_request, self._renderer)
        if entry.name != key.sha256.removeprefix("sha256:"):
            raise ContractError(
                ErrorCode.HASH_INTEGRITY_MISMATCH,
                "cache entry path does not match its content-addressed key",
            )
        return _verify_cache_entry(
            entry,
            parsed_request,
            key,
            self._renderer,
            None,
            reused=True,
            verify_pixels=False,
        )


def verify_sealed_image_cache_entry(
    entry: Path,
    *,
    request: Mapping[str, Any],
    renderer: Mapping[str, Any],
) -> MaterializedImage:
    """One-shot fast verification of an entry bound into sealed evidence."""

    return SealedImageCacheVerifier(renderer).verify(
        entry,
        request=request,
        renderer=renderer,
    )


def verify_image_cache_entry(
    entry: Path,
    *,
    request: Mapping[str, Any],
    renderer: Mapping[str, Any],
) -> MaterializedImage:
    """Strongly re-verify one published cache entry, including pixels.

    Cache creation and reuse call this equivalent boundary. Higher-level
    sealed workflow reloads use :class:`SealedImageCacheVerifier` only after
    the strong result and its exact PNG SHA-256 have been bound into the
    immutable overlay evidence.
    """

    parsed_request = _image_request_from_dict(dict(request))
    declared_renderer = _renderer_identity_from_dict(dict(renderer))
    current_renderer = _load_renderer_identity()
    if declared_renderer != current_renderer:
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "cached renderer identity differs from the installed runtime",
        )
    key = image_cache_key(parsed_request, current_renderer)
    if entry.name != key.sha256.removeprefix("sha256:"):
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "cache entry path does not match its content-addressed key",
        )
    image_module = _load_pillow_image_module(current_renderer)
    return _verify_cache_entry(
        entry,
        parsed_request,
        key,
        current_renderer,
        image_module,
        reused=True,
    )


def materialize_image(
    request: ImageRequest,
    *,
    source_root: Path,
    cache_root: Path,
) -> MaterializedImage:
    """Materialize a PDF request to an atomic, content-addressed PNG entry.

    The source is re-read and re-hashed before any cache access.  Existing
    entries are verified exactly and are never overwritten.  The function
    writes only the caller-selected cache root and owned stages below it.
    """

    if request.source_format != "pdf":
        raise ContractError(
            ErrorCode.BACKEND_CAPABILITY_MISSING,
            "SVG/EPS and raster materialization are intentionally unsupported",
        )
    source_path = resolve_within(source_root, request.source_path)
    source_bytes = read_stable_bytes(source_path, max_bytes=request.quality.max_source_bytes)
    source_digest = digest_bytes(source_bytes)
    if (
        source_digest.sha256 != request.source_sha256
        or source_digest.size_bytes != request.source_size_bytes
    ):
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "image source changed after planning")

    renderer = _load_renderer_identity()
    image_module = _load_pillow_image_module(renderer)
    key = image_cache_key(request, renderer)
    cache = _prepare_cache_root(source_root, cache_root)
    entry_name = key.sha256.removeprefix("sha256:")
    entry = cache / entry_name
    if entry.exists() or entry.is_symlink():
        return _verify_cache_entry(
            entry,
            request,
            key,
            renderer,
            image_module,
            reused=True,
        )

    png_bytes, width_px, height_px, pixel_sha256, effective_dpi = _run_pdf_worker(
        source_bytes,
        request,
        renderer,
        image_module,
        cache,
    )
    manifest = _cache_manifest(
        request,
        key,
        png_bytes=png_bytes,
        pixel_sha256=pixel_sha256,
        width_px=width_px,
        height_px=height_px,
        effective_dpi=effective_dpi,
    )
    # Keep the owned stage name short enough for deeply nested Windows workflow
    # roots; the final cache entry still carries the full content-addressed key.
    prefix = ".stage-"
    try:
        stage = Path(tempfile.mkdtemp(prefix=prefix, dir=cache))
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "cache stage cannot be created") from exc
    try:
        _write_exclusive(stage / _CACHE_PNG_NAME, png_bytes)
        _write_exclusive(stage / _CACHE_MANIFEST_NAME, canonical_json(manifest) + b"\n")
        try:
            publish_new_directory(stage, entry)
        except OSError as exc:
            if entry.exists() and entry.is_dir():
                _remove_owned_stage(stage, cache, prefix)
                return _verify_cache_entry(
                    entry,
                    request,
                    key,
                    renderer,
                    image_module,
                    reused=True,
                )
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT, "cache entry publication failed"
            ) from exc
        return _verify_cache_entry(
            entry,
            request,
            key,
            renderer,
            image_module,
            reused=False,
        )
    finally:
        if stage.exists():
            _remove_owned_stage(stage, cache, prefix)


__all__ = [
    "DEFAULT_GRAPHICS_EXTENSIONS",
    "REVIEW_QUALITY",
    "GraphicOption",
    "GraphicsDiagnostic",
    "GraphicsDirective",
    "GraphicsScan",
    "ImageCacheKey",
    "ImageOperation",
    "ImagePlan",
    "ImageRequest",
    "IncludeGraphics",
    "MaterializedImage",
    "QualityProfile",
    "RendererIdentity",
    "ResolvedGraphic",
    "SealedImageCacheVerifier",
    "image_cache_key",
    "materialize_image",
    "plan_image_request",
    "resolve_graphic",
    "scan_graphics_source",
    "scan_graphics_text",
    "verify_image_cache_entry",
    "verify_sealed_image_cache_entry",
]
