"""Auditable PDF-image overlay built from an immutable LaTeX snapshot.

The overlay is a derived working tree.  Source files are copied byte-for-byte,
PDF ``includegraphics`` occurrences are materialized through
``image_materializer``, and only the copied TeX files are rewritten.  The
original discovery and every original file digest are rechecked before the
derived tree is atomically published.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

from latex_word_review.atomic_publish import publish_new_directory
from latex_word_review.canonical import canonical_json, sha256_canonical
from latex_word_review.discovery import ProjectDiscovery, discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import FileDigest, digest_bytes, read_stable_bytes
from latex_word_review.ids import derive_artifact_id
from latex_word_review.image_materializer import (
    REVIEW_QUALITY,
    GraphicOption,
    GraphicsDiagnostic,
    ImageRequest,
    IncludeGraphics,
    MaterializedImage,
    QualityProfile,
    materialize_image,
    plan_image_request,
    resolve_graphic,
    scan_graphics_text,
)
from latex_word_review.paths import ensure_disjoint_roots, resolve_within, validate_relative_path
from latex_word_review.tex2word_compat import (
    normalize_tex2word_manual_figure_minipage_body,
    rewrite_tex2word_front_matter,
    rewrite_tex2word_layout_controls,
    rewrite_tex2word_subcaptionboxes,
)

IMAGE_OVERLAY_MANIFEST: Final[str] = "image-overlay-manifest.json"
IMAGE_OVERLAY_ROOT: Final[str] = "lwr-images"
IMAGE_OVERLAY_FORMAT: Final[str] = "latex-word-review-image-overlay-v1"
TEX2WORD_COMPATIBILITY_PROFILE: Final[str] = "tex2word-1.0.5-review-compat-v5"
_RASTER_FORMATS: Final[frozenset[str]] = frozenset({"png", "jpg", "jpeg"})
_LAYOUT_OPTIONS: Final[frozenset[str]] = frozenset(
    {"width", "height", "totalheight", "scale", "keepaspectratio"}
)
_ENVIRONMENT_RE: Final[re.Pattern[str]] = re.compile(r"\\(begin|end)\s*\{([A-Za-z*@]+)\}")
_INCLUDE_GRAPHICS_RE: Final[re.Pattern[str]] = re.compile(r"\\includegraphics\b")
_CAPTION_RE: Final[re.Pattern[str]] = re.compile(r"\\caption\b")
_LABEL_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"\\label\s*\{([^{}]+)\}")
_LABEL_COMMAND_RE: Final[re.Pattern[str]] = re.compile(r"\\label\b")
_SUBFIGURE_CONSTRUCT_RE: Final[re.Pattern[str]] = re.compile(
    r"\\begin\s*\{subfigure\*?\}|\\subfigure\b|\\subfloat\b"
)
_OUTER_FIGURE_LAYOUT_RE: Final[re.Pattern[str]] = re.compile(
    r"\\(?:centering|hfill|quad|qquad)\b|\\hspace\*?\s*\{[^{}\r\n]*\}"
)
_MAX_IMAGE_MATERIALIZATION_WORKERS: Final[int] = 2


@dataclass(frozen=True, slots=True)
class ImageOverlayDiagnostic:
    """One path-safe reason why an image occurrence remains manual."""

    code: str
    message: str
    source_path: str
    line: int
    column: int

    def __post_init__(self) -> None:
        validate_relative_path(self.source_path)
        if not self.code or len(self.code) > 96:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "overlay diagnostic code is invalid")
        if not self.message or len(self.message) > 1024:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "overlay diagnostic message is invalid",
            )
        if self.line < 1 or self.column < 1:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "overlay diagnostic location is invalid",
            )

    @property
    def diagnostic_id(self) -> str:
        digest = sha256_canonical(
            {
                "code": self.code,
                "message": self.message,
                "source_path": self.source_path,
                "line": self.line,
                "column": self.column,
            }
        )
        return f"image_diag_{digest.removeprefix('sha256:')}"

    def as_dict(self) -> dict[str, str | int]:
        return {
            "diagnostic_id": self.diagnostic_id,
            "code": self.code,
            "message": self.message,
            "source_path": self.source_path,
            "line": self.line,
            "column": self.column,
            "disposition": "manual",
        }


@dataclass(frozen=True, slots=True)
class ImageOverlayResult:
    """Published overlay plus its original/derived discovery bindings."""

    derived_root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest_size_bytes: int
    original_source_tree_sha256: str
    discovery: ProjectDiscovery
    source_image_instances: int
    materialized_pdf_instances: int
    passthrough_raster_instances: int
    diagnostics: tuple[ImageOverlayDiagnostic, ...]
    compatibility_profile: str | None
    compatibility_transformations: int

    @property
    def ready(self) -> bool:
        return not self.diagnostics

    def as_dict(self) -> dict[str, object]:
        return {
            "manifest_path": IMAGE_OVERLAY_MANIFEST,
            "manifest_sha256": self.manifest_sha256,
            "manifest_size_bytes": self.manifest_size_bytes,
            "original_source_tree_sha256": self.original_source_tree_sha256,
            "derived_source_tree_sha256": self.discovery.source_tree_sha256,
            "source_image_instances": self.source_image_instances,
            "materialized_pdf_instances": self.materialized_pdf_instances,
            "passthrough_raster_instances": self.passthrough_raster_instances,
            "compatibility_profile": self.compatibility_profile,
            "compatibility_transformations": self.compatibility_transformations,
            "ready": self.ready,
            "diagnostic_ids": [item.diagnostic_id for item in self.diagnostics],
        }

    def as_contract(
        self,
        *,
        manifest_artifact_path: str,
        confidentiality: Literal["public_fixture", "local_private", "derived_private"],
    ) -> dict[str, object]:
        path = validate_relative_path(manifest_artifact_path)
        return {
            "status": "ready" if self.ready else "manual_required",
            "manifest": {
                "artifact_id": derive_artifact_id(self.manifest_sha256),
                "path": path,
                "path_base": "run_root",
                "role": "image_overlay_manifest",
                "media_type": "application/json",
                "size_bytes": self.manifest_size_bytes,
                "sha256": self.manifest_sha256,
                "immutable": True,
                "confidentiality": confidentiality,
            },
            "original_source_tree_sha256": self.original_source_tree_sha256,
            "derived_source_tree_sha256": self.discovery.source_tree_sha256,
            "source_image_instances": self.source_image_instances,
            "materialized_pdf_instances": self.materialized_pdf_instances,
            "passthrough_raster_instances": self.passthrough_raster_instances,
        }


def discard_image_overlay(result: ImageOverlayResult) -> None:
    """Remove one verified tool-owned overlay after a downstream export failure."""

    target = result.derived_root
    if result.manifest_path != target / IMAGE_OVERLAY_MANIFEST:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "image-overlay cleanup binding is invalid",
        )
    manifest_bytes = read_stable_bytes(
        result.manifest_path,
        max_bytes=max(1, result.manifest_size_bytes),
    )
    manifest_digest = digest_bytes(manifest_bytes)
    if (
        manifest_digest.sha256 != result.manifest_sha256
        or manifest_digest.size_bytes != result.manifest_size_bytes
    ):
        raise ContractError(
            ErrorCode.HASH_INTEGRITY_MISMATCH,
            "image-overlay manifest changed before cleanup",
        )
    _remove_owned_stage(target, target.parent, target.name)


def _same_discovery(left: ProjectDiscovery, right: ProjectDiscovery) -> bool:
    return (
        left.main_document == right.main_document
        and left.files == right.files
        and left.dependency_edges == right.dependency_edges
        and left.external_references == right.external_references
        and left.source_tree_sha256 == right.source_tree_sha256
        and left.profile_sha256 == right.profile_sha256
    )


def _write_exclusive(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "image overlay stage write failed",
        ) from exc


def _replace_stage_file(path: Path, data: bytes, stage_root: Path) -> None:
    try:
        resolved_parent = path.parent.resolve(strict=True)
        resolved_stage = stage_root.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "overlay stage disappeared") from exc
    if not resolved_parent.is_relative_to(resolved_stage):
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "overlay rewrite escaped its stage")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.rewrite-",
        dir=resolved_parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink()
        raise


def _copy_discovery(source_root: Path, stage: Path, discovery: ProjectDiscovery) -> None:
    for source_file in discovery.files:
        relative = validate_relative_path(source_file.path)
        folded = relative.casefold()
        if folded in (
            IMAGE_OVERLAY_MANIFEST.casefold(),
            IMAGE_OVERLAY_ROOT.casefold(),
        ) or folded.startswith(f"{IMAGE_OVERLAY_ROOT.casefold()}/"):
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "source project uses a reserved image-overlay path",
            )
        source = resolve_within(source_root, relative)
        data = read_stable_bytes(source, max_bytes=max(1, source_file.size_bytes))
        if digest_bytes(data) != FileDigest(source_file.size_bytes, source_file.sha256):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "source changed before image-overlay copying",
            )
        target = stage.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_exclusive(target, data)


def _make_stage_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in (root, *root.rglob("*")):
        try:
            if path.is_dir():
                path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            else:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            continue


def _remove_owned_stage(stage: Path, parent: Path, prefix: str) -> None:
    if not stage.exists():
        return
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_stage = stage.resolve(strict=False)
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "image overlay stage cannot be inspected",
        ) from exc
    junction_probe = getattr(stage, "is_junction", None)
    if (
        resolved_stage.parent != resolved_parent
        or not stage.name.startswith(prefix)
        or stage.is_symlink()
        or bool(junction_probe is not None and junction_probe())
    ):
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "refusing to remove an unowned image-overlay stage",
        )
    for child in stage.rglob("*"):
        child_junction_probe = getattr(child, "is_junction", None)
        if child.is_symlink() or bool(child_junction_probe is not None and child_junction_probe()):
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "image-overlay stage contains a link",
            )
    _make_stage_writable(stage)
    try:
        shutil.rmtree(stage)
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "image-overlay stage cleanup failed",
        ) from exc


def _scan_diagnostic(diagnostic: GraphicsDiagnostic) -> ImageOverlayDiagnostic:
    return ImageOverlayDiagnostic(
        code=diagnostic.code,
        message=diagnostic.message,
        source_path=diagnostic.source_path,
        line=diagnostic.line,
        column=diagnostic.column,
    )


def _contract_diagnostic(
    occurrence: IncludeGraphics,
    error: ContractError,
) -> ImageOverlayDiagnostic:
    return ImageOverlayDiagnostic(
        code=error.code.value,
        message=error.violation.message,
        source_path=occurrence.source_path,
        line=occurrence.line,
        column=occurrence.column,
    )


def _manual_diagnostic(
    occurrence: IncludeGraphics,
    *,
    code: str,
    message: str,
) -> ImageOverlayDiagnostic:
    return ImageOverlayDiagnostic(
        code=code,
        message=message,
        source_path=occurrence.source_path,
        line=occurrence.line,
        column=occurrence.column,
    )


def _has_unescaped_comment(command: str) -> bool:
    for index, character in enumerate(command):
        if character != "%":
            continue
        slash_count = 0
        probe = index - 1
        while probe >= 0 and command[probe] == "\\":
            slash_count += 1
            probe -= 1
        if slash_count % 2 == 0:
            return True
    return False


@dataclass(frozen=True, slots=True)
class _OpenEnvironment:
    name: str
    begin_start: int
    begin_name_start: int
    begin_name_end: int
    body_start: int
    parent: str | None


@dataclass(frozen=True, slots=True)
class _DirectMinipage:
    begin_start: int
    body_start: int
    end_start: int
    end_command_end: int
    line: int
    labels: tuple[str, ...]
    caption_mode: Literal["caption", "manual_figure_counter"]
    normalized_body: str | None


@dataclass(frozen=True, slots=True)
class _FigureMinipageSplitCandidate:
    name: str
    begin_start: int
    body_start: int
    end_start: int
    end_command_end: int
    line: int
    children: tuple[_DirectMinipage, ...]
    copy_centering: bool


@dataclass(frozen=True, slots=True)
class _PendingMaterialization:
    occurrence: IncludeGraphics
    entry: dict[str, object]
    passthrough_options: tuple[GraphicOption, ...]
    request: ImageRequest
    future: Future[MaterializedImage]


def _mask_tex_comments(text: str) -> str:
    """Mask TeX comments without changing character offsets."""

    characters = list(text)
    in_comment = False
    slash_count = 0
    for index, character in enumerate(characters):
        if in_comment:
            if character in "\r\n":
                in_comment = False
                slash_count = 0
            else:
                characters[index] = " "
            continue
        if character == "%" and slash_count % 2 == 0:
            characters[index] = " "
            in_comment = True
            slash_count = 0
            continue
        if character == "\\":
            slash_count += 1
        else:
            slash_count = 0
    return "".join(characters)


def _figure_body_start(scanned: str, start: int, limit: int) -> int | None:
    cursor = start
    while cursor < limit and scanned[cursor].isspace():
        cursor += 1
    if cursor >= limit or scanned[cursor] != "[":
        return start
    end = scanned.find("]", cursor + 1, limit)
    if end < 0 or "[" in scanned[cursor + 1 : end]:
        return None
    return end + 1


def _tex2word_minipage_candidates(
    text: str,
) -> tuple[_FigureMinipageSplitCandidate, ...]:
    """Find only complete multi-minipage figures that are safe to split.

    tex2word 1.0.5 drops figure-number bookmarks when an uncaptioned outer
    figure is converted through its subfigure path.  The source semantics of
    the supported shape are several independent minipage captions, so the
    derived overlay presents each minipage as its own consecutive figure.
    """

    scanned = _mask_tex_comments(text)
    stack: list[_OpenEnvironment] = []
    direct_children: dict[int, list[_DirectMinipage]] = {}
    candidates: list[_FigureMinipageSplitCandidate] = []
    for match in _ENVIRONMENT_RE.finditer(scanned):
        action = match.group(1)
        name = match.group(2)
        if action == "begin":
            stack.append(
                _OpenEnvironment(
                    name=name,
                    begin_start=match.start(),
                    begin_name_start=match.start(2),
                    begin_name_end=match.end(2),
                    body_start=match.end(),
                    parent=None if not stack else stack[-1].name,
                )
            )
            continue
        if not stack or stack[-1].name != name:
            # A cross-file or malformed environment is not locally provable.
            return ()
        opened = stack.pop()
        if opened.name == "minipage" and opened.parent in {"figure", "figure*"}:
            body = scanned[opened.body_start : match.start()]
            body_text = text[opened.body_start : match.start()]
            labels = tuple(item.group(1) for item in _LABEL_VALUE_RE.finditer(body))
            caption_count = len(_CAPTION_RE.findall(body))
            manual = normalize_tex2word_manual_figure_minipage_body(body_text)
            if (
                len(_INCLUDE_GRAPHICS_RE.findall(body)) != 1
                or len(_LABEL_COMMAND_RE.findall(body)) != len(labels)
                or len(labels) > 1
                or _SUBFIGURE_CONSTRUCT_RE.search(body) is not None
                or _ENVIRONMENT_RE.search(body) is not None
                or not stack
            ):
                continue
            if caption_count == 1:
                caption_mode: Literal["caption", "manual_figure_counter"] = "caption"
                normalized_body = None
            elif caption_count == 0 and manual is not None and labels == (manual[1],):
                caption_mode = "manual_figure_counter"
                normalized_body = manual[0]
            else:
                continue
            direct_children.setdefault(stack[-1].begin_start, []).append(
                _DirectMinipage(
                    begin_start=opened.begin_start,
                    body_start=opened.body_start,
                    end_start=match.start(),
                    end_command_end=match.end(),
                    line=text.count("\n", 0, opened.begin_start) + 1,
                    labels=labels,
                    caption_mode=caption_mode,
                    normalized_body=normalized_body,
                )
            )
            continue
        if opened.name not in {"figure", "figure*"}:
            continue
        children = tuple(direct_children.pop(opened.begin_start, ()))
        if len(children) < 2:
            continue
        body_start = _figure_body_start(scanned, opened.body_start, match.start())
        if body_start is None:
            continue
        residual = list(scanned[body_start : match.start()])
        for child in children:
            if child.begin_start < body_start or child.end_command_end > match.start():
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "minipage split child escaped its outer figure",
                )
            relative_start = child.begin_start - body_start
            relative_end = child.end_command_end - body_start
            residual[relative_start:relative_end] = " " * (relative_end - relative_start)
        layout = "".join(residual)
        if _OUTER_FIGURE_LAYOUT_RE.sub(" ", layout).strip():
            continue
        candidates.append(
            _FigureMinipageSplitCandidate(
                name=opened.name,
                begin_start=opened.begin_start,
                body_start=body_start,
                end_start=match.start(),
                end_command_end=match.end(),
                line=text.count("\n", 0, opened.begin_start) + 1,
                children=children,
                copy_centering=re.search(r"\\centering\b", layout) is not None,
            )
        )
    if stack:
        return ()
    return tuple(candidates)


def _rewrite_tex2word_minipages(
    text: str,
    *,
    source_path: str,
) -> tuple[str, list[dict[str, object]]]:
    """Split only proven multi-minipage figures and return hash-bound evidence."""

    validate_relative_path(source_path)
    candidates = _tex2word_minipage_candidates(text)
    replacements: list[tuple[int, int, str, str]] = []
    evidence: list[dict[str, object]] = []
    for candidate in candidates:
        block = text[candidate.begin_start : candidate.end_command_end]
        begin_command = text[candidate.begin_start : candidate.body_start]
        end_command = text[candidate.end_start : candidate.end_command_end]
        derived_figures: list[str] = []
        for child in candidate.children:
            if child.normalized_body is None:
                child_block = text[child.begin_start : child.end_command_end]
            else:
                child_block = (
                    text[child.begin_start : child.body_start]
                    + child.normalized_body
                    + text[child.end_start : child.end_command_end]
                )
            centering = "\n  \\centering" if candidate.copy_centering else ""
            derived_figures.append(f"{begin_command}{centering}\n{child_block}\n{end_command}")
        normalized_block = "\n".join(derived_figures)
        original_sha256 = digest_bytes(block.encode("utf-8")).sha256
        derived_sha256 = digest_bytes(normalized_block.encode("utf-8")).sha256
        start_utf8 = len(text[: candidate.begin_start].encode("utf-8"))
        end_utf8 = start_utf8 + len(block.encode("utf-8"))
        labels = [label for child in candidate.children for label in child.labels]
        transformation_id = "compat_" + sha256_canonical(
            {
                "profile": TEX2WORD_COMPATIBILITY_PROFILE,
                "source_path": source_path,
                "start_utf8": start_utf8,
                "end_utf8": end_utf8,
                "original_sha256": original_sha256,
                "derived_sha256": derived_sha256,
            }
        ).removeprefix("sha256:")
        evidence.append(
            {
                "transformation_id": transformation_id,
                "kind": "figure_minipage_split",
                "source_path": source_path,
                "source_span": {
                    "start_char": candidate.begin_start,
                    "end_char": candidate.end_command_end,
                    "start_utf8": start_utf8,
                    "end_utf8": end_utf8,
                    "line": candidate.line,
                },
                "original_environment": candidate.name,
                "derived_environment": "figure_sequence",
                "direct_child_count": len(candidate.children),
                "includegraphics_count": len(candidate.children),
                "caption_count": len(candidate.children),
                "source_caption_count": sum(
                    1 for child in candidate.children if child.caption_mode == "caption"
                ),
                "manual_caption_normalizations": sum(
                    1
                    for child in candidate.children
                    if child.caption_mode == "manual_figure_counter"
                ),
                "label_count": len(labels),
                "labels": labels,
                "nested_subfigure_constructs": 0,
                "original_block_sha256": original_sha256,
                "derived_block_sha256": derived_sha256,
            }
        )
        replacements.append(
            (
                candidate.begin_start,
                candidate.end_command_end,
                block,
                normalized_block,
            )
        )

    result = text
    previous_start = len(text) + 1
    for start, end, expected, replacement in sorted(replacements, reverse=True):
        if end > previous_start:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "tex2word compatibility rewrite spans overlap",
            )
        if result[start:end] != expected:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "tex2word compatibility figure changed before rewrite",
            )
        result = result[:start] + replacement + result[end:]
        previous_start = start
    return result, evidence


def _byte_offsets(text: str, occurrences: tuple[IncludeGraphics, ...]) -> dict[int, int]:
    requested = sorted(
        {boundary for item in occurrences for boundary in (item.start_char, item.end_char)}
    )
    result: dict[int, int] = {}
    previous_char = 0
    previous_byte = 0
    for boundary in requested:
        if not previous_char <= boundary <= len(text):
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "image source span is invalid")
        previous_byte += len(text[previous_char:boundary].encode("utf-8"))
        result[boundary] = previous_byte
        previous_char = boundary
    return result


def _passthrough_options(options: tuple[GraphicOption, ...]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for option in options:
        result.append(
            {
                "key": option.key,
                "value": option.value,
                "ordinal": option.ordinal,
                "source_text": option.source_text,
            }
        )
    return result


def _derived_command(options: tuple[GraphicOption, ...], target: str) -> str:
    validate_relative_path(target)
    option_text = ",".join(option.source_text for option in options)
    option_group = f"[{option_text}]" if option_text else ""
    return f"\\includegraphics{option_group}{{{target}}}"


def _base_entry(
    occurrence: IncludeGraphics,
    *,
    byte_offsets: dict[int, int],
) -> dict[str, object]:
    original_bytes = occurrence.source_text.encode("utf-8")
    return {
        "occurrence_id": "image_"
        + sha256_canonical(
            {
                "source_path": occurrence.source_path,
                "start_utf8": byte_offsets[occurrence.start_char],
                "end_utf8": byte_offsets[occurrence.end_char],
                "original_command_sha256": digest_bytes(original_bytes).sha256,
            }
        ).removeprefix("sha256:"),
        "source_path": occurrence.source_path,
        "source_span": {
            "start_char": occurrence.start_char,
            "end_char": occurrence.end_char,
            "start_utf8": byte_offsets[occurrence.start_char],
            "end_utf8": byte_offsets[occurrence.end_char],
            "line": occurrence.line,
            "column": occurrence.column,
        },
        "original_command": occurrence.source_text,
        "original_command_sha256": digest_bytes(original_bytes).sha256,
        "original_target": occurrence.target,
        "original_options": _passthrough_options(occurrence.options),
    }


def _materialized_record(materialized: MaterializedImage) -> dict[str, object]:
    return {
        "cache_key_sha256": materialized.cache_key_sha256,
        "request_sha256": materialized.request_sha256,
        "png_path": f"{IMAGE_OVERLAY_ROOT}/{materialized.png_path}",
        "cache_manifest_path": f"{IMAGE_OVERLAY_ROOT}/{materialized.manifest_path}",
        "png_sha256": materialized.png_sha256,
        "pixel_sha256": materialized.pixel_sha256,
        "width_px": materialized.width_px,
        "height_px": materialized.height_px,
        "dpi": materialized.dpi,
        "renderer": materialized.renderer.as_dict(),
        "reused": materialized.reused,
    }


def _rewrite_text(
    text: str,
    replacements: list[tuple[IncludeGraphics, str]],
) -> str:
    result = text
    previous_start = len(text) + 1
    for occurrence, replacement in sorted(
        replacements,
        key=lambda item: item[0].start_char,
        reverse=True,
    ):
        if occurrence.end_char > previous_start:
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "image rewrite spans overlap")
        if result[occurrence.start_char : occurrence.end_char] != occurrence.source_text:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "image command changed before rewrite",
            )
        result = result[: occurrence.start_char] + replacement + result[occurrence.end_char :]
        previous_start = occurrence.start_char
    return result


def build_image_overlay(
    source_root: Path,
    destination: Path,
    discovery: ProjectDiscovery,
    *,
    quality: QualityProfile = REVIEW_QUALITY,
    compatibility_profile: str | None = None,
) -> ImageOverlayResult:
    """Build and atomically publish a derived image overlay.

    Manual diagnostics are recorded in the published manifest and returned to
    the caller.  The caller must check ``result.ready`` before invoking a
    conversion backend.
    """

    if compatibility_profile not in {None, TEX2WORD_COMPATIBILITY_PROFILE}:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "image-overlay compatibility profile is unsupported",
        )
    source, target = ensure_disjoint_roots(source_root, destination)
    if target.exists() or target.is_symlink():
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "image-overlay destination already exists",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    current = discover_project(source, main_document=discovery.main_document)
    if not _same_discovery(current, discovery):
        raise ContractError(
            ErrorCode.HASH_SOURCE_MISMATCH,
            "source discovery binding changed before image-overlay creation",
        )
    prefix = ".lwr-img-"
    try:
        stage = Path(tempfile.mkdtemp(prefix=prefix, dir=target.parent))
    except OSError as exc:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "image-overlay stage cannot be created",
        ) from exc

    entries: list[dict[str, object]] = []
    diagnostics: list[ImageOverlayDiagnostic] = []
    source_image_instances = 0
    materialized_pdf_instances = 0
    passthrough_raster_instances = 0
    compatibility_transformations: list[dict[str, object]] = []
    materialization_futures: dict[str, Future[MaterializedImage]] = {}
    materialization_use_counts: dict[str, int] = {}
    materializer: ThreadPoolExecutor | None = None
    published = False
    try:
        materializer = ThreadPoolExecutor(max_workers=_MAX_IMAGE_MATERIALIZATION_WORKERS)
        _copy_discovery(source, stage, discovery)
        for source_file in discovery.files:
            if not source_file.path.casefold().endswith(".tex"):
                continue
            source_path = resolve_within(source, source_file.path)
            source_bytes = read_stable_bytes(
                source_path,
                max_bytes=max(1, source_file.size_bytes),
            )
            if digest_bytes(source_bytes) != FileDigest(
                source_file.size_bytes,
                source_file.sha256,
            ):
                raise ContractError(
                    ErrorCode.HASH_SOURCE_MISMATCH,
                    "TeX source changed before image-overlay scanning",
                )
            try:
                text = source_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContractError(ErrorCode.SCHEMA_INVALID, "LaTeX source must be UTF-8") from exc
            scan = scan_graphics_text(text, source_path=source_file.path, quality=quality)
            scan_diagnostics = [_scan_diagnostic(item) for item in scan.diagnostics]
            diagnostics.extend(scan_diagnostics)
            byte_offsets = _byte_offsets(text, scan.includes)
            replacements: list[tuple[IncludeGraphics, str]] = []
            pending_materializations: list[_PendingMaterialization] = []

            for occurrence in scan.includes:
                source_image_instances += 1
                entry = _base_entry(occurrence, byte_offsets=byte_offsets)
                occurrence_diagnostics: list[ImageOverlayDiagnostic] = []
                if _has_unescaped_comment(occurrence.source_text):
                    occurrence_diagnostics.append(
                        _manual_diagnostic(
                            occurrence,
                            code="IMAGE_PARSE_MALFORMED",
                            message=(
                                "comments inside includegraphics cannot be rewritten "
                                "without changing source trivia"
                            ),
                        )
                    )
                try:
                    resolved = resolve_graphic(source, occurrence, quality=quality)
                    entry["resolved_source"] = {
                        "path": resolved.source_path,
                        "format": resolved.source_format,
                        "size_bytes": resolved.size_bytes,
                        "sha256": resolved.sha256,
                    }
                    plan = plan_image_request(source, occurrence, quality=quality)
                except ContractError as error:
                    occurrence_diagnostics.append(_contract_diagnostic(occurrence, error))
                    plan = None
                    resolved = None

                if plan is not None:
                    plan_diagnostics = plan.blocking_diagnostics
                    raster_format_only = (
                        resolved is not None
                        and resolved.source_format in _RASTER_FORMATS
                        and plan_diagnostics
                        and all(
                            item.code == "IMAGE_UNSUPPORTED_FORMAT" for item in plan_diagnostics
                        )
                    )
                    if not raster_format_only:
                        occurrence_diagnostics.extend(
                            _scan_diagnostic(item) for item in plan_diagnostics
                        )

                if occurrence_diagnostics:
                    diagnostics.extend(occurrence_diagnostics)
                    entry.update(
                        status="manual_required",
                        passthrough_options=(
                            [] if plan is None else _passthrough_options(plan.passthrough_options)
                        ),
                        request=(
                            None
                            if plan is None or plan.request is None
                            else plan.request.as_cache_input()
                        ),
                        request_sha256=(
                            None
                            if plan is None or plan.request is None
                            else plan.request.request_sha256
                        ),
                        derived_command=None,
                        materialized=None,
                        diagnostics=[item.as_dict() for item in occurrence_diagnostics],
                    )
                    entries.append(entry)
                    continue

                assert resolved is not None
                assert plan is not None
                if resolved.source_format in _RASTER_FORMATS:
                    renderer_options = {
                        option.key.casefold() for option in occurrence.options
                    } - _LAYOUT_OPTIONS
                    if renderer_options:
                        diagnostic = _manual_diagnostic(
                            occurrence,
                            code="IMAGE_UNSUPPORTED_OPTION",
                            message=(
                                "raster passthrough accepts layout options only; "
                                "pixel operations remain manual"
                            ),
                        )
                        diagnostics.append(diagnostic)
                        entry.update(
                            status="manual_required",
                            passthrough_options=_passthrough_options(plan.passthrough_options),
                            request=None,
                            request_sha256=None,
                            derived_command=None,
                            materialized=None,
                            diagnostics=[diagnostic.as_dict()],
                        )
                    else:
                        derived_command = _derived_command(
                            plan.passthrough_options,
                            resolved.source_path,
                        )
                        replacements.append((occurrence, derived_command))
                        passthrough_raster_instances += 1
                        entry.update(
                            status="passthrough_raster",
                            passthrough_options=_passthrough_options(plan.passthrough_options),
                            request=None,
                            request_sha256=None,
                            derived_command=derived_command,
                            derived_command_sha256=digest_bytes(
                                derived_command.encode("utf-8")
                            ).sha256,
                            materialized=None,
                            diagnostics=[],
                        )
                    entries.append(entry)
                    continue

                # Every non-PDF format produces a blocking plan diagnostic
                # above.  Reaching this point therefore proves a PDF request.
                assert resolved.source_format == "pdf"
                assert plan.request is not None
                request = plan.request
                future = materialization_futures.get(request.request_sha256)
                if future is None:
                    assert materializer is not None
                    future = materializer.submit(
                        materialize_image,
                        request,
                        source_root=source,
                        cache_root=stage / IMAGE_OVERLAY_ROOT,
                    )
                    materialization_futures[request.request_sha256] = future
                pending_materializations.append(
                    _PendingMaterialization(
                        occurrence=occurrence,
                        entry=entry,
                        passthrough_options=plan.passthrough_options,
                        request=request,
                        future=future,
                    )
                )
                # The mutable entry is finalized only after all requests from
                # this TeX file have been submitted.  Appending it now keeps
                # manifest order identical to source occurrence order even
                # when workers complete out of order.
                entries.append(entry)

            for pending in pending_materializations:
                try:
                    materialized = pending.future.result()
                except ContractError as error:
                    diagnostic = _contract_diagnostic(pending.occurrence, error)
                    diagnostics.append(diagnostic)
                    pending.entry.update(
                        status="manual_required",
                        passthrough_options=_passthrough_options(pending.passthrough_options),
                        request=pending.request.as_cache_input(),
                        request_sha256=pending.request.request_sha256,
                        derived_command=None,
                        materialized=None,
                        diagnostics=[diagnostic.as_dict()],
                    )
                    continue

                use_count = materialization_use_counts.get(
                    pending.request.request_sha256,
                    0,
                )
                materialization_use_counts[pending.request.request_sha256] = use_count + 1
                if use_count:
                    materialized = replace(materialized, reused=True)
                target_path = f"{IMAGE_OVERLAY_ROOT}/{materialized.png_path}"
                derived_command = _derived_command(
                    pending.passthrough_options,
                    target_path,
                )
                replacements.append((pending.occurrence, derived_command))
                materialized_pdf_instances += 1
                pending.entry.update(
                    status="materialized_pdf",
                    passthrough_options=_passthrough_options(pending.passthrough_options),
                    request=pending.request.as_cache_input(),
                    request_sha256=pending.request.request_sha256,
                    derived_command=derived_command,
                    derived_command_sha256=digest_bytes(derived_command.encode("utf-8")).sha256,
                    materialized=_materialized_record(materialized),
                    diagnostics=[],
                )

            rewritten = _rewrite_text(text, replacements)
            if compatibility_profile == TEX2WORD_COMPATIBILITY_PROFILE:
                rewritten, subcaptionbox_transformations = rewrite_tex2word_subcaptionboxes(
                    rewritten,
                    source_path=source_file.path,
                    profile=compatibility_profile,
                )
                compatibility_transformations.extend(subcaptionbox_transformations)
                rewritten, file_transformations = _rewrite_tex2word_minipages(
                    rewritten,
                    source_path=source_file.path,
                )
                compatibility_transformations.extend(file_transformations)
                rewritten, front_matter_transformations = rewrite_tex2word_front_matter(
                    rewritten,
                    source_path=source_file.path,
                    profile=compatibility_profile,
                )
                compatibility_transformations.extend(front_matter_transformations)
                rewritten, layout_control_transformations = rewrite_tex2word_layout_controls(
                    rewritten,
                    source_path=source_file.path,
                    profile=compatibility_profile,
                )
                compatibility_transformations.extend(layout_control_transformations)
            if rewritten != text:
                _replace_stage_file(
                    resolve_within(stage, source_file.path),
                    rewritten.encode("utf-8"),
                    stage,
                )

        after = discover_project(source, main_document=discovery.main_document)
        if not _same_discovery(after, discovery):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "original source changed while building the image overlay",
            )
        derived_discovery = discover_project(stage, main_document=discovery.main_document)
        manifest = {
            "format": IMAGE_OVERLAY_FORMAT,
            "status": "ready" if not diagnostics else "manual_required",
            "source": {
                "main_document": discovery.main_document,
                "source_tree_sha256": discovery.source_tree_sha256,
                "profile_sha256": discovery.profile_sha256,
            },
            "derived": {
                "main_document": derived_discovery.main_document,
                "source_tree_sha256": derived_discovery.source_tree_sha256,
                "profile_sha256": derived_discovery.profile_sha256,
                "image_root": IMAGE_OVERLAY_ROOT,
            },
            "quality": quality.as_dict(),
            "compatibility": {
                "profile": compatibility_profile,
                "transformation_count": len(compatibility_transformations),
                "transformations": compatibility_transformations,
            },
            "counts": {
                "source_image_instances": source_image_instances,
                "materialized_pdf_instances": materialized_pdf_instances,
                "passthrough_raster_instances": passthrough_raster_instances,
                "manual_instances": sum(
                    1 for entry in entries if entry["status"] == "manual_required"
                ),
            },
            "entries": entries,
            "diagnostics": [item.as_dict() for item in diagnostics],
        }
        manifest_bytes = canonical_json(manifest) + b"\n"
        _write_exclusive(stage / IMAGE_OVERLAY_MANIFEST, manifest_bytes)
        manifest_sha256 = digest_bytes(manifest_bytes).sha256
        try:
            publish_new_directory(stage, target)
        except FileExistsError as exc:
            raise ContractError(
                ErrorCode.BACKEND_FAILED,
                "image-overlay destination appeared during publication",
            ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "image-overlay publication failed",
            ) from exc
        try:
            final_source = discover_project(source, main_document=discovery.main_document)
            if not _same_discovery(final_source, discovery):
                raise ContractError(
                    ErrorCode.HASH_SOURCE_MISMATCH,
                    "original source changed during image-overlay publication",
                )
            published_manifest = read_stable_bytes(
                target / IMAGE_OVERLAY_MANIFEST,
                max_bytes=max(1, len(manifest_bytes)),
            )
            if published_manifest != manifest_bytes:
                raise ContractError(
                    ErrorCode.HASH_INTEGRITY_MISMATCH,
                    "published image-overlay manifest changed",
                )
        except Exception:
            _remove_owned_stage(target, target.parent, target.name)
            raise
        published = True
        return ImageOverlayResult(
            derived_root=target,
            manifest_path=target / IMAGE_OVERLAY_MANIFEST,
            manifest_sha256=manifest_sha256,
            manifest_size_bytes=len(manifest_bytes),
            original_source_tree_sha256=discovery.source_tree_sha256,
            discovery=derived_discovery,
            source_image_instances=source_image_instances,
            materialized_pdf_instances=materialized_pdf_instances,
            passthrough_raster_instances=passthrough_raster_instances,
            diagnostics=tuple(diagnostics),
            compatibility_profile=compatibility_profile,
            compatibility_transformations=len(compatibility_transformations),
        )
    finally:
        # No stage cleanup may race a renderer.  On an unexpected exit, queued
        # work is cancelled and any already-running contained workers are
        # reaped before the owned stage is inspected or removed.
        if materializer is not None:
            materializer.shutdown(wait=True, cancel_futures=True)
        if not published and stage.exists():
            _remove_owned_stage(stage, target.parent, prefix)


__all__ = [
    "IMAGE_OVERLAY_FORMAT",
    "IMAGE_OVERLAY_MANIFEST",
    "IMAGE_OVERLAY_ROOT",
    "TEX2WORD_COMPATIBILITY_PROFILE",
    "ImageOverlayDiagnostic",
    "ImageOverlayResult",
    "build_image_overlay",
    "discard_image_overlay",
]
