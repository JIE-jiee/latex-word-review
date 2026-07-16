"""Read-only semantic projections for exported and returned Word documents.

The projection deliberately ignores ZIP serialization, run segmentation,
proofing markers, and ``w:rsid*`` attributes.  It models only visible text,
paragraph/table structure, bookmarks, and text-bearing tracked revisions.  A
caller must not infer formatting, OMML, or image equivalence from this module.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.docx_reader import DocxPackage, DocxReadLimits, read_docx_package
from latex_word_review.errors import ContractError, ErrorCode

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"

type RevisionKind = Literal["insert", "delete", "move_from", "move_to"]
type DriftStatus = Literal["clean", "accepted_or_untracked_drift"]

_REVISION_KIND_BY_LOCAL: Mapping[str, RevisionKind] = {
    "ins": "insert",
    "del": "delete",
    "moveFrom": "move_from",
    "moveTo": "move_to",
}
_INSERTED_KINDS = frozenset({"insert", "move_to"})
_DELETED_KINDS = frozenset({"delete", "move_from"})
_PROPERTY_CONTAINERS = frozenset(
    {"pPr", "rPr", "tblPr", "tblPrEx", "trPr", "tcPr", "sectPr", "numPr"}
)
_SKIPPED_SUBTREES = _PROPERTY_CONTAINERS | frozenset(
    {
        "drawing",
        "pict",
        "object",
        "alternateContent",
    }
)
_FIELD_ATOM_PREFIX = "word_field_"
_STRUCTURE_KINDS: Mapping[str, str] = {
    "p": "paragraph",
    "tbl": "table",
    "tr": "table_row",
    "tc": "table_cell",
}
_MOVE_RANGE_MARKERS: Mapping[str, tuple[RevisionKind, bool]] = {
    "moveFromRangeStart": ("move_from", True),
    "moveFromRangeEnd": ("move_from", False),
    "moveToRangeStart": ("move_to", True),
    "moveToRangeEnd": ("move_to", False),
}
_IGNORED_FORMAT_REVISION_LOCALS = frozenset(
    {
        "rPrChange",
        "pPrChange",
        "tblPrChange",
        "tblPrExChange",
        "tblGridChange",
        "sectPrChange",
        "trPrChange",
        "tcPrChange",
        "numberingChange",
    }
)
_UNSUPPORTED_REVISION_LOCALS = frozenset(
    {
        "cellIns",
        "cellDel",
        "cellMerge",
        "customXmlInsRangeStart",
        "customXmlInsRangeEnd",
        "customXmlDelRangeStart",
        "customXmlDelRangeEnd",
        "customXmlMoveFromRangeStart",
        "customXmlMoveFromRangeEnd",
        "customXmlMoveToRangeStart",
        "customXmlMoveToRangeEnd",
        "conflictIns",
        "conflictDel",
    }
)


@dataclass(frozen=True, slots=True)
class WordSemanticLimits:
    """Resource limits applied in addition to the validated DOCX reader."""

    max_story_parts: int = 512
    max_xml_bytes_per_story: int = 16 * 1024 * 1024
    max_nodes_per_story: int = 1_000_000
    max_atoms_per_view: int = 1_000_000
    max_visible_chars_per_view: int = 16 * 1024 * 1024
    max_bookmarks_per_story: int = 100_000
    max_revisions_per_story: int = 1_000_000
    max_revision_bookmark_links_per_story: int = 1_000_000

    def __post_init__(self) -> None:
        values = (
            self.max_story_parts,
            self.max_xml_bytes_per_story,
            self.max_nodes_per_story,
            self.max_atoms_per_view,
            self.max_visible_chars_per_view,
            self.max_bookmarks_per_story,
            self.max_revisions_per_story,
            self.max_revision_bookmark_links_per_story,
        )
        if any(value <= 0 for value in values):
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "Word semantic projection limits must be positive",
            )


@dataclass(frozen=True, slots=True)
class SemanticCapabilities:
    """Explicit boundary of what equality of two projections means."""

    visible_text: bool = True
    paragraph_table_structure: bool = True
    bookmarks: bool = True
    insert_delete_revisions: bool = True
    move_revisions: bool = True
    field_structure: bool = True
    paragraph_mark_revisions: bool = False
    formatting: bool = False
    omml: bool = False
    images: bool = False
    unsupported: tuple[str, ...] = (
        "formatting and formatting revisions",
        "paragraph-mark and table-structure revisions",
        "OMML equation semantics",
        "drawing and image semantics",
    )


CAPABILITIES = SemanticCapabilities()


@dataclass(frozen=True, slots=True)
class SemanticAtom:
    """One canonical visible token or structural boundary."""

    kind: str
    value: str = ""


@dataclass(frozen=True, slots=True)
class ViewProjection:
    """Canonical projection for one accept/reject view of a story part."""

    text: str
    atoms: tuple[SemanticAtom, ...]


@dataclass(frozen=True, slots=True)
class BookmarkRange:
    """A visible bookmark range in story-relative Unicode code-point offsets."""

    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class RevisionSpan:
    """Reject/base coordinates for one text-bearing revision wrapper.

    ``insert`` and ``move_to`` have a zero-width span.  ``delete`` and
    ``move_from`` consume the text that is present in the reject/original view.
    ``node_ordinal`` is exactly the ordinal produced by ``list(root.iter())``.
    """

    node_ordinal: int
    kind: RevisionKind
    base_start: int
    base_end: int
    bookmark_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BookmarkRevisionSpan:
    """A revision span in both story/base and bookmark-relative coordinates."""

    node_ordinal: int
    kind: RevisionKind
    base_start: int
    base_end: int
    relative_start: int
    relative_end: int


@dataclass(frozen=True, slots=True)
class BookmarkProjection:
    """One physical bookmark and its visibility in reject and accept views."""

    name: str
    native_id: str
    part_uri: str
    start_node_ordinal: int
    end_node_ordinal: int | None
    original: BookmarkRange | None
    final: BookmarkRange | None
    revision_spans: tuple[BookmarkRevisionSpan, ...]

    @property
    def reject(self) -> BookmarkRange | None:
        return self.original

    @property
    def accept(self) -> BookmarkRange | None:
        return self.final

    def revision_span(self, node_ordinal: int) -> BookmarkRevisionSpan | None:
        """Return bookmark-relative coordinates for one revision ordinal."""

        return next(
            (span for span in self.revision_spans if span.node_ordinal == node_ordinal),
            None,
        )


@dataclass(frozen=True, slots=True)
class StoryProjection:
    """Reject/original and accept/final projections of one Word story part."""

    part_uri: str
    original: ViewProjection
    final: ViewProjection
    bookmarks: tuple[BookmarkProjection, ...]
    revision_spans: tuple[RevisionSpan, ...]

    @property
    def reject(self) -> ViewProjection:
        return self.original

    @property
    def accept(self) -> ViewProjection:
        return self.final

    def revision_span(self, node_ordinal: int) -> RevisionSpan | None:
        """Return the reject/base span for an exact revision node ordinal."""

        return next(
            (span for span in self.revision_spans if span.node_ordinal == node_ordinal),
            None,
        )


@dataclass(frozen=True, slots=True)
class DocumentSemanticProjection:
    """Immutable semantic evidence for all story parts in a DOCX."""

    source_name: str
    file_sha256: str
    stories: tuple[StoryProjection, ...]
    capabilities: SemanticCapabilities = CAPABILITIES

    def story(self, part_uri: str) -> StoryProjection | None:
        """Return one story projection by OPC part URI."""

        return next((story for story in self.stories if story.part_uri == part_uri), None)


type SemanticSource = str | Path | DocxPackage | DocumentSemanticProjection


@dataclass(frozen=True, slots=True)
class StoryDifference:
    """Bounded diagnostic evidence for a changed story projection."""

    part_uri: str
    baseline_text_sha256: str | None
    returned_original_text_sha256: str | None
    text_changed: bool
    structure_changed: bool


@dataclass(frozen=True, slots=True)
class BookmarkIssue:
    """A missing, duplicate, or semantically moved export bookmark."""

    name: str
    kind: Literal["missing", "duplicate", "changed"]
    detail: str


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    """Result of comparing an export baseline with a returned Word document."""

    baseline: DocumentSemanticProjection
    returned: DocumentSemanticProjection
    drift_status: DriftStatus
    story_differences: tuple[StoryDifference, ...]
    bookmark_issues: tuple[BookmarkIssue, ...]
    missing_bookmarks: tuple[str, ...]
    duplicate_bookmarks: tuple[str, ...]
    changed_bookmarks: tuple[str, ...]

    @property
    def accepted_or_untracked_drift(self) -> bool:
        return self.drift_status == "accepted_or_untracked_drift"

    @property
    def safe_for_automatic_patch(self) -> bool:
        return not self.accepted_or_untracked_drift and not self.bookmark_issues


@dataclass(frozen=True, slots=True)
class _RawBookmark:
    name: str
    native_id: str
    start_node: etree._Element
    start_ordinal: int
    end_node: etree._Element | None
    end_ordinal: int | None


@dataclass(frozen=True, slots=True)
class _ViewResult:
    projection: ViewProjection
    marker_offsets: Mapping[etree._Element, int]
    revision_spans: tuple[RevisionSpan, ...]
    revision_bookmark_ordinals: Mapping[int, tuple[int, ...]]


def _local_name(node: etree._Element) -> str:
    return str(etree.QName(node).localname)


def _namespace(node: etree._Element) -> str | None:
    namespace = etree.QName(node).namespace
    return None if namespace is None else str(namespace)


def _attr(node: etree._Element, local: str) -> str | None:
    value = node.get(f"{W}{local}")
    return None if value is None else str(value)


def _suspicious_revision_local(local: str) -> bool:
    folded = local.casefold()
    return (
        "revision" in folded
        or folded.endswith("change")
        or folded.endswith("prchange")
        or folded.startswith("customxmlins")
        or folded.startswith("customxmldel")
        or folded.startswith("customxmlmove")
        or folded in {"conflictins", "conflictdel"}
        or ("range" in folded and any(fragment in folded for fragment in ("ins", "del", "move")))
    )


def _unsupported_revision(part_uri: str, local: str, node_ordinal: int) -> ContractError:
    return ContractError(
        ErrorCode.DOCX_INVALID_PACKAGE,
        "semantic projection does not support this revision markup",
        details={
            "part_uri": part_uri,
            "native_kind": f"w:{local}",
            "node_ordinal": node_ordinal,
        },
    )


def _validate_revision_markup(
    part_uri: str,
    nodes: Sequence[etree._Element],
    ordinals: Mapping[etree._Element, int],
) -> None:
    move_ranges: dict[tuple[RevisionKind, str], list[bool]] = {}

    def visit(node: etree._Element, property_depth: int, revision_depth: int) -> None:
        namespace = _namespace(node)
        local = _local_name(node)
        next_property_depth = property_depth
        next_revision_depth = revision_depth
        if namespace == W_NS:
            if local in _PROPERTY_CONTAINERS:
                next_property_depth += 1
            kind = _REVISION_KIND_BY_LOCAL.get(local)
            if kind is not None:
                if property_depth or revision_depth:
                    raise _unsupported_revision(part_uri, local, ordinals[node])
                next_revision_depth += 1
            elif local in _IGNORED_FORMAT_REVISION_LOCALS:
                pass
            elif local in _UNSUPPORTED_REVISION_LOCALS or (
                local not in _MOVE_RANGE_MARKERS and _suspicious_revision_local(local)
            ):
                raise _unsupported_revision(part_uri, local, ordinals[node])
            marker = _MOVE_RANGE_MARKERS.get(local)
            if marker is not None:
                native_id = _attr(node, "id")
                if not native_id:
                    raise ContractError(
                        ErrorCode.DOCX_INVALID_PACKAGE,
                        "move range marker is missing w:id",
                        details={"part_uri": part_uri, "node_ordinal": ordinals[node]},
                    )
                direction, is_start = marker
                move_ranges.setdefault((direction, native_id), []).append(is_start)
            if local in {"delText", "delInstrText"} and revision_depth == 0:
                raise ContractError(
                    ErrorCode.DOCX_INVALID_PACKAGE,
                    "deleted text is outside a supported deletion revision",
                    details={"part_uri": part_uri, "node_ordinal": ordinals[node]},
                )
        elif _suspicious_revision_local(local):
            raise _unsupported_revision(part_uri, local, ordinals[node])
        for child in node:
            visit(child, next_property_depth, next_revision_depth)

    visit(nodes[0], 0, 0)
    for (direction, native_id), events in move_ranges.items():
        if events != [True, False]:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "move range markers are incomplete, duplicated, or out of order",
                details={
                    "part_uri": part_uri,
                    "direction": direction,
                    "native_id": native_id,
                },
            )


def _bookmarks(
    part_uri: str,
    nodes: Sequence[etree._Element],
    ordinals: Mapping[etree._Element, int],
    limits: WordSemanticLimits,
) -> tuple[_RawBookmark, ...]:
    starts: list[etree._Element] = []
    ends_by_id: dict[str, list[etree._Element]] = {}
    for node in nodes:
        if node.tag == f"{W}bookmarkStart":
            starts.append(node)
        elif node.tag == f"{W}bookmarkEnd":
            native_id = _attr(node, "id")
            if native_id is not None:
                ends_by_id.setdefault(native_id, []).append(node)
    if len(starts) > limits.max_bookmarks_per_story:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "story part exceeds semantic bookmark limit",
            details={"part_uri": part_uri},
        )

    result: list[_RawBookmark] = []
    for start in starts:
        native_id = _attr(start, "id")
        name = _attr(start, "name")
        if native_id is None or name is None:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "bookmark start is missing w:id or w:name",
                details={"part_uri": part_uri, "node_ordinal": ordinals[start]},
            )
        matching_ends = ends_by_id.get(native_id, [])
        end = matching_ends[0] if len(matching_ends) == 1 else None
        result.append(
            _RawBookmark(
                name=name,
                native_id=native_id,
                start_node=start,
                start_ordinal=ordinals[start],
                end_node=end,
                end_ordinal=None if end is None else ordinals[end],
            )
        )
    return tuple(result)


class _ViewBuilder:
    def __init__(
        self,
        *,
        part_uri: str,
        mode: Literal["original", "final"],
        ordinals: Mapping[etree._Element, int],
        bookmark_by_start: Mapping[etree._Element, _RawBookmark],
        limits: WordSemanticLimits,
    ) -> None:
        self.part_uri = part_uri
        self.mode = mode
        self.ordinals = ordinals
        self.bookmark_by_start = bookmark_by_start
        self.bookmark_name_by_ordinal = {
            bookmark.start_ordinal: bookmark.name for bookmark in bookmark_by_start.values()
        }
        self.limits = limits
        self.atoms: list[SemanticAtom] = []
        self.text_chunks: list[str] = []
        self.char_count = 0
        self.marker_offsets: dict[etree._Element, int] = {}
        self.revision_spans: list[RevisionSpan] = []
        self.revision_bookmarks: dict[int, tuple[int, ...]] = {}
        self.active_bookmarks: dict[str, list[tuple[int, bool]]] = {}
        self.hidden_move_ranges: set[tuple[RevisionKind, str]] = set()
        self.revision_bookmark_link_count = 0

    @property
    def hidden(self) -> bool:
        return bool(self.hidden_move_ranges)

    def _append_atom(self, kind: str, value: str = "", *, visible: bool = True) -> None:
        if value and visible:
            next_count = self.char_count + len(value)
            if next_count > self.limits.max_visible_chars_per_view:
                raise ContractError(
                    ErrorCode.DOCX_INVALID_PACKAGE,
                    "story part exceeds semantic visible-text limit",
                    details={"part_uri": self.part_uri, "view": self.mode},
                )
            self.text_chunks.append(value)
            self.char_count = next_count
        if kind == "text" and self.atoms and self.atoms[-1].kind == "text":
            previous = self.atoms[-1]
            self.atoms[-1] = SemanticAtom("text", previous.value + value)
            return
        if len(self.atoms) >= self.limits.max_atoms_per_view:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "story part exceeds semantic atom limit",
                details={"part_uri": self.part_uri, "view": self.mode},
            )
        self.atoms.append(SemanticAtom(kind, value))

    def _append_field_atom(self, kind: str, value: str) -> None:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        self._append_atom(f"{_FIELD_ATOM_PREFIX}{kind}", digest, visible=False)

    def _active_bookmark_ordinals(self) -> tuple[int, ...]:
        return tuple(
            start_ordinal
            for stack in self.active_bookmarks.values()
            for start_ordinal, visible in stack
            if visible
        )

    def _active_bookmark_names(self, start_ordinals: Sequence[int]) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        for start_ordinal in start_ordinals:
            name = self.bookmark_name_by_ordinal[start_ordinal]
            if name not in seen:
                seen.add(name)
                names.append(name)
        return tuple(names)

    def _move_marker(self, node: etree._Element, local: str) -> bool:
        marker = _MOVE_RANGE_MARKERS.get(local)
        if marker is None:
            return False
        direction, is_start = marker
        native_id = _attr(node, "id")
        if native_id is None:  # Already rejected during validation.
            return True
        hidden_direction: RevisionKind = "move_to" if self.mode == "original" else "move_from"
        if direction == hidden_direction:
            key = (direction, native_id)
            if is_start:
                self.hidden_move_ranges.add(key)
            else:
                self.hidden_move_ranges.discard(key)
        return True

    def _bookmark_marker(self, node: etree._Element) -> bool:
        if node.tag == f"{W}bookmarkStart":
            native_id = _attr(node, "id")
            bookmark = self.bookmark_by_start.get(node)
            if native_id is not None and bookmark is not None:
                visible = not self.hidden
                self.active_bookmarks.setdefault(native_id, []).append(
                    (bookmark.start_ordinal, visible)
                )
                if visible:
                    self.marker_offsets[node] = self.char_count
            return True
        if node.tag == f"{W}bookmarkEnd":
            native_id = _attr(node, "id")
            if native_id is not None:
                stack = self.active_bookmarks.get(native_id)
                if stack:
                    _start_ordinal, visible = stack.pop()
                    if visible and not self.hidden:
                        self.marker_offsets[node] = self.char_count
                    if not stack:
                        self.active_bookmarks.pop(native_id, None)
            return True
        return False

    def _revision(self, node: etree._Element, kind: RevisionKind) -> None:
        start = self.char_count
        node_ordinal = self.ordinals[node]
        bookmark_ordinals = self._active_bookmark_ordinals()
        self.revision_bookmark_link_count += len(bookmark_ordinals)
        if self.revision_bookmark_link_count > self.limits.max_revision_bookmark_links_per_story:
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "story part exceeds semantic revision/bookmark association limit",
                details={"part_uri": self.part_uri},
            )
        bookmark_names = self._active_bookmark_names(bookmark_ordinals)
        include = (self.mode == "original" and kind in _DELETED_KINDS) or (
            self.mode == "final" and kind in _INSERTED_KINDS
        )
        if include and not self.hidden:
            for child in node:
                self.walk(child)
        if self.mode == "original":
            end = self.char_count if kind in _DELETED_KINDS and not self.hidden else start
            if len(self.revision_spans) >= self.limits.max_revisions_per_story:
                raise ContractError(
                    ErrorCode.DOCX_INVALID_PACKAGE,
                    "story part exceeds semantic revision limit",
                    details={"part_uri": self.part_uri},
                )
            self.revision_spans.append(RevisionSpan(node_ordinal, kind, start, end, bookmark_names))
            self.revision_bookmarks[node_ordinal] = bookmark_ordinals

    def walk(self, node: etree._Element) -> None:
        namespace = _namespace(node)
        local = _local_name(node)
        if namespace == W_NS and self._move_marker(node, local):
            return
        if namespace == W_NS and self._bookmark_marker(node):
            return
        if namespace == W_NS:
            revision_kind = _REVISION_KIND_BY_LOCAL.get(local)
            if revision_kind is not None:
                self._revision(node, revision_kind)
                return
        if self.hidden:
            for child in node:
                self.walk(child)
            return
        if namespace == W_NS and local == "fldSimple":
            attributes = "\x1f".join(
                f"{name}={value}" for name, value in sorted(node.attrib.items())
            )
            self._append_field_atom("simple_start", attributes)
            for child in node:
                self.walk(child)
            self._append_atom(f"{_FIELD_ATOM_PREFIX}simple_end")
            return
        if namespace == W_NS and local == "fldChar":
            attributes = "\x1f".join(
                f"{name}={value}" for name, value in sorted(node.attrib.items())
            )
            self._append_field_atom("marker", attributes)
            for child in node:
                self.walk(child)
            return
        if namespace == W_NS and local in {"instrText", "delInstrText", "fldData"}:
            self._append_field_atom(local, node.text or "")
            return
        if namespace == W_NS and local in _SKIPPED_SUBTREES:
            return
        if namespace == W_NS and local in _STRUCTURE_KINDS:
            structure = _STRUCTURE_KINDS[local]
            self._append_atom(f"{structure}_start")
            for child in node:
                self.walk(child)
            self._append_atom(f"{structure}_end")
            return
        if namespace == W_NS:
            if local in {"t", "delText"}:
                self._append_atom("text", node.text or "")
                return
            if local == "tab":
                self._append_atom("tab", "\t")
                return
            if local in {"br", "cr"}:
                self._append_atom("line_break", "\n")
                return
            if local == "noBreakHyphen":
                self._append_atom("text", "\N{NON-BREAKING HYPHEN}")
                return
            if local == "softHyphen":
                self._append_atom("text", "\N{SOFT HYPHEN}")
                return
        for child in node:
            self.walk(child)

    def result(self, root: etree._Element) -> _ViewResult:
        self.walk(root)
        return _ViewResult(
            projection=ViewProjection("".join(self.text_chunks), tuple(self.atoms)),
            marker_offsets=dict(self.marker_offsets),
            revision_spans=tuple(self.revision_spans),
            revision_bookmark_ordinals=dict(self.revision_bookmarks),
        )


def _bookmark_range(
    bookmark: _RawBookmark,
    view: _ViewResult,
) -> BookmarkRange | None:
    if bookmark.end_node is None:
        return None
    start = view.marker_offsets.get(bookmark.start_node)
    end = view.marker_offsets.get(bookmark.end_node)
    if start is None or end is None or end < start:
        return None
    return BookmarkRange(start, end, view.projection.text[start:end])


def _bookmark_revision_spans(
    original_range: BookmarkRange | None,
    spans: Sequence[RevisionSpan],
) -> tuple[BookmarkRevisionSpan, ...]:
    if original_range is None:
        return ()
    result: list[BookmarkRevisionSpan] = []
    for span in spans:
        relative_start = max(0, min(original_range.end, span.base_start) - original_range.start)
        relative_end = max(0, min(original_range.end, span.base_end) - original_range.start)
        result.append(
            BookmarkRevisionSpan(
                node_ordinal=span.node_ordinal,
                kind=span.kind,
                base_start=span.base_start,
                base_end=span.base_end,
                relative_start=relative_start,
                relative_end=relative_end,
            )
        )
    return tuple(result)


def _project_story(
    package: DocxPackage,
    part_uri: str,
    limits: WordSemanticLimits,
) -> StoryProjection:
    if len(package.xml_bytes(part_uri)) > limits.max_xml_bytes_per_story:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "story part exceeds semantic XML byte limit",
            details={"part_uri": part_uri},
        )
    root = package.xml_root(part_uri)
    nodes = tuple(root.iter())
    if not nodes or len(nodes) > limits.max_nodes_per_story:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "story part exceeds semantic node limit",
            details={"part_uri": part_uri},
        )
    ordinals = {node: index for index, node in enumerate(nodes)}
    _validate_revision_markup(part_uri, nodes, ordinals)
    raw_bookmarks = _bookmarks(part_uri, nodes, ordinals, limits)
    bookmark_by_start = {bookmark.start_node: bookmark for bookmark in raw_bookmarks}

    original = _ViewBuilder(
        part_uri=part_uri,
        mode="original",
        ordinals=ordinals,
        bookmark_by_start=bookmark_by_start,
        limits=limits,
    ).result(root)
    final = _ViewBuilder(
        part_uri=part_uri,
        mode="final",
        ordinals=ordinals,
        bookmark_by_start=bookmark_by_start,
        limits=limits,
    ).result(root)

    span_by_ordinal = {span.node_ordinal: span for span in original.revision_spans}
    spans_by_bookmark: dict[int, list[RevisionSpan]] = {}
    for node_ordinal, bookmark_ordinals in original.revision_bookmark_ordinals.items():
        span = span_by_ordinal[node_ordinal]
        for bookmark_ordinal in bookmark_ordinals:
            spans_by_bookmark.setdefault(bookmark_ordinal, []).append(span)

    bookmarks: list[BookmarkProjection] = []
    for bookmark in raw_bookmarks:
        original_range = _bookmark_range(bookmark, original)
        final_range = _bookmark_range(bookmark, final)
        bookmarks.append(
            BookmarkProjection(
                name=bookmark.name,
                native_id=bookmark.native_id,
                part_uri=part_uri,
                start_node_ordinal=bookmark.start_ordinal,
                end_node_ordinal=bookmark.end_ordinal,
                original=original_range,
                final=final_range,
                revision_spans=_bookmark_revision_spans(
                    original_range,
                    spans_by_bookmark.get(bookmark.start_ordinal, ()),
                ),
            )
        )
    return StoryProjection(
        part_uri=part_uri,
        original=original.projection,
        final=final.projection,
        bookmarks=tuple(bookmarks),
        revision_spans=original.revision_spans,
    )


def _resolve_package(
    source: str | Path | DocxPackage,
    docx_limits: DocxReadLimits | None,
) -> DocxPackage:
    if isinstance(source, DocxPackage):
        return source
    return read_docx_package(source, limits=docx_limits or DocxReadLimits())


def project_word_semantics(
    source: str | Path | DocxPackage,
    *,
    limits: DocxReadLimits | None = None,
    semantic_limits: WordSemanticLimits | None = None,
) -> DocumentSemanticProjection:
    """Project reject/original and accept/final semantics without mutating a DOCX."""

    package = _resolve_package(source, limits)
    resolved_limits = semantic_limits or WordSemanticLimits()
    if len(package.story_parts) > resolved_limits.max_story_parts:
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "DOCX exceeds semantic story-part limit",
        )
    stories = tuple(
        _project_story(package, part_uri, resolved_limits) for part_uri in package.story_parts
    )
    return DocumentSemanticProjection(
        source_name=package.source_name,
        file_sha256=package.file_sha256,
        stories=stories,
    )


def _resolve_projection(
    source: SemanticSource,
    docx_limits: DocxReadLimits | None,
    semantic_limits: WordSemanticLimits | None,
) -> DocumentSemanticProjection:
    if isinstance(source, DocumentSemanticProjection):
        return source
    return project_word_semantics(source, limits=docx_limits, semantic_limits=semantic_limits)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _structure_fingerprint(view: ViewProjection) -> tuple[tuple[str, str], ...]:
    return tuple(
        (atom.kind, atom.value)
        for atom in view.atoms
        if atom.kind.endswith(("_start", "_end")) or atom.kind.startswith(_FIELD_ATOM_PREFIX)
    )


def _story_differences(
    baseline: DocumentSemanticProjection,
    returned: DocumentSemanticProjection,
) -> tuple[StoryDifference, ...]:
    baseline_by_part = {story.part_uri: story for story in baseline.stories}
    returned_by_part = {story.part_uri: story for story in returned.stories}
    result: list[StoryDifference] = []
    for part_uri in sorted(baseline_by_part.keys() | returned_by_part.keys()):
        baseline_story = baseline_by_part.get(part_uri)
        returned_story = returned_by_part.get(part_uri)
        if baseline_story is None or returned_story is None:
            result.append(
                StoryDifference(
                    part_uri=part_uri,
                    baseline_text_sha256=(
                        None
                        if baseline_story is None
                        else _text_sha256(baseline_story.original.text)
                    ),
                    returned_original_text_sha256=(
                        None
                        if returned_story is None
                        else _text_sha256(returned_story.original.text)
                    ),
                    text_changed=True,
                    structure_changed=True,
                )
            )
            continue
        text_changed = baseline_story.original.text != returned_story.original.text
        baseline_structure = _structure_fingerprint(baseline_story.original)
        returned_structure = _structure_fingerprint(returned_story.original)
        structure_changed = baseline_structure != returned_structure
        if text_changed or structure_changed:
            result.append(
                StoryDifference(
                    part_uri=part_uri,
                    baseline_text_sha256=_text_sha256(baseline_story.original.text),
                    returned_original_text_sha256=_text_sha256(returned_story.original.text),
                    text_changed=text_changed,
                    structure_changed=structure_changed,
                )
            )
    return tuple(result)


def _all_bookmarks(
    projection: DocumentSemanticProjection,
    name: str,
) -> tuple[BookmarkProjection, ...]:
    return tuple(
        bookmark
        for story in projection.stories
        for bookmark in story.bookmarks
        if bookmark.name == name
    )


def _bookmark_issues(
    baseline: DocumentSemanticProjection,
    returned: DocumentSemanticProjection,
    expected_bookmarks: Sequence[str],
) -> tuple[BookmarkIssue, ...]:
    issues: list[BookmarkIssue] = []
    for name in expected_bookmarks:
        baseline_matches = _all_bookmarks(baseline, name)
        if (
            len(baseline_matches) != 1
            or baseline_matches[0].original is None
            or baseline_matches[0].final is None
        ):
            raise ContractError(
                ErrorCode.DOCX_INVALID_PACKAGE,
                "expected export bookmark is not unique and visible in the baseline",
                details={"bookmark": name, "count": len(baseline_matches)},
            )
        returned_matches = _all_bookmarks(returned, name)
        if len(returned_matches) > 1:
            issues.append(
                BookmarkIssue(
                    name,
                    "duplicate",
                    f"returned document contains {len(returned_matches)}",
                )
            )
            continue
        if (
            not returned_matches
            or returned_matches[0].original is None
            or returned_matches[0].final is None
        ):
            issues.append(
                BookmarkIssue(name, "missing", "bookmark is absent in the original or final view")
            )
            continue
        baseline_bookmark = baseline_matches[0]
        returned_bookmark = returned_matches[0]
        if (
            baseline_bookmark.part_uri != returned_bookmark.part_uri
            or baseline_bookmark.original != returned_bookmark.original
        ):
            issues.append(
                BookmarkIssue(name, "changed", "bookmark base range or visible text changed")
            )
    return tuple(issues)


def compare_export_baseline(
    baseline: SemanticSource,
    returned: SemanticSource,
    expected_bookmarks: Iterable[str],
    *,
    limits: DocxReadLimits | None = None,
    semantic_limits: WordSemanticLimits | None = None,
) -> BaselineComparison:
    """Detect accepted/untracked drift and export-bookmark damage.

    The returned document is compared in its reject/original view.  Therefore a
    properly tracked edit leaves the baseline invariant, while Accept All or an
    untracked edit changes it.  Formatting, OMML, and images remain explicitly
    outside this comparison's capability boundary.
    """

    expected = tuple(expected_bookmarks)
    if any(not isinstance(name, str) or not name for name in expected):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "expected bookmark names must be non-empty strings",
        )
    if len(set(expected)) != len(expected):
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "expected bookmark names must be unique",
        )
    baseline_projection = _resolve_projection(baseline, limits, semantic_limits)
    returned_projection = _resolve_projection(returned, limits, semantic_limits)
    if any(story.revision_spans for story in baseline_projection.stories):
        raise ContractError(
            ErrorCode.DOCX_INVALID_PACKAGE,
            "export baseline must not contain tracked text revisions",
        )

    differences = _story_differences(baseline_projection, returned_projection)
    bookmark_issues = _bookmark_issues(
        baseline_projection,
        returned_projection,
        expected,
    )
    missing = tuple(issue.name for issue in bookmark_issues if issue.kind == "missing")
    duplicate = tuple(issue.name for issue in bookmark_issues if issue.kind == "duplicate")
    changed = tuple(issue.name for issue in bookmark_issues if issue.kind == "changed")
    status: DriftStatus = "accepted_or_untracked_drift" if differences else "clean"
    return BaselineComparison(
        baseline=baseline_projection,
        returned=returned_projection,
        drift_status=status,
        story_differences=differences,
        bookmark_issues=bookmark_issues,
        missing_bookmarks=missing,
        duplicate_bookmarks=duplicate,
        changed_bookmarks=changed,
    )


__all__ = [
    "BaselineComparison",
    "BookmarkIssue",
    "BookmarkProjection",
    "BookmarkRange",
    "BookmarkRevisionSpan",
    "CAPABILITIES",
    "DocumentSemanticProjection",
    "RevisionSpan",
    "SemanticAtom",
    "SemanticCapabilities",
    "StoryDifference",
    "StoryProjection",
    "ViewProjection",
    "WordSemanticLimits",
    "compare_export_baseline",
    "project_word_semantics",
]
