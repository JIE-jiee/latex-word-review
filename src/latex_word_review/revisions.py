"""Canonical Word revision evidence extraction and ChangeSet normalization.

This module consumes only :mod:`latex_word_review.docx_reader` output.  It does
not accept revisions, save DOCX files, or infer missing authors and timestamps.
Fixture-specific expected JSON is never imported by production code.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.__about__ import __version__
from latex_word_review.canonical import sha256_bytes, sha256_canonical
from latex_word_review.contracts import make_envelope
from latex_word_review.docx_reader import DocxPackage, DocxReadLimits, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.ids import (
    derive_artifact_id,
    derive_change_id,
    derive_diagnostic_id,
    derive_raw_event_id,
    stable_id,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
_UNIT_ID = re.compile(r"^unit_[a-z0-9]{26,64}$")
_RAW_KIND_BY_LOCAL = {
    "ins": "insert",
    "del": "delete",
    "moveFrom": "move_from",
    "moveTo": "move_to",
    "rPrChange": "run_format",
    "pPrChange": "paragraph_format",
    "tblPrChange": "table_format",
}
_UNSUPPORTED_REVISION_LOCALS = {
    "sectPrChange",
    "trPrChange",
    "tcPrChange",
    "numberingChange",
    "cellIns",
    "cellDel",
    "cellMerge",
    "customXmlInsRangeStart",
    "customXmlInsRangeEnd",
    "customXmlDelRangeStart",
    "customXmlDelRangeEnd",
}
_MOVE_START = {
    "moveFromRangeStart": "move_from",
    "moveToRangeStart": "move_to",
}
_MOVE_END = {
    "moveFromRangeEnd": "move_from",
    "moveToRangeEnd": "move_to",
}
_COMBINE_PROFILE_VERSION = "revision-combine-v1"
_READER_INTERFACE_VERSION = "revision-reader-v1alpha1"


@dataclass(frozen=True, slots=True)
class BookmarkBinding:
    """A SourceMap-provided binding for one native DOCX bookmark."""

    unit_id: str
    source_location: Mapping[str, Any]

    def __post_init__(self) -> None:
        if _UNIT_ID.fullmatch(self.unit_id) is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "bookmark binding has invalid unit_id")
        object.__setattr__(
            self,
            "source_location",
            MappingProxyType(copy.deepcopy(dict(self.source_location))),
        )


@dataclass(frozen=True, slots=True)
class ExtractedRevision:
    """One raw contract event plus non-Schema native matching evidence."""

    raw_event: Mapping[str, Any]
    bookmark_name: str | None
    parent_node_ordinal: int | None
    sibling_index: int | None
    move_name: str | None
    anchor_text: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_event", MappingProxyType(copy.deepcopy(dict(self.raw_event))))


@dataclass(frozen=True, slots=True)
class RevisionExtraction:
    """Deterministic raw evidence extracted from one immutable DOCX."""

    package: DocxPackage
    events: tuple[ExtractedRevision, ...]
    diagnostics: tuple[Mapping[str, Any], ...]
    configuration_sha256: str


@dataclass(frozen=True, slots=True)
class NormalizedChange:
    """One approval-facing change plus the preserved native bookmark name."""

    change: Mapping[str, Any]
    bookmark_name: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "change", MappingProxyType(copy.deepcopy(dict(self.change))))


@dataclass(frozen=True, slots=True)
class ChangeNormalization:
    changes: tuple[NormalizedChange, ...]
    diagnostics: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _Issue:
    code: str
    severity: str
    message: str
    remediation: str | None = None


@dataclass(slots=True)
class _MoveRange:
    part_uri: str
    direction: str
    native_id: str | None
    name: str | None
    author: str | None
    timestamp: str | None
    start_seen: bool = True
    end_seen: bool = False
    pair_native_id: str | None = None


@dataclass(slots=True)
class _CommentAnchor:
    native_id: str
    part_uri: str | None = None
    start_count: int = 0
    end_count: int = 0
    reference_count: int = 0
    text_chunks: list[str] = field(default_factory=list)
    bookmark_name: str | None = None

    @property
    def anchor_text(self) -> str:
        return "".join(self.text_chunks)


@dataclass(slots=True)
class _EventDraft:
    part_uri: str
    node: etree._Element
    node_ordinal: int
    document_order: int
    kind: str
    native_kind: str | None
    bookmark_name: str | None
    parent_node_ordinal: int | None
    sibling_index: int | None
    move_range: _MoveRange | None = None
    comment_anchor: _CommentAnchor | None = None
    issues: list[_Issue] = field(default_factory=list)


def _attr(node: etree._Element, name: str) -> str | None:
    return cast("str | None", node.get(f"{W}{name}"))


def _local_name(node: etree._Element) -> str:
    return cast("str", etree.QName(node).localname)


def _text_token(node: etree._Element, *, include_deleted: bool) -> str | None:
    local = _local_name(node)
    allowed = {"t", "tab", "br", "cr", "noBreakHyphen", "softHyphen"}
    if include_deleted:
        allowed.update({"delText", "delInstrText"})
    if local not in allowed:
        return None
    if local == "tab":
        return "\t"
    if local in {"br", "cr"}:
        return "\n"
    if local == "noBreakHyphen":
        return "‑"
    if local == "softHyphen":
        return "\u00ad"
    return node.text or ""


def _element_text(node: etree._Element, *, deleted: bool = False) -> str:
    chunks: list[str] = []
    for child in node.iter():
        token = _text_token(child, include_deleted=deleted)
        if token is None:
            continue
        if deleted and _local_name(child) == "t":
            continue
        if not deleted and _local_name(child) in {"delText", "delInstrText"}:
            continue
        chunks.append(token)
    return "".join(chunks)


def _canonical_fragment(node: etree._Element) -> bytes:
    try:
        return cast(
            "bytes",
            etree.tostring(node, method="c14n", exclusive=True, with_comments=False),
        )
    except (ValueError, etree.C14NError) as exc:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "revision XML fragment cannot be canonicalized",
        ) from exc


def _on_off(rpr: etree._Element | None, local: str) -> bool:
    if rpr is None:
        return False
    node = rpr.find(f"{W}{local}")
    if node is None:
        return False
    value = _attr(node, "val")
    return value is None or value.casefold() not in {"0", "false", "off", "no"}


def _value(rpr: etree._Element | None, local: str, attribute: str = "val") -> str | None:
    if rpr is None:
        return None
    node = rpr.find(f"{W}{local}")
    return None if node is None else _attr(node, attribute)


def _run_properties(rpr: etree._Element | None) -> dict[str, Any]:
    fonts = None if rpr is None else rpr.find(f"{W}rFonts")
    return {
        "bold": _on_off(rpr, "b"),
        "italic": _on_off(rpr, "i"),
        "strike": _on_off(rpr, "strike"),
        "hidden": _on_off(rpr, "vanish"),
        "underline": _value(rpr, "u"),
        "color": _value(rpr, "color"),
        "highlight": _value(rpr, "highlight"),
        "font_ascii": None if fonts is None else _attr(fonts, "ascii"),
        "font_east_asia": None if fonts is None else _attr(fonts, "eastAsia"),
        "size_half_points": _value(rpr, "sz"),
        "vertical_alignment": _value(rpr, "vertAlign"),
    }


def _property_fragment(node: etree._Element | None) -> dict[str, str | None]:
    if node is None:
        return {"fragment_sha256": None}
    clone = copy.deepcopy(node)
    for child in list(clone):
        if _local_name(child).endswith("PrChange"):
            clone.remove(child)
    return {"fragment_sha256": sha256_bytes(_canonical_fragment(clone))}


def _timestamp(node: etree._Element, issues: list[_Issue]) -> str | None:
    raw = _attr(node, "date")
    if raw:
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return raw
    issues.append(
        _Issue(
            ErrorCode.REVISION_TIMESTAMP_MISSING.value,
            "warning",
            "revision timestamp is missing, invalid, or lacks a UTC offset",
        )
    )
    return None


def _author(node: etree._Element, issues: list[_Issue]) -> str | None:
    value = _attr(node, "author")
    if value:
        return value
    issues.append(
        _Issue(
            ErrorCode.REVISION_AUTHOR_MISSING.value,
            "warning",
            "revision author is missing",
        )
    )
    return None


def _diagnostic(issue: _Issue, fingerprint: str) -> dict[str, Any]:
    return {
        "diagnostic_id": derive_diagnostic_id(
            code=issue.code,
            phase="ingest",
            location_or_evidence_fingerprint=fingerprint,
        ),
        "code": issue.code,
        "severity": issue.severity,
        "phase": "ingest",
        "message": issue.message,
        "recoverable": issue.severity in {"info", "warning"},
        "unit_id": None,
        "change_id": None,
        "source_location": None,
        "evidence": None,
        "remediation": issue.remediation,
    }


def _current_bookmark(active: Mapping[str, str]) -> str | None:
    for name in reversed(tuple(active.values())):
        if name and not name.startswith("_"):
            return name
    return None


def _revision_kind(node: etree._Element) -> tuple[str, str | None] | None:
    if etree.QName(node).namespace != W_NS:
        return None
    local = _local_name(node)
    if local in _RAW_KIND_BY_LOCAL:
        return _RAW_KIND_BY_LOCAL[local], None
    if local in _UNSUPPORTED_REVISION_LOCALS or local.endswith("PrChange"):
        return "unknown", f"w:{local}"
    return None


def _scan_story_part(
    package: DocxPackage,
    part_uri: str,
    drafts: list[_EventDraft],
    comment_anchors: dict[str, _CommentAnchor],
    move_ranges: list[_MoveRange],
    *,
    include_comments: bool,
) -> None:
    root = package.xml_root(part_uri)
    nodes = list(root.iter())
    ordinals = {node: index for index, node in enumerate(nodes)}
    parents = {child: parent for parent in nodes for child in parent}
    active_bookmarks: dict[str, str] = {}
    active_comments: dict[str, _CommentAnchor] = {}
    active_moves: dict[str, dict[str, _MoveRange]] = {"move_from": {}, "move_to": {}}

    for node in nodes:
        local = _local_name(node)
        if node.tag == f"{W}bookmarkStart":
            native_id = _attr(node, "id")
            name = _attr(node, "name")
            if native_id and name:
                active_bookmarks[native_id] = name
            continue
        if node.tag == f"{W}bookmarkEnd":
            native_id = _attr(node, "id")
            if native_id:
                active_bookmarks.pop(native_id, None)
            continue

        if local in _MOVE_START and etree.QName(node).namespace == W_NS:
            direction = _MOVE_START[local]
            native_id = _attr(node, "id")
            move_range = _MoveRange(
                part_uri=part_uri,
                direction=direction,
                native_id=native_id,
                name=_attr(node, "name"),
                author=_attr(node, "author"),
                timestamp=_attr(node, "date"),
            )
            move_ranges.append(move_range)
            if native_id:
                active_moves[direction][native_id] = move_range
            continue
        if local in _MOVE_END and etree.QName(node).namespace == W_NS:
            direction = _MOVE_END[local]
            native_id = _attr(node, "id")
            if native_id:
                closed_range = active_moves[direction].get(native_id)
                if closed_range is not None:
                    active_moves[direction].pop(native_id)
                    closed_range.end_seen = True
            continue

        if node.tag == f"{W}commentRangeStart":
            native_id = _attr(node, "id")
            if native_id:
                anchor = comment_anchors.setdefault(native_id, _CommentAnchor(native_id))
                anchor.part_uri = anchor.part_uri or part_uri
                anchor.start_count += 1
                anchor.bookmark_name = anchor.bookmark_name or _current_bookmark(active_bookmarks)
                active_comments[native_id] = anchor
            continue
        if node.tag == f"{W}commentRangeEnd":
            native_id = _attr(node, "id")
            if native_id:
                anchor = comment_anchors.setdefault(native_id, _CommentAnchor(native_id))
                anchor.end_count += 1
                active_comments.pop(native_id, None)
            continue
        if node.tag == f"{W}commentReference":
            native_id = _attr(node, "id")
            if native_id:
                anchor = comment_anchors.setdefault(native_id, _CommentAnchor(native_id))
                anchor.reference_count += 1
                anchor.bookmark_name = anchor.bookmark_name or _current_bookmark(active_bookmarks)

        token = _text_token(node, include_deleted=True)
        if token is not None:
            for anchor in active_comments.values():
                anchor.text_chunks.append(token)

        event_kind = _revision_kind(node)
        if include_comments and node.tag == f"{W}comment":
            event_kind = ("comment", None)
        if event_kind is None:
            continue
        kind, native_kind = event_kind
        parent = parents.get(node)
        event_move_range: _MoveRange | None = None
        if kind in {"move_from", "move_to"}:
            candidates = tuple(active_moves[kind].values())
            event_move_range = candidates[-1] if candidates else None
        native_id = _attr(node, "id")
        event_anchor: _CommentAnchor | None = (
            comment_anchors.get(native_id or "") if kind == "comment" else None
        )
        bookmark = (
            event_anchor.bookmark_name
            if event_anchor is not None and event_anchor.bookmark_name is not None
            else _current_bookmark(active_bookmarks)
        )
        drafts.append(
            _EventDraft(
                part_uri=part_uri,
                node=node,
                node_ordinal=ordinals[node],
                document_order=len(drafts),
                kind=kind,
                native_kind=native_kind,
                bookmark_name=bookmark,
                parent_node_ordinal=None if parent is None else ordinals[parent],
                sibling_index=None if parent is None else parent.index(node),
                move_range=event_move_range,
                comment_anchor=event_anchor,
            )
        )


def _pair_move_ranges(move_ranges: Sequence[_MoveRange]) -> None:
    groups: dict[tuple[str, str], dict[str, list[_MoveRange]]] = {}
    for item in move_ranges:
        if not item.name:
            continue
        directions = groups.setdefault((item.part_uri, item.name), {})
        directions.setdefault(item.direction, []).append(item)
    for directions in groups.values():
        from_ranges = directions.get("move_from", [])
        to_ranges = directions.get("move_to", [])
        if len(from_ranges) == len(to_ranges) == 1:
            from_ranges[0].pair_native_id = to_ranges[0].native_id
            to_ranges[0].pair_native_id = from_ranges[0].native_id


def _format_content(
    node: etree._Element,
    parents: Mapping[etree._Element, etree._Element],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    current_properties = parents.get(node)
    prior_properties = node.find(f"{W}rPr")
    run = parents.get(current_properties) if current_properties is not None else None
    text = _element_text(run) if run is not None and run.tag == f"{W}r" else ""
    return text, _run_properties(prior_properties), _run_properties(current_properties)


def _finalize_draft(package: DocxPackage, draft: _EventDraft) -> ExtractedRevision:
    root = package.xml_root(draft.part_uri)
    node = list(root.iter())[draft.node_ordinal]
    parents = {child: parent for parent in root.iter() for child in parent}
    fragment_sha256 = sha256_bytes(_canonical_fragment(node))
    issues = list(draft.issues)
    author = _author(node, issues)
    timestamp = _timestamp(node, issues)
    native_id = _attr(node, "id")
    if native_id is None:
        issues.append(
            _Issue(
                ErrorCode.DOCX_INVALID_PACKAGE.value,
                "error",
                "revision node is missing its native id",
            )
        )

    text: str | None = None
    deleted_text: str | None = None
    comment_text: str | None = None
    format_before: dict[str, Any] | None = None
    format_after: dict[str, Any] | None = None
    range_data: dict[str, str | None] | None = None
    anchor_text: str | None = None

    if draft.kind in {"insert", "move_to"}:
        text = _element_text(node)
    elif draft.kind == "delete":
        deleted_nodes = node.findall(f".//{W}delText") + node.findall(f".//{W}delInstrText")
        if not deleted_nodes:
            raise ContractError(
                ErrorCode.REVISION_DELETE_TEXT_MISSING,
                "deletion event is missing w:delText evidence",
                details={"part_uri": draft.part_uri, "native_id": native_id},
            )
        deleted_text = _element_text(node, deleted=True)
    elif draft.kind == "move_from":
        # A move source is revision-marked by its enclosing ``w:moveFrom``;
        # unlike ``w:del``, its run text must remain ordinary ``w:t``. Word
        # rejects move sources encoded with ``w:delText`` even though older
        # Open XML SDK schema validation does not report the semantic error.
        moved_text_nodes = node.findall(f".//{W}t")
        if not moved_text_nodes:
            raise ContractError(
                ErrorCode.REVISION_DELETE_TEXT_MISSING,
                "move-from event is missing w:t evidence",
                details={"part_uri": draft.part_uri, "native_id": native_id},
            )
        deleted_text = _element_text(node)
    elif draft.kind == "run_format":
        text, format_before, format_after = _format_content(node, parents)
    elif draft.kind in {"paragraph_format", "table_format"}:
        current_properties = parents.get(node)
        prior_local = "pPr" if draft.kind == "paragraph_format" else "tblPr"
        format_before = _property_fragment(node.find(f"{W}{prior_local}"))
        format_after = _property_fragment(current_properties)
    elif draft.kind == "comment":
        comment_text = _element_text(node)
        anchor = draft.comment_anchor
        if anchor is None:
            range_data = {
                "start_native_id": None,
                "end_native_id": None,
                "pair_native_id": None,
                "anchor_kind": "unknown",
            }
            issues.append(
                _Issue(
                    ErrorCode.DOCX_INVALID_PACKAGE.value,
                    "error",
                    "comment has no matching anchor evidence",
                )
            )
        else:
            anchor_text = anchor.anchor_text
            complete_range = anchor.start_count == anchor.end_count == anchor.reference_count == 1
            point_reference = (
                anchor.start_count == anchor.end_count == 0 and anchor.reference_count == 1
            )
            if complete_range:
                anchor_kind = "range" if anchor.anchor_text else "point"
            elif point_reference:
                anchor_kind = "point"
            else:
                anchor_kind = "unknown"
                issues.append(
                    _Issue(
                        ErrorCode.DOCX_INVALID_PACKAGE.value,
                        "error",
                        "comment start/end/reference plumbing is incomplete or duplicated",
                    )
                )
            range_data = {
                "start_native_id": anchor.native_id if anchor.start_count else None,
                "end_native_id": anchor.native_id if anchor.end_count else None,
                "pair_native_id": anchor.native_id if anchor.reference_count else None,
                "anchor_kind": anchor_kind,
            }
            text = anchor.anchor_text
    else:
        text = _element_text(node)
        deleted_text = _element_text(node, deleted=True) or None

    move_name = None
    if draft.kind in {"move_from", "move_to"}:
        move_range = draft.move_range
        if move_range is None:
            range_data = {
                "start_native_id": None,
                "end_native_id": None,
                "pair_native_id": None,
                "anchor_kind": "unknown",
            }
            issues.append(
                _Issue(
                    ErrorCode.DOCX_INVALID_PACKAGE.value,
                    "error",
                    "move event is not enclosed by a matching move range",
                )
            )
        else:
            move_name = move_range.name
            paired = (
                move_range.start_seen
                and move_range.end_seen
                and move_range.native_id is not None
                and move_range.pair_native_id is not None
                and move_range.name is not None
            )
            range_data = {
                "start_native_id": move_range.native_id if move_range.start_seen else None,
                "end_native_id": move_range.native_id if move_range.end_seen else None,
                "pair_native_id": move_range.pair_native_id,
                "anchor_kind": "paired_move" if paired else "unknown",
            }
            if not paired:
                issues.append(
                    _Issue(
                        ErrorCode.DOCX_INVALID_PACKAGE.value,
                        "error",
                        "move range is incomplete, unnamed, or unpaired",
                    )
                )
            if move_range.author not in {None, author} or move_range.timestamp not in {
                None,
                timestamp,
            }:
                issues.append(
                    _Issue(
                        ErrorCode.DOCX_INVALID_PACKAGE.value,
                        "error",
                        "move range and move event metadata disagree",
                    )
                )

    diagnostic_fingerprint = sha256_canonical([draft.part_uri, draft.node_ordinal, fragment_sha256])
    diagnostics = [_diagnostic(issue, diagnostic_fingerprint) for issue in issues]
    raw_event_id = derive_raw_event_id(
        returned_docx_sha256=package.file_sha256,
        part_uri=draft.part_uri,
        kind=draft.kind,
        native_id=native_id,
        document_order=draft.document_order,
        fragment_sha256=fragment_sha256,
    )
    raw_event = {
        "raw_event_id": raw_event_id,
        "returned_docx_sha256": package.file_sha256,
        "part_uri": draft.part_uri,
        "part_sha256": package.xml_sha256(draft.part_uri),
        "kind": draft.kind,
        "native_id": native_id,
        "native_kind": draft.native_kind,
        "author": author,
        "timestamp": timestamp,
        "document_order": draft.document_order,
        "content": {
            "text": text,
            "deleted_text": deleted_text,
            "comment_text": comment_text,
            "format_before": format_before,
            "format_after": format_after,
        },
        "range": range_data,
        "evidence": {
            "node_ordinal": draft.node_ordinal,
            "fragment_sha256": fragment_sha256,
            "artifact": None,
        },
        "diagnostics": diagnostics,
    }
    bookmark = (
        draft.comment_anchor.bookmark_name
        if draft.comment_anchor is not None and draft.comment_anchor.bookmark_name is not None
        else draft.bookmark_name
    )
    return ExtractedRevision(
        raw_event=raw_event,
        bookmark_name=bookmark,
        parent_node_ordinal=draft.parent_node_ordinal,
        sibling_index=draft.sibling_index,
        move_name=move_name,
        anchor_text=anchor_text,
    )


def extract_revision_events(
    path: str | Path,
    *,
    limits: DocxReadLimits | None = None,
) -> RevisionExtraction:
    """Extract ordered raw revision/comment evidence without modifying the DOCX."""

    resolved_limits = limits or DocxReadLimits()
    package = read_docx_package(path, limits=resolved_limits)
    drafts: list[_EventDraft] = []
    anchors: dict[str, _CommentAnchor] = {}
    move_ranges: list[_MoveRange] = []
    for part_uri in package.story_parts:
        _scan_story_part(
            package,
            part_uri,
            drafts,
            anchors,
            move_ranges,
            include_comments=False,
        )
    if package.comments_part is not None:
        _scan_story_part(
            package,
            package.comments_part,
            drafts,
            anchors,
            move_ranges,
            include_comments=True,
        )
    _pair_move_ranges(move_ranges)

    duplicate_keys = {
        key
        for key, count in Counter(
            (draft.part_uri, draft.kind, _attr(draft.node, "id"))
            for draft in drafts
            if _attr(draft.node, "id") is not None
        ).items()
        if count > 1
    }
    for draft in drafts:
        if (draft.part_uri, draft.kind, _attr(draft.node, "id")) in duplicate_keys:
            draft.issues.append(
                _Issue(
                    ErrorCode.DOCX_INVALID_PACKAGE.value,
                    "error",
                    "duplicate native revision id for the same part and kind",
                )
            )

    events = tuple(_finalize_draft(package, draft) for draft in drafts)
    diagnostics_by_id: dict[str, Mapping[str, Any]] = {}
    for event in events:
        for diagnostic in event.raw_event["diagnostics"]:
            diagnostics_by_id.setdefault(str(diagnostic["diagnostic_id"]), diagnostic)
    configuration_sha256 = sha256_canonical(
        {
            "reader": "canonical-opc-revision-reader",
            "interface_version": _READER_INTERFACE_VERSION,
            "limits": asdict(resolved_limits),
        }
    )
    return RevisionExtraction(
        package=package,
        events=events,
        diagnostics=tuple(diagnostics_by_id.values()),
        configuration_sha256=configuration_sha256,
    )


def _ordered_unique(values: Sequence[str | None]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value is not None and value not in result:
            result.append(value)
    return result


def _replacement_pair(first: ExtractedRevision, second: ExtractedRevision) -> bool:
    a = first.raw_event
    b = second.raw_event
    return (
        a["kind"] == "delete"
        and b["kind"] == "insert"
        and a["part_uri"] == b["part_uri"]
        and first.parent_node_ordinal is not None
        and first.parent_node_ordinal == second.parent_node_ordinal
        and first.sibling_index is not None
        and second.sibling_index == first.sibling_index + 1
        and a["author"] == b["author"]
        and a["timestamp"] == b["timestamp"]
        and first.bookmark_name == second.bookmark_name
    )


def _change_shape(
    events: Sequence[ExtractedRevision],
) -> tuple[str, Any, Any, str | None, str | None]:
    raw = [event.raw_event for event in events]
    kinds = [str(item["kind"]) for item in raw]
    if kinds == ["delete", "insert"]:
        return (
            "replacement",
            raw[0]["content"]["deleted_text"],
            raw[1]["content"]["text"],
            None,
            None,
        )
    if kinds == ["move_from", "move_to"] or kinds == ["move_to", "move_from"]:
        by_kind = {str(item["kind"]): item for item in raw}
        return (
            "move",
            by_kind["move_from"]["content"]["deleted_text"],
            by_kind["move_to"]["content"]["text"],
            None,
            None,
        )
    item = raw[0]
    kind = str(item["kind"])
    content = item["content"]
    if kind == "insert":
        return "insertion", "", content["text"], None, None
    if kind == "delete":
        return "deletion", content["deleted_text"], "", None, None
    if kind in {"move_from", "move_to"}:
        before = content["deleted_text"] if kind == "move_from" else ""
        after = content["text"] if kind == "move_to" else ""
        return "move", before, after, None, None
    if kind == "comment":
        return "comment", None, None, content["comment_text"], None
    if kind in {"run_format", "paragraph_format", "table_format"}:
        return "format", content["format_before"], content["format_after"], None, kind
    return (
        "unknown",
        content["deleted_text"],
        content["text"],
        None,
        str(item["native_kind"] or kind),
    )


def _move_pairs(events: Sequence[ExtractedRevision]) -> dict[int, int]:
    groups: dict[tuple[str, str], dict[str, list[int]]] = {}
    for index, event in enumerate(events):
        kind = str(event.raw_event["kind"])
        if kind not in {"move_from", "move_to"} or event.move_name is None:
            continue
        key = str(event.raw_event["part_uri"]), event.move_name
        groups.setdefault(key, {}).setdefault(kind, []).append(index)
    pairs: dict[int, int] = {}
    for directions in groups.values():
        from_indices = directions.get("move_from", [])
        to_indices = directions.get("move_to", [])
        if len(from_indices) == len(to_indices) == 1:
            pairs[from_indices[0]] = to_indices[0]
            pairs[to_indices[0]] = from_indices[0]
    return pairs


def _binding_for(
    bookmark_name: str | None,
    bindings: Mapping[str, BookmarkBinding],
) -> BookmarkBinding | None:
    return None if bookmark_name is None else bindings.get(bookmark_name)


def _normalize_group(
    group: Sequence[ExtractedRevision],
    bindings: Mapping[str, BookmarkBinding],
    *,
    forced_conflict: bool = False,
) -> NormalizedChange:
    raw = [event.raw_event for event in group]
    raw_event_ids = [str(item["raw_event_id"]) for item in raw]
    kind, before, after, comment, native_kind = _change_shape(group)
    bookmark_names = _ordered_unique([event.bookmark_name for event in group])
    bookmark_name = bookmark_names[0] if len(bookmark_names) == 1 else None
    binding = _binding_for(bookmark_name, bindings)
    authors = _ordered_unique([item["author"] for item in raw])
    timestamps = _ordered_unique([item["timestamp"] for item in raw])
    metadata_conflict = len(authors) > 1 or len(timestamps) > 1 or len(bookmark_names) > 1
    conflict = forced_conflict or metadata_conflict

    if conflict:
        resolution = {"status": "conflict", "method": "manual", "confidence": 0.0, "candidates": []}
        unit_id = None
        source_location = None
    elif binding is not None:
        resolution = {"status": "exact", "method": "bookmark", "confidence": 1.0, "candidates": []}
        unit_id = binding.unit_id
        source_location = copy.deepcopy(dict(binding.source_location))
    else:
        resolution = {"status": "unmatched", "method": "none", "confidence": 0.0, "candidates": []}
        unit_id = None
        source_location = None

    if kind == "unknown":
        safety_class = "denied_unknown"
        initial_decision = "manual"
    elif kind == "comment":
        safety_class = "ledger_only"
        initial_decision = "pending" if not conflict else "conflict"
    elif kind in {"move", "format"}:
        safety_class = "manual_high_risk"
        initial_decision = "manual" if not conflict else "conflict"
    elif binding is not None and not conflict:
        safety_class = "plain_text_candidate"
        initial_decision = "pending"
    else:
        safety_class = "manual_high_risk"
        initial_decision = "manual" if not conflict else "conflict"

    change_id = derive_change_id(raw_event_ids, profile_version=_COMBINE_PROFILE_VERSION)
    fingerprint_input = {
        "kind": kind,
        "native_kind": native_kind,
        "raw_event_ids": raw_event_ids,
        "author": authors[0] if len(authors) == 1 else None,
        "authors": authors,
        "timestamp": timestamps[0] if len(timestamps) == 1 else None,
        "timestamps": timestamps,
        "before": before,
        "after": after,
        "comment": comment,
        "unit_id": unit_id,
        "source_location": source_location,
        "resolution": resolution,
        "safety_class": safety_class,
    }
    change = {
        "change_id": change_id,
        "kind": kind,
        "native_kind": native_kind,
        "raw_event_ids": raw_event_ids,
        "author": authors[0] if len(authors) == 1 else None,
        "authors": authors,
        "timestamp": timestamps[0] if len(timestamps) == 1 else None,
        "timestamps": timestamps,
        "before": before,
        "after": after,
        "comment": comment,
        "unit_id": unit_id,
        "source_location": source_location,
        "resolution": resolution,
        "safety_class": safety_class,
        "initial_decision": initial_decision,
        "change_fingerprint": sha256_canonical(fingerprint_input),
    }
    return NormalizedChange(change=change, bookmark_name=bookmark_name)


def normalize_revision_changes(
    extraction: RevisionExtraction,
    *,
    bookmark_bindings: Mapping[str, BookmarkBinding] | None = None,
) -> ChangeNormalization:
    """Combine replacements/moves while preserving every raw event unchanged."""

    bindings = bookmark_bindings or {}
    events = extraction.events
    move_pairs = _move_pairs(events)
    consumed: set[int] = set()
    normalized: list[NormalizedChange] = []
    for index, event in enumerate(events):
        if index in consumed:
            continue
        group: list[ExtractedRevision] = [event]
        forced_conflict = False
        paired_index = move_pairs.get(index)
        if paired_index is not None and paired_index not in consumed:
            group = sorted(
                [event, events[paired_index]], key=lambda item: item.raw_event["document_order"]
            )
            consumed.add(paired_index)
        elif index + 1 < len(events) and _replacement_pair(event, events[index + 1]):
            group = [event, events[index + 1]]
            consumed.add(index + 1)
        elif event.raw_event["kind"] in {"move_from", "move_to"}:
            forced_conflict = True
        consumed.add(index)
        normalized.append(_normalize_group(group, bindings, forced_conflict=forced_conflict))

    normalized.sort(
        key=lambda item: min(
            event.raw_event["document_order"]
            for event in events
            if event.raw_event["raw_event_id"] in item.change["raw_event_ids"]
        )
    )
    return ChangeNormalization(changes=tuple(normalized), diagnostics=extraction.diagnostics)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def build_changeset(
    path: str | Path,
    *,
    run_id: str,
    source_manifest_sha256: str,
    source_map_sha256: str,
    revision_reader_capabilities_sha256: str,
    returned_artifact_path: str = "ingest/returned-original.docx",
    confidentiality: str = "local_private",
    bookmark_bindings: Mapping[str, BookmarkBinding] | None = None,
    generated_at: str | None = None,
    limits: DocxReadLimits | None = None,
) -> dict[str, Any]:
    """Build and validate a v1alpha ChangeSet envelope from read-only evidence."""

    extraction = extract_revision_events(path, limits=limits)
    normalization = normalize_revision_changes(
        extraction,
        bookmark_bindings=bookmark_bindings,
    )
    package = extraction.package
    ingest_configuration_sha256 = sha256_canonical(
        {
            "reader_configuration_sha256": extraction.configuration_sha256,
            "combine_profile": _COMBINE_PROFILE_VERSION,
            "source_mapping": "caller-supplied-bookmark-bindings-v1",
        }
    )
    changes = [dict(item.change) for item in normalization.changes]
    raw_events = [dict(item.raw_event) for item in extraction.events]
    counts: dict[str, int] = {
        "raw_events": len(raw_events),
        "changes": len(changes),
        "unmatched": sum(1 for change in changes if change["resolution"]["status"] == "unmatched"),
        "conflicts": sum(1 for change in changes if change["resolution"]["status"] == "conflict"),
    }
    for kind, count in Counter(str(event["kind"]) for event in raw_events).items():
        counts[f"raw_{kind}"] = count
    for kind, count in Counter(str(change["kind"]) for change in changes).items():
        counts[f"change_{kind}"] = count

    payload = {
        "returned_original": {
            "artifact_id": derive_artifact_id(package.file_sha256),
            "path": returned_artifact_path,
            "path_base": "run_root",
            "role": "returned_original",
            "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size_bytes": package.size_bytes,
            "sha256": package.file_sha256,
            "immutable": True,
            "confidentiality": confidentiality,
        },
        "source_manifest_sha256": source_manifest_sha256,
        "source_map_sha256": source_map_sha256,
        "revision_reader_capabilities_sha256": revision_reader_capabilities_sha256,
        "ingest_profile": {
            "name": "canonical-word-revision-ingest",
            "version": _COMBINE_PROFILE_VERSION,
            "configuration_sha256": ingest_configuration_sha256,
        },
        "raw_events": raw_events,
        "changes": changes,
        "counts": counts,
        "diagnostics": [dict(item) for item in normalization.diagnostics],
    }
    object_id = stable_id(
        "changes_",
        [
            package.file_sha256,
            source_manifest_sha256,
            source_map_sha256,
            revision_reader_capabilities_sha256,
            ingest_configuration_sha256,
            [event["raw_event_id"] for event in raw_events],
            [change["change_id"] for change in changes],
        ],
    )
    producer = {
        "name": "latex-word-review",
        "version": __version__,
        "interface_version": _READER_INTERFACE_VERSION,
        "distribution": "python-package",
        "executable_sha256": None,
        "configuration_sha256": ingest_configuration_sha256,
    }
    return make_envelope(
        schema_name="ChangeSet",
        object_id=object_id,
        run_id=run_id,
        generated_at=generated_at or _utc_now(),
        producer=producer,
        payload=payload,
    )


__all__ = [
    "BookmarkBinding",
    "ChangeNormalization",
    "ExtractedRevision",
    "NormalizedChange",
    "RevisionExtraction",
    "build_changeset",
    "extract_revision_events",
    "normalize_revision_changes",
]
