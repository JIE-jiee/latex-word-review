"""Conservative unique-substring Word bookmark tests."""

from __future__ import annotations

from lxml import etree  # type: ignore[import-untyped]

from latex_word_review.docx_anchor import AnchorReason, insert_unique_source_bookmarks
from latex_word_review.hashing import digest_bytes
from latex_word_review.source_units import SourceUnit, build_text_provenance

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _unit(text: str, ordinal: int) -> SourceUnit:
    raw = text.encode()
    normalized, provenance = build_text_provenance(raw)
    return SourceUnit(
        unit_id=f"unit_{ordinal:032x}",
        path="main.tex",
        start_byte=ordinal * 100,
        end_byte=ordinal * 100 + len(raw),
        slice_sha256=digest_bytes(raw).sha256,
        normalized_text=normalized,
        normalized_text_sha256=digest_bytes(normalized.encode()).sha256,
        newline="lf",
        start_line=ordinal + 1,
        end_line=ordinal + 1,
        start_column=1,
        end_column=len(text) + 1,
        ordinal=ordinal,
        text_provenance=provenance,
    )


def _document(paragraphs: str) -> etree._Element:
    return etree.fromstring(
        f'<w:document xmlns:w="{W_NS}"><w:body>{paragraphs}</w:body></w:document>'.encode()
    )


def _bookmark_text(root: etree._Element, name: str) -> str:
    start = root.xpath(
        ".//w:bookmarkStart[@w:name=$name]",
        namespaces={"w": W_NS},
        name=name,
    )[0]
    native_id = start.get(f"{{{W_NS}}}id")
    fragments: list[str] = []
    for sibling in start.itersiblings():
        if sibling.tag == f"{{{W_NS}}}bookmarkEnd" and sibling.get(f"{{{W_NS}}}id") == native_id:
            break
        fragments.extend(element.text or "" for element in sibling.iter(f"{{{W_NS}}}t"))
    return "".join(fragments)


def test_unique_substring_across_runs_is_anchored_exactly() -> None:
    text = "中文🙂 cross-run paragraph."
    root = _document(
        "<w:p><w:r><w:t>Prefix: </w:t></w:r>"
        "<w:r><w:rPr><w:b/></w:rPr><w:t>中文🙂 cross-</w:t></w:r>"
        "<w:r><w:t>run paragraph.</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> Suffix.</w:t></w:r></w:p>'
    )

    placement = insert_unique_source_bookmarks(root, (_unit(text, 1),), first_numeric_id=7)[0]

    assert placement.status == "exact"
    assert placement.reason is AnchorReason.EXACT
    assert placement.bookmark_name is not None
    assert _bookmark_text(root, placement.bookmark_name) == text
    assert "".join(root.itertext()) == f"Prefix: {text} Suffix."


def test_two_disjoint_units_in_one_word_run_remain_exact() -> None:
    first = _unit("First source paragraph.", 1)
    second = _unit("Second source paragraph.", 2)
    root = _document(
        "<w:p><w:r><w:t>Prefix: First source paragraph. "
        "Second source paragraph. Suffix.</w:t></w:r></w:p>"
    )

    placements = insert_unique_source_bookmarks(root, (first, second), first_numeric_id=1)

    assert [placement.status for placement in placements] == ["exact", "exact"]
    assert _bookmark_text(root, placements[0].bookmark_name or "") == first.normalized_text
    assert _bookmark_text(root, placements[1].bookmark_name or "") == second.normalized_text


def test_repeated_or_word_internal_substrings_fail_closed() -> None:
    repeated = _document("<w:p><w:r><w:t>Target. Target.</w:t></w:r></w:p>")
    internal = _document("<w:p><w:r><w:t>concatenate</w:t></w:r></w:p>")

    repeated_result = insert_unique_source_bookmarks(
        repeated, (_unit("Target.", 1),), first_numeric_id=1
    )[0]
    internal_result = insert_unique_source_bookmarks(
        internal, (_unit("cat", 2),), first_numeric_id=1
    )[0]

    assert repeated_result.status == "conflict"
    assert repeated_result.reason is AnchorReason.AMBIGUOUS
    assert internal_result.status == "unmapped"
    assert internal_result.reason is AnchorReason.UNSAFE_BOUNDARY
    assert not repeated.xpath(".//w:bookmarkStart", namespaces={"w": W_NS})
    assert not internal.xpath(".//w:bookmarkStart", namespaces={"w": W_NS})


def test_collapsed_whitespace_and_nested_structure_fail_closed() -> None:
    whitespace = _document(
        "<w:p><w:r><w:t>Prefix: Safe  collapsed whitespace. Suffix.</w:t></w:r></w:p>"
    )
    nested = _document(
        '<w:p><w:r><w:t>Prefix: </w:t></w:r><w:hyperlink w:anchor="x">'
        "<w:r><w:t>Nested target.</w:t></w:r></w:hyperlink></w:p>"
    )

    whitespace_result = insert_unique_source_bookmarks(
        whitespace, (_unit("Safe collapsed whitespace.", 1),), first_numeric_id=1
    )[0]
    nested_result = insert_unique_source_bookmarks(
        nested, (_unit("Nested target.", 2),), first_numeric_id=1
    )[0]

    assert whitespace_result.status == "unmapped"
    assert whitespace_result.reason is AnchorReason.NON_NORMALIZED_RANGE
    assert nested_result.status == "unmapped"
    assert nested_result.reason is AnchorReason.UNSAFE_STRUCTURE
